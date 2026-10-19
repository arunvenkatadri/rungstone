"""
dependency_map.py - cross-reference every tag/address across PLC code and
HMI/SCADA files; report readers, writers and conflicts.

PLC inputs (--plc): anything parse_project.py reads (.L5X, SimaticML .xml,
  .scl, .st, PLCopen .xml, .TcPOU/.TcGVL, ladder text, I/O-map .csv) or a JSON
  file previously written by parse_project.py.
HMI/SCADA inputs (--hmi): tag exports (.csv, e.g. FactoryTalk View, WinCC,
  Ignition CSV), display/screen/project XML, Ignition .json (tags, views),
  scripts (.vbs .vba .bas .js .py .cs .txt ...).

Conflicts reported:
  competing_writers      same tag written from more than one place in a way
                         that can fight (duplicate OTE, PLC vs HMI, same timer
                         in two instructions, multiple MOVs)
  read_never_written     read somewhere, written nowhere, and not a physical
                         input / block input -> open question for the plant
  label_mismatch         descriptions for the same tag disagree between PLC,
                         I/O map and HMI
  broken_link            HMI/SCADA references a tag the PLC does not have
  translation_break      (with --target and/or --mapping) a reference that
                         will break or change meaning after translation, e.g.
                         a Logix TIMER .ACC read by an HMI (IEC ET is a TIME
                         value with different reset semantics)
  translation_watch      same as translation_break, reported when no
                         --target is given

Usage:
  python3 dependency_map.py --plc FILE... [--hmi FILE...]
         [--target siemens-scl|iec-st|rockwell] [--mapping map.csv]
         [-o map.json] [--md map.md]
"""

import argparse
import csv
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_project as pp  # noqa: E402
from _shared import safe_xml  # noqa: E402

# --------------------------------------------------------------------------
# Instruction knowledge (Logix 5000 and SLC/PLC-5)
# --------------------------------------------------------------------------

# mnemonic -> (operand indices written, operand indices that are not tags)
WRITES = {
    "OTE": [0], "OTL": [0], "OTU": [0], "ONS": [0], "OSR": [0, 1], "OSF": [0, 1],
    "TON": [0], "TOF": [0], "RTO": [0], "TONR": [0], "TOFR": [0], "RTOR": [0],
    "CTU": [0], "CTD": [0], "CTUD": [0], "RES": [0],
    "MOV": [1], "MOVE": [1], "MVM": [2], "BTD": [2], "COP": [1], "CPS": [1], "FLL": [1], "CLR": [0],
    "CPT": [0], "ADD": [2], "SUB": [2], "MUL": [2], "DIV": [2], "MOD": [2], "DDV": [1],
    "SQR": [1], "SQRT": [1], "NEG": [1], "ABS": [1], "AND": [2], "OR": [2], "XOR": [2], "NOT": [1],
    "BAND": [2], "BOR": [2], "BXOR": [2], "BNOT": [1],
    "TOD": [1], "FRD": [1], "DEG": [1], "RAD": [1], "SIN": [1], "COS": [1], "TAN": [1], "ASN": [1],
    "ACS": [1], "ATN": [1], "LN": [1], "LOG": [1], "XPY": [2], "TRN": [1], "SWPB": [2],
    "SCP": [5], "SCL": [3], "DCD": [2], "ENC": [2],
    "GSV": [3], "MSG": [0], "PID": [0, 3],
    "BSL": [0, 1], "BSR": [0, 1], "FFL": [1, 2], "FFU": [1, 2], "LFL": [1, 2], "LFU": [1, 2],
    "SQO": [2, 3], "SQI": [3], "SQL": [0, 2], "FAL": [0, 4], "FSC": [0], "AVE": [2, 3], "SRT": [0, 2],
    "STD": [2, 3], "SIZE": [2], "CONCAT": [2], "MID": [3], "DTOS": [1], "STOD": [1], "RTOS": [1],
    "STOR": [1], "UPPER": [1], "LOWER": [1], "DELETE": [3], "INSERT": [3], "ALMD": [0], "ALMA": [0],
    "IOT": [], "IIN": [],
}
READS_ONLY = {"XIC", "XIO", "EQU", "NEQ", "GRT", "GEQ", "LES", "LEQ", "LIM", "MEQ", "CMP", "AFI", "NOP",
              "JMP", "LBL", "TND", "MCR", "RET", "SBR", "END", "UID", "UIE", "EVENT", "SSV", "IOT"}
NON_TAG_ARGS = {"JSR": "all", "JMP": [0], "LBL": [0], "GSV": [0, 1, 2], "SSV": [0, 1, 2], "SBR": [], "TND": "all"}
# SLC ASCII operand counts (RSLogix 500 export format)
SLC_ARITY = {
    "XIC": 1, "XIO": 1, "OTE": 1, "OTL": 1, "OTU": 1, "ONS": 1, "OSR": 2, "OSF": 2, "TON": 4, "TOF": 4, "RTO": 4,
    "CTU": 3, "CTD": 3, "RES": 1, "MOV": 2, "MVM": 3, "ADD": 3, "SUB": 3, "MUL": 3, "DIV": 3, "DDV": 2, "CLR": 1,
    "NEG": 2, "SQR": 2, "SCP": 6, "SCL": 4, "LIM": 3, "EQU": 2, "NEQ": 2, "GRT": 2, "GEQ": 2, "LES": 2, "LEQ": 2,
    "MEQ": 3, "AND": 3, "OR": 3, "XOR": 3, "NOT": 2, "COP": 3, "FLL": 3, "JSR": 1, "JMP": 1, "LBL": 1, "RET": 0,
    "END": 0, "TND": 0, "MCR": 0, "BSL": 4, "BSR": 4, "FFL": 5, "FFU": 5, "LFL": 5, "LFU": 5, "SQO": 6, "SQC": 6,
    "SQL": 5, "TOD": 2, "FRD": 2, "DCD": 2, "ENC": 2, "PID": 3, "IIM": 3, "IOM": 3, "SBR": 0, "TOT": 0,
}
SLC_STRUCT = {"SOR", "EOR", "BST", "NXB", "BND"}
MOTION_PREFIX = re.compile(r"^M(SO|SF|AJ|AM|AS|AH|AFR|CD|RP|AG|CLM|CCM|CCD|CS|CT|DO|DF|DS|GS|AW|RAT|AHD|SR|CSR)$")
CPT_FUNCS = {"ABS", "SQR", "SQRT", "SIN", "COS", "TAN", "ASN", "ACS", "ATN", "LN", "LOG", "DEG", "RAD", "FRD", "TOD",
             "TRN", "NOT", "AND", "OR", "XOR", "MOD"}

SLC_RE = re.compile(r"#?(?:[IOSBTCRNFLAD]|ST|MG|PD)\d{0,3}:\d+(?:\.\d+)?(?:/\d+)?(?:[./](?:ACC|PRE|DN|EN|TT|CU|CD|OV|UN|UA|LEN|POS|ER|EU|EM|FD|IN|UL|RN|FN|SP|PV|CV))?|B\d+/\d+", re.I)
LOGIX_TOKEN_RE = re.compile(r"\\?[A-Za-z_]\w*(?::\d+)?(?::[IOCS])?(?:\[[^\]]*\])?(?:\.\w+(?:\[[^\]]*\])?)*")

# --------------------------------------------------------------------------
# Member semantics that change across platforms
# --------------------------------------------------------------------------

STRUCT_KIND = {"TIMER": "timer", "FBD_TIMER": "timer", "COUNTER": "counter", "FBD_COUNTER": "counter",
               "CONTROL": "control", "PID": "pid", "PIDE_AUTOTUNE": "pid", "MESSAGE": "message",
               "TON": "iec_timer", "TOF": "iec_timer", "TP": "iec_timer", "TONR": "iec_timer",
               "IEC_TIMER": "iec_timer", "TON_TIME": "iec_timer", "CTU": "iec_counter", "CTD": "iec_counter",
               "CTUD": "iec_counter", "IEC_COUNTER": "iec_counter"}
SLC_FILE_KIND = {"T": "timer", "C": "counter", "R": "control", "PD": "pid", "MG": "message"}

MEMBER_RULES = {
    "timer": {
        "acc": ("high", "Logix/SLC .ACC is a DINT/INT count of milliseconds (or time-base ticks) that the HMI reads as a number. "
                        "An IEC/S7 timer exposes ET as a TIME value; it is not the same type and resets differently "
                        "(TON ET resets when IN goes false; RTO-style retention has no IEC TON equivalent). No direct equivalent: "
                        "add an explicit DINT tag (e.g. TIME_TO_DINT(ET)) for the HMI or re-point and rescale the HMI object."),
        "pre": ("medium", ".PRE is a DINT ms (or ticks) preset; IEC PT is TIME. An HMI numeric entry writing .PRE needs a new "
                          "DINT setpoint tag converted to TIME in logic, and SLC time-base scaling must be applied."),
        "dn": ("medium", ".DN maps to Q (TON/TOF); re-point the HMI reference."),
        "en": ("medium", ".EN maps to the IN input; it is not exposed as a status bit in S7 IEC_TIMER, so expose it explicitly."),
        "tt": ("high", ".TT (timing) has no IEC equivalent; compute IN AND NOT Q into a new BOOL for the HMI."),
    },
    "counter": {
        "acc": ("medium", ".ACC maps to CV (INT/DINT). Check width: S7 CTU CV is INT unless CTU_DINT is used."),
        "pre": ("medium", ".PRE maps to PV."),
        "dn": ("medium", ".DN maps to Q / QU."),
        "cu": ("high", ".CU (rung-condition memory) has no IEC equivalent; edge memory is internal to CTU."),
        "cd": ("high", ".CD has no IEC equivalent."),
        "ov": ("high", ".OV overflow bit has no IEC equivalent; IEC counters stop at PVmax / type limit instead of wrapping."),
        "un": ("high", ".UN underflow bit has no IEC equivalent."),
    },
    "control": {m: ("high", "CONTROL .%s (file/FIFO/sequencer instruction state) has no IEC equivalent; the instruction must be re-implemented." % m.upper())
                for m in ("pos", "len", "dn", "en", "er", "ul", "em", "eu", "in", "fd", "ip", "um")},
    "pid": {m: ("high", "PID .%s: loop structure, gain form (dependent/independent) and units differ between Logix PID/PIDE, "
                        "Siemens PID_Compact and IEC libraries; the HMI faceplate must be re-mapped and re-tuned." % m.upper())
            for m in ("sp", "pv", "cv", "kp", "ki", "kd", "so", "mo", "sws", "cvh", "cvl", "mode")},
    "message": {m: ("high", "MSG .%s: messaging is protocol-specific (DH+/CIP/EtherNet/IP); the target communication block "
                            "has different status members." % m.upper()) for m in ("dn", "er", "en", "st", "err", "exerr", "tmo")},
    "iec_timer": {
        "et": ("high", "IEC ET (TIME) -> Logix .ACC (DINT ms): type changes and an RTO/TON reset difference applies; the HMI must be re-pointed and re-scaled."),
        "pt": ("medium", "IEC PT (TIME) -> Logix .PRE (DINT ms)."),
        "q": ("medium", "Q -> .DN."),
        "in": ("medium", "IN -> rung condition / .EN."),
    },
    "iec_counter": {"cv": ("medium", "CV -> .ACC"), "pv": ("medium", "PV -> .PRE"), "q": ("medium", "Q -> .DN"),
                    "qu": ("medium", "QU -> .DN"), "qd": ("high", "QD has no single Logix equivalent (use ACC <= 0)")},
}

STOPWORDS = {"the", "a", "an", "of", "to", "for", "and", "or", "in", "on", "at", "is", "pb", "no", "nc", "-", "1", "2"}

ST_KEYWORDS = set("""IF THEN ELSE ELSIF END_IF CASE OF END_CASE FOR TO BY DO END_FOR WHILE END_WHILE REPEAT UNTIL
END_REPEAT AND OR XOR NOT MOD TRUE FALSE RETURN EXIT CONTINUE REGION END_REGION GOTO JMP BEGIN VAR END_VAR
AND_THEN OR_ELSE THIS SUPER NULL""".split())
ST_FUNCS = set("""ABS SQRT SQR LN LOG EXP SIN COS TAN ASIN ACOS ATAN EXPT MIN MAX LIMIT SEL MUX MOVE SHL SHR ROL ROR
LEN LEFT RIGHT MID CONCAT INSERT DELETE REPLACE FIND TRUNC ROUND CEIL FLOOR NORM_X SCALE_X SIZEOF ADR REF
TIME_TO_DINT DINT_TO_TIME TIME_TO_REAL REAL_TO_TIME TO_INT TO_DINT TO_REAL TO_BOOL TO_TIME
SCALE UNSCALE PEEK POKE SWAP UPPER_BOUND LOWER_BOUND T_ADD T_SUB T_DIFF WR_SYS_T RD_SYS_T RD_LOC_T""".split())
CONV_RE = re.compile(r"^[A-Z]+_TO_[A-Z]+$", re.I)


# --------------------------------------------------------------------------
# Normalisation and symbol index
# --------------------------------------------------------------------------

def norm_slc(addr):
    a = addr.upper().lstrip("#")
    m = re.match(r"^B(\d+)/(\d+)$", a)
    if m:
        bit = int(m.group(2))
        return "B%s:%d/%d" % (m.group(1), bit // 16, bit % 16)
    m = re.match(r"^([IO]):(\d+)\.0/(\d+)$", a)
    if m:
        a = "%s:%s/%s" % m.groups()
    a = re.sub(r"/(ACC|PRE|DN|EN|TT|CU|CD|OV|UN|UA|LEN|POS|ER|EU|EM|FD|IN|UL|RN|FN|SP|PV|CV)$", r".\1", a)
    return a


def norm_path(s):
    s = s.strip().strip("{}").strip()
    if SLC_RE.fullmatch(s):
        return norm_slc(s)
    s = s.replace('"', "")
    s = re.sub(r"\s*\[\s*", "[", s)
    s = re.sub(r"\s*\]\s*", "]", s)
    return s.lower()


def strip_index(p):
    return re.sub(r"\[[^\]]*\]", "[]", p)


class Index(object):
    """Declared symbols (lowercase key -> info)."""

    def __init__(self):
        self.sym = {}

    def add(self, key, **info):
        key = key.lower()
        cur = self.sym.setdefault(key, {"labels": []})
        for k, v in info.items():
            if k == "label":
                if v and v not in [l["text"] for l in cur["labels"]]:
                    cur["labels"].append({"source": info.get("label_source", "plc"), "text": v})
            elif v is not None and k != "label_source":
                cur.setdefault(k, v)
        return cur

    def resolve(self, path, prefixes):
        """Return (base_key, member) for a normalised path, trying scope prefixes."""
        if SLC_RE.fullmatch(path.upper()) or path.upper().startswith(("I:", "O:", "S:")):
            base = re.split(r"[./]", path, 1)[0] if re.match(r"^[TCR]\d*:\d+[./]", path.upper()) else path
            if re.match(r"^(N|F|L|B|ST|A|D)\d*:\d+/\d+$", path.upper()):
                return path.upper(), ""
            member = path[len(base) + 1:] if len(path) > len(base) else ""
            return base.upper(), member.lower()
        parts = re.split(r"(?<=[^\\])\.", path)
        for pre in prefixes:
            for n in range(len(parts), 0, -1):
                cand = pre + ".".join(parts[:n])
                if cand in self.sym:
                    return cand, ".".join(parts[n:])
        return prefixes[-1] + parts[0] if prefixes else parts[0], ".".join(parts[1:])


def build_index(projects):
    idx = Index()
    for pr in projects:
        fmt = pr["format"]
        io_labels = {}
        for m in pr.get("io_modules", []):
            if m.get("parent") == "Local" and m.get("address") and m.get("name") != "Local":
                prefix = "local:%s" % m["address"]
            else:
                prefix = m.get("name", "").lower()
            for c in m.get("io_comments", []):
                key = "%s:%s%s" % (prefix, "i" if c["direction"] == "input" else "o", (c.get("operand") or "").lower())
                io_labels[key] = c.get("comment")
                idx.add(key, label=c.get("comment"), label_source="io module comment", physical_input=c["direction"] == "input",
                        safety=m.get("safety"))
        for t in pr.get("tags", []):
            name = t.get("name") or ""
            scope = t.get("scope", "")
            if scope.startswith("program:"):
                key = "program:%s.%s" % (scope.split(":", 1)[1], name)
            else:
                key = name
            key = norm_path(key) if not SLC_RE.fullmatch(name) else norm_slc(name)
            alias = t.get("alias_for")
            addr = t.get("address")
            phys_in = bool(
                (alias and re.search(r":I(\.|$)", alias, re.I)) or (addr and re.match(r"^%?I[XBWD]?\d", addr, re.I)) or
                (addr and re.match(r"^I:", addr, re.I)) or re.match(r"^I:", name, re.I) or t.get("usage") == "Input")
            info = idx.add(key, display=name if not scope.startswith("program:") else "Program:%s.%s" % (scope.split(":", 1)[1], name),
                           data_type=t.get("data_type"), alias_for=alias, address=addr, physical_input=phys_in or None,
                           constant=t.get("constant"), safety=t.get("safety"), scope=scope,
                           label=t.get("description"), label_source="io map" if scope == "io map" else "plc tag description")
            if alias:
                al = io_labels.get(norm_path(alias))
                if al:
                    idx.add(key, label=al, label_source="io module comment")
            if addr and scope == "io map":
                a = norm_slc(addr) if SLC_RE.fullmatch(addr) else norm_path(addr)
                idx.add(a, display=addr, label=t.get("description"), label_source="io map")
        for r in pr.get("routines", []):
            owner = r["id"].lower()
            for sec in r.get("interface", []):
                s = sec["section"].lower()
                for m in sec["members"]:
                    idx.add("%s.%s" % (owner, m["name"].lower()), display="%s.%s" % (r["id"], m["name"]),
                            data_type=m.get("data_type"), physical_input=("input" in s or "inout" in s or "in_out" in s or
                                                                          bool(m.get("address") and m["address"].upper().startswith("%I"))) or None,
                            constant=m.get("constant"), address=m.get("address"), scope="local:" + r["id"],
                            label=m.get("description"), label_source="plc declaration comment")
        for a in pr.get("aois", []):
            owner = "aoi:%s" % a["name"].lower()
            for p in a.get("parameters", []) + a.get("local_tags", []):
                idx.add("%s.%s" % (owner, p["name"].lower()), display="%s.%s" % (a["name"], p["name"]),
                        data_type=p.get("data_type"), physical_input=p.get("usage") in ("Input", "InOut") or None,
                        scope="aoi:" + a["name"], label=p.get("description"), label_source="aoi parameter")
    return idx


# --------------------------------------------------------------------------
# Reference extraction from code
# --------------------------------------------------------------------------

def split_args(s):
    args, depth, cur = [], 0, []
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur or args:
        args.append("".join(cur).strip())
    return args


def operand_tokens(op):
    """Tag tokens inside an operand or expression (literals and functions removed)."""
    out = []
    op = re.sub(r"'[^']*'", " ", op)
    taken = []
    for m in SLC_RE.finditer(op):
        out.append(m.group(0))
        taken.append((m.start(), m.end()))
    for m in LOGIX_TOKEN_RE.finditer(op):
        if any(a <= m.start() < b for a, b in taken):
            continue
        tok = m.group(0)
        tail = op[m.end():].lstrip()
        if tok.upper() in CPT_FUNCS and tail.startswith("("):
            continue
        if tok.upper() in ("AND", "OR", "XOR", "NOT", "MOD"):
            continue
        if re.match(r"^\d", tok):
            continue
        out.append(tok)
        inner = re.findall(r"\[([^\]]*)\]", tok)
        for i in inner:
            out.extend(t for t in operand_tokens(i))
    return out


def ladder_instructions(text):
    """Yield (mnemonic, [operands]) from Logix neutral text or SLC ASCII."""
    if re.search(r"[A-Za-z_]\w*\(", text):
        i = 0
        for m in re.finditer(r"([A-Za-z_]\w*)\(", text):
            if m.start() < i:
                continue
            depth, j = 1, m.end()
            while j < len(text) and depth:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            i = j
            yield m.group(1).upper(), split_args(text[m.end():j - 1])
        return
    toks = text.split()
    k = 0
    while k < len(toks):
        mn = toks[k].upper()
        k += 1
        if mn in SLC_STRUCT:
            continue
        n = SLC_ARITY.get(mn)
        if n is None:
            ops = []
            while k < len(toks) and not (re.match(r"^[A-Z]{2,5}$", toks[k]) and toks[k] not in ("ACC", "PRE")):
                ops.append(toks[k])
                k += 1
            yield mn, ops
        else:
            yield mn, toks[k:k + n]
            k += n


def refs_from_ladder(text, aoi_params):
    out = []
    for mn, ops in ladder_instructions(text):
        if mn == "JSR":
            continue
        skip = NON_TAG_ARGS.get(mn, [])
        if skip == "all":
            continue
        if mn in aoi_params:
            params = aoi_params[mn]
            for i, op in enumerate(ops):
                if i == 0:
                    acc = "write"
                elif i - 1 < len(params):
                    u = params[i - 1].get("usage")
                    acc = "read" if u == "Input" else "write" if u == "Output" else "unknown"
                else:
                    acc = "unknown"
                for t in operand_tokens(op):
                    out.append((t, acc if t == op.strip() else "read", mn))
            continue
        if mn == "PID" and len(ops) == 3:
            writes = [0, 2]
        elif MOTION_PREFIX.match(mn):
            writes = [1]
        elif mn in WRITES:
            writes = WRITES[mn]
        elif mn in READS_ONLY:
            writes = []
        else:
            writes = None
        for i, op in enumerate(ops):
            if i in skip:
                continue
            toks = operand_tokens(op)
            for t in toks:
                if writes is None:
                    acc = "unknown"
                elif i in writes and t == op.strip().lstrip("#"):
                    acc = "write"
                elif i in writes and t == op.strip():
                    acc = "write"
                else:
                    acc = "read"
                out.append((t.lstrip("#"), acc, mn))
    return out


def strip_st(text, siemens):
    text = re.sub(r"\(\*.*?\*\)", " ", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    text = re.sub(r"'(?:[^'$]|\$.)*'", "''", text)
    if not siemens:
        text = re.sub(r'"(?:[^"$]|\$.)*"', '""', text)
    text = re.sub(r"\b(T|TIME|LT|LTIME|TOD|D|DT|DATE|TIME_OF_DAY|DATE_AND_TIME)#[\w.:-]+", " 0 ", text, flags=re.I)
    text = re.sub(r"\b\w+#[\w.]+", " 0 ", text)  # typed literals 16#FF, INT#5
    return text


ST_OPERAND_RE = re.compile(
    r'(?:"[^"]+"|#?[A-Za-z_]\w*|%[IQM][XBWDL]?\d+(?:\.\d+)?)(?:\s*\[[^\]]*\])?(?:\.(?:"[^"]+"|[A-Za-z_%]\w*)(?:\s*\[[^\]]*\])?)*')


def refs_from_st(text, siemens, known_types):
    """Return (token, access, kind) for an ST/SCL body."""
    s = strip_st(text, siemens)
    out = []
    depth_at = []
    d = 0
    for ch in s:
        depth_at.append(d)
        if ch == "(":
            d += 1
        elif ch == ")":
            d -= 1
    for m in ST_OPERAND_RE.finditer(s):
        tok = m.group(0)
        head = re.split(r"[.\[]", tok.lstrip("#"), 1)[0].strip('"')
        if head.upper() in ST_KEYWORDS:
            continue
        before = s[:m.start()].rstrip()
        after = s[m.end():].lstrip()
        if before.endswith("=>"):
            out.append((tok, "write", "output-param"))
            continue
        if after.startswith(":=") or re.match(r"^(S|R)=", after):
            dep = depth_at[m.start()] if m.start() < len(depth_at) else 0
            if dep == 0:
                out.append((tok, "write", "assign"))
            # inside a call: formal parameter name, not a tag
            continue
        if after.startswith("=>"):
            continue  # formal output parameter name
        if after.startswith("("):
            up = head.upper()
            if up in ST_FUNCS or CONV_RE.match(up) or up in known_types or before.endswith(":"):
                continue
            out.append((tok, "write", "call"))  # FB instance call updates the instance
            continue
        if before.endswith(":") and not before.endswith(":="):
            continue  # declaration / case label / type
        out.append((tok, "read", "expr"))
    return out


def code_refs(projects):
    """Yield dicts describing every reference in PLC code."""
    aoi_params = {}
    known_types = set()
    for pr in projects:
        for a in pr.get("aois", []):
            aoi_params[a["name"].upper()] = [p for p in a.get("parameters", [])
                                             if p.get("required") and p["name"] not in ("EnableIn", "EnableOut")]
        for r in pr.get("routines", []):
            known_types.add(r["name"].upper())
        for dt in pr.get("data_types", []):
            known_types.add(dt["name"].upper())
    for pr in projects:
        fmt = pr["format"]
        siemens = fmt.startswith("siemens")
        for r in pr.get("routines", []):
            rid = r["id"]
            if fmt == "rockwell-l5x":
                prog = r.get("program") or ""
                prefixes = ["aoi:%s." % prog[4:].lower()] if prog.startswith("AOI:") else []
                prefixes += ["program:%s." % prog.lower(), ""]
            elif siemens:
                prefixes = [rid.lower() + ".", ""]
            else:
                prefixes = [rid.lower() + "."] + ([r["program"].lower() + "."] if r.get("program") else []) + [""]
            items = [("rung", x) for x in r.get("rungs", [])] + [("line", x) for x in r.get("lines", [])] + \
                    [("network", x) for x in r.get("networks", [])]
            st_lines = [x for kind, x in items if kind == "line"]
            if st_lines:
                # analyse statements across line breaks, then attribute to the line of the token
                body = "\n".join(x["text"] for x in st_lines)
                offsets = []
                pos = 0
                for x in st_lines:
                    offsets.append((pos, x["number"]))
                    pos += len(x["text"]) + 1
                lang_siemens = siemens or r.get("language") == "SCL"
                for tok, acc, kind in refs_from_st(body, lang_siemens, known_types):
                    yield {"routine": rid, "where": "%s line %s" % (rid, line_of(body, tok, offsets)), "token": tok,
                           "access": acc, "instr": kind, "prefixes": prefixes, "siemens": lang_siemens,
                           "system": "plc", "file": pr["file"]}
            for kind, x in items:
                if kind == "line":
                    continue
                where = "%s %s %s" % (rid, kind, x.get("number"))
                if x.get("withheld"):
                    continue
                if x.get("refs"):
                    for ref in x["refs"]:
                        yield {"routine": rid, "where": where, "token": ref["tag"], "access": ref["access"],
                               "instr": ref.get("instr"), "prefixes": prefixes, "siemens": siemens, "system": "plc",
                               "file": pr["file"]}
                    continue
                txt = x.get("text") or ""
                if kind == "network" and (r.get("language") in ("SCL", "STL") or x.get("language") in ("SCL", "STL")):
                    found = refs_from_st(txt, True, known_types)
                else:
                    found = refs_from_ladder(txt, aoi_params)
                for tok, acc, instr in found:
                    yield {"routine": rid, "where": where, "token": tok, "access": acc, "instr": instr,
                           "prefixes": prefixes, "siemens": siemens, "system": "plc", "file": pr["file"]}


_line_cursor = {}


def line_of(body, tok, offsets):
    key = (id(body), tok)
    start = _line_cursor.get(key, 0)
    i = body.find(tok, start)
    if i < 0:
        i = body.find(tok)
    _line_cursor[key] = i + 1 if i >= 0 else 0
    num = offsets[0][1] if offsets else 0
    for off, n in offsets:
        if off <= i:
            num = n
    return num


def code_token_path(tok, siemens, prefixes):
    t = tok.strip()
    if SLC_RE.fullmatch(t):
        return norm_slc(t), [""]
    if siemens and t.startswith("#"):
        return norm_path(t[1:]), prefixes[:1]
    if siemens and t.startswith('"'):
        return norm_path(t), [""]
    return norm_path(t.lstrip("\\")), prefixes


# --------------------------------------------------------------------------
# HMI / SCADA extraction
# --------------------------------------------------------------------------

HMI_BRACED_RE = re.compile(r"\{(?:::)?\[([^\]]+)\]([^}]+)\}")
HMI_SHORTCUT_RE = re.compile(r"(?:::)?\[([A-Za-z_][\w ]*)\]((?:Program:)?[A-Za-z_][\w:.\[\]/]*)")
OPC_RE = re.compile(r"ns=\d+;s=(?:\[([^\]]+)\])?([^\s\"'<>,;]+)")
SIEMENS_SYM_RE = re.compile(r'"[^"]+"(?:\.[A-Za-z_]\w*)*')
WRITER_WORDS = re.compile(r"(?i)(button|input|numeric.?entry|write|setpoint|toggle|maintained|momentary|latched|slider|"
                          r"entry|recipe|setvalue|set_value|onclick|push|selector|spinner)")
LABEL_ATTRS = ("caption", "text", "label", "description", "tooltip", "tooltiptext", "title", "documentation", "alias")
REF_ATTRS = ("tag", "tagname", "address", "expression", "connection", "value", "source", "item", "itempath",
             "opcitempath", "variable", "plctag", "tagref", "tagpath", "binding", "indicator")
SCRIPT_WRITE_RE = re.compile(r"(?i)(write|\.value\s*=(?!=)|settag|setvalue|set_value|tags?\(.+\)\s*=(?!=)|"
                             r"smarttags\(.+\)\s*=(?!=)|\bput\b)")
SCRIPT_EXT = (".vbs", ".vba", ".bas", ".js", ".py", ".cs", ".txt", ".vb", ".ps1", ".jy")


def hmi_ref(raw):
    """Return (device, path) or None for an HMI reference string."""
    m = HMI_BRACED_RE.search(raw)
    if m:
        return m.group(1), m.group(2)
    m = OPC_RE.search(raw)
    if m:
        return m.group(1), m.group(2)
    m = HMI_SHORTCUT_RE.search(raw)
    if m:
        return m.group(1), m.group(2)
    return None


def hmi_path(path):
    p = path.strip()
    m = re.match(r"(?i)^program:([^.]+)\.(.+)$", p)
    if m:
        return "program:%s.%s" % (m.group(1).lower(), norm_path(m.group(2)))
    return norm_path(p)


def scan_csv(path_, text):
    rows = list(csv.reader(io.StringIO(text)))
    hi = None
    for i, row in enumerate(rows[:30]):
        low = [c.strip().strip(";").lower() for c in row]
        if any(c in ("address", "tag name", "name", "tagname", "item", "opc item", "plc tag", "connection", "source",
                     "tag", "data source", "opcitempath") for c in low) and len(low) > 1:
            hi = i
            header = low
            break
    out = []
    if hi is None:
        return out

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    ci_name = col("tag name", "tagname", "name", "tag")
    ci_addr = col("address", "opc item", "item", "plc tag", "connection", "opcitempath", "source tag", "external tag")
    ci_desc = col("tag description", "description", "comment", "label", "documentation")
    ci_ro = col("read only", "readonly")
    ci_acc = col("access", "access mode", "accessrights", "direction", "rw")
    for n, row in enumerate(rows[hi + 1:], start=hi + 2):
        cells = [c.strip() for c in row]
        if not any(cells):
            continue
        g = lambda i: cells[i] if i is not None and i < len(cells) else ""
        target = None
        for cand in ([g(ci_addr)] if ci_addr is not None else []) + cells:
            r = hmi_ref(cand)
            if r:
                target = r
                break
        if target is None and ci_addr is not None and g(ci_addr):
            target = (None, g(ci_addr))
        if target is None:
            continue
        ro, acc = g(ci_ro).lower(), g(ci_acc).lower()
        if ro in ("t", "true", "yes", "1", "y"):
            access = "read"
        elif ro in ("f", "false", "no", "0", "n"):
            access = "may_write"
        elif acc:
            access = "may_write" if "w" in acc else "read"
        else:
            access = "read"
        out.append({"device": target[0], "path": target[1], "access": access, "label": g(ci_desc) or None,
                    "hmi_name": g(ci_name) or None, "where": "%s row %d%s" % (os.path.basename(path_), n,
                                                                              " (%s)" % g(ci_name) if g(ci_name) else "")})
    return out


def scan_xml(path_, text, known):
    out = []
    try:
        root = safe_xml.fromstring(text)
    except (safe_xml.UnsafeXMLError, safe_xml.ParseError) as e:
        return [{"error": "XML parse error in %s: %s" % (path_, e)}]

    def walk(el, ancestry, label):
        n = pp.lname(el)
        attrs = dict((k.split("}")[-1].lower(), v) for k, v in el.attrib.items())
        lab = next((attrs[a] for a in LABEL_ATTRS if attrs.get(a)), None) or label
        anc = ancestry + [n] + [attrs.get("name", ""), attrs.get("type", "")]
        writer = bool(WRITER_WORDS.search(" ".join(anc[-6:])))
        vals = [(k, v) for k, v in attrs.items()] + ([("#text", el.text)] if el.text and el.text.strip() else [])
        for k, v in vals:
            r = hmi_ref(v)
            refs = []
            if r:
                refs.append(r)
                for m in HMI_BRACED_RE.finditer(v):
                    if (m.group(1), m.group(2)) != r:
                        refs.append((m.group(1), m.group(2)))
            elif k in REF_ATTRS or k == "#text":
                for m in SIEMENS_SYM_RE.finditer(v):
                    refs.append((None, m.group(0)))
                for m in SLC_RE.finditer(v):
                    refs.append((None, m.group(0)))
                if not refs and k in REF_ATTRS and v.strip().lower() in known:
                    refs.append((None, v.strip()))
            for dev, pth in refs:
                acc = "write" if writer and k.lower() in ("value", "tag", "connection", "expression", "#text", "tagname",
                                                          "address", "binding", "variable") else "read"
                out.append({"device": dev, "path": pth, "access": acc, "label": lab,
                            "where": "%s <%s%s>" % (os.path.basename(path_), n,
                                                     " name=%s" % attrs["name"] if attrs.get("name") else "")})
        for c in list(el):
            walk(c, anc, lab)

    walk(root, [], None)
    return out


def scan_json(path_, text, ign_map):
    out = []
    try:
        data = json.loads(text)
    except ValueError as e:
        return [{"error": "JSON parse error in %s: %s" % (path_, e)}]

    def walk(o, folder, label):
        if isinstance(o, dict):
            name = o.get("name")
            lab = o.get("documentation") or o.get("tooltip") or o.get("tooltipText") or label
            if o.get("opcItemPath"):
                r = hmi_ref(str(o["opcItemPath"])) or (None, str(o["opcItemPath"]))
                ro = str(o.get("readOnly", "")).lower() == "true" or "read_only" in str(o.get("accessRights", "")).lower()
                full = "/".join([f for f in folder + [name or ""] if f])
                ign_map[full.lower()] = r
                out.append({"device": r[0], "path": r[1], "access": "read" if ro else "may_write", "label": lab,
                            "hmi_name": full, "where": "%s tag %s" % (os.path.basename(path_), full)})
            if o.get("tagPath"):
                tp = str(o["tagPath"])
                out.append({"ignition_path": tp, "access": "write" if o.get("bidirectional") else "read",
                            "label": lab, "where": "%s binding %s" % (os.path.basename(path_), tp)})
            sub = folder + [name] if o.get("tagType") == "Folder" and name else folder
            for k, v in o.items():
                walk(v, sub, lab)
        elif isinstance(o, list):
            for v in o:
                walk(v, folder, label)

    walk(data, [], None)
    return out


def scan_script(path_, text, known):
    out = []
    for n, line in enumerate(text.splitlines(), start=1):
        if re.match(r"^\s*('|//|#|REM\b)", line, re.I):
            continue
        write = bool(SCRIPT_WRITE_RE.search(line))
        for m in re.finditer(r"\"([^\"]+)\"|'([^']+)'", line):
            s = m.group(1) or m.group(2)
            r = hmi_ref(s)
            if r is None and (SLC_RE.fullmatch(s.strip()) or s.strip().lower() in known):
                r = (None, s.strip())
            if r is None and re.match(r"^\[[^\]]+\]", s):
                out.append({"ignition_path": s, "access": "write" if write else "read",
                            "where": "%s line %d" % (os.path.basename(path_), n)})
                continue
            if r:
                out.append({"device": r[0], "path": r[1], "access": "write" if write else "read",
                            "where": "%s line %d" % (os.path.basename(path_), n)})
    return out


def scan_hmi_files(files, known):
    refs, errors, ign_map = [], [], {}
    pending_ign = []
    for f in files:
        text = pp.read_text(f)
        ext = os.path.splitext(f)[1].lower()
        if ext == ".csv":
            got = scan_csv(f, text)
        elif ext == ".json":
            got = scan_json(f, text, ign_map)
        elif text.lstrip().startswith("<"):
            got = scan_xml(f, text, known)
        elif ext in SCRIPT_EXT or True:
            got = scan_script(f, text, known)
        for g in got:
            g["file"] = f
            if "error" in g:
                errors.append(g["error"])
            elif "ignition_path" in g:
                pending_ign.append(g)
            else:
                refs.append(g)
    for g in pending_ign:
        tp = re.sub(r"^\[[^\]]*\]", "", g["ignition_path"]).lower()
        r = ign_map.get(tp)
        if r:
            g = dict(g, device=r[0], path=r[1])
            refs.append(g)
        else:
            errors.append("%s: Ignition tag path %s not found in any supplied tag export; supply the tag JSON to resolve it."
                          % (g["where"], g["ignition_path"]))
    return refs, errors


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------

def struct_kind(info, base):
    dt = (info or {}).get("data_type") or ""
    k = STRUCT_KIND.get(dt.upper())
    if k:
        return k
    m = re.match(r"^(T|C|R|PD|MG)\d*:\d+$", base.upper())
    if m:
        return SLC_FILE_KIND.get(m.group(1))
    return None


def words(s):
    return set(w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOPWORDS)


OPPOSITES = [("open", "closed"), ("open", "close"), ("start", "stop"), ("on", "off"), ("high", "low"), ("full", "empty"),
             ("rejected", "accepted"), ("rejected", "completed"), ("rejected", "good"), ("reject", "accept"),
             ("fault", "ok"), ("fault", "healthy"), ("fill", "drain"), ("forward", "reverse"), ("up", "down"),
             ("raise", "lower"), ("enable", "disable"), ("enabled", "disabled"), ("auto", "manual"), ("in", "out"),
             ("inlet", "outlet"), ("left", "right"), ("min", "max"), ("minimum", "maximum"), ("pass", "fail"),
             ("heat", "cool"), ("extend", "retract"), ("lock", "unlock"), ("energize", "deenergize")]


def contradiction(a, b):
    wa, wb = set(re.findall(r"[a-z]+", (a or "").lower())), set(re.findall(r"[a-z]+", (b or "").lower()))
    for x, y in OPPOSITES:
        if (x in wa and y in wb and y not in wa and x not in wb) or (y in wa and x in wb and x not in wa and y not in wb):
            return "%s/%s" % (x, y)
    return None


def jaccard(a, b):
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 1.0
    return len(wa & wb) / float(len(wa | wb))


def load_mapping(path_):
    rows = list(csv.reader(io.StringIO(pp.read_text(path_))))
    if not rows:
        return {}
    head = [c.strip().lower() for c in rows[0]]
    oi = next((i for i, h in enumerate(head) if h in ("old", "source", "from", "old address", "old tag", "legacy")), 0)
    ni = next((i for i, h in enumerate(head) if h in ("new", "target", "to", "new address", "new tag")), 1)
    start = 1 if any(h in ("old", "source", "from", "old address", "old tag", "legacy") for h in head) else 0
    out = {}
    for r in rows[start:]:
        if len(r) > max(oi, ni) and r[oi].strip():
            k = r[oi].strip()
            out[norm_slc(k) if SLC_RE.fullmatch(k) else norm_path(k)] = r[ni].strip()
    return out


def analyse(projects, hmi_refs, target=None, mapping=None):
    idx = build_index(projects)
    refs = []
    for c in code_refs(projects):
        path_, prefixes = code_token_path(c["token"], c["siemens"], c["prefixes"])
        base, member = idx.resolve(path_, prefixes)
        refs.append({"base": base, "member": member, "path": base + ("." + member if member else ""),
                     "display": c["token"], "access": c["access"], "instr": c["instr"], "system": "plc",
                     "where": c["where"], "file": os.path.basename(c["file"])})
    unresolved = []
    for h in hmi_refs:
        p = hmi_path(h["path"])
        if SLC_RE.fullmatch(h["path"].strip()):
            p = norm_slc(h["path"].strip())
        parts = p.split(".")
        found = None
        for start in range(len(parts)):
            cand = ".".join(parts[start:])
            b, m = idx.resolve(cand, [""])
            if b in idx.sym or SLC_RE.fullmatch(b) or b.upper().startswith(("I:", "O:")):
                found = (b, m)
                break
        rec = {"display": h["path"], "access": h["access"], "instr": "hmi", "system": "hmi", "where": h["where"],
               "file": os.path.basename(h["file"]), "label": h.get("label"), "device": h.get("device"),
               "hmi_name": h.get("hmi_name")}
        if found is None:
            b, m = idx.resolve(p, [""])
            rec.update(base=b, member=m, path=p, unresolved=True)
            unresolved.append(rec)
        else:
            rec.update(base=found[0], member=found[1], path=found[0] + ("." + found[1] if found[1] else ""))
        refs.append(rec)

    by_base = {}
    for r in refs:
        by_base.setdefault(r["base"], []).append(r)

    conflicts = []
    questions = []

    def add(ctype, sev, tag, detail, evidence):
        conflicts.append({"type": ctype, "severity": sev, "tag": tag, "detail": detail,
                          "evidence": sorted(set(evidence))[:12]})

    # broken links
    for r in unresolved:
        add("broken_link", "high", r["display"],
            "%s references '%s' but no PLC tag or address with that name exists in the supplied PLC files." % (r["system"].upper(), r["display"]),
            [r["where"]])

    for base, rs in by_base.items():
        info = idx.sym.get(base, {})
        disp = info.get("display") or rs[0]["display"].split(".")[0].strip("{}") if not info.get("display") else info["display"]
        if info.get("safety"):
            continue  # safety tags are listed but never analysed further
        plc_w = [r for r in rs if r["system"] == "plc" and r["access"] == "write"]
        plc_unknown = [r for r in rs if r["system"] == "plc" and r["access"] == "unknown"]
        hmi_w = [r for r in rs if r["system"] == "hmi" and r["access"] == "write"]
        hmi_mw = [r for r in rs if r["system"] == "hmi" and r["access"] == "may_write"]
        reads = [r for r in rs if r["access"] == "read"]

        # competing writers, per exact path (array indices collapsed)
        paths = {}
        for r in plc_w + hmi_w + hmi_mw:
            paths.setdefault(strip_index(r["path"]), []).append(r)
        for p, ws in paths.items():
            p = ws[0]["display"] if strip_index(norm_path(ws[0]["display"].lstrip("#"))).endswith(p.split(".")[-1]) else p
            ote = [w for w in ws if w["instr"] in ("OTE", "Coil", "coil", "assign") and w["system"] == "plc"]
            ote_coils = [w for w in ws if w["instr"] in ("OTE", "Coil", "coil")]
            timers = [w for w in ws if w["instr"] in ("TON", "TOF", "RTO", "CTU", "CTD", "TONR", "TOFR", "RTOR", "CTUD")]
            plc_routines = set(w["where"] for w in ws if w["system"] == "plc")
            moves = [w for w in ws if w["system"] == "plc" and w["instr"] not in ("OTL", "OTU", "SCoil", "RCoil", "coil:set", "coil:reset")]
            if len(ote_coils) > 1:
                add("competing_writers", "high", p, "Destructive output written by %d coils (OTE/=) - only the last one executed in the scan wins; "
                    "translation may reorder execution." % len(ote_coils), [w["where"] for w in ote_coils])
            if len(set(w["where"] for w in timers)) > 1:
                add("competing_writers", "high", p, "Same timer/counter structure driven by more than one instruction.",
                    [w["where"] for w in timers])
            p_hmi_w = [w for w in ws if w["system"] == "hmi" and w["access"] == "write"]
            p_hmi_mw = [w for w in ws if w["system"] == "hmi" and w["access"] == "may_write"]
            p_plc = [w for w in ws if w["system"] == "plc"]
            if p_hmi_w and p_plc and (ote or moves):
                add("competing_writers", "high", p, "HMI/SCADA writes this tag and PLC logic also writes it; the PLC will overwrite the operator value.",
                    [w["where"] for w in p_hmi_w + ote + moves])
            elif p_hmi_mw and ote:
                add("competing_writers", "medium", p, "HMI tag is not read-only but the PLC drives this tag every scan; any HMI write is overwritten.",
                    [w["where"] for w in p_hmi_mw + ote])
            ass = [w for w in moves if w["instr"] not in ("OTE", "Coil", "coil") and w["instr"] not in
                   ("TON", "TOF", "RTO", "CTU", "CTD", "TONR", "TOFR", "RTOR", "CTUD", "ONS", "OSR", "OSF", "call", "RES")]
            if len(set(w["where"] for w in ass)) > 1 and not ote_coils:
                add("competing_writers", "low", p, "Written from %d places; confirm the writes are mutually exclusive (sequence state, mode select)."
                    % len(set(w["where"] for w in ass)), [w["where"] for w in ass])

        # read but never written
        only_broken = all(r.get("unresolved") for r in rs)
        if reads and not only_broken and not plc_w and not plc_unknown and not hmi_w and not info.get("physical_input") and not info.get("constant"):
            if base.upper().startswith(("I:", "S:")) or re.match(r"^local:\d+:i", base) or re.search(r":i(\.|$)", base) \
                    or re.match(r"^%?i[xbwd]?\d", base) or ":i." in base:
                pass
            elif hmi_mw:
                questions.append({"tag": disp, "question": "'%s' is read by the PLC but only the HMI can write it (%s). Confirm it is an operator "
                                                          "setpoint/command and what its default is after a power cycle." % (disp, ", ".join(sorted(set(r["where"] for r in hmi_mw)))[:200]),
                                  "evidence": sorted(set(r["where"] for r in reads))[:8]})
            else:
                add("read_never_written", "medium", disp, "Read in logic/HMI but never written by any supplied PLC code or HMI file.",
                    [r["where"] for r in reads])
                questions.append({"tag": disp, "question": "Who or what sets '%s'? It is read (%s) but never written in the files supplied. "
                                                          "Possibilities: written by another controller/SCADA, set once from the programming terminal, "
                                                          "a forced bit, dead logic, or a field input whose I/O alias/map was not supplied." % (disp, ", ".join(sorted(set(r["where"] for r in reads)))[:240]),
                                  "evidence": sorted(set(r["where"] for r in reads))[:8]})

        # label mismatch: contradictions anywhere (incl. HMI labels on members), weak overlap on whole-tag labels
        labels = list(info.get("labels", []))
        for r in rs:
            if r.get("label") and r["system"] == "hmi":
                labels.append({"source": "hmi " + r["file"], "text": r["label"], "member": r.get("member")})
        seen_pairs = set()
        for i in range(len(labels)):
            for j in range(i + 1, len(labels)):
                a, b = labels[i], labels[j]
                if a["source"] == b["source"] or (a["text"], b["text"]) in seen_pairs:
                    continue
                seen_pairs.add((a["text"], b["text"]))
                opp = contradiction(a["text"], b["text"])
                if opp:
                    add("label_mismatch", "high", disp, "Descriptions contradict (%s): '%s' (%s) vs '%s' (%s)" % (
                        opp, a["text"], a["source"], b["text"], b["source"]), [a["source"], b["source"]])
                elif not a.get("member") and not b.get("member") and jaccard(a["text"], b["text"]) < 0.2:
                    add("label_mismatch", "medium", disp, "Descriptions disagree: '%s' (%s) vs '%s' (%s)" % (
                        a["text"], a["source"], b["text"], b["source"]), [a["source"], b["source"]])

        # translation breaks
        kind = struct_kind(info, base)
        external = [r for r in rs if r["system"] == "hmi"]
        for r in external:
            mem = (r.get("member") or "").split(".")[0].lower()
            rule = MEMBER_RULES.get(kind or "", {}).get(mem)
            ctype = "translation_break" if target else "translation_watch"
            if rule:
                add(ctype, rule[0], "%s.%s" % (disp, mem.upper()), rule[1], [r["where"]])
            elif target and (r["base"].upper().startswith(("I:", "O:", "N", "B", "F", "T", "C")) and SLC_RE.fullmatch(r["base"])):
                add(ctype, "high", r["display"], "Data-table address will not exist on the target; the HMI must be re-pointed to the new symbol.",
                    [r["where"]])
            if mapping is not None:
                key = r["path"]
                if key not in mapping and r["base"] not in mapping:
                    add("translation_break", "high", r["display"], "Referenced by HMI/SCADA but missing from the old->new mapping table.",
                        [r["where"]])

    # tag table
    table = []
    for base, rs in sorted(by_base.items(), key=lambda kv: kv[0]):
        info = idx.sym.get(base, {})
        row = {"tag": info.get("display") or rs[0]["display"], "key": base,
               "status": "not found in PLC" if all(r.get("unresolved") for r in rs) else None, "data_type": info.get("data_type"),
               "address": info.get("address") or info.get("alias_for"),
               "descriptions": info.get("labels", []) + [{"source": "hmi " + r["file"], "text": r["label"]}
                                                          for r in rs if r.get("label") and r["system"] == "hmi"],
               "readers": sorted(set("%s%s" % (r["where"], " (." + r["member"] + ")" if r.get("member") else "") for r in rs if r["access"] == "read")),
               "writers": sorted(set("%s [%s]%s" % (r["where"], r["instr"], " (." + r["member"] + ")" if r.get("member") else "")
                                     for r in rs if r["access"] == "write")),
               "may_write": sorted(set(r["where"] for r in rs if r["access"] == "may_write")),
               "unknown_access": sorted(set("%s [%s]" % (r["where"], r["instr"]) for r in rs if r["access"] == "unknown")),
               "systems": sorted(set(r["system"] for r in rs))}
        if info.get("safety"):
            row["safety"] = info["safety"]
        if info.get("physical_input") or re.match(r"^I:", base) or re.match(r"^local:\d+:i", base):
            row["physical_input"] = True
        table.append(pp.compact(row))

    sev_rank = {"high": 0, "medium": 1, "low": 2, "info": 3}
    conflicts.sort(key=lambda c: (sev_rank.get(c["severity"], 9), c["type"], c["tag"]))
    excluded = [dict(e, file=os.path.basename(pr["file"])) for pr in projects for e in pr.get("excluded_safety", [])]
    return {
        "tool": "rungstone/dependency_map",
        "target": target,
        "summary": {"tags": len(table), "references": len(refs), "conflicts": len(conflicts),
                    "by_type": dict((t, sum(1 for c in conflicts if c["type"] == t)) for t in sorted(set(c["type"] for c in conflicts))),
                    "open_questions": len(questions), "excluded_safety_items": len(excluded)},
        "tags": table,
        "conflicts": conflicts,
        "open_questions": questions,
        "excluded_safety": excluded,
        "notes": ["Access is derived statically from instruction semantics; indirect addressing, pointers and "
                  "communications instructions can hide additional readers/writers.",
                  "Safety-flagged tags are listed but not analysed; excluded safety logic was not read."],
    }


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------

def md_cell(s):
    return (s or "").replace("|", "\\|").replace("\n", " ")


def to_markdown(res):
    L = ["# Dependency map", ""]
    s = res["summary"]
    L.append("%d tags, %d references, %d conflicts (%s), %d open questions. Target: %s." % (
        s["tags"], s["references"], s["conflicts"], ", ".join("%s %d" % kv for kv in s["by_type"].items()) or "none",
        s["open_questions"], res.get("target") or "not specified"))
    if res["excluded_safety"]:
        L += ["", "## Excluded safety logic (not analysed)", "", "| Item | Kind | Reason | Confidence | File |", "|---|---|---|---|---|"]
        for e in res["excluded_safety"]:
            L.append("| %s | %s | %s | %s | %s |" % (md_cell(e["name"]), e["kind"], md_cell(e["reason"]), e["confidence"], e["file"]))
    L += ["", "## Conflicts", "", "| Severity | Type | Tag | Detail | Evidence |", "|---|---|---|---|---|"]
    for c in res["conflicts"]:
        L.append("| %s | %s | %s | %s | %s |" % (c["severity"], c["type"], md_cell(c["tag"]), md_cell(c["detail"]),
                                              md_cell("; ".join(c["evidence"]))))
    if not res["conflicts"]:
        L.append("| - | - | - | none found | - |")
    L += ["", "## Open questions", ""]
    for i, q in enumerate(res["open_questions"], 1):
        L.append("%d. %s" % (i, q["question"]))
    if not res["open_questions"]:
        L.append("None.")
    L += ["", "## Cross-reference", "", "| Tag | Type | Address/alias | Descriptions | Readers | Writers | Notes |",
          "|---|---|---|---|---|---|---|"]
    for t in res["tags"]:
        notes = []
        if t.get("safety"):
            notes.append("SAFETY - not analysed")
        if t.get("physical_input"):
            notes.append("physical/block input")
        if t.get("may_write"):
            notes.append("HMI may write: " + "; ".join(t["may_write"]))
        if t.get("unknown_access"):
            notes.append("unknown access: " + "; ".join(t["unknown_access"]))
        L.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            md_cell(t["tag"]), md_cell(t.get("data_type")), md_cell(t.get("address")),
            md_cell("; ".join("%s (%s)" % (d["text"], d["source"]) for d in t.get("descriptions", []))),
            md_cell("; ".join(t.get("readers", []))), md_cell("; ".join(t.get("writers", []))), md_cell("; ".join(notes))))
    L += ["", "_Static analysis of supplied files only. %s_" % " ".join(res["notes"])]
    return "\n".join(L) + "\n"


def load_projects(files, allow):
    ctx = pp.Ctx(allow)
    projects = []
    for f in files:
        if f.lower().endswith(".json"):
            try:
                d = json.loads(pp.read_text(f))
                if d.get("tool") == "rungstone/parse_project":
                    projects.extend(d["projects"])
                    continue
            except ValueError:
                pass
        pr = pp.parse_file(f, ctx)
        pp.flag_safety_refs(pr)
        projects.append(pr)
    for pr in projects:
        for k in ("tags", "routines", "aois", "io_modules", "excluded_safety", "data_types"):
            pr.setdefault(k, [])
    return projects


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plc", nargs="+", required=True, help="PLC exports or parse_project.py JSON")
    ap.add_argument("--hmi", nargs="*", default=[], help="HMI/SCADA files: tag CSV, display XML, Ignition JSON, scripts")
    ap.add_argument("--target", choices=["siemens-scl", "iec-st", "rockwell"], help="planned translation target")
    ap.add_argument("--mapping", help="CSV with old,new columns (address mapping table from a translation)")
    ap.add_argument("--allow-suspected", action="append", default=[], metavar="NAME")
    ap.add_argument("-o", "--output", help="write JSON here (default stdout)")
    ap.add_argument("--md", help="also write a Markdown report here")
    args = ap.parse_args(argv)

    projects = load_projects(args.plc, args.allow_suspected)
    idx = build_index(projects)
    known = set(k for k in idx.sym) | set(k.split(".")[-1] for k in idx.sym)
    hmi_refs, errors = scan_hmi_files(args.hmi, known)
    mapping = load_mapping(args.mapping) if args.mapping else None
    res = analyse(projects, hmi_refs, args.target, mapping)
    if errors:
        res["warnings"] = errors
    for pr in projects:
        for w in pr.get("warnings", []):
            res.setdefault("warnings", []).append(w)
    s = json.dumps(res, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(s)
    else:
        sys.stdout.write(s + "\n")
    if args.md:
        with open(args.md, "w", encoding="utf-8") as f:
            f.write(to_markdown(res))
    sm = res["summary"]
    sys.stderr.write("dependency map: %d tags, %d conflicts, %d open questions, %d safety exclusions\n" % (
        sm["tags"], sm["conflicts"], sm["open_questions"], sm["excluded_safety_items"]))


if __name__ == "__main__":
    main()
