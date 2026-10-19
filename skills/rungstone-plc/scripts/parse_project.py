"""
parse_project.py - turn PLC project exports into compact, data-free JSON.

Supported inputs (auto-detected from content, not just extension):
  * Rockwell Studio 5000 / RSLogix 5000 .L5X  (full project or partial export:
    program, routine, rung, AOI, UDT)
  * Siemens TIA Portal SimaticML .xml  (blocks, DBs, PLC tag tables, UDTs;
    SCL token streams, LAD/FBD FlgNet networks, STL)
  * Siemens SCL source .scl / IEC Structured Text source .st / .exp
  * PLCopen XML (CODESYS, Beckhoff export, Beremiz, ...)
  * Beckhoff TwinCAT 3 native .TcPOU / .TcGVL / .TcDUT
  * Ladder neutral text / RSLogix 500 ASCII (.txt, .rll, .lad): one rung per
    line, e.g. "XIC(I:1/14)OTE(O:2/15);" or "SOR XIC I:1/14 OTE O:2/15 EOR"
  * I/O map / tag list .csv  (address + description columns)

What is kept: programs, routines, rungs, ST lines, networks, comments, tag
names, data types, descriptions, aliases, I/O addresses, module catalog
numbers.  What is dropped on purpose: every tag data value, start value,
default value and raw data block.  Tag data is usually most of an L5X file and
carries no logic.

Safety: logic that is vendor-flagged as safety (GuardLogix safety task /
Class="Safety", Siemens F-blocks, PLCopen SF_ function blocks, TwinSAFE) or
whose name looks like a safety function (E-stop, guard, light curtain, ...)
is NOT emitted.  It is listed under "excluded_safety" with the reason, so it
can be reported to the user but never analysed or translated.  Heuristic
(name-only) exclusions can be released with --allow-suspected NAME after an
engineer confirms the item is not safety-rated; vendor-flagged items cannot.

Usage:
  python3 parse_project.py FILE [FILE ...] [-o out.json] [--pretty]
                           [--allow-suspected NAME ...]
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET  # Element/tostring only; parsing goes through safe_xml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shared import safe_xml  # noqa: E402  (defusedxml when available, DTD/entity guard otherwise)

# --------------------------------------------------------------------------
# Safety detection
# --------------------------------------------------------------------------

# Names that suggest a safety function.  Matched against program, routine,
# POU, block and AOI names (not against every tag - standard logic that merely
# reads an "EStop_OK" status bit is normal and stays in scope, flagged).
SAFETY_NAME_RE = re.compile(
    r"(?i)(e[-_ ]?stop|emerg|safety|safe[-_ ]?(torque|stop|guard|zone)|"
    r"(?:^|[^a-z])sto(?:[^a-z]|$)|light[-_ ]?curtain|guard[-_ ]?(door|switch|lock|monitor)|"
    r"two[-_ ]?hand|muting|safe[-_ ]?relay|f[-_ ]?runtime|^f_|^f-)"
)

# Rockwell GuardLogix / Compact GuardLogix safety application instructions and
# integrated safety drive instructions.
ROCKWELL_SAFETY_INSTR = {
    "ESTOP", "ROUT", "RIN", "DCS", "DCST", "DCSTL", "DCSTM", "DCSRT", "DCM",
    "DCA", "DCAF", "DIN", "EMD", "ENPEN", "FPMS", "FSBM", "LC", "SMAT", "THRS",
    "THRSE", "TSAM", "TSSM", "CROUT", "CBCM", "CBIM", "CBSSM", "MMVC", "SFX",
    "SS1", "SS2", "SOS", "SBC", "SLS", "SLP", "SDI", "SBT", "AVC", "MDSC",
}
# Siemens Safety Advanced / STEP 7 Safety library instructions.
SIEMENS_SAFETY_INSTR = {
    "ESTOP1", "TWO_H_EN", "TWO_HAND", "MUTING", "MUT_P", "EV1OO2DI", "FDBACK",
    "SFDOOR", "ACK_GL", "ACK_OP", "SENDDP", "RCVDP", "SENDS7", "RCVS7",
    "SAFETY_ESTOP", "F_TRIG_SAFE",
}
ROCKWELL_SAFETY_RE = re.compile(r"\b(%s)\s*\(" % "|".join(sorted(ROCKWELL_SAFETY_INSTR, key=len, reverse=True)))
SIEMENS_SAFETY_RE = re.compile(r'(?i)"?\b(%s)\b"?' % "|".join(sorted(SIEMENS_SAFETY_INSTR, key=len, reverse=True)))
PLCOPEN_SAFETY_RE = re.compile(r"\bSF_[A-Za-z0-9_]+\b")
SAFE_TYPE_RE = re.compile(r"\bSAFE(BOOL|INT|DINT|WORD|BYTE|DWORD|TIME|UINT|SINT|USINT|UDINT)\b", re.I)
SAFETY_MODULE_RE = re.compile(
    r"(?i)(1734-[IO]B\d+S|1734-OBV\d+S|1732ES|1791DS|1791ES|1756-[IO]B\d+S|5069-[IO]B\d+S|"
    r"5069-OBV\d+S|1756-L\d+[A-Z]*S\b|5069-L\d+[A-Z]*S|1769-L\d+[A-Z]*S|440R|440C|GSR|F-DI|F-DQ|F-AI)"
)


class Ctx(object):
    """Per-run options shared by the format parsers."""

    def __init__(self, allow_suspected=None):
        self.allow_suspected = set(n.lower() for n in (allow_suspected or []))

    def name_is_suspected(self, *names):
        for n in names:
            if n and SAFETY_NAME_RE.search(n) and n.lower() not in self.allow_suspected:
                return n
        return None


def new_project(path, fmt):
    return {
        "file": path,
        "format": fmt,
        "target": None,
        "controller": None,
        "tasks": [],
        "routines": [],
        "aois": [],
        "data_types": [],
        "tags": [],
        "io_modules": [],
        "excluded_safety": [],
        "warnings": [],
    }


def exclude(project, kind, name, reason, confidence, program=None, size=None):
    entry = {"kind": kind, "name": name, "reason": reason, "confidence": confidence}
    if program:
        entry["program"] = program
    if size is not None:
        entry["size"] = size
    project["excluded_safety"].append(entry)


# --------------------------------------------------------------------------
# XML helpers
# --------------------------------------------------------------------------

def lname(el):
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def kids(el, name=None):
    return [c for c in list(el) if name is None or lname(c) == name]


def kid(el, name):
    for c in list(el):
        if lname(c) == name:
            return c
    return None


def path(el, *names):
    cur = el
    for n in names:
        if cur is None:
            return None
        cur = kid(cur, n)
    return cur


def text_of(el):
    if el is None:
        return None
    t = "".join(el.itertext()).strip()
    return t or None


def descendants(el, name):
    return [d for d in el.iter() if lname(d) == name]


def compact(d):
    """Drop keys whose value is None / empty so the JSON stays small."""
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


# --------------------------------------------------------------------------
# Rockwell L5X
# --------------------------------------------------------------------------

def l5x_description(el):
    d = kid(el, "Description")
    return text_of(d)


def l5x_tag(el, scope):
    attrs = el.attrib
    t = {
        "name": attrs.get("Name"),
        "scope": scope,
        "data_type": attrs.get("DataType"),
        "tag_type": attrs.get("TagType"),
        "alias_for": attrs.get("AliasFor"),
        "usage": attrs.get("Usage"),
        "dimensions": attrs.get("Dimensions"),
        "constant": attrs.get("Constant") == "true" or None,
        "external_access": attrs.get("ExternalAccess"),
        "description": l5x_description(el),
        "source": "l5x",
    }
    if attrs.get("Class") == "Safety":
        t["safety"] = "vendor-flagged (Class=Safety)"
    comments = kid(el, "Comments")
    if comments is not None:
        mc = {}
        for c in kids(comments, "Comment"):
            if c.attrib.get("Operand"):
                mc[c.attrib["Operand"]] = text_of(c)
        t["member_comments"] = mc
    return compact(t)


def l5x_routine(project, rt, program, kind, ctx, safety_program):
    name = rt.attrib.get("Name")
    rtype = rt.attrib.get("Type", "RLL")
    rid = "%s/%s" % (program, name)
    out = {"id": rid, "program": program, "name": name, "language": rtype, "kind": kind,
           "description": l5x_description(rt)}

    if kid(rt, "EncodedData") is not None or rt.find(".//EncodedData") is not None:
        project["warnings"].append("%s is source-protected (EncodedData); its logic cannot be read." % rid)
        out["protected"] = True
        project["routines"].append(compact(out))
        return

    if safety_program:
        exclude(project, "routine", rid, safety_program, "vendor-flagged", program=program)
        return
    hit = ctx.name_is_suspected(name)
    if hit:
        exclude(project, "routine", rid, "name suggests a safety function (%s)" % hit, "suspected", program=program)
        return

    if rtype == "RLL":
        rungs = []
        content = kid(rt, "RLLContent")
        for r in kids(content, "Rung") if content is not None else []:
            txt = text_of(kid(r, "Text")) or ""
            num = int(r.attrib.get("Number", len(rungs)))
            m = ROCKWELL_SAFETY_RE.search(txt)
            if m:
                exclude(project, "rung", "%s rung %d" % (rid, num),
                        "contains safety instruction %s" % m.group(1), "vendor-flagged", program=program)
                rungs.append({"number": num, "text": "[withheld: safety instruction %s]" % m.group(1), "withheld": True})
                continue
            rungs.append(compact({"number": num, "text": txt, "comment": text_of(kid(r, "Comment")),
                                  "type": r.attrib.get("Type") if r.attrib.get("Type") not in (None, "N") else None}))
        out["rungs"] = rungs
    elif rtype == "ST":
        content = kid(rt, "STContent")
        lines = []
        for ln in kids(content, "Line") if content is not None else []:
            lines.append({"number": int(ln.attrib.get("Number", len(lines))), "text": (ln.text or "").strip("\r\n").rstrip()})
        body = "\n".join(l["text"] for l in lines)
        m = ROCKWELL_SAFETY_RE.search(body)
        if m:
            exclude(project, "routine", rid, "contains safety instruction %s" % m.group(1), "vendor-flagged", program=program)
            return
        out["lines"] = lines
    elif rtype == "FBD":
        out["networks"] = l5x_fbd(kid(rt, "FBDContent"))
    elif rtype == "SFC":
        out["lines"] = l5x_sfc(kid(rt, "SFCContent"))
    else:
        project["warnings"].append("%s: routine type %s not decoded." % (rid, rtype))
    project["routines"].append(compact(out))


def l5x_fbd(content):
    nets = []
    if content is None:
        return nets
    for sheet in kids(content, "Sheet"):
        parts, refs = [], []
        for el in list(sheet):
            n = lname(el)
            op = el.attrib.get("Operand")
            if n == "IRef":
                parts.append("IRef(%s)" % op)
                refs.append({"tag": op, "access": "read"})
            elif n == "ORef":
                parts.append("ORef(%s)" % op)
                refs.append({"tag": op, "access": "write"})
            elif n in ("Block", "AddOnInstruction", "JSR", "SBR", "RET", "Function"):
                bt = el.attrib.get("Type") or el.attrib.get("Name") or n
                pins = el.attrib.get("VisiblePins", "")
                parts.append("%s(%s)%s" % (bt, op or "", (" pins=" + pins) if pins else ""))
                if op:
                    refs.append({"tag": op, "access": "write", "instr": bt})
            elif n == "TextBox":
                t = text_of(kid(el, "Text"))
                if t:
                    parts.append("/* %s */" % t)
        nets.append({"number": int(sheet.attrib.get("Number", len(nets) + 1)), "text": "  ".join(parts), "refs": refs})
    return nets


def l5x_sfc(content):
    lines = []
    if content is None:
        return lines
    n = 0
    for el in content.iter():
        ln = lname(el)
        if ln == "Step":
            lines.append({"number": n, "text": "STEP %s%s" % (el.attrib.get("Operand"), " (initial)" if el.attrib.get("InitialStep") == "true" else "")})
            n += 1
        elif ln == "Transition":
            lines.append({"number": n, "text": "TRANSITION %s" % el.attrib.get("Operand")})
            n += 1
        elif ln == "Line" :
            lines.append({"number": n, "text": "    " + (el.text or "").strip("\r\n").rstrip()})
            n += 1
    return lines


def l5x_aoi(project, aoi, ctx):
    name = aoi.attrib.get("Name")
    if kid(aoi, "EncodedData") is not None:
        project["warnings"].append("AOI %s is source-protected; its logic cannot be read." % name)
    params = []
    for p in descendants(aoi, "Parameter"):
        params.append(compact({"name": p.attrib.get("Name"), "data_type": p.attrib.get("DataType"),
                               "usage": p.attrib.get("Usage"), "required": p.attrib.get("Required") == "true",
                               "visible": p.attrib.get("Visible") == "true",
                               "description": l5x_description(p)}))
    local = [compact({"name": t.attrib.get("Name"), "data_type": t.attrib.get("DataType"),
                      "description": l5x_description(t)}) for t in descendants(aoi, "LocalTag")]
    entry = compact({"name": name, "revision": aoi.attrib.get("Revision"), "description": l5x_description(aoi),
                     "parameters": params, "local_tags": local})
    if aoi.attrib.get("Class") == "Safety":
        exclude(project, "aoi", name, "safety AOI (Class=Safety)", "vendor-flagged")
        return
    hit = ctx.name_is_suspected(name)
    if hit:
        exclude(project, "aoi", name, "name suggests a safety function (%s)" % hit, "suspected")
        return
    project["aois"].append(entry)
    routines = kid(aoi, "Routines")
    for rt in kids(routines, "Routine") if routines is not None else []:
        l5x_routine(project, rt, "AOI:" + name, "aoi", ctx, None)


def parse_l5x(path_, root, ctx):
    project = new_project(path_, "rockwell-l5x")
    project["target"] = compact({"name": root.attrib.get("TargetName"), "type": root.attrib.get("TargetType"),
                                 "software_revision": root.attrib.get("SoftwareRevision")})
    ctrl = kid(root, "Controller")
    if ctrl is None:
        project["warnings"].append("No <Controller> element found.")
        return project
    ptype = ctrl.attrib.get("ProcessorType", "")
    project["controller"] = compact({"name": ctrl.attrib.get("Name"), "processor": ptype,
                                     "revision": "%s.%s" % (ctrl.attrib.get("MajorRev"), ctrl.attrib.get("MinorRev"))
                                     if ctrl.attrib.get("MajorRev") else None,
                                     "description": l5x_description(ctrl)})
    if ptype and SAFETY_MODULE_RE.search(ptype):
        project["controller"]["guardlogix"] = True

    # Tasks -> which programs run in a safety task
    safety_programs = set()
    tasks = kid(ctrl, "Tasks")
    for t in kids(tasks, "Task") if tasks is not None else []:
        progs = [sp.attrib.get("Name") for sp in descendants(t, "ScheduledProgram")]
        cls = t.attrib.get("Class")
        project["tasks"].append(compact({"name": t.attrib.get("Name"), "type": t.attrib.get("Type"),
                                         "rate_ms": t.attrib.get("Rate"), "priority": t.attrib.get("Priority"),
                                         "class": cls, "programs": progs}))
        if cls == "Safety":
            safety_programs.update(progs)

    # Data types
    dts = kid(ctrl, "DataTypes")
    for dt in kids(dts, "DataType") if dts is not None else []:
        members = []
        for m in descendants(dt, "Member"):
            if m.attrib.get("Hidden") == "true":
                continue
            members.append(compact({"name": m.attrib.get("Name"), "data_type": m.attrib.get("DataType"),
                                    "dimension": m.attrib.get("Dimension") if m.attrib.get("Dimension") not in (None, "0") else None,
                                    "description": l5x_description(m)}))
        project["data_types"].append(compact({"name": dt.attrib.get("Name"), "description": l5x_description(dt),
                                              "members": members}))

    # Modules (I/O map)
    mods = kid(ctrl, "Modules")
    for m in kids(mods, "Module") if mods is not None else []:
        addr = None
        for p in descendants(m, "Port"):
            if p.attrib.get("Address"):
                addr = p.attrib.get("Address")
                break
        io_comments = []
        for conn in descendants(m, "Connection"):
            for direction in ("InputTag", "OutputTag"):
                tagel = kid(conn, direction)
                if tagel is None:
                    continue
                for c in descendants(tagel, "Comment"):
                    io_comments.append({"direction": "input" if direction == "InputTag" else "output",
                                        "operand": c.attrib.get("Operand"), "comment": text_of(c)})
        cat = m.attrib.get("CatalogNumber", "")
        entry = compact({"name": m.attrib.get("Name"), "catalog": cat, "parent": m.attrib.get("ParentModule"),
                         "address": addr, "description": l5x_description(m), "io_comments": io_comments,
                         "inhibited": m.attrib.get("Inhibited") == "true" or None})
        if SAFETY_MODULE_RE.search(cat):
            entry["safety"] = "safety I/O module (catalog %s)" % cat
        project["io_modules"].append(entry)

    # AOIs
    aois = kid(ctrl, "AddOnInstructionDefinitions")
    for a in kids(aois, "AddOnInstructionDefinition") if aois is not None else []:
        l5x_aoi(project, a, ctx)

    # Controller tags
    tags = kid(ctrl, "Tags")
    for t in kids(tags, "Tag") if tags is not None else []:
        project["tags"].append(l5x_tag(t, "controller"))

    # Programs
    progs = kid(ctrl, "Programs")
    for p in kids(progs, "Program") if progs is not None else []:
        pname = p.attrib.get("Name")
        reason = None
        if p.attrib.get("Class") == "Safety":
            reason = "safety program (Class=Safety)"
        elif pname in safety_programs:
            reason = "scheduled in a safety task"
        ptags = kid(p, "Tags")
        for t in kids(ptags, "Tag") if ptags is not None else []:
            tg = l5x_tag(t, "program:" + pname)
            if reason:
                tg["safety"] = "in " + reason
            project["tags"].append(tg)
        if reason:
            n_rt = len(descendants(p, "Routine"))
            exclude(project, "program", pname, reason, "vendor-flagged", size="%d routines" % n_rt)
            continue
        hit = ctx.name_is_suspected(pname)
        if hit:
            n_rt = len(descendants(p, "Routine"))
            exclude(project, "program", pname, "name suggests a safety function (%s)" % hit, "suspected",
                    size="%d routines" % n_rt)
            continue
        routines = kid(p, "Routines")
        for rt in kids(routines, "Routine") if routines is not None else []:
            l5x_routine(project, rt, pname, "routine", ctx, None)
        if p.attrib.get("MainRoutineName"):
            for r in project["routines"]:
                if r["program"] == pname and r["name"] == p.attrib["MainRoutineName"]:
                    r["main"] = True
    return project


# --------------------------------------------------------------------------
# Siemens SimaticML (TIA Portal Openness XML)
# --------------------------------------------------------------------------

def ml_text(container, composition):
    """Pick the first non-empty MultilingualText of a composition (Title/Comment)."""
    best = None
    own = kid(container, "ObjectList")
    for mt in kids(own, "MultilingualText") if own is not None else []:
        if mt.attrib.get("CompositionName") != composition:
            continue
        for item in descendants(mt, "MultilingualTextItem"):
            t = text_of(path(item, "AttributeList", "Text"))
            cul = text_of(path(item, "AttributeList", "Culture")) or ""
            if t:
                if cul.startswith("en"):
                    return t
                best = best or t
        if best:
            return best
    return best


def member_comment(m):
    c = kid(m, "Comment")
    if c is None:
        return None
    best = None
    for t in descendants(c, "MultiLanguageText"):
        if t.text and t.text.strip():
            if (t.attrib.get("Lang") or "").startswith("en"):
                return t.text.strip()
            best = best or t.text.strip()
    return best


def sml_members(container, prefix=""):
    out = []
    for m in kids(container, "Member"):
        name = prefix + m.attrib.get("Name", "")
        out.append(compact({"name": name, "data_type": (m.attrib.get("Datatype") or "").replace('"', ""),
                            "retain": m.attrib.get("Remanence") if m.attrib.get("Remanence") not in (None, "NonRetain") else None,
                            "description": member_comment(m)}))
        if prefix.count(".") < 3:
            out.extend(sml_members(m, name + "."))
            for sec in kids(m, "Sections"):
                for s in kids(sec, "Section"):
                    out.extend(sml_members(s, name + "."))
    return out


def sml_access(el):
    """Render an <Access> element as SCL text."""
    scope = el.attrib.get("Scope", "")
    if scope in ("LiteralConstant", "TypedConstant"):
        cv = None
        for d in el.iter():
            if lname(d) == "ConstantValue":
                cv = d.text
        return cv or "?"
    if scope == "Call":
        return sml_call(kid(el, "CallInfo"))
    sym = kid(el, "Symbol")
    if sym is None:
        cv = text_of(el)
        return cv or "?"
    parts = []
    prev_component = False
    first = True
    for c in list(sym):
        n = lname(c)
        if n == "Component":
            name = c.attrib.get("Name", "")
            if first and scope == "GlobalVariable":
                name = '"%s"' % name
            if prev_component:
                parts.append(".")
            idx = [sml_access(a) for a in kids(c, "Access")]
            parts.append(name + ("[%s]" % ", ".join(idx) if idx else ""))
            prev_component = True
            first = False
        elif n == "Token":
            parts.append(c.attrib.get("Text", ""))
            prev_component = False
    s = "".join(parts)
    if scope in ("LocalVariable",):
        s = "#" + s
    return s


def sml_call(ci):
    if ci is None:
        return "?()"
    name = ci.attrib.get("Name", "?")
    inst = kid(ci, "Instance")
    head = '"%s"' % name
    if inst is not None:
        comps = [c.attrib.get("Name") for c in inst.iter() if lname(c) == "Component"]
        head = ("#" if inst.attrib.get("Scope") == "LocalVariable" else "") + ".".join(
            ('"%s"' % c if i == 0 and inst.attrib.get("Scope") != "LocalVariable" else c) for i, c in enumerate(comps))
    params = []
    for p in kids(ci, "Parameter"):
        params.append(p.attrib.get("Name", "?"))
    return "%s(%s)" % (head, ", ".join(params))


def sml_structured_text(st, warnings, where):
    out = []
    unknown = set()
    for c in list(st):
        n = lname(c)
        if n == "Token":
            out.append(c.attrib.get("Text", ""))
        elif n == "Blank":
            out.append(" " * int(c.attrib.get("Num", "1")))
        elif n == "NewLine":
            out.append("\n" * int(c.attrib.get("Num", "1")))
        elif n == "LineComment":
            out.append("//" + (text_of(c) or ""))
        elif n == "Comment":
            out.append("(*" + (text_of(c) or "") + "*)")
        elif n == "Access":
            out.append(sml_access(c))
        elif n == "Text":
            out.append(c.text or "")
        else:
            unknown.add(n)
    if unknown:
        warnings.append("%s: SCL token types not rendered: %s (export as .scl source for exact text)." % (where, ", ".join(sorted(unknown))))
    return "".join(out).replace("\r\n", "\n").replace("\r", "\n")


WRITE_PINS = {"out", "q", "et", "cv", "qu", "qd", "ret_val", "out1", "out2", "eno", "ret"}
WRITE_PARTS = {"coil", "scoil", "rcoil", "setcoil", "resetcoil", "pcoil", "ncoil", "sr", "rs",
               "move", "set_bf", "reset_bf"}


def sml_flgnet(fn):
    """Summarise a LAD/FBD network: parts with their connected operands."""
    access = {}
    parts = []
    for p in descendants(fn, "Access"):
        if p.attrib.get("UId"):
            access[p.attrib["UId"]] = sml_access(p)
    for p in list(kid(fn, "Parts") if kid(fn, "Parts") is not None else []):
        n = lname(p)
        if n in ("Part", "Call"):
            name = p.attrib.get("Name") or (kid(p, "CallInfo").attrib.get("Name") if kid(p, "CallInfo") is not None else "Call")
            inst = kid(p, "Instance")
            if inst is None and kid(p, "CallInfo") is not None:
                inst = kid(kid(p, "CallInfo"), "Instance")
            inst_name = None
            if inst is not None:
                comps = [c.attrib.get("Name") for c in inst.iter() if lname(c) == "Component"]
                inst_name = ("#" if inst.attrib.get("Scope") == "LocalVariable" else '"') + ".".join(comps) + \
                            ("" if inst.attrib.get("Scope") == "LocalVariable" else '"')
            negated = set(x.attrib.get("Name") for x in kids(p, "Negated"))
            parts.append({"uid": p.attrib.get("UId"), "name": name, "instance": inst_name, "pins": {}, "negated": negated})
    by_uid = dict((p["uid"], p) for p in parts)
    wires = kid(fn, "Wires")
    order = []
    for w in kids(wires, "Wire") if wires is not None else []:
        ident = [access.get(c.attrib.get("UId")) for c in kids(w, "IdentCon")]
        for nc in kids(w, "NameCon"):
            part = by_uid.get(nc.attrib.get("UId"))
            if part is None:
                continue
            if part["uid"] not in order:
                order.append(part["uid"])
            if ident and ident[0]:
                part["pins"][nc.attrib.get("Name")] = ident[0]
    text, refs = [], []
    for uid in order + [p["uid"] for p in parts if p["uid"] not in order]:
        p = by_uid[uid]
        pin_txt = ", ".join("%s%s=%s" % ("NOT " if k in p["negated"] else "", k, v) for k, v in p["pins"].items())
        label = p["name"] + ("<%s>" % p["instance"] if p["instance"] else "")
        text.append("%s(%s)" % (label, pin_txt))
        if p["instance"]:
            refs.append({"tag": p["instance"], "access": "write", "instr": p["name"]})
        for pin, opnd in p["pins"].items():
            written = p["name"].lower() in WRITE_PARTS and pin == "operand" or pin.lower() in WRITE_PINS or pin == "bit"
            refs.append({"tag": opnd, "access": "write" if written else "read", "instr": p["name"]})
    return " -> ".join(text), refs


def sml_stl(sl):
    lines = []
    for stmt in descendants(sl, "StlStatement"):
        toks = []
        for c in stmt.iter():
            n = lname(c)
            if n == "StlToken":
                toks.append(c.attrib.get("Text", ""))
            elif n == "Access" and c.attrib.get("Scope") != "Call":
                toks.append(sml_access(c))
            elif n == "LineComment":
                toks.append("//" + (text_of(c) or ""))
        lines.append(" ".join(t for t in toks if t))
    return "\n".join(lines)


def parse_simaticml(path_, root, ctx):
    project = new_project(path_, "siemens-simaticml")
    eng = kid(root, "Engineering")
    project["target"] = compact({"tia_version": eng.attrib.get("version") if eng is not None else None})
    for blk in list(root):
        kind = lname(blk)
        if not kind.startswith("SW."):
            continue
        al = kid(blk, "AttributeList")
        name = text_of(kid(al, "Name")) if al is not None else None
        lang = text_of(kid(al, "ProgrammingLanguage")) if al is not None else None
        number = text_of(kid(al, "Number")) if al is not None else None

        if kind == "SW.Tags.PlcTagTable":
            for t in descendants(blk, "SW.Tags.PlcTag"):
                tal = kid(t, "AttributeList")
                project["tags"].append(compact({
                    "name": text_of(kid(tal, "Name")), "scope": "tag table:" + (name or ""),
                    "data_type": text_of(kid(tal, "DataTypeName")), "address": text_of(kid(tal, "LogicalAddress")),
                    "description": ml_text(t, "Comment"), "source": "plc tag table"}))
            for t in descendants(blk, "SW.Tags.PlcUserConstant"):
                tal = kid(t, "AttributeList")
                project["tags"].append(compact({"name": text_of(kid(tal, "Name")), "scope": "constant",
                                                "data_type": text_of(kid(tal, "DataTypeName")), "constant": True}))
            continue

        iface = path(al, "Interface") if al is not None else None
        sections = []
        if iface is not None:
            for s in descendants(iface, "Section"):
                if lname(s) == "Section" and s.attrib.get("Name"):
                    sections.append((s.attrib["Name"], sml_members(s)))

        if kind == "SW.Types.PlcStruct":
            members = [m for _, ms in sections for m in ms]
            project["data_types"].append(compact({"name": name, "description": ml_text(blk, "Comment"), "members": members}))
            continue

        fail = (lang or "").upper().startswith("F_")
        if fail:
            exclude(project, "block", name, "F-block (fail-safe, language %s)" % lang, "vendor-flagged")
            continue
        hit = ctx.name_is_suspected(name)
        if hit:
            exclude(project, "block", name, "name suggests a safety function (%s)" % hit, "suspected")
            continue

        if kind in ("SW.Blocks.GlobalDB", "SW.Blocks.InstanceDB", "SW.Blocks.ArrayDB"):
            inst_of = text_of(kid(al, "InstanceOfName")) if al is not None else None
            for _, ms in sections:
                for m in ms:
                    t = dict(m)
                    t["name"] = '"%s".%s' % (name, m["name"])
                    t["scope"] = "db:" + name
                    project["tags"].append(t)
            project["routines"].append(compact({"id": name, "program": None, "name": name, "kind": kind.split(".")[-1],
                                                "number": number, "instance_of": inst_of,
                                                "description": ml_text(blk, "Comment"), "networks": []}))
            continue

        entry = {"id": name, "program": None, "name": name, "kind": kind.split(".")[-1], "number": number,
                 "language": lang, "title": ml_text(blk, "Title"), "description": ml_text(blk, "Comment"),
                 "interface": [{"section": s, "members": ms} for s, ms in sections if ms]}
        nets = []
        for i, cu in enumerate(descendants(blk, "SW.Blocks.CompileUnit")):
            cal = kid(cu, "AttributeList")
            src = path(cal, "NetworkSource") if cal is not None else None
            clang = text_of(kid(cal, "ProgrammingLanguage")) if cal is not None else None
            net = {"number": i + 1, "language": clang, "title": ml_text(cu, "Title"), "comment": ml_text(cu, "Comment")}
            if src is not None and len(src):
                body = list(src)[0]
                bn = lname(body)
                where = "%s network %d" % (name, i + 1)
                if bn == "StructuredText":
                    net["text"] = sml_structured_text(body, project["warnings"], where)
                elif bn == "FlgNet":
                    net["text"], net["refs"] = sml_flgnet(body)
                    net["summarized"] = True
                elif bn == "StatementList":
                    net["text"] = sml_stl(body)
                else:
                    project["warnings"].append("%s: network source %s not decoded." % (where, bn))
            txt = net.get("text") or ""
            m = SIEMENS_SAFETY_RE.search(txt)
            if m:
                exclude(project, "network", "%s network %d" % (name, i + 1), "calls safety instruction %s" % m.group(1),
                        "vendor-flagged")
                net = {"number": i + 1, "text": "[withheld: safety instruction %s]" % m.group(1), "withheld": True}
            nets.append(compact(net))
        entry["networks"] = nets
        project["routines"].append(compact(entry))
    return project


# --------------------------------------------------------------------------
# IEC / SCL textual declarations and bodies (SCL source, ST source, TwinCAT)
# --------------------------------------------------------------------------

BLOCK_START_RE = re.compile(
    r'^\s*(FUNCTION_BLOCK|FUNCTION|ORGANIZATION_BLOCK|DATA_BLOCK|TYPE|PROGRAM|INTERFACE|METHOD|ACTION|PROPERTY)\s+'
    r'("[^"]+"|[A-Za-z_][\w.]*)', re.I | re.M)
VAR_START_RE = re.compile(r"^\s*(VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT|VAR_TEMP|VAR_GLOBAL|VAR_EXTERNAL|VAR_STAT|VAR_INST|VAR)\b(.*)$", re.I)
DECL_RE = re.compile(
    r'^\s*("[^"]+"|[A-Za-z_]\w*)\s*(\{[^}]*\})?\s*(?:AT\s+(%[A-Z]+[\d.]+))?\s*:\s*([^;:=]+?)\s*(?::=\s*[^;]*)?;\s*(?://\s*(.*)|\(\*\s*(.*?)\s*\*\))?\s*$',
    re.I)


def parse_decls(text):
    """Return list of (section, decl) from VAR...END_VAR blocks of IEC/SCL text."""
    out = []
    section = None
    struct_stack = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = VAR_START_RE.match(line)
        if m and section is None:
            section = m.group(1).upper() + ("_" + m.group(2).strip().upper().split()[0] if m.group(2).strip() and not m.group(2).strip().startswith("//") else "")
            continue
        if re.match(r"^END_VAR\b", line, re.I):
            section = None
            struct_stack = []
            continue
        if section is None:
            continue
        if re.match(r"^END_STRUCT", line, re.I):
            if struct_stack:
                struct_stack.pop()
            continue
        sm = re.match(r'^("[^"]+"|[A-Za-z_]\w*)\s*(\{[^}]*\})?\s*:\s*STRUCT\b(.*)$', line, re.I)
        if sm:
            comment = sm.group(3).split("//", 1)[1].strip() if "//" in sm.group(3) else None
            out.append((section, compact({"name": ".".join(struct_stack + [sm.group(1)]), "data_type": "STRUCT", "description": comment})))
            struct_stack.append(sm.group(1))
            continue
        dm = DECL_RE.match(line)
        if dm:
            out.append((section, compact({"name": ".".join(struct_stack + [dm.group(1)]), "data_type": dm.group(4).strip(),
                                          "address": dm.group(3), "description": dm.group(5) or dm.group(6),
                                          "retain": "RETAIN" in section or None,
                                          "constant": "CONSTANT" in section or None})))
    return out


def text_block_safety(project, ctx, kind, name, body):
    """Return True if this textual block must be excluded."""
    m = PLCOPEN_SAFETY_RE.search(body) or SAFE_TYPE_RE.search(body) or SIEMENS_SAFETY_RE.search(body)
    if m:
        exclude(project, kind, name, "uses safety function/type %s" % m.group(0), "vendor-flagged")
        return True
    hit = ctx.name_is_suspected(name)
    if hit:
        exclude(project, kind, name, "name suggests a safety function (%s)" % hit, "suspected")
        return True
    return False


def parse_iec_text(path_, text, ctx, fmt):
    project = new_project(path_, fmt)
    starts = list(BLOCK_START_RE.finditer(text))
    if not starts:
        project["warnings"].append("No FUNCTION_BLOCK/FUNCTION/PROGRAM/... header found; treated as one body.")
        starts_text = [("BODY", os.path.basename(path_), text, 1)]
    else:
        starts_text = []
        for i, m in enumerate(starts):
            end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
            starts_text.append((m.group(1).upper(), m.group(2).strip('"'), text[m.start():end], text.count("\n", 0, m.start()) + 1))
    for kind, name, chunk, first_line in starts_text:
        decls = parse_decls(chunk)
        if kind == "TYPE":
            project["data_types"].append(compact({"name": name, "members": [d for _, d in decls]}))
            continue
        if kind == "DATA_BLOCK":
            for _, d in decls:
                t = dict(d)
                t["name"] = '"%s".%s' % (name, d["name"])
                t["scope"] = "db:" + name
                project["tags"].append(t)
            continue
        if text_block_safety(project, ctx, kind.lower(), name, chunk):
            continue
        # Body: after BEGIN (SCL) or after the last END_VAR (IEC)
        lines = chunk.splitlines()
        body_start = 0
        for i, ln in enumerate(lines):
            if re.match(r"^\s*BEGIN\b", ln, re.I):
                body_start = i + 1
                break
            if re.match(r"^\s*END_VAR\b", ln, re.I):
                body_start = i + 1
        body = []
        for i, ln in enumerate(lines[body_start:], start=body_start):
            if re.match(r"^\s*END_(FUNCTION_BLOCK|FUNCTION|ORGANIZATION_BLOCK|PROGRAM|METHOD|ACTION|PROPERTY)\b", ln, re.I):
                break
            body.append({"number": first_line + i, "text": ln.rstrip()})
        while body and not body[-1]["text"].strip():
            body.pop()
        global_decls = [d for s, d in decls if s.startswith("VAR_GLOBAL")]
        for d in global_decls:
            t = dict(d)
            t["scope"] = "global:" + name
            project["tags"].append(t)
        sections = {}
        for s, d in decls:
            if not s.startswith("VAR_GLOBAL"):
                sections.setdefault(s, []).append(d)
        project["routines"].append(compact({"id": name, "program": None, "name": name, "kind": kind.lower(),
                                            "language": "SCL" if fmt == "siemens-scl" else "ST",
                                            "interface": [{"section": s, "members": ms} for s, ms in sections.items()],
                                            "lines": body}))
    return project


# --------------------------------------------------------------------------
# PLCopen XML
# --------------------------------------------------------------------------

def po_type(var):
    t = kid(var, "type")
    if t is None or not len(t):
        return None
    inner = list(t)[0]
    n = lname(inner)
    if n == "derived":
        return inner.attrib.get("name")
    if n == "array":
        base = po_type_from(kid(inner, "baseType"))
        dims = ",".join("%s..%s" % (d.attrib.get("lower"), d.attrib.get("upper")) for d in kids(inner, "dimension"))
        return "ARRAY[%s] OF %s" % (dims, base)
    if n in ("string", "wstring"):
        return n.upper() + ("[%s]" % inner.attrib["length"] if inner.attrib.get("length") else "")
    return n


def po_type_from(el):
    if el is None or not len(el):
        return None
    inner = list(el)[0]
    return inner.attrib.get("name") if lname(inner) == "derived" else lname(inner)


def po_doc(el):
    d = kid(el, "documentation")
    return text_of(d)


def po_vars(iface):
    sections = []
    if iface is None:
        return sections
    for sec in list(iface):
        n = lname(sec)
        if not n.endswith("Vars"):
            continue
        ms = []
        for v in kids(sec, "variable"):
            ms.append(compact({"name": v.attrib.get("name"), "data_type": po_type(v), "address": v.attrib.get("address"),
                               "description": po_doc(v),
                               "retain": sec.attrib.get("retain") == "true" or sec.attrib.get("persistent") == "true" or None,
                               "constant": sec.attrib.get("constant") == "true" or None}))
        sections.append({"section": n, "members": ms})
    return sections


def po_body(body):
    """Return (language, lines|networks)."""
    if body is None or not len(body):
        return None, None
    lang_el = list(body)[0]
    lang = lname(lang_el)
    if lang in ("ST", "IL"):
        txt = text_of(lang_el) or ""
        return lang, [{"number": i + 1, "text": l.rstrip()} for i, l in enumerate(txt.splitlines())]
    if lang in ("FBD", "LD", "SFC"):
        return lang, [po_graphic(lang_el)]
    return lang, None


def po_graphic(g):
    by_id = {}
    for el in g.iter():
        if el.attrib.get("localId"):
            by_id[el.attrib["localId"]] = el

    def label(lid, fp=None):
        el = by_id.get(lid)
        if el is None:
            return "?"
        n = lname(el)
        if n in ("inVariable", "inOutVariable", "outVariable"):
            return text_of(kid(el, "expression")) or "?"
        if n == "block":
            return "%s.%s" % (el.attrib.get("instanceName") or el.attrib.get("typeName"), fp or "OUT")
        if n in ("contact", "coil"):
            return "%s(%s)" % (n, text_of(kid(el, "variable")))
        if n == "leftPowerRail":
            return "POWER"
        return n

    parts, refs = [], []
    for el in g.iter():
        n = lname(el)
        if n == "block":
            inst = el.attrib.get("instanceName")
            ins = []
            for v in descendants(kid(el, "inputVariables") if kid(el, "inputVariables") is not None else el, "variable"):
                conns = descendants(v, "connection")
                src = label(conns[0].attrib.get("refLocalId"), conns[0].attrib.get("formalParameter")) if conns else "-"
                ins.append("%s:=%s" % (v.attrib.get("formalParameter"), src))
            parts.append("%s%s(%s)" % (el.attrib.get("typeName"), "<%s>" % inst if inst else "", ", ".join(ins)))
            if inst:
                refs.append({"tag": inst, "access": "write", "instr": el.attrib.get("typeName")})
        elif n in ("inVariable", "outVariable", "inOutVariable"):
            expr = text_of(kid(el, "expression"))
            if not expr:
                continue
            if n == "outVariable":
                conns = descendants(el, "connection")
                src = label(conns[0].attrib.get("refLocalId"), conns[0].attrib.get("formalParameter")) if conns else "?"
                parts.append("%s := %s" % (expr, src))
                refs.append({"tag": expr, "access": "write"})
            else:
                refs.append({"tag": expr, "access": "read" if n == "inVariable" else "unknown"})
        elif n in ("contact", "coil"):
            var = text_of(kid(el, "variable"))
            neg = el.attrib.get("negated") == "true"
            store = el.attrib.get("storage")
            parts.append("%s%s(%s)%s" % ("NOT " if neg else "", n, var, " " + store if store else ""))
            refs.append({"tag": var, "access": "write" if n == "coil" else "read",
                         "instr": "coil:" + store if store else n})
        elif n == "step":
            parts.append("STEP %s%s" % (el.attrib.get("name"), " (initial)" if el.attrib.get("initialStep") == "true" else ""))
        elif n == "transition":
            cond = text_of(kid(el, "condition"))
            if cond:
                parts.append("TRANSITION %s" % cond.replace("\n", " "))
        elif n == "action" and el.attrib.get("qualifier"):
            ref = kid(el, "reference")
            parts.append("ACTION %s %s" % (el.attrib.get("qualifier"), ref.attrib.get("name") if ref is not None else ""))
    return {"number": 1, "text": "  ".join(parts), "refs": refs, "summarized": True}


def parse_plcopen(path_, root, ctx):
    project = new_project(path_, "plcopen-xml")
    hdr = kid(root, "fileHeader")
    ch = kid(root, "contentHeader")
    project["target"] = compact({"product": hdr.attrib.get("productName") if hdr is not None else None,
                                 "name": ch.attrib.get("name") if ch is not None else None})
    for dt in descendants(root, "dataType"):
        bt = kid(dt, "baseType")
        members = []
        if bt is not None:
            for v in descendants(bt, "variable"):
                members.append(compact({"name": v.attrib.get("name"), "data_type": po_type(v), "description": po_doc(v)}))
        project["data_types"].append(compact({"name": dt.attrib.get("name"), "members": members, "description": po_doc(dt)}))

    for pou in descendants(root, "pou"):
        name = pou.attrib.get("name")
        ptype = pou.attrib.get("pouType")
        iface = kid(pou, "interface")
        raw = ET.tostring(pou, encoding="unicode")
        if text_block_safety(project, ctx, "pou", name, raw):
            continue
        lang, content = po_body(kid(pou, "body"))
        entry = {"id": name, "program": None, "name": name, "kind": ptype, "language": lang,
                 "description": po_doc(pou), "interface": [s for s in po_vars(iface) if s["members"]]}
        if lang in ("ST", "IL"):
            entry["lines"] = content
        elif content:
            entry["networks"] = content
        project["routines"].append(compact(entry))
        # actions, transitions, CODESYS methods
        for sub_name in ("action", "transition", "Method", "method"):
            for sub in descendants(pou, sub_name):
                b = kid(sub, "body")
                if b is None or sub.attrib.get("name") is None:
                    continue
                slang, scontent = po_body(b)
                sid = "%s.%s" % (name, sub.attrib.get("name"))
                se = {"id": sid, "program": name, "name": sub.attrib.get("name"), "kind": sub_name.lower(), "language": slang}
                if slang in ("ST", "IL"):
                    se["lines"] = scontent
                elif scontent:
                    se["networks"] = scontent
                project["routines"].append(compact(se))

    for gv in descendants(root, "globalVars"):
        scope = "global:" + (gv.attrib.get("name") or "")
        for v in kids(gv, "variable"):
            project["tags"].append(compact({"name": v.attrib.get("name"), "scope": scope, "data_type": po_type(v),
                                            "address": v.attrib.get("address"), "description": po_doc(v),
                                            "retain": gv.attrib.get("retain") == "true" or None,
                                            "constant": gv.attrib.get("constant") == "true" or None}))
    for task in descendants(root, "task"):
        progs = [pi.attrib.get("typeName") for pi in descendants(task, "pouInstance")]
        project["tasks"].append(compact({"name": task.attrib.get("name"), "interval": task.attrib.get("interval"),
                                         "priority": task.attrib.get("priority"), "programs": progs}))
    return project


# --------------------------------------------------------------------------
# Beckhoff TwinCAT native (.TcPOU / .TcGVL / .TcDUT)
# --------------------------------------------------------------------------

def parse_twincat(path_, root, ctx):
    project = new_project(path_, "beckhoff-twincat")
    for obj in list(root):
        kind = lname(obj)
        name = obj.attrib.get("Name")
        decl = text_of(kid(obj, "Declaration")) or ""
        if kind == "GVL":
            for s, d in parse_decls(decl):
                t = dict(d)
                t["scope"] = "global:" + name
                project["tags"].append(t)
            continue
        if kind == "DUT":
            project["data_types"].append(compact({"name": name, "members": [d for _, d in parse_decls(decl)]}))
            continue
        if kind != "POU":
            continue
        raw = ET.tostring(obj, encoding="unicode")
        if text_block_safety(project, ctx, "pou", name, raw):
            continue

        def add(el, rid, sub_kind):
            impl = kid(el, "Implementation")
            st = kid(impl, "ST") if impl is not None else None
            d = text_of(kid(el, "Declaration")) or ""
            sections = {}
            for s, dd in parse_decls(d):
                sections.setdefault(s, []).append(dd)
            e = {"id": rid, "program": name if rid != name else None, "name": el.attrib.get("Name"), "kind": sub_kind,
                 "interface": [{"section": s, "members": ms} for s, ms in sections.items()]}
            if st is not None:
                e["language"] = "ST"
                e["lines"] = [{"number": i + 1, "text": l.rstrip()} for i, l in enumerate((text_of(st) or "").splitlines())]
            elif impl is not None and len(impl):
                e["language"] = lname(list(impl)[0])
                project["warnings"].append("%s: %s implementation not decoded; export as PLCopen XML for graphical languages." % (rid, e["language"]))
            project["routines"].append(compact(e))

        m = re.search(r"^\s*(FUNCTION_BLOCK|FUNCTION|PROGRAM)", decl, re.I | re.M)
        add(obj, name, m.group(1).lower() if m else "pou")
        for sub in list(obj):
            if lname(sub) in ("Method", "Action", "Transition"):
                add(sub, "%s.%s" % (name, sub.attrib.get("Name")), lname(sub).lower())
            if lname(sub) == "Property":
                for acc in kids(sub, "Get") + kids(sub, "Set"):
                    acc.attrib.setdefault("Name", "%s.%s" % (sub.attrib.get("Name"), lname(acc)))
                    add(acc, "%s.%s.%s" % (name, sub.attrib.get("Name"), lname(acc)), "property")
    return project


# --------------------------------------------------------------------------
# Ladder neutral text / RSLogix 500 ASCII and CSV I/O maps
# --------------------------------------------------------------------------

LADDER_HINT_RE = re.compile(r"\b(XIC|XIO|OTE|OTL|OTU|TON|TOF|RTO|CTU|CTD|MOV|ONS|OSR|EQU|GRT|LES|NEQ|SOR|EOR)\b")
RUNG_PREFIX_RE = re.compile(r"^\s*(?:rung\s*)?(\d+)\s*[:.)]\s*|^\s*(\d{1,4})\s+(?=[A-Z]{2,5}[\s(])", re.I)
COMMENT_LINE_RE = re.compile(r"^\s*(//|#|;|\(\*|RC:|rung comment:?)\s*(.*?)\s*(\*\))?\s*$", re.I)
SLC_ADDR_RE = re.compile(r"(?<![\w:])#?([IOSBTCRNFLAD]|ST|MG|PD)(\d{0,3}):(\d+)(?:\.(\d+))?(?:/(\d+))?|(?<![\w:])B(\d+)/(\d+)")
SLC_FILE_TYPES = {"I": "input image", "O": "output image", "S": "status", "B": "BOOL file", "T": "TIMER",
                  "C": "COUNTER", "R": "CONTROL", "N": "INT", "F": "REAL", "L": "LONG", "ST": "STRING",
                  "A": "ASCII", "D": "BCD", "MG": "MESSAGE", "PD": "PID"}


def parse_neutral_text(path_, text, ctx):
    project = new_project(path_, "ladder-text")
    rungs = []
    pending = []          # (is_rung_comment, text)
    header = []
    routine = os.path.splitext(os.path.basename(path_))[0]
    for raw in text.splitlines():
        if not raw.strip():
            continue
        cm = COMMENT_LINE_RE.match(raw)
        if cm and not LADDER_HINT_RE.search(raw[:cm.start(2)]):
            is_rc = cm.group(1).lower().startswith(("rc", "rung"))
            if is_rc or not LADDER_HINT_RE.search(cm.group(2) or ""):
                if is_rc and not rungs:
                    header.extend(t for rc, t in pending if not rc)
                    pending = [(rc, t) for rc, t in pending if rc]
                pending.append((is_rc, cm.group(2)))
                continue
        line = raw.strip()
        num = None
        pm = RUNG_PREFIX_RE.match(line)
        if pm:
            num = int(pm.group(1) or pm.group(2))
            line = line[pm.end():].strip()
        if not LADDER_HINT_RE.search(line) and not re.search(r"[A-Z]{2,6}\(", line):
            pending.append((False, line))
            continue
        rungs.append(compact({"number": num if num is not None else len(rungs), "text": line,
                              "comment": " ".join(t for _, t in pending) if pending else None}))
        pending = []
    body = "\n".join(r["text"] for r in rungs)
    if ctx.name_is_suspected(routine):
        exclude(project, "routine", routine, "name suggests a safety function", "suspected")
        return project
    m = ROCKWELL_SAFETY_RE.search(body)
    if m:
        exclude(project, "routine", routine, "contains safety instruction %s" % m.group(1), "vendor-flagged")
        return project
    project["routines"].append(compact({"id": routine, "program": None, "name": routine, "language": "RLL",
                                        "kind": "routine", "description": " ".join(header) or None, "rungs": rungs}))
    seen = set()
    for m in SLC_ADDR_RE.finditer(body):
        addr = m.group(0).lstrip("#")
        if addr in seen:
            continue
        seen.add(addr)
        ftype = m.group(1) or "B"
        project["tags"].append({"name": addr, "scope": "data table",
                                "data_type": SLC_FILE_TYPES.get(ftype, ftype) + (" bit" if (m.group(5) or m.group(7)) else ""),
                                "source": "address in code (type inferred from file letter)"})
    return project


CSV_NAME_COLS = ("tag name", "tagname", "tag", "name", "symbol", "symbol name", "variable")
CSV_ADDR_COLS = ("address", "plc address", "io address", "i/o address", "logical address", "item", "opc item",
                 "data source", "source", "connection", "alias", "alias for")
CSV_DESC_COLS = ("description", "desc", "comment", "label", "tag description", "text", "meaning")


def find_header(rows):
    for i, row in enumerate(rows[:30]):
        low = [c.strip().strip(";").lower() for c in row]
        if any(c in CSV_DESC_COLS for c in low) and any(c in CSV_NAME_COLS + CSV_ADDR_COLS for c in low):
            return i, low
    return None, None


def xlsx_rows(path_):
    """Rows of every sheet in an .xlsx I/O list (values only, formulas not evaluated or run)."""
    try:
        import openpyxl
    except ImportError:
        return None
    wb = openpyxl.load_workbook(path_, read_only=True, data_only=True)
    rows = []
    for ws in wb.worksheets:
        for r in ws.iter_rows(values_only=True):
            rows.append(["" if v is None else str(v) for v in r])
        rows.append([])
    wb.close()
    return rows


def parse_csv_map(path_, text, ctx, rows=None):
    project = new_project(path_, "io-map-csv" if rows is None else "io-map-xlsx")
    if rows is None:
        rows = list(csv.reader(io.StringIO(text)))
    hi, header = find_header(rows)
    if hi is None:
        project["warnings"].append("CSV has no recognisable header (need a description/comment column plus a name or address column).")
        return project

    def col(names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    ci_name, ci_addr, ci_desc = col(CSV_NAME_COLS), col(CSV_ADDR_COLS), col(CSV_DESC_COLS)
    for row in rows[hi + 1:]:
        if not any(c.strip() for c in row):
            continue
        get = lambda i: row[i].strip() if i is not None and i < len(row) and row[i].strip() else None
        name, addr, desc = get(ci_name), get(ci_addr), get(ci_desc)
        if not (name or addr):
            continue
        project["tags"].append(compact({"name": name or addr, "address": addr if name else None, "description": desc,
                                        "scope": "io map", "source": "io map: " + os.path.basename(path_)}))
    return project


# --------------------------------------------------------------------------
# Detection and driver
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


def parse_file(p, ctx):
    ext = os.path.splitext(p)[1].lower()
    if ext in (".sal", ".splcproj", ".tsc"):
        pr = new_project(p, "twinsafe")
        exclude(pr, "file", os.path.basename(p), "TwinSAFE safety project file", "vendor-flagged")
        return pr
    if ext in (".acd", ".rss", ".ap17", ".ap18", ".ap19", ".ap20", ".zap17", ".zap18", ".project", ".s7p"):
        pr = new_project(p, "binary-project")
        pr["warnings"].append(
            "%s is a binary/native project archive and cannot be read directly. Export it from the vendor IDE "
            "(Studio 5000: Save As .L5X; TIA Portal: Openness XML export or 'Generate source' to .scl; "
            "CODESYS/TwinCAT: Export PLCopenXML; RSLogix 500: Save As .SLC/ASCII or print to text)." % os.path.basename(p))
        return pr
    if ext in (".xlsx", ".xlsm"):
        rows = xlsx_rows(p)
        if rows is None:
            pr = new_project(p, "io-map-xlsx")
            pr["warnings"].append("Reading .xlsx needs openpyxl (not installed); save the I/O list as CSV instead.")
            return pr
        return parse_csv_map(p, "", ctx, rows=rows)
    text = read_text(p)
    head = text.lstrip()[:4000]
    if head.startswith("<"):
        try:
            if os.path.getsize(p) > 5 * 1024 * 1024:
                # stream big exports and drop tag data subtrees while parsing
                root = safe_xml.parse_file(p, drop=("Data", "DefaultData", "DataValue", "Structure", "ArrayMember",
                                                    "StartValue"))
            else:
                root = safe_xml.fromstring(text)
        except safe_xml.UnsafeXMLError as e:
            pr = new_project(p, "refused-xml")
            pr["warnings"].append(str(e))
            return pr
        except ET.ParseError as e:
            pr = new_project(p, "unknown-xml")
            pr["warnings"].append("XML parse error: %s" % e)
            return pr
        rn = lname(root)
        if rn == "RSLogix5000Content":
            return parse_l5x(p, root, ctx)
        if rn == "Document" and any(lname(c).startswith("SW.") for c in root):
            return parse_simaticml(p, root, ctx)
        if rn == "project" and "plcopen" in (root.tag + text[:500]).lower():
            return parse_plcopen(p, root, ctx)
        if rn == "TcPlcObject":
            return parse_twincat(p, root, ctx)
        pr = new_project(p, "unknown-xml")
        pr["warnings"].append("Unrecognised XML root <%s>. If this is an HMI/SCADA file, give it to dependency_map.py instead." % rn)
        return pr
    if ext == ".csv":
        return parse_csv_map(p, text, ctx)
    if BLOCK_START_RE.search(text) and re.search(r"\bEND_(FUNCTION_BLOCK|FUNCTION|PROGRAM|ORGANIZATION_BLOCK|DATA_BLOCK|TYPE)\b", text, re.I):
        fmt = "siemens-scl" if ext in (".scl", ".db", ".udt") or re.search(r"\bBEGIN\b|ORGANIZATION_BLOCK|DATA_BLOCK|\{\s*S7_", text) else "iec-st"
        return parse_iec_text(p, text, ctx, fmt)
    if LADDER_HINT_RE.search(text):
        return parse_neutral_text(p, text, ctx)
    pr = new_project(p, "unknown")
    pr["warnings"].append("Could not identify the format of %s." % os.path.basename(p))
    return pr


def flag_safety_refs(pr):
    """Mark standard-logic items that read safety signals or call excluded logic.

    The item stays in scope (it is standard logic), but every workflow must
    say that the signal comes from, or the call goes to, safety logic that was
    excluded and must be re-created by the safety team on the target.
    """
    names = set()
    for t in pr["tags"]:
        n = (t.get("name") or "").split(".")[-1].strip('"')
        if t.get("safety") or SAFETY_NAME_RE.search(n):
            names.add(n)
    for e in pr["excluded_safety"]:
        names.add(e["name"].split("/")[-1].split(" ")[0])
    names.discard("")
    if not names:
        return
    rx = re.compile(r"(?<![\w.])(%s)(?![\w])" % "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)), re.I)
    for r in pr["routines"]:
        for key in ("rungs", "lines", "networks"):
            for item in r.get(key, []):
                hits = sorted(set(m.group(1) for m in rx.finditer(item.get("text") or "")))
                if hits and not item.get("withheld"):
                    item["safety_refs"] = hits


def project_stats(pr):
    code_chars = 0
    rungs = lines = nets = 0
    for r in pr["routines"]:
        for x in r.get("rungs", []):
            rungs += 1
            code_chars += len(x.get("text", "")) + len(x.get("comment", "") or "")
        for x in r.get("lines", []):
            lines += 1
            code_chars += len(x.get("text", "")) + 1
        for x in r.get("networks", []):
            nets += 1
            code_chars += len(x.get("text", "") or "") + len(x.get("comment", "") or "") + len(x.get("title", "") or "")
    decl_chars = sum(len(json.dumps(t)) for t in pr["tags"]) + sum(len(json.dumps(d)) for d in pr["data_types"])
    decl_chars += sum(len(json.dumps(r.get("interface", []))) for r in pr["routines"])
    return {"routines": len(pr["routines"]), "rungs": rungs, "st_lines": lines, "networks": nets,
            "tags": len(pr["tags"]), "data_types": len(pr["data_types"]), "aois": len(pr["aois"]),
            "code_chars": code_chars, "declaration_chars": decl_chars,
            "excluded_safety_items": len(pr["excluded_safety"])}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("-o", "--output", help="write JSON here instead of stdout")
    ap.add_argument("--pretty", action="store_true", help="indent JSON")
    ap.add_argument("--allow-suspected", action="append", default=[], metavar="NAME",
                    help="release a name-heuristic safety exclusion after an engineer confirmed it is NOT safety-rated "
                         "(exact program/routine/block name; repeatable). Vendor-flagged safety cannot be released.")
    args = ap.parse_args(argv)
    ctx = Ctx(args.allow_suspected)
    projects = []
    for f in args.files:
        if os.path.isdir(f):
            for dp, _, fns in os.walk(f):
                for fn in sorted(fns):
                    projects.append(parse_file(os.path.join(dp, fn), ctx))
        else:
            projects.append(parse_file(f, ctx))
    for pr in projects:
        flag_safety_refs(pr)
        pr["stats"] = project_stats(pr)
        for k in [k for k, v in pr.items() if v in (None, [], {})]:
            del pr[k]
    out = {"tool": "rungstone/parse_project", "projects": projects,
           "excluded_safety": [dict(e, file=pr["file"]) for pr in projects for e in pr.get("excluded_safety", [])],
           "note": "Tag data values are intentionally omitted. Items under excluded_safety were not emitted and must not be analysed or translated."}
    s = json.dumps(out, indent=2 if args.pretty else None, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(s)
        tot = [pr.get("stats", {}) for pr in projects]
        sys.stderr.write("wrote %s: %d file(s), %d routines, %d tags, %d safety exclusions\n" % (
            args.output, len(projects), sum(t.get("routines", 0) for t in tot), sum(t.get("tags", 0) for t in tot),
            len(out["excluded_safety"])))
    else:
        sys.stdout.write(s + "\n")


if __name__ == "__main__":
    main()
