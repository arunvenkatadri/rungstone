"""
make_tests.py - generate unit tests for a function block from scenarios, with expected values from the simulator.

For each scenario the FB is simulated scan by scan (simulate.py). Checkpoints are taken where the
signals are stable: the last scan before each input change, and the last scan. A signal still
changing at a checkpoint (within the two scans before it) is not asserted there, so the tests don't
depend on exact timer timing. Expected values are what the code does today (characterisation tests):
run them before refactoring or migrating, and any change in behaviour shows up as a failed assertion.

Frameworks:
  cfunit   CODESYS + CfUnit: FB_<FB>_Tests EXTENDS CfUnit.FB_TestSuite, one method per scenario,
           and PRG_TEST calling CfUnit.RUN(). Written as text to paste into CODESYS objects.
  tcunit   TwinCAT + TcUnit: the same, as .TcPOU files to add to the PLC project.
  plain    IEC 61131-3 only: PROGRAM prog_Tests with pass/fail counters, no framework (runs on any IEC
           runtime; --openplc adds the CONFIGURATION and puts the counters on %MW0-%MW2).

Only function blocks are supported: a test calls its own instance every cycle. Inputs are set through
VAR_INPUT; only VAR_OUTPUT (and inputs) can be asserted, because internal variables aren't readable
from outside an FB.

Usage:
  python3 make_tests.py --program FILE --entry FB_NAME --scenarios S.json --framework cfunit|tcunit|plain
                        [-o OUTDIR] [--openplc]
"""

import argparse
import json
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_project as pp  # noqa: E402
import simulate as sim  # noqa: E402

ASSERT_TYPES = {"BOOL", "BYTE", "WORD", "DWORD", "LWORD", "SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT",
                "ULINT", "REAL", "LREAL", "TIME", "STRING"}


def find_fb(path, entry):
    projects = [pp.parse_file(path, pp.Ctx())]
    for pr in projects:
        for r in pr["routines"]:
            if (r.get("name") or "").lower() == entry.lower() and not r.get("program"):
                return r
    raise SystemExit("POU %s not found in %s" % (entry, path))


def interface(r):
    ins, outs = {}, {}
    for sec in r.get("interface") or []:
        s = (sec.get("section") or "").lower()
        for m in sec.get("members") or []:
            t = (m.get("data_type") or "").upper()
            if s in ("var_input", "inputvars"):
                ins[m["name"]] = t
            elif s in ("var_output", "outputvars"):
                outs[m["name"]] = t
    return ins, outs


def lit(v, t):
    t = t.upper().split("[")[0].split("(")[0]
    if t == "BOOL":
        return "TRUE" if v else "FALSE"
    if t in ("REAL", "LREAL"):
        x = float(v)
        return repr(x) if "e" not in repr(x) else "%.9g" % x
    if t == "TIME":
        return "T#%dms" % int(v)
    if t.startswith("STRING"):
        return "'%s'" % str(v).replace("'", "$'")
    return str(int(v))


def checkpoints(sc):
    scan = sc["scan_ms"]
    n = int(sc["duration_ms"] // scan) + 1
    cps = set()
    for e in sc.get("events", []):
        k = int(e["at_ms"] // scan) - 1
        if 0 <= k < n:
            cps.add(k)
    cps.add(n - 1)
    return sorted(cps), n


def build(path, entry, scenarios, notes):
    fb = find_fb(path, entry)
    kind = (fb.get("kind") or "").lower()
    if kind not in ("functionblock", "function_block"):
        raise SystemExit("%s is a %s. Tests call their own instance of a FUNCTION_BLOCK; move the logic into a function "
                         "block (recommended for testing) or test the program in a separate application." % (entry, kind or "POU"))
    ins, outs = interface(fb)
    prog = sim.Program(path, entry)
    tests = []
    for sc in scenarios:
        observe = []
        for o in sc.get("observe", []):
            name = next((n for n in list(outs) + list(ins) if n.lower() == o.lower()), None)
            if name is None:
                notes.append("%s: %s is internal to %s and can't be read from a test; expose it as an output." % (sc["id"], o, entry))
            else:
                observe.append(name)
        sets = list(sc.get("initial", {}).items()) + [(k, v) for e in sc.get("events", []) for k, v in (e.get("set") or {}).items()]
        bad = sorted({k for k, _ in sets if not any(k.lower() == i.lower() for i in ins)})
        if bad:
            raise SystemExit("%s sets %s, which are not inputs of %s." % (sc["id"], ", ".join(bad), entry))
        run_sc = dict(sc, observe=observe)
        trace, err = sim.run_scenario(prog, run_sc)
        if err:
            notes.append("%s not generated: the simulator could not run it (%s)." % (sc["id"], err.reason))
            continue
        cps, n = checkpoints(sc)
        asserts = []
        for k in cps:
            for o in observe:
                vals = trace[o]
                if k >= 2 and not (vals[k] == vals[k - 1] == vals[k - 2]):
                    notes.append("%s: %s is still changing at %d ms; not asserted there." % (sc["id"], o, k * sc["scan_ms"]))
                    continue
                t = (outs.get(o) or ins.get(o) or "").split("[")[0].split("(")[0]
                asserts.append({"cycle": k, "ms": k * sc["scan_ms"], "name": o, "type": t, "value": vals[k]})
        events = {}
        for name, v in sc.get("initial", {}).items():
            events.setdefault(0, []).append((name, v))
        for e in sorted(sc.get("events", []), key=lambda e: e["at_ms"]):
            for name, v in (e.get("set") or {}).items():
                events.setdefault(int(e["at_ms"] // sc["scan_ms"]), []).append((name, v))
        tests.append({"id": sc["id"], "name": sc.get("name") or sc["id"], "scan_ms": sc["scan_ms"], "cycles": n,
                      "events": events, "asserts": asserts})
    return fb, ins, outs, tests


def ident(s):
    return re.sub(r"\W+", "_", s).strip("_")


def input_type(ins, name):
    return next(t for n, t in ins.items() if n.lower() == name.lower())


def input_name(ins, name):
    return next(n for n in ins if n.lower() == name.lower())


def method_body(fb_name, t, ins, framework):
    lines = ["TEST('%s %s');" % (t["id"], t["name"].replace("'", "$'")), ""]
    for cyc in sorted(t["events"]):
        sets = " ".join("uut.%s := %s;" % (input_name(ins, n), lit(v, input_type(ins, n))) for n, v in t["events"][cyc])
        lines.append("IF nCycle = %d THEN %s END_IF" % (cyc, sets))
    lines += ["", "uut();", ""]
    for a in t["asserts"]:
        typ = a["type"] if a["type"] in ASSERT_TYPES else "DINT"
        msg = "'%s at %d ms: %s'" % (t["id"], a["ms"], a["name"])
        if typ in ("REAL", "LREAL"):
            call = "AssertEquals_%s(Expected := %s, Actual := uut.%s, Delta := 0.001, Message := %s);" % (
                typ, lit(a["value"], typ), a["name"], msg)
        else:
            call = "AssertEquals_%s(Expected := %s, Actual := uut.%s, Message := %s);" % (typ, lit(a["value"], typ), a["name"], msg)
        lines.append("IF nCycle = %d THEN %s END_IF" % (a["cycle"], call))
    lines += ["", "nCycle := nCycle + 1;", "IF nCycle >= %d THEN" % t["cycles"], "    TEST_FINISHED();", "END_IF"]
    return "\n".join(lines)


HEADER = ("Generated by Rungstone from %s scenarios - DRAFT FOR ENGINEER REVIEW. Expected values are the simulated "
          "behaviour of %s today (simulated, not hardware-tested). Run this in a task with a cycle time of %d ms.")


def render_xunit(fb, ins, tests, framework, src):
    lib = "CfUnit" if framework == "cfunit" else "TcUnit"
    name = "%s_Tests" % fb["name"] if fb["name"].upper().startswith("FB_") else "FB_%s_Tests" % fb["name"]
    scan = tests[0]["scan_ms"] if tests else 10
    head = HEADER % (os.path.basename(src), fb["name"], scan)
    decl = "(* %s *)\nFUNCTION_BLOCK %s EXTENDS %s.FB_TestSuite\nVAR\nEND_VAR" % (head, name, lib)
    impl = "\n".join("%s();" % ident("Test_" + t["id"]) for t in tests)
    methods = []
    for t in tests:
        mdecl = "METHOD PRIVATE %s\nVAR_INST\n    uut : %s;\n    nCycle : UDINT;\nEND_VAR" % (ident("Test_" + t["id"]), fb["name"])
        methods.append((ident("Test_" + t["id"]), mdecl, method_body(fb["name"], t, ins, framework)))
    prg_decl = "PROGRAM PRG_TEST\nVAR\n    fbTests : %s;\nEND_VAR" % name
    prg_impl = "%s.RUN();" % lib
    return name, decl, impl, methods, prg_decl, prg_impl, lib


def text_bundle(name, decl, impl, methods, prg_decl, prg_impl, lib):
    out = ["(* ===================== How to add these to CODESYS =====================",
           "   1. Add the %s library (Library Manager)." % lib,
           "   2. Add a function block %s; paste DECLARATION and IMPLEMENTATION below." % name,
           "   3. For each METHOD below: right-click %s > Add Object > Method, same name, paste both parts." % name,
           "   4. Add PRG_TEST and call it from a task whose cycle time matches the header; log in and run.",
           "   ======================================================================= *)", "",
           "(* ---------- %s: DECLARATION ---------- *)" % name, decl, "",
           "(* ---------- %s: IMPLEMENTATION ---------- *)" % name, impl, ""]
    for mname, mdecl, mbody in methods:
        out += ["(* ---------- METHOD %s: DECLARATION ---------- *)" % mname, mdecl, "",
                "(* ---------- METHOD %s: IMPLEMENTATION ---------- *)" % mname, mbody, ""]
    out += ["(* ---------- PRG_TEST: DECLARATION ---------- *)", prg_decl, "",
            "(* ---------- PRG_TEST: IMPLEMENTATION ---------- *)", prg_impl, ""]
    return "\n".join(out)


def cdata(s):
    return "<![CDATA[%s]]>" % s.replace("]]>", "]] >")


def guid():
    return "{%s}" % uuid.uuid4()


def tcpou(name, decl, impl, methods):
    x = ['<?xml version="1.0" encoding="utf-8"?>', '<TcPlcObject Version="1.1.0.1">',
         '  <POU Name="%s" Id="%s" SpecialFunc="None">' % (name, guid()),
         "    <Declaration>%s</Declaration>" % cdata(decl),
         "    <Implementation>", "      <ST>%s</ST>" % cdata(impl), "    </Implementation>"]
    for mname, mdecl, mbody in methods:
        x += ['    <Method Name="%s" Id="%s">' % (mname, guid()), "      <Declaration>%s</Declaration>" % cdata(mdecl),
              "      <Implementation>", "        <ST>%s</ST>" % cdata(mbody), "      </Implementation>", "    </Method>"]
    x += ["  </POU>", "</TcPlcObject>"]
    return "\n".join(x) + "\n"


SECTION_ST = {"inputvars": "VAR_INPUT", "outputvars": "VAR_OUTPUT", "inoutvars": "VAR_IN_OUT", "localvars": "VAR",
              "tempvars": "VAR_TEMP"}


def fb_source(fb, notes):
    """Rebuild a plain ST FUNCTION_BLOCK from the parsed export (declarations without initial values)."""
    out = ["FUNCTION_BLOCK %s" % fb["name"]]
    for sec in fb.get("interface") or []:
        kw = SECTION_ST.get((sec.get("section") or "").lower(), sec.get("section") or "VAR").upper()
        out.append("  %s" % kw.replace("_RETAIN", " RETAIN").replace("_CONSTANT", " CONSTANT"))
        for m in sec.get("members") or []:
            out.append("    %s : %s;" % (m["name"], m["data_type"]))
        out.append("  END_VAR")
    out += ["  " + re.sub(r"\b(END_(?:IF|CASE|FOR|WHILE|REPEAT))\b(?!\s*;)", r"\1;", l.get("text") or "")
            for l in fb.get("lines") or []]
    out.append("END_FUNCTION_BLOCK")
    notes.append("FB source rebuilt from the export without initial values; check them against the original.")
    return "\n".join(out) + "\n\n"


def render_plain(fb, ins, tests, src, openplc, with_source=False, notes=None):
    scan = tests[0]["scan_ms"] if tests else 10
    out = ["(* %s *)" % (HEADER % (os.path.basename(src), fb["name"], scan)), ""]
    if with_source:
        out.append(fb_source(fb, notes if notes is not None else []))
    out.append("PROGRAM prog_Tests")
    if openplc:
        out += ["  VAR", "    nPass AT %MW0 : INT;  (* holding register 1024 *)",
                "    nFail AT %MW1 : INT;  (* holding register 1025 *)",
                "    nDone AT %MW2 : INT;  (* holding register 1026: scenarios finished *)", "  END_VAR"]
    else:
        out += ["  VAR", "    nPass : INT;", "    nFail : INT;", "    nDone : INT;", "  END_VAR"]
    out.append("  VAR")
    for t in tests:
        out += ["    uut_%s : %s;" % (ident(t["id"]), fb["name"]), "    nCycle_%s : UDINT;" % ident(t["id"])]
    out += ["    sFirstFail : STRING;", "  END_VAR", ""]
    for t in tests:
        tid = ident(t["id"])
        out.append("  (* %s %s *)" % (t["id"], t["name"]))
        out.append("  IF nCycle_%s < %d THEN" % (tid, t["cycles"]))
        for cyc in sorted(t["events"]):
            sets = " ".join("uut_%s.%s := %s;" % (tid, input_name(ins, n), lit(v, input_type(ins, n))) for n, v in t["events"][cyc])
            out.append("    IF nCycle_%s = %d THEN %s END_IF;" % (tid, cyc, sets))
        out.append("    uut_%s();" % tid)
        for a in t["asserts"]:
            actual = "uut_%s.%s" % (tid, a["name"])
            if a["type"] in ("REAL", "LREAL"):
                cond = "ABS(%s - %s) < 0.001" % (actual, lit(a["value"], a["type"]))
            else:
                cond = "%s = %s" % (actual, lit(a["value"], a["type"]))
            out.append("    IF nCycle_%s = %d THEN IF %s THEN nPass := nPass + 1; ELSE nFail := nFail + 1; "
                       "IF nFail = 1 THEN sFirstFail := '%s %dms %s'; END_IF; END_IF; END_IF;" % (
                           tid, a["cycle"], cond, t["id"], a["ms"], a["name"]))
        out += ["    nCycle_%s := nCycle_%s + 1;" % (tid, tid),
                "    IF nCycle_%s = %d THEN nDone := nDone + 1; END_IF;" % (tid, t["cycles"]), "  END_IF;", ""]
    out.append("END_PROGRAM")
    if openplc:
        out += ["", "CONFIGURATION Config0", "  RESOURCE Res0 ON PLC",
                "    TASK Main(INTERVAL := T#%dms, PRIORITY := 0);" % scan,
                "    PROGRAM Inst0 WITH Main : prog_Tests;", "  END_RESOURCE", "END_CONFIGURATION"]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--program", required=True)
    ap.add_argument("--entry", required=True)
    ap.add_argument("--scenarios", required=True)
    ap.add_argument("--framework", choices=("cfunit", "tcunit", "plain"), required=True)
    ap.add_argument("--openplc", action="store_true", help="plain only: add CONFIGURATION Config0 and %%MW counters")
    ap.add_argument("--with-source", action="store_true", help="plain only: include the FB itself, rebuilt as ST")
    ap.add_argument("-o", "--outdir", default=".")
    a = ap.parse_args(argv)
    scenarios = sim.load_scenarios(a.scenarios)
    notes = []
    fb, ins, outs, tests = build(a.program, a.entry, scenarios, notes)
    os.makedirs(a.outdir, exist_ok=True)
    written = []
    if a.framework == "plain":
        p = os.path.join(a.outdir, "%s_tests%s.st" % (fb["name"], "_openplc" if a.openplc else ""))
        with open(p, "w", encoding="utf-8") as f:
            f.write(render_plain(fb, ins, tests, a.program, a.openplc, a.with_source, notes))
        written.append(p)
    else:
        name, decl, impl, methods, prg_decl, prg_impl, lib = render_xunit(fb, ins, tests, a.framework, a.program)
        if a.framework == "cfunit":
            p = os.path.join(a.outdir, "%s_cfunit.st" % name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(text_bundle(name, decl, impl, methods, prg_decl, prg_impl, lib))
            written.append(p)
        else:
            p = os.path.join(a.outdir, "%s.TcPOU" % name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(tcpou(name, decl, impl, methods))
            p2 = os.path.join(a.outdir, "PRG_TEST.TcPOU")
            with open(p2, "w", encoding="utf-8") as f:
                f.write(tcpou("PRG_TEST", prg_decl, prg_impl, []))
            written += [p, p2]
    print(json.dumps({"written": written, "fb": fb["name"], "tests": [
        {"id": t["id"], "cycles": t["cycles"], "asserts": len(t["asserts"])} for t in tests],
        "notes": notes, "label": sim.LABEL}, indent=1))


if __name__ == "__main__":
    main()
