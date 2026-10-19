"""
hmi_common.py - shared helpers for the rungstone-hmi-scada scripts.

Reference extraction (FactoryTalk {[shortcut]tag}, OPC UA ns=..;s=, Ignition
[provider]path, Siemens "DB".member, SLC data-table addresses), normalisation,
member semantics (timer/counter members that change meaning across
platforms) and label comparison.

When the sibling rungstone-plc skill is installed next to this one, its
dependency_map / parse_project modules are imported (read-only) so PLC
resolution behaves identically in both skills.  Otherwise the local
fall-back copies below are used, so this skill still works on its own as a
standalone .skill package.

Customer scripts are only ever read as text.  Nothing here executes them.
"""

import csv
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _shared import safe_xml  # noqa: E402

PLC_SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "..", "rungstone-plc", "scripts"))
dm = None
pp = None
if os.path.isdir(PLC_SCRIPTS):
    try:  # pragma: no cover - depends on install layout
        sys.path.insert(0, PLC_SCRIPTS)
        import dependency_map as dm  # noqa: E402,F401
        import parse_project as pp  # noqa: E402,F401
    except Exception:  # noqa: BLE001
        dm = None
        pp = None
    finally:
        if PLC_SCRIPTS in sys.path:
            sys.path.remove(PLC_SCRIPTS)

HAVE_PLC_SKILL = dm is not None

# --------------------------------------------------------------------------
# Reference patterns (local copies; kept in step with rungstone-plc)
# --------------------------------------------------------------------------

SLC_RE = re.compile(r"#?(?:[IOSBTCRNFLAD]|ST|MG|PD)\d{0,3}:\d+(?:\.\d+)?(?:/\d+)?"
                    r"(?:[./](?:ACC|PRE|DN|EN|TT|CU|CD|OV|UN|UA|LEN|POS|ER|EU|EM|FD|IN|UL|RN|FN|SP|PV|CV))?|B\d+/\d+", re.I)
BRACED_RE = re.compile(r"\{(?:::)?\[([^\]]+)\]([^}]+)\}")
SHORTCUT_RE = re.compile(r"(?<![\w\]])(?:::)?\[([A-Za-z_][\w ]*)\]((?:Program:)?[A-Za-z_][\w:.\[\]/]*)")
OPC_RE = re.compile(r"ns=\d+;s=(?:\[([^\]]+)\])?([^\s\"'<>,;]+)")
SIEMENS_SYM_RE = re.compile(r'"[^"]+"(?:\.[A-Za-z_]\w*)*')
IGN_PATH_RE = re.compile(r"\[([A-Za-z_][\w ]*)\]([A-Za-z_][\w /.-]*)")
FT_SCRIPT_ITEM_RE = re.compile(r"(?:gTagGroup\.Item|Tags|HMIRuntime\.Tags|SmartTags|GetTagValue|SetTagValue|"
                               r"ReadTag|WriteTag)\s*\(\s*\"([^\"]+)\"", re.I)
WRITE_LINE_RE = re.compile(r"(?i)(write(blocking|async)?\s*\(|\.value\s*=(?!=)|settag|setvalue|set_value|"
                           r"tags?\(.+\)\s*=(?!=)|smarttags\(.+\)\s*=(?!=)|\.write\b)")
READ_LINE_RE = re.compile(r"(?i)(read(blocking|async)?\s*\(|\.value\b|gettag|\.read\b)")


def lname(el):
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def norm_slc(addr):
    if dm:
        return dm.norm_slc(addr)
    a = addr.upper().lstrip("#")
    m = re.match(r"^B(\d+)/(\d+)$", a)
    if m:
        bit = int(m.group(2))
        return "B%s:%d/%d" % (m.group(1), bit // 16, bit % 16)
    m = re.match(r"^([IO]):(\d+)\.0/(\d+)$", a)
    if m:
        a = "%s:%s/%s" % m.groups()
    return re.sub(r"/(ACC|PRE|DN|EN|TT|CU|CD|OV|UN|UA|LEN|POS|ER|EU|EM|FD|IN|UL|RN|FN|SP|PV|CV)$", r".\1", a)


def norm_path(s):
    s = (s or "").strip().strip("{}").strip()
    if SLC_RE.fullmatch(s):
        return norm_slc(s)
    m = re.match(r"(?i)^program:([^.]+)\.(.+)$", s)
    if m:
        return "program:%s.%s" % (m.group(1).lower(), norm_path(m.group(2)))
    s = s.replace('"', "")
    s = re.sub(r"\s*\[\s*", "[", s)
    s = re.sub(r"\s*\]\s*", "]", s)
    return s.lower()


def split_member(path):
    """'fill_timer.acc' -> ('fill_timer', 'acc'); SLC 'T4:0.ACC' -> ('T4:0', 'acc')."""
    p = path
    if re.match(r"^(T|C|R)\d*:\d+[./]", p, re.I):
        base, mem = re.split(r"[./]", p, 1)
        return base.upper(), mem.lower()
    if SLC_RE.fullmatch(p) or re.match(r"^[IO]:", p, re.I):
        return p.upper(), ""
    if p.startswith("program:"):
        head, rest = p.split(".", 1) if "." in p else (p, "")
        sub = rest.split(".", 1)
        return head + "." + sub[0], sub[1] if len(sub) > 1 else ""
    parts = p.split(".", 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def find_refs(text, siemens="auto"):
    """All PLC-ish references in a string: list of dicts with span, device, path, kind.

    siemens: True  - also report "DB".member symbols anywhere in the text
             False - never (raw XML / scripts, where quotes are syntax)
             "auto"- only when the whole string is a Siemens symbol (CSV cell, JSON value, attribute value)
    """
    out = []
    taken = []

    def free(a, b):
        return not any(x < b and a < y for x, y in taken)

    for rx, kind in ((BRACED_RE, "factorytalk"), (OPC_RE, "opc"), (SHORTCUT_RE, "shortcut")):
        for m in rx.finditer(text):
            if free(m.start(), m.end()):
                taken.append((m.start(), m.end()))
                out.append({"span": (m.start(), m.end()), "device": m.group(1), "path": m.group(2).strip(),
                            "kind": kind, "raw": m.group(0)})
    if siemens == "auto":
        st = text.strip()
        sym_iter = [m for m in SIEMENS_SYM_RE.finditer(text)] if SIEMENS_SYM_RE.fullmatch(st) else []
    elif siemens:
        sym_iter = list(SIEMENS_SYM_RE.finditer(text))
    else:
        sym_iter = []
    for m in sym_iter:
        if free(m.start(), m.end()):
            taken.append((m.start(), m.end()))
            out.append({"span": (m.start(), m.end()), "device": None, "path": m.group(0), "kind": "siemens",
                        "raw": m.group(0)})
    for m in SLC_RE.finditer(text):
        if free(m.start(), m.end()) and _isolated(text, m):
            taken.append((m.start(), m.end()))
            out.append({"span": (m.start(), m.end()), "device": None, "path": m.group(0), "kind": "slc",
                        "raw": m.group(0)})
    out.sort(key=lambda r: r["span"][0])
    return out


def _isolated(text, m):
    before = text[m.start() - 1] if m.start() > 0 else " "
    after = text[m.end()] if m.end() < len(text) else " "
    return not (before.isalnum() or before in "_:") and not (after.isalnum() or after in "_")


# --------------------------------------------------------------------------
# Member semantics across platforms
# --------------------------------------------------------------------------

STRUCT_KIND = {"TIMER": "timer", "FBD_TIMER": "timer", "COUNTER": "counter", "FBD_COUNTER": "counter",
               "CONTROL": "control", "PID": "pid", "MESSAGE": "message", "TON": "iec_timer", "TOF": "iec_timer",
               "TP": "iec_timer", "IEC_TIMER": "iec_timer", "TON_TIME": "iec_timer", "CTU": "iec_counter",
               "CTD": "iec_counter", "CTUD": "iec_counter", "IEC_COUNTER": "iec_counter"}
SLC_FILE_KIND = {"T": "timer", "C": "counter", "R": "control", "PD": "pid", "MG": "message"}
TIMER_MEMBERS = {"acc", "pre", "dn", "en", "tt"}
COUNTER_MEMBERS = {"acc", "pre", "dn", "cu", "cd", "ov", "un"}

# (kind, member, target) -> (new member or None, new-tag suffix or None, new type, risk, why)
# target family: "iec" (siemens-scl / iec-st) or "rockwell"
REBIND = {
    ("timer", "acc", "iec"): (None, "_ACC_ms", "DINT", "high",
                              "Logix/SLC .ACC is a DINT/INT millisecond (or time-base) count; IEC ET is TIME with different "
                              "reset semantics. No direct equivalent: create a DINT tag in the PLC, e.g. "
                              "<new>_ACC_ms := TIME_TO_DINT(<new>.ET), and bind the HMI to it."),
    ("timer", "pre", "iec"): (None, "_PRE_ms", "DINT", "high",
                              "HMI writes the preset. IEC PT is TIME: create a DINT setpoint tag and convert in logic "
                              "(<new>.PT := DINT_TO_TIME(<new>_PRE_ms)); apply SLC time-base scaling if the source was SLC."),
    ("timer", "dn", "iec"): ("Q", None, "BOOL", "medium", ".DN maps to Q."),
    ("timer", "en", "iec"): (None, "_EN", "BOOL", "medium", ".EN is the IN input; not exposed by IEC_TIMER as status. Expose a BOOL."),
    ("timer", "tt", "iec"): (None, "_TT", "BOOL", "high", ".TT has no IEC equivalent; create BOOL := IN AND NOT Q."),
    ("counter", "acc", "iec"): ("CV", None, "INT/DINT", "medium", ".ACC maps to CV; check width (S7 CTU CV is INT)."),
    ("counter", "pre", "iec"): ("PV", None, "INT/DINT", "medium", ".PRE maps to PV."),
    ("counter", "dn", "iec"): ("Q", None, "BOOL", "medium", ".DN maps to Q / QU."),
    ("counter", "cu", "iec"): (None, "_CU", "BOOL", "high", ".CU has no IEC equivalent."),
    ("counter", "cd", "iec"): (None, "_CD", "BOOL", "high", ".CD has no IEC equivalent."),
    ("counter", "ov", "iec"): (None, "_OV", "BOOL", "high", ".OV has no IEC equivalent; IEC counters saturate."),
    ("counter", "un", "iec"): (None, "_UN", "BOOL", "high", ".UN has no IEC equivalent."),
    ("iec_timer", "et", "rockwell"): ("ACC", None, "DINT", "high", "ET (TIME) -> .ACC (DINT ms): type and scaling change."),
    ("iec_timer", "pt", "rockwell"): ("PRE", None, "DINT", "medium", "PT (TIME) -> .PRE (DINT ms)."),
    ("iec_timer", "q", "rockwell"): ("DN", None, "BOOL", "medium", "Q -> .DN."),
    ("iec_timer", "in", "rockwell"): ("EN", None, "BOOL", "medium", "IN -> .EN."),
    ("iec_counter", "cv", "rockwell"): ("ACC", None, "DINT", "medium", "CV -> .ACC."),
    ("iec_counter", "pv", "rockwell"): ("PRE", None, "DINT", "medium", "PV -> .PRE."),
    ("iec_counter", "q", "rockwell"): ("DN", None, "BOOL", "medium", "Q -> .DN."),
}


def target_family(target):
    return "rockwell" if (target or "").lower().startswith("rockwell") else "iec"


def kind_of(data_type, base):
    k = STRUCT_KIND.get((data_type or "").upper().strip())
    if k:
        return k
    m = re.match(r"^(T|C|R|PD|MG)\d*:\d+$", base or "", re.I)
    if m:
        return SLC_FILE_KIND.get(m.group(1).upper())
    return None


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------

OPPOSITES = [("open", "closed"), ("open", "close"), ("start", "stop"), ("on", "off"), ("high", "low"),
             ("full", "empty"), ("rejected", "accepted"), ("rejected", "completed"), ("rejected", "good"),
             ("reject", "accept"), ("fault", "ok"), ("fault", "healthy"), ("fill", "drain"), ("forward", "reverse"),
             ("up", "down"), ("raise", "lower"), ("enable", "disable"), ("enabled", "disabled"), ("auto", "manual"),
             ("inlet", "outlet"), ("left", "right"), ("min", "max"), ("minimum", "maximum"),
             ("pass", "fail"), ("heat", "cool"), ("extend", "retract"), ("lock", "unlock")]
STOPWORDS = {"the", "a", "an", "of", "to", "for", "and", "or", "in", "on", "at", "is", "pb", "no", "nc", "ms", "s"}


def contradiction(a, b):
    if dm:
        return dm.contradiction(a, b)
    wa, wb = set(re.findall(r"[a-z]+", (a or "").lower())), set(re.findall(r"[a-z]+", (b or "").lower()))
    for x, y in OPPOSITES:
        if (x in wa and y in wb and y not in wa and x not in wb) or (y in wa and x in wb and x not in wa and y not in wb):
            return "%s/%s" % (x, y)
    return None


def jaccard(a, b):
    wa = set(w for w in re.findall(r"[a-z0-9]+", (a or "").lower()) if w not in STOPWORDS)
    wb = set(w for w in re.findall(r"[a-z0-9]+", (b or "").lower()) if w not in STOPWORDS)
    if not wa or not wb:
        return 1.0
    return len(wa & wb) / float(len(wa | wb))


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def read_text(p):
    with open(p, "rb") as f:
        data = f.read()
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def sniff_rows(text):
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = ";" if sample.count(";") > sample.count(",") else ","
    return list(csv.reader(io.StringIO(text), delimiter=delim)), delim


def iter_csv(path, delim=None):
    """Stream rows from a (possibly huge) CSV without loading it whole."""
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
        if delim is None:
            head = f.read(20000)
            f.seek(0)
            try:
                delim = csv.Sniffer().sniff(head, delimiters=",;\t|").delimiter
            except csv.Error:
                delim = ";" if head.count(";") > head.count(",") else ","
        for row in csv.reader(f, delimiter=delim):
            yield row


def xlsx_sheets(path):
    """Yield (sheet_name, rows) using openpyxl if available; else raise RuntimeError."""
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        raise RuntimeError("openpyxl is not installed; export the workbook sheets to CSV and pass those instead")
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        rows = []
        for r in ws.iter_rows(values_only=True):
            rows.append(["" if v is None else str(v) for v in r])
        yield ws.title, rows


def load_mapping(path):
    """mapping.csv (Rungstone columns or simple old,new) -> {norm_old: row-dict}."""
    rows, _ = sniff_rows(read_text(path))
    if not rows:
        return {}
    head = [c.strip().lower() for c in rows[0]]
    out = {}

    def idx(*names):
        for n in names:
            if n in head:
                return head.index(n)
        return None

    if "old_address" in head or "old_symbol" in head or "new_symbol" in head:
        io_, os_, ot, ns, na, nt, bs, nt2 = (idx("old_address"), idx("old_symbol"), idx("old_type"), idx("new_symbol"),
                                            idx("new_address"), idx("new_type"), idx("basis"), idx("notes"))
        for r in rows[1:]:
            g = lambda i: r[i].strip() if i is not None and i < len(r) else ""
            rec = {"old_address": g(io_), "old_symbol": g(os_), "old_type": g(ot), "new_symbol": g(ns),
                   "new_address": g(na), "new_type": g(nt), "basis": g(bs), "notes": g(nt2)}
            new = rec["new_symbol"] or rec["new_address"]
            if not new:
                continue
            rec["new"] = new
            for k in (rec["old_address"], rec["old_symbol"]):
                if k:
                    out[norm_path(k)] = rec
        return out
    oi = idx("old", "source", "from", "legacy") or 0
    ni = idx("new", "target", "to")
    ni = 1 if ni is None else ni
    start = 1 if any(h in ("old", "source", "from", "legacy", "new", "target", "to") for h in head) else 0
    for r in rows[start:]:
        if len(r) > max(oi, ni) and r[oi].strip() and r[ni].strip():
            out[norm_path(r[oi])] = {"old_address": r[oi].strip(), "new": r[ni].strip(), "new_symbol": r[ni].strip(),
                                     "old_type": "", "new_type": ""}
    return out


def find_state_dir(explicit=None):
    from _shared import state
    if explicit:
        d = explicit if os.path.basename(explicit) == state.STATE_DIR else os.path.join(explicit, state.STATE_DIR)
        return d if os.path.isdir(d) else None
    return state.find_dir()
