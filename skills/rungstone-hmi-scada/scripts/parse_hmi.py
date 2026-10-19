"""
parse_hmi.py - inventory HMI/SCADA exports into compact JSON.

Reads (format detected from content):
  * FactoryTalk View ME/SE display XML exports (<gfx> ...): objects, captions,
    connections, animations, button actions (momentary/maintained/latched,
    numeric input, goto display, ME press/release commands)
  * Ignition: tag exports (tags.json, or 7.x/8.x tag XML), alarm configs on
    tags, Perspective view.json (components, tag/expression bindings, event
    scripts, navigation), Vision window XML (generic scan)
  * WinCC (Comfort/Advanced/Unified) tag and alarm exports as CSV or XLSX
    (HMI tags, discrete/analog alarms), generic WinCC/Openness XML (scan)
  * generic tag database CSVs (FactoryTalk tag export, etc.)
  * scripts as TEXT ONLY: VBScript/VBA, Python/Jython, JavaScript.  Never run.

Output: {"screens": [...], "tags": [...], "alarms": [...], "scripts": [...],
         "refs": [...aggregated PLC references with readers/writers...],
         "warnings": [...]}

Every PLC reference is reported as {device, path, norm}.  Ignition tag paths
used by views/scripts are resolved through any tag export passed in the same
run (tag path -> OPC item path -> PLC tag).

Usage:
  python3 parse_hmi.py FILE_OR_DIR [...] [-o out.json] [--pretty]
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hmi_common as hc  # noqa: E402
from _shared import safe_xml  # noqa: E402

FT_WRITERS = {
    "momentarypushbutton": "momentary (1 while pressed, 0 on release)",
    "maintainedpushbutton": "toggle (maintained)",
    "latchedpushbutton": "set 1 (latched until PLC handshake resets it)",
    "interlockedpushbutton": "write button value",
    "multistatepushbutton": "write next state value",
    "numericinput": "write numeric entry",
    "numericinputenable": "write numeric entry",
    "numericinputcursorpoint": "write numeric entry",
    "stringinput": "write string entry",
    "stringinputenable": "write string entry",
    "rampbutton": "ramp value up/down",
    "slider": "write slider value",
}
FT_READERS = ("numericdisplay", "stringdisplay", "multistateindicator", "listindicator", "symbolindicator",
              "bargraph", "gauge", "trend", "alarmbanner", "alarmsummary", "indicator", "display")
FT_SKIP = {"displaysettings", "state", "states", "connection", "connections", "animations", "parameters"}
IGN_WRITE_COMPONENTS = ("input", "toggle", "slider", "checkbox", "dropdown", "numeric-entry", "text-field", "switch")
SIDE_EFFECT_RE = re.compile(r"(?i)(system\.db\.\w+|system\.net\.\w+|system\.util\.execute|system\.file\.write\w*|"
                            r"CreateObject\s*\(|Shell\s*\(|WScript\.Shell|system\.opc\.write\w*|ExecuteCommand|"
                            r"LogDiagnosticsMessage|MsgBox|system\.gui\.\w+|system\.nav\.\w+|ShowDisplay|Display\s+\w)")

ALARM_COL_HINTS = ("alarm text", "trigger tag", "alarm class", "alarmtext", "trigger", "alarm message")
HISTORY_TIME_HINTS = ("eventtime", "event time", "timestamp", "time", "date/time", "datetime", "activetime")
HISTORY_STATE_HINTS = ("state", "event", "eventtype", "transition", "status")


def ref_dicts(text, access):
    out = []
    for r in hc.find_refs(text or ""):
        out.append({"device": r["device"], "path": r["path"], "norm": hc.norm_path(r["path"]), "kind": r["kind"],
                    "access": access})
    return out


# --------------------------------------------------------------------------
# FactoryTalk View display XML
# --------------------------------------------------------------------------

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def nearest_text(obj, texts):
    """Text object on the same row to the left (or directly above) - by position; else None."""
    ox, oy = _num(obj.attrib.get("left")), _num(obj.attrib.get("top"))
    if ox is None or oy is None:
        return None
    best, bestd = None, None
    for t in texts:
        tx, ty = _num(t.attrib.get("left")), _num(t.attrib.get("top"))
        if tx is None or ty is None:
            continue
        if abs(ty - oy) <= 12 and tx < ox:
            d = ox - tx
        elif 0 < oy - ty <= 40 and abs(tx - ox) <= 60:
            d = 1000 + (oy - ty)
        else:
            continue
        if bestd is None or d < bestd:
            best, bestd = t, d
    return best


def ft_display(path, root, warnings):
    settings = None
    for c in root.iter():
        if hc.lname(c).lower() == "displaysettings":
            settings = c
            break
    name = (settings.attrib.get("title") if settings is not None else None) or os.path.splitext(os.path.basename(path))[0]
    objects = []

    def visit(el, group, group_labels):
        for c in list(el):
            ln = hc.lname(c)
            low = ln.lower()
            if low in FT_SKIP:
                continue
            if low == "group":
                labels = [x for x in list(c) if hc.lname(x).lower() == "text" and x.attrib.get("caption")]
                visit(c, c.attrib.get("name"), labels)
                continue
            if "name" not in c.attrib:
                visit(c, group, group_labels)
                continue
            obj = {"name": c.attrib.get("name"), "type": ln, "group": group}
            for k in ("left", "top", "width", "height"):
                if c.attrib.get(k):
                    obj.setdefault("position", {})[k] = c.attrib[k]
            caption = c.attrib.get("caption")
            states = []
            for s in c.iter():
                if hc.lname(s).lower() == "state" and s.attrib.get("caption"):
                    states.append("%s=%s" % (s.attrib.get("stateId", len(states)), s.attrib["caption"]))
            if caption:
                obj["label"], obj["label_source"] = caption, "caption"
            elif states:
                obj["label"], obj["label_source"] = "; ".join(states), "state captions"
            elif low != "text":
                near = nearest_text(c, group_labels or [x for x in list(el) if hc.lname(x).lower() == "text" and x.attrib.get("caption")])
                if near is not None:
                    obj["label"], obj["label_source"] = near.attrib["caption"], "adjacent text (inferred association)"
            if states:
                obj["states"] = states
            bindings = []
            writer_action = FT_WRITERS.get(low)
            for conn in c.iter():
                cl = hc.lname(conn).lower()
                expr = conn.attrib.get("expression")
                if not expr:
                    continue
                if cl == "connection":
                    prop = conn.attrib.get("name", "Value")
                    access = "write" if writer_action and prop.lower() in ("value", "value1", "tag") else "read"
                    bindings.append({"property": prop, "expression": expr, "refs": ref_dicts(expr, access)})
                elif cl.startswith("animate") or cl.endswith("animation"):
                    bindings.append({"property": hc.lname(conn), "expression": expr, "refs": ref_dicts(expr, "read")})
            for k, v in c.attrib.items():
                if k in ("pressAction", "releaseAction", "repeatAction", "pressCommand", "releaseCommand"):
                    m = re.match(r"(?i)^\s*(set|toggle|display|abort|invoke)\s+(.*)$", v)
                    bindings.append({"property": k, "expression": v,
                                     "refs": ref_dicts(v, "write" if m and m.group(1).lower() in ("set", "toggle") else "read")})
            if bindings:
                obj["bindings"] = bindings
            actions = []
            if writer_action:
                tgt = [r for b in bindings for r in b["refs"] if r["access"] == "write"]
                actions.append({"kind": writer_action, "targets": sorted(set(r["path"] for r in tgt))})
                for lim in ("minimum", "maximum"):
                    if c.attrib.get(lim):
                        actions[-1][lim] = c.attrib[lim]
            if low == "gotodisplaybutton" or c.attrib.get("display"):
                actions.append({"kind": "navigate", "display": c.attrib.get("display")})
            if low == "closedisplaybutton":
                actions.append({"kind": "close display"})
            if actions:
                obj["actions"] = actions
            if low in FT_READERS or any(low.startswith(r) for r in FT_READERS):
                obj["role"] = "indicator"
            elif writer_action:
                obj["role"] = "operator input"
            elif low == "text":
                obj["role"] = "static text"
            else:
                obj["role"] = "graphic"
            objects.append(obj)
            visit(c, group, group_labels)

    visit(root, None, [])
    return {"name": name, "file": path, "platform": "FactoryTalk View", "objects": objects}


# --------------------------------------------------------------------------
# Ignition
# --------------------------------------------------------------------------

def ign_tags_json(path, data, tag_map, warnings):
    tags, alarms = [], []

    def walk(o, folder):
        if isinstance(o, dict):
            name = o.get("name")
            ttype = o.get("tagType")
            if ttype in ("AtomicTag", "UdtInstance") or ("opcItemPath" in o) or (ttype is None and o.get("valueSource")):
                full = "/".join([f for f in folder + [name or ""] if f])
                plc = None
                if o.get("opcItemPath"):
                    r = hc.OPC_RE.search(str(o["opcItemPath"]))
                    plc = {"device": r.group(1), "path": r.group(2)} if r else {"device": None, "path": str(o["opcItemPath"])}
                    plc["norm"] = hc.norm_path(plc["path"])
                    tag_map[full.lower()] = plc
                ro = bool(o.get("readOnly")) or "read_only" in str(o.get("accessRights", "")).lower()
                t = {"name": full, "platform": "Ignition", "file": path, "data_type": o.get("dataType"),
                     "value_source": o.get("valueSource"), "description": o.get("documentation") or o.get("tooltip"),
                     "access": "read" if ro else "read/write", "plc_ref": plc}
                tags.append(hc_compact(t))
                for a in o.get("alarms", []) or []:
                    alarms.append(hc_compact({
                        "name": a.get("name"), "tag": full, "plc_ref": plc, "platform": "Ignition", "file": path,
                        "condition": a.get("mode"), "setpoint": a.get("setpointA", a.get("setpoint")),
                        "setpoint_b": a.get("setpointB"), "priority": a.get("priority"),
                        "message": a.get("label") or a.get("displayPath") or a.get("name"),
                        "notes": a.get("notes"), "deadband": a.get("deadband"),
                        "on_delay_s": a.get("timeOnDelaySeconds"), "off_delay_s": a.get("timeOffDelaySeconds")}))
                return
            sub = folder + [name] if (ttype in ("Folder",) and name) else folder
            for v in o.values():
                walk(v, sub)
        elif isinstance(o, list):
            for v in o:
                walk(v, folder)

    walk(data, [])
    return tags, alarms


def ign_tags_xml(path, root, tag_map):
    tags = []

    def walk(el, folder):
        for c in list(el):
            if hc.lname(c) == "Tag":
                name = c.attrib.get("name")
                props = dict((p.attrib.get("name", "").lower(), (p.text or "").strip()) for p in list(c)
                             if hc.lname(p) == "Property")
                ttype = (c.attrib.get("type") or props.get("tagtype") or "").lower()
                full = "/".join([f for f in folder + [name or ""] if f])
                if ttype in ("folder",):
                    walk(c, folder + [name])
                    continue
                opc = props.get("opcitempath")
                plc = None
                if opc:
                    r = hc.OPC_RE.search(opc)
                    plc = {"device": r.group(1), "path": r.group(2)} if r else {"device": None, "path": opc}
                    plc["norm"] = hc.norm_path(plc["path"])
                    tag_map[full.lower()] = plc
                tags.append(hc_compact({"name": full, "platform": "Ignition", "file": path, "plc_ref": plc,
                                        "data_type": props.get("datatype"), "description": props.get("documentation")}))
                walk(c, folder + [name])
            else:
                walk(c, folder)

    walk(root, [])
    return tags


def ign_view(path, data, warnings):
    name = os.path.splitext(os.path.basename(path))[0]
    if name == "view":
        name = os.path.basename(os.path.dirname(os.path.abspath(path))) or name
    objects = []

    def comp(c, parent):
        ctype = c.get("type", "")
        meta = c.get("meta", {}) or {}
        props = c.get("props", {}) or {}
        obj = {"name": meta.get("name"), "type": ctype, "group": parent}
        if c.get("position"):
            obj["position"] = c["position"]
        label = props.get("text") if isinstance(props.get("text"), str) else None
        if label:
            obj["label"], obj["label_source"] = label, "props.text"
        bindings = []
        writer = any(w in ctype for w in IGN_WRITE_COMPONENTS)
        for prop, cfg in (c.get("propConfig") or {}).items():
            b = (cfg or {}).get("binding")
            if not b:
                continue
            btype = b.get("type")
            conf = b.get("config", {}) or {}
            if btype == "tag":
                tp = conf.get("tagPath", "")
                acc = "write" if conf.get("bidirectional") or (writer and prop.endswith(".value")) else "read"
                bindings.append({"property": prop, "binding": "tag", "ignition_path": tp, "access": acc})
            elif btype == "expression":
                ex = conf.get("expression", "")
                paths = [m.group(0) for m in re.finditer(r"\{(\[[^\]]*\][^}]+)\}", ex)]
                bindings.append({"property": prop, "binding": "expression", "expression": ex,
                                 "ignition_paths": [p.strip("{}") for p in paths], "access": "read"})
            else:
                bindings.append({"property": prop, "binding": btype, "config": conf, "access": "read"})
        actions = []
        for evgroup in (c.get("events") or {}).values():
            for evname, h in (evgroup or {}).items():
                for hh in (h if isinstance(h, list) else [h]):
                    t = (hh or {}).get("type")
                    conf = (hh or {}).get("config", {}) or {}
                    if t == "script":
                        sc = script_summary(conf.get("script", ""), "%s.%s" % (obj["name"], evname), "python")
                        actions.append({"kind": "script", "event": evname, "writes": sc["writes"], "reads": sc["reads"],
                                        "side_effects": sc["side_effects"]})
                    elif t == "nav":
                        actions.append({"kind": "navigate", "event": evname, "page": conf.get("page") or conf.get("view")})
                    elif t == "popup":
                        actions.append({"kind": "popup", "event": evname, "view": conf.get("viewPath")})
                    else:
                        actions.append({"kind": t, "event": evname})
        if bindings:
            obj["bindings"] = bindings
        if actions:
            obj["actions"] = actions
        obj["role"] = "operator input" if (writer or any(a["kind"] == "script" and a.get("writes") for a in actions)) \
            else "indicator" if bindings else "static text" if label else "container" if c.get("children") else "graphic"
        objects.append(hc_compact(obj))
        for ch in c.get("children", []) or []:
            comp(ch, obj["name"])

    comp(data.get("root", {}), None)
    return {"name": name, "file": path, "platform": "Ignition Perspective", "objects": objects}


# --------------------------------------------------------------------------
# Scripts (text only)
# --------------------------------------------------------------------------

def script_summary(text, name, lang):
    reads, writes, side = [], [], []
    var_ref = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("'", "#", "//")) or re.match(r"(?i)^rem\b", s):
            continue
        found = []
        for m in hc.FT_SCRIPT_ITEM_RE.finditer(s):
            found.append(m.group(1))
        for m in re.finditer(r"\"([^\"]+)\"|'([^']+)'", s):
            q = m.group(1) or m.group(2)
            if hc.IGN_PATH_RE.fullmatch(q.strip()) or hc.BRACED_RE.search(q) or hc.OPC_RE.search(q) or \
                    hc.SLC_RE.fullmatch(q.strip()):
                if q not in found:
                    found.append(q)
        vm = re.match(r"(?i)^(?:set\s+)?(\w+)\s*=\s*.*?(?:Item|Tags|SmartTags)\s*\(\s*\"([^\"]+)\"\s*\)\s*$", s)
        if vm:
            var_ref[vm.group(1).lower()] = vm.group(2)
            continue
        wm = re.match(r"(?i)^(\w+)\.value\s*=(?!=)", s)
        if wm and wm.group(1).lower() in var_ref:
            writes.append(var_ref[wm.group(1).lower()])
            continue
        rm = re.search(r"(?i)\b(\w+)\.value\b", s)
        if rm and rm.group(1).lower() in var_ref:
            reads.append(var_ref[rm.group(1).lower()])
        is_write = bool(hc.WRITE_LINE_RE.search(s))
        for f in found:
            (writes if is_write else reads).append(f)
        for m in SIDE_EFFECT_RE.finditer(s):
            side.append(m.group(1))
    uniq = lambda xs: sorted(set(xs))
    return {"name": name, "language": lang, "reads": uniq(reads), "writes": uniq(writes), "side_effects": uniq(side)}


def parse_script_file(path, text):
    ext = os.path.splitext(path)[1].lower()
    lang = {".vbs": "vbscript", ".vba": "vba", ".bas": "vba", ".vb": "vb", ".py": "python", ".jy": "python",
            ".js": "javascript"}.get(ext, "text")
    blocks = []
    if lang in ("vbscript", "vba", "vb"):
        rx = re.compile(r"(?im)^\s*(?:private\s+|public\s+)?(sub|function)\s+(\w+)")
        ends = re.compile(r"(?im)^\s*end\s+(sub|function)\b")
    elif lang == "python":
        rx = re.compile(r"(?m)^def\s+(\w+)()")
        ends = None
    else:
        rx = re.compile(r"(?m)^\s*function\s+(\w+)()")
        ends = None
    starts = list(rx.finditer(text))
    if not starts:
        blocks.append(("(file)", text, 1))
    else:
        head = text[:starts[0].start()]
        if head.strip() and re.search(r"[A-Za-z]", re.sub(r"(?m)^\s*('|#|//).*$", "", head)):
            blocks.append(("(module level)", head, 1))
        for i, m in enumerate(starts):
            end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
            body = text[m.start():end]
            if ends:
                e = ends.search(body)
                if e:
                    body = body[:e.end()]
            nm = m.group(2) if lang in ("vbscript", "vba", "vb") else m.group(1)
            blocks.append((nm, body, text.count("\n", 0, m.start()) + 1))
    out = []
    for nm, body, line in blocks:
        s = script_summary(body, nm, lang)
        s.update({"file": path, "line": line, "lines": body.count("\n") + 1})
        out.append(s)
    return out


# --------------------------------------------------------------------------
# CSV / XLSX tag and alarm databases
# --------------------------------------------------------------------------

def header_index(rows):
    for i, row in enumerate(rows[:30]):
        low = [c.strip().strip(";").lower() for c in row]
        if len([c for c in low if c]) >= 2 and any(c in low for c in (
                "name", "tag name", "tagname", "tag", "address", "plc tag", "alarm text", "trigger tag", "alarmname",
                "alarm", "eventtime", "timestamp", "opcitempath", "connection")):
            return i, low
    return None, None


def table_kind(header):
    h = set(header)
    if any(x in h for x in HISTORY_TIME_HINTS) and any(x in h for x in HISTORY_STATE_HINTS):
        return "alarm-history"
    if any(any(hint in c for hint in ALARM_COL_HINTS) for c in header):
        return "alarms"
    return "tags"


def rows_to_records(path, rows, platform_hint, warnings):
    hi, header = header_index(rows)
    if hi is None:
        warnings.append("%s: no recognisable header row" % path)
        return "unknown", [], []
    kind = table_kind(header)

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    tags, alarms = [], []
    if kind == "alarm-history":
        return kind, [], []
    if kind == "alarms":
        ci = dict(name=col("name", "alarm name", "alarmname", "id"), text=col("alarm text", "alarmtext", "message", "alarm message", "text"),
                  cls=col("alarm class", "class", "priority", "severity"), trig=col("trigger tag", "trigger", "tag", "tag name"),
                  bit=col("trigger bit", "bit"), info=col("info text", "infotext", "notes", "help"),
                  lim=col("limit", "setpoint", "threshold"), cond=col("limit mode", "condition", "mode", "type"))
        for n, r in enumerate(rows[hi + 1:], start=hi + 2):
            g = lambda k: r[ci[k]].strip() if ci[k] is not None and ci[k] < len(r) and r[ci[k]].strip() else None
            if not any(x.strip() for x in r):
                continue
            trig = g("trig")
            refs = ref_dicts(trig or "", "read")
            alarms.append(hc_compact({"name": g("name"), "tag": trig, "trigger_bit": g("bit"), "condition": g("cond"),
                                      "setpoint": g("lim"), "priority": g("cls"), "message": g("text"),
                                      "notes": g("info"), "platform": platform_hint, "file": path, "row": n,
                                      "plc_ref": ({"device": refs[0]["device"], "path": refs[0]["path"], "norm": refs[0]["norm"]}
                                                  if refs else None)}))
        return kind, tags, alarms
    ci = dict(name=col("name", "tag name", "tagname", "tag"),
              plc=col("plc tag", "address", "opc item", "item", "opcitempath", "connection tag", "source", "data source"),
              addr=col("address", "plc address", "logical address"),
              conn=col("connection", "device", "shortcut"),
              dtype=col("datatype", "data type", "type", "tag type", "native type", "hmi datatype"),
              desc=col("comment", "description", "tag description", "documentation", "label"),
              ro=col("read only", "readonly"), acc=col("access", "access mode", "access method", "accessrights"))
    for n, r in enumerate(rows[hi + 1:], start=hi + 2):
        if not any(x.strip() for x in r):
            continue
        g = lambda k: r[ci[k]].strip() if ci[k] is not None and ci[k] < len(r) and r[ci[k]].strip() else None
        raw = g("plc") or g("addr") or ""
        refs = ref_dicts(raw, "read")
        if refs:
            plc = {"device": refs[0]["device"] or g("conn"), "path": refs[0]["path"], "norm": refs[0]["norm"]}
        elif raw:
            plc = {"device": g("conn"), "path": raw, "norm": hc.norm_path(raw)}
        else:
            plc = None
        ro = (g("ro") or "").lower()
        access = "read" if ro in ("t", "true", "yes", "1", "y") else "read/write" if ro in ("f", "false", "no", "0", "n") else None
        tags.append(hc_compact({"name": g("name") or raw, "plc_ref": plc, "data_type": g("dtype"), "description": g("desc"),
                                "access": access, "platform": platform_hint, "file": path, "row": n}))
    return kind, tags, alarms


def hc_compact(d):
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


# --------------------------------------------------------------------------
# Generic XML scan (WinCC/Openness screens, Vision windows, unknown)
# --------------------------------------------------------------------------

def generic_xml(path, root):
    objects = []
    for el in root.iter():
        attrs = el.attrib
        refs = []
        for k, v in attrs.items():
            refs.extend(ref_dicts(v, "read"))
        if el.text and el.text.strip():
            refs.extend(ref_dicts(el.text, "read"))
        if refs:
            low = (hc.lname(el) + " " + attrs.get("name", "") + " " + attrs.get("type", "")).lower()
            writer = bool(re.search(r"button|input|entry|toggle|slider|switch|setpoint|write", low))
            for r in refs:
                if writer:
                    r["access"] = "write"
            objects.append(hc_compact({"name": attrs.get("name") or attrs.get("Name") or hc.lname(el), "type": hc.lname(el),
                                       "label": attrs.get("caption") or attrs.get("text") or attrs.get("Text"),
                                       "bindings": [{"property": "(scan)", "refs": refs}],
                                       "role": "operator input" if writer else "indicator"}))
    return {"name": os.path.splitext(os.path.basename(path))[0], "file": path, "platform": "generic XML (scanned)",
            "objects": objects, "note": "structure not recognised; references found by scanning attributes - roles inferred"}


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

SCRIPT_EXT = (".vbs", ".vba", ".bas", ".vb", ".py", ".jy", ".js")


def parse_paths(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for dp, _, fns in os.walk(p):
                for fn in sorted(fns):
                    if not fn.startswith("."):
                        files.append(os.path.join(dp, fn))
        else:
            files.append(p)
    res = {"tool": "rungstone/parse_hmi", "files": [], "screens": [], "tags": [], "alarms": [], "scripts": [],
           "warnings": []}
    tag_map = {}
    deferred_views = []
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        kind = "unknown"
        try:
            if ext in (".xlsx", ".xlsm"):
                try:
                    for sheet, rows in hc.xlsx_sheets(f):
                        k, t, a = rows_to_records("%s[%s]" % (f, sheet), rows, "WinCC" if "hmi" in sheet.lower() or "alarm" in sheet.lower() else None, res["warnings"])
                        res["tags"].extend(t)
                        res["alarms"].extend(a)
                    kind = "workbook"
                except RuntimeError as e:
                    res["warnings"].append("%s: %s" % (f, e))
            elif ext == ".csv":
                text = hc.read_text(f)
                rows, _ = hc.sniff_rows(text[:2000000])
                plat = "WinCC" if ";" in text[:500] and ("PLC tag" in text[:500] or "Trigger tag" in text[:500]) else None
                kind, t, a = rows_to_records(f, rows, plat, res["warnings"])
                if kind == "alarm-history":
                    res["warnings"].append("%s looks like alarm HISTORY (events over time); analyse it with alarm_analysis.py" % f)
                res["tags"].extend(t)
                res["alarms"].extend(a)
            elif ext == ".json":
                data = json.loads(hc.read_text(f))
                if isinstance(data, dict) and isinstance(data.get("root"), dict) and str(data["root"].get("type", "")).startswith("ia."):
                    deferred_views.append((f, data))
                    kind = "ignition-view"
                elif "tagType" in json.dumps(data)[:200000]:
                    t, a = ign_tags_json(f, data, tag_map, res["warnings"])
                    res["tags"].extend(t)
                    res["alarms"].extend(a)
                    kind = "ignition-tags"
                else:
                    res["warnings"].append("%s: JSON not recognised as Ignition tags or Perspective view" % f)
            elif ext in SCRIPT_EXT:
                res["scripts"].extend(parse_script_file(f, hc.read_text(f)))
                kind = "script"
            else:
                text = hc.read_text(f)
                if text.lstrip().startswith("<"):
                    try:
                        root = safe_xml.fromstring(text)
                    except (safe_xml.UnsafeXMLError, safe_xml.ParseError) as e:
                        res["warnings"].append("%s: %s" % (f, e))
                        res["files"].append({"file": f, "kind": "refused-or-invalid-xml"})
                        continue
                    rn = hc.lname(root).lower()
                    if rn in ("gfx", "display"):
                        res["screens"].append(ft_display(f, root, res["warnings"]))
                        kind = "factorytalk-display"
                    elif rn in ("tags",) or (rn == "tag" and root.find("Property") is not None):
                        res["tags"].extend(ign_tags_xml(f, root, tag_map))
                        kind = "ignition-tags-xml"
                    elif "inductiveautomation" in text[:5000]:
                        res["screens"].append(generic_xml(f, root))
                        kind = "ignition-vision-window (scanned)"
                    else:
                        res["screens"].append(generic_xml(f, root))
                        kind = "xml (scanned)"
                elif ext in (".txt",):
                    res["scripts"].extend(parse_script_file(f, text))
                    kind = "script"
                else:
                    res["warnings"].append("%s: format not recognised" % f)
        except ValueError as e:
            res["warnings"].append("%s: %s" % (f, e))
        res["files"].append({"file": f, "kind": kind})
    for f, data in deferred_views:
        res["screens"].append(ign_view(f, data, res["warnings"]))

    # resolve Ignition tag paths -> PLC refs
    def resolve_ign(tp):
        key = re.sub(r"^\[[^\]]*\]", "", tp or "").lower()
        return tag_map.get(key)

    for sc in res["screens"]:
        for o in sc["objects"]:
            for b in o.get("bindings", []):
                if b.get("ignition_path"):
                    plc = resolve_ign(b["ignition_path"])
                    if plc:
                        b["refs"] = [dict(plc, access=b.get("access", "read"), kind="ignition->opc")]
                    else:
                        b["unresolved"] = "Ignition tag path not found in supplied tag exports"
                for p in b.get("ignition_paths", []):
                    plc = resolve_ign(p)
                    if plc:
                        b.setdefault("refs", []).append(dict(plc, access="read", kind="ignition->opc"))
            for a in o.get("actions", []):
                if a.get("kind") == "script":
                    a["plc_writes"] = [resolve_ign(x) for x in a.get("writes", []) if resolve_ign(x)]
    for s in res["scripts"]:
        for key in ("reads", "writes"):
            plc = []
            for x in s[key]:
                ign = hc.IGN_PATH_RE.fullmatch(x.strip()) and "/" in x
                r = resolve_ign(x) if ign else None
                if r:
                    plc.append(r)
                elif ign:
                    s.setdefault("non_plc_or_unresolved", []).append(x)
                else:
                    plc.extend({"device": d["device"], "path": d["path"], "norm": d["norm"]} for d in ref_dicts(x, key))
            s["plc_" + key] = plc

    res["refs"] = aggregate(res)
    res["summary"] = {"files": len(res["files"]), "screens": len(res["screens"]),
                      "objects": sum(len(s["objects"]) for s in res["screens"]), "tags": len(res["tags"]),
                      "alarms": len(res["alarms"]), "scripts": len(res["scripts"]), "plc_refs": len(res["refs"])}
    return res


def aggregate(res):
    agg = {}

    def add(ref, who, access, label=None):
        if not ref or not ref.get("path"):
            return
        key = ref.get("norm") or hc.norm_path(ref["path"])
        a = agg.setdefault(key, {"norm": key, "path": ref["path"], "devices": set(), "readers": set(), "writers": set(),
                                 "labels": set()})
        if ref.get("device"):
            a["devices"].add(ref["device"])
        (a["writers"] if access == "write" else a["readers"]).add(who)
        if label:
            a["labels"].add(label)

    for sc in res["screens"]:
        for o in sc["objects"]:
            who = "%s:%s" % (sc["name"], o.get("name"))
            for b in o.get("bindings", []):
                for r in b.get("refs", []):
                    add(r, who, r.get("access"), o.get("label"))
            for a in o.get("actions", []):
                for r in a.get("plc_writes", []) or []:
                    add(r, who + " (script)", "write", o.get("label"))
    for t in res["tags"]:
        acc = "write" if t.get("access") == "read/write" else "read"
        add(t.get("plc_ref"), "tag %s" % t["name"], "may_write" if acc == "write" else "read", t.get("description"))
    for a in res["alarms"]:
        add(a.get("plc_ref"), "alarm %s" % a.get("name"), "read", a.get("message"))
    for s in res["scripts"]:
        for r in s.get("plc_reads", []):
            add(r, "script %s:%s" % (os.path.basename(s["file"]), s["name"]), "read")
        for r in s.get("plc_writes", []):
            add(r, "script %s:%s" % (os.path.basename(s["file"]), s["name"]), "write")
    out = []
    for k in sorted(agg):
        a = agg[k]
        out.append(hc_compact({"norm": a["norm"], "path": a["path"], "devices": sorted(a["devices"]),
                               "readers": sorted(a["readers"]), "writers": sorted(a["writers"]),
                               "labels": sorted(a["labels"])}))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+")
    ap.add_argument("-o", "--output")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args(argv)
    res = parse_paths(args.paths)
    s = json.dumps(res, indent=2 if args.pretty else None, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(s)
        sm = res["summary"]
        sys.stderr.write("parse_hmi: %d files, %d screens/%d objects, %d tags, %d alarms, %d scripts, %d PLC refs\n" % (
            sm["files"], sm["screens"], sm["objects"], sm["tags"], sm["alarms"], sm["scripts"], sm["plc_refs"]))
    else:
        sys.stdout.write(s + "\n")


if __name__ == "__main__":
    main()
