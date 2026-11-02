"""
iec61499_check.py - check IEC 61499 function block types (.fbt, Eclipse 4diac 3.x) before they go to the engineer.

Reads the XML as data and never runs it. Errors break the type in 4diac or make it behave wrongly;
warnings are things to decide.

Checks:
  structure   FBType root with Name; Identification Standard 61499-2; InterfaceList; unique event and
              variable names; With references point at declared variables of the right direction
  ECC         (Basic FB) first state is START; transitions reference existing states; conditions use
              input events (or 1 / [guard]); actions reference existing algorithms and output events;
              every state reachable; unused algorithms
  sampling    data inputs read by an algorithm must be With-associated to the event that triggers it,
              and outputs it writes must be With-associated to the output event it fires: IEC 61499 only
              samples inputs and publishes outputs through those associations (stale values otherwise)
  ST          algorithm text wrapped as ALGORITHM <name> ... END_ALGORITHM; no calls to FB instances (no
              timers/edges/counters: use CYCLE arithmetic); no located variables (AT %I/%Q); END_IF etc.
              followed by ';'; no CODESYS extensions (POINTER TO, ADR, __NEW, S=, ...)
  composite   FB instances and connection end points exist

Usage:
  python3 iec61499_check.py FILE.fbt [FILE.fbt ...] [-o out.json]
Exit code 0 when no file has errors.
"""

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET  # noqa: F401  (Element type only; parsing goes through safe_xml)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _shared import safe_xml  # noqa: E402
from portability import strip_code  # noqa: E402

STD_FUNCS = set("""ABS SQRT LN LOG EXP SIN COS TAN ASIN ACOS ATAN ATAN2 EXPT MOVE ADD SUB MUL DIV MOD SHL SHR ROL ROR AND OR
XOR NOT SEL MAX MIN LIMIT MUX GT GE EQ LE LT NE LEN LEFT RIGHT MID CONCAT INSERT DELETE REPLACE FIND TRUNC
ROUND CEIL FLOOR LOWER_BOUND UPPER_BOUND""".split())
CONV_RE = re.compile(r"^\w+_TO_\w+$|^TO_\w+$|^TRUNC_\w+$", re.I)
CODESYS_ONLY = [
    (re.compile(r"\bPOINTER\s+TO\b|\bADR\s*\(", re.I), "POINTER TO / ADR(): use REF_TO / REF() or array indexing."),
    (re.compile(r"__(NEW|DELETE|TRY|CATCH|ENDTRY|ISVALIDREF)\b", re.I), "CODESYS-only built-in."),
    (re.compile(r"[\w\]\)]\s+[SR]=(?!=)"), "S= / R=: write IF ... THEN x := TRUE; END_IF;."),
    (re.compile(r"\b(THIS|SUPER)\s*\^", re.I), "THIS^/SUPER^: not supported."),
    (re.compile(r"\b(LTIME|__XWORD|__UXINT|__XINT)\b", re.I), "Platform-specific type."),
]


def lname(el):
    return el.tag.split("}", 1)[-1] if isinstance(el.tag, str) else ""


def kids(el, n):
    return [c for c in list(el) if lname(c) == n] if el is not None else []


def kid(el, n):
    k = kids(el, n)
    return k[0] if k else None


def check_fbt(path, known_types):
    errors, warnings = [], []

    def err(where, msg):
        errors.append({"where": where, "message": msg})

    def warn(where, msg):
        warnings.append({"where": where, "message": msg})

    try:
        with open(path, encoding="utf-8") as f:
            root = safe_xml.fromstring(f.read())
    except Exception as e:  # noqa: BLE001 - report any parse failure as an error
        return {"file": path, "ok": False, "errors": [{"where": "xml", "message": "Not readable XML: %s" % e}], "warnings": []}
    if lname(root) != "FBType" or not root.attrib.get("Name"):
        err("root", "Root element must be <FBType Name=...>.")
        return {"file": path, "ok": False, "errors": errors, "warnings": warnings}
    name = root.attrib["Name"]
    ident = kid(root, "Identification")
    if ident is None or ident.attrib.get("Standard") != "61499-2":
        warn("Identification", 'Missing <Identification Standard="61499-2"/>.')
    il = kid(root, "InterfaceList")
    if il is None:
        err("InterfaceList", "Missing InterfaceList.")
        return {"file": path, "ok": False, "errors": errors, "warnings": warnings}

    def evs(tag):
        return {e.attrib.get("Name"): [w.attrib.get("Var") for w in kids(e, "With")] for e in kids(kid(il, tag), "Event")}

    def vs(tag):
        out = {}
        for v in kids(kid(il, tag), "VarDeclaration"):
            out[v.attrib.get("Name")] = v.attrib.get("Type")
        return out
    ein, eout, vin, vout = evs("EventInputs"), evs("EventOutputs"), vs("InputVars"), vs("OutputVars")
    vinout = vs("InOutVars")
    names = list(ein) + list(eout) + list(vin) + list(vout) + list(vinout)
    dups = sorted({n for n in names if names.count(n) > 1})
    if dups:
        err("InterfaceList", "Duplicate names: %s." % ", ".join(dups))
    for e, ws in ein.items():
        for w in ws:
            if w not in vin and w not in vinout:
                err("event %s" % e, "With %s: not a data input." % w)
    for e, ws in eout.items():
        for w in ws:
            if w not in vout and w not in vinout:
                err("event %s" % e, "With %s: not a data output." % w)
    for tag in ("InputVars", "OutputVars"):
        for v in kids(kid(il, tag), "VarDeclaration"):
            if "%" in (v.attrib.get("Type") or "") + (v.attrib.get("InitialValue") or ""):
                err("var %s" % v.attrib.get("Name"), "Located address in a 61499 interface; wire I/O with I/O FBs instead.")

    body = kid(root, "BasicFB") or kid(root, "SimpleFB")
    algs = {}
    internal = {}
    if body is not None:
        for v in kids(kid(body, "InternalVars"), "VarDeclaration"):
            internal[v.attrib.get("Name")] = v.attrib.get("Type")
        for v in kids(kid(body, "InternalFBs"), "FB"):
            internal[v.attrib.get("Name")] = "FB"
        for a in kids(body, "Algorithm"):
            st = kid(a, "ST")
            txt = (("".join(st.itertext()) if st is not None else "") or (st.attrib.get("Text", "") if st is not None else ""))
            algs[a.attrib.get("Name")] = txt
        declared = {n.lower(): t for n, t in list(vin.items()) + list(vout.items()) + list(vinout.items()) + list(internal.items())}
        for an, txt in algs.items():
            where = "algorithm %s" % an
            m = re.match(r"^\s*ALGORITHM\s+(\w+)\b", txt, re.I)
            if m and not re.search(r"\bEND_ALGORITHM\s*$", txt, re.I):
                err(where, "ALGORITHM %s has no END_ALGORITHM." % an)
            elif m and m.group(1) != an:
                err(where, "ALGORITHM name %s doesn't match Algorithm Name %s." % (m.group(1), an))
            code_lines = strip_code(txt.splitlines())
            code = "\n".join(code_lines)
            temps = set(n.lower() for n in re.findall(r"^\s*(\w+)\s*:", code, re.M))
            for i, ln in enumerate(code_lines, 1):
                if re.search(r"\bEND_(IF|CASE|FOR|WHILE|REPEAT)\b(?!\s*;)", ln, re.I):
                    err("%s line %d" % (where, i), "END_IF/END_CASE/... needs ';' after it.")
                if re.search(r"\bAT\s+%[IQM]", ln, re.I):
                    err("%s line %d" % (where, i), "Located variables don't exist in IEC 61499.")
                for rx, msg in CODESYS_ONLY:
                    if rx.search(ln):
                        err("%s line %d" % (where, i), msg)
                for cm in re.finditer(r"(?<![\w.#])([A-Za-z_]\w*)\s*\(", ln):
                    fn = cm.group(1)
                    if fn.upper() in STD_FUNCS or CONV_RE.match(fn) or fn.upper() in ("IF", "WHILE", "ELSIF", "UNTIL", "CASE", "AND", "OR", "XOR", "NOT", "MOD"):
                        continue
                    if declared.get(fn.lower()) == "FB":
                        continue
                    if fn.lower() in declared:
                        err("%s line %d" % (where, i), "%s(...) calls an FB instance (%s). Basic FB algorithms here use CYCLE "
                            "arithmetic instead of timer/edge/counter instances (references/iec61499.md)." % (fn, declared[fn.lower()]))
                    elif fn.upper() not in known_types:
                        warn("%s line %d" % (where, i), "%s(...) is not a standard function; make sure the 4diac library has it." % fn)
            called = set(n.lower() for n in re.findall(r"(?<![\w.#'])([A-Za-z_]\w*)\s*\(", code))
            used = set(n.lower() for n in re.findall(r"(?<![\w.#'])([A-Za-z_]\w*)", code)) - called
            undeclared = sorted(n for n in used if n not in declared and n not in temps and n.upper() not in STD_FUNCS
                                and not CONV_RE.match(n) and n.upper() not in ST_WORDS and n.upper() not in known_types
                                and n.lower() != an.lower())
            if undeclared:
                err(where, "Not declared in the FB: %s." % ", ".join(undeclared))

    ecc = kid(body, "ECC") if body is not None and lname(body) == "BasicFB" else None
    if body is not None and lname(body) == "BasicFB":
        if ecc is None:
            err("ECC", "Basic FB without ECC.")
        else:
            states = {s.attrib.get("Name"): s for s in kids(ecc, "ECState")}
            order = [s.attrib.get("Name") for s in kids(ecc, "ECState")]
            if not order or order[0] != "START":
                warn("ECC", "The first ECState is the initial state; 4diac names it START.")
            used_algs = set()
            for sn, s in states.items():
                for a in kids(s, "ECAction"):
                    al, out = a.attrib.get("Algorithm"), a.attrib.get("Output")
                    if al:
                        used_algs.add(al)
                        if al not in algs:
                            err("state %s" % sn, "Action uses algorithm %s, which doesn't exist." % al)
                    if out and out not in eout:
                        err("state %s" % sn, "Action fires %s, which isn't an output event." % out)
            reach = {order[0]} if order else set()
            trans = kids(ecc, "ECTransition")
            for _ in range(len(states)):
                for t in trans:
                    if t.attrib.get("Source") in reach:
                        reach.add(t.attrib.get("Destination"))
            for t in trans:
                src, dst, cond = t.attrib.get("Source"), t.attrib.get("Destination"), (t.attrib.get("Condition") or "").strip()
                for e in (src, dst):
                    if e not in states:
                        err("transition %s->%s" % (src, dst), "State %s doesn't exist." % e)
                ev = re.split(r"\[", cond, 1)[0].strip()
                if ev and ev != "1" and ev not in ein:
                    err("transition %s->%s" % (src, dst), "Condition %s: %s is not an input event." % (cond, ev))
                # sampling: what the destination's algorithms read/write vs. the event's With list
                if ev in ein and dst in states:
                    for a in kids(states[dst], "ECAction"):
                        txt = "\n".join(strip_code(algs.get(a.attrib.get("Algorithm"), "").splitlines()))
                        idents = set(re.findall(r"(?<![\w.#'])([A-Za-z_]\w*)", txt))
                        missing = sorted(n for n in idents if n in vin and n not in ein[ev])
                        if missing:
                            warn("state %s" % dst, "[sampling] " + "Algorithm %s reads %s, but event %s doesn't carry them (With): it uses the "
                                 "values from the last event that did." % (a.attrib.get("Algorithm"), ", ".join(missing), ev))
                        out = a.attrib.get("Output")
                        written = set(re.findall(r"^\s*([A-Za-z_]\w*)\s*:=", txt, re.M))
                        miss_out = sorted(n for n in written if n in vout and out and n not in eout.get(out, []))
                        if miss_out:
                            warn("state %s" % dst, "[sampling] Algorithm %s writes %s, but output event %s doesn't carry them "
                                 "(With): connected FBs don't see the new values with that event."
                                 % (a.attrib.get("Algorithm"), ", ".join(miss_out), out))
            for sn in states:
                if sn not in reach:
                    warn("state %s" % sn, "Not reachable from START.")
            for al in algs:
                if al not in used_algs:
                    warn("algorithm %s" % al, "Not used by any state.")

    net = kid(root, "FBNetwork")
    if net is not None:
        fbs = {f.attrib.get("Name"): f.attrib.get("Type") for f in kids(net, "FB")}
        pins = set(ein) | set(eout) | set(vin) | set(vout) | set(vinout)
        for grp in ("Sockets", "Plugs"):
            for ad in kids(kid(il, grp), "AdapterDeclaration"):
                fbs[ad.attrib.get("Name")] = ad.attrib.get("Type")
        for grp in ("EventConnections", "DataConnections"):
            for c in kids(kid(net, grp), "Connection"):
                for end in (c.attrib.get("Source"), c.attrib.get("Destination")):
                    if "." in (end or ""):
                        if end.split(".", 1)[0] not in fbs:
                            err("connection %s" % end, "FB %s isn't in the network." % end.split(".", 1)[0])
                    elif end not in pins:
                        err("connection %s" % end, "%s isn't on the composite's interface." % end)
        for fbn, t in fbs.items():
            short = (t or "").split("::")[-1]
            if short.upper() not in known_types and not t.startswith("iec61499::"):
                warn("FB %s" % fbn, "Type %s: make sure it's in the 4diac project (not found next to this file)." % t)
    return {"file": path, "name": name, "ok": not errors, "errors": errors, "warnings": warnings,
            "kind": lname(body) if body is not None else ("Composite" if net is not None else "Service")}


ST_WORDS = set("""IF THEN ELSE ELSIF END_IF CASE OF END_CASE FOR TO BY DO END_FOR WHILE END_WHILE REPEAT UNTIL END_REPEAT
RETURN EXIT CONTINUE AND OR XOR NOT MOD TRUE FALSE ALGORITHM END_ALGORITHM VAR_TEMP VAR END_VAR BOOL SINT INT DINT LINT
USINT UINT UDINT ULINT REAL LREAL TIME DATE TOD DT BYTE WORD DWORD LWORD STRING WSTRING CHAR T TIME_OF_DAY
DATE_AND_TIME REF REF_TO NULL ARRAY ms s m h d""".split())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    known = set()
    for f in a.files:
        for g in os.listdir(os.path.dirname(os.path.abspath(f))):
            if g.lower().endswith(".fbt"):
                known.add(g[:-4].upper())
    res = [check_fbt(f, known) for f in a.files]
    text = json.dumps(res, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fo:
            fo.write(text)
    print(text)
    return 0 if all(r["ok"] for r in res) else 1


if __name__ == "__main__":
    sys.exit(main())
