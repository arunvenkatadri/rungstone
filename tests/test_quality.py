"""Tests for static analysis (lint.py), readable diffs (diff_exports.py) and generated unit tests (make_tests.py)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "rungstone-plc", "scripts")
FIX = os.path.join(ROOT, "fixtures", "plc")
PAL = os.path.join(FIX, "codesys_palletizer", "Palletizer_PLCopen.xml")
PAL_SCEN = os.path.join(FIX, "codesys_palletizer", "tests", "scenarios_layercounter.json")
FILL = os.path.join(FIX, "timers_hmi", "Fill_Station.L5X")
sys.path.insert(0, SCRIPTS)
import openplc_check  # noqa: E402

ST_BAD = """FUNCTION_BLOCK FB_Bad
VAR_INPUT
  bRun : BOOL;
  rLevel : REAL;
  iDiv : INT;
END_VAR
VAR_OUTPUT
  bOut : BOOL;
  bFault : BOOL;
  iRes : INT;
END_VAR
VAR
  tDelay : TON;
  tNever : TON;
  rtStart : R_TRIG;
  iUnused : INT;
  i : INT;
  x : INT;
END_VAR
IF bRun THEN
  tDelay(IN := TRUE, PT := T#2S);
END_IF;
rtStart(CLK := bRun);
rtStart(CLK := bRun);
bOut := tNever.Q;
IF rLevel = 2.5 THEN
  bOut := TRUE;
END_IF;
iRes := 100 / iDiv;
WHILE i < 10 DO
  i := i + 1;
END_WHILE;
IF FALSE THEN
  x := 1;
END_IF;
CASE i OF
  1: x := 2;
END_CASE;
x := x;
IF bRun THEN
END_IF;
iRes := 1;
iRes := 2;
IF tDelay.Q THEN
  bFault := TRUE;
END_IF;
END_FUNCTION_BLOCK
"""


def run(script, *args, cwd=None):
    p = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, script)] + list(args), capture_output=True,
                       text=True, cwd=cwd or ROOT, timeout=120)
    assert p.returncode == 0, p.stderr + p.stdout
    return p.stdout


def lint(*files):
    return json.loads(run("lint.py", *files))


class LintTests(unittest.TestCase):
    def test_st_rules(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.st")
            with open(p, "w") as f:
                f.write(ST_BAD)
            res = lint(p)
        rules = {f["rule"]: f for f in res["findings"]}
        for r in ("fb_call_conditional", "fb_called_twice", "fb_never_called", "real_equality", "division_unguarded",
                  "loop_scan_risk", "constant_condition", "case_without_else", "self_assignment", "empty_branch",
                  "unused_variable", "overwritten_assignment", "set_never_cleared"):
            self.assertIn(r, rules, r)
        self.assertEqual(rules["fb_call_conditional"]["severity"], "high")
        self.assertIn("tDelay", rules["fb_call_conditional"]["detail"])
        self.assertIn("iUnused", rules["unused_variable"]["detail"])
        self.assertIn("bFault", rules["set_never_cleared"]["detail"])

    def test_no_false_positives_on_clean_code(self):
        res = lint(os.path.join(FIX, "two_tank", "openplc", "two_tank_openplc.st"))
        self.assertEqual(res["findings"], [])

    def test_ladder_rules(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "lad.txt")
            with open(p, "w") as f:
                f.write("0 SOR XIC I:1/0 OTL B3:0/0 EOR\n1 SOR XIC I:1/1 ONS B3:1/0 OTE O:2/0 EOR\n"
                        "2 SOR XIC I:1/2 ONS B3:1/0 OTE O:2/1 EOR\n3 SOR XIC I:1/3 TON T4:0 1.0 10 0 EOR\n"
                        "4 SOR XIC I:1/4 TON T4:0 1.0 20 0 EOR\n5 SOR XIC I:1/5 RTO T4:1 1.0 5 0 EOR\n"
                        "6 SOR DIV N7:0 N7:1 N7:2 EOR\n7 SOR AFI OTE O:2/2 EOR\n8 SOR XIC B3:0/0 OTE O:2/3 EOR\n")
            res = lint(p)
        rules = {f["rule"] for f in res["findings"]}
        for r in ("latch_never_unlatched", "oneshot_storage_shared", "timer_shared", "rto_without_reset",
                  "ladder_division_unguarded", "rung_disabled"):
            self.assertIn(r, rules, r)

    def test_double_coil_and_conditional_user_fb(self):
        res = lint(PAL)
        self.assertIn("user_fb_call_conditional", {f["rule"] for f in res["findings"]})
        self.assertEqual([x["name"] for x in res["not_assessed"]], ["PRG_SafetyMonitor"])
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "two.txt")
            with open(p, "w") as f:
                f.write("0 SOR XIC I:1/0 OTE O:2/0 EOR\n1 SOR XIC I:1/1 OTE O:2/0 EOR\n")
            res = lint(p)
        self.assertEqual(res["findings"][0]["rule"], "competing_writers")


class DiffTests(unittest.TestCase):
    def test_l5x_diff(self):
        with open(FILL) as f:
            v1 = f.read()
        import re
        v2 = re.sub(r"(<Rung Number=\"1\".*?<Text><!\[CDATA\[)(.*?)(\]\]>)", lambda m: m.group(1) + m.group(2).replace("Fill_Timer", "Fill_Timer2") + m.group(3), v1, count=1, flags=re.S)
        v2 = v2.replace("<Tags>", '<Tags><Tag Name="New_Tag" TagType="Base" DataType="DINT"/>', 1)
        self.assertNotEqual(v1, v2)
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "v1.L5X"), os.path.join(d, "v2.L5X")
            with open(a, "w") as f:
                f.write(v1)
            with open(b, "w") as f:
                f.write(v2)
            md = os.path.join(d, "d.md")
            run("diff_exports.py", a, b, "--md", md, "-o", os.path.join(d, "d.json"))
            with open(os.path.join(d, "d.json")) as f:
                res = json.load(f)
            with open(md) as f:
                text = f.read()
        ch = res["routines"]["changed"][0]
        self.assertEqual(ch["hunks"][0]["op"], "changed")
        self.assertIn("Fill_Timer2", ch["hunks"][0]["writes"])
        self.assertEqual(res["tags"]["added"][0]["name"], "New_Tag")
        self.assertIn("```diff", text)
        self.assertIn("## Not compared", text)

    def test_identical_is_empty(self):
        res = json.loads(run("diff_exports.py", PAL, PAL))
        self.assertEqual(res["summary"]["items_changed"], 0)
        self.assertEqual(res["summary"]["tags_changed"], 0)
        self.assertEqual(res["safety"][0]["change"], "present in both (not compared)")

    def test_st_diff_affects_and_tasks(self):
        with open(PAL) as f:
            v1 = f.read()
        v2 = v1.replace("PT := T#8S", "PT := T#12S").replace('interval="PT0.01S"', 'interval="PT0.005S"')
        with tempfile.TemporaryDirectory() as d:
            b = os.path.join(d, "v2.xml")
            with open(b, "w") as f:
                f.write(v2)
            res = json.loads(run("diff_exports.py", PAL, b))
        aff = {a["tag"]: a for a in res["affects"]}
        self.assertTrue(aff["tJam"]["read_by"])  # tJam.Q is read on other lines
        self.assertEqual(res["tasks"][0]["new"]["interval"], "PT0.005S")


class MakeTestsTests(unittest.TestCase):
    def gen(self, fw, *extra):
        d = tempfile.mkdtemp()
        out = json.loads(run("make_tests.py", "--program", PAL, "--entry", "FB_LayerCounter", "--scenarios", PAL_SCEN,
                             "--framework", fw, "-o", d, *extra))
        return d, out

    def test_cfunit(self):
        d, out = self.gen("cfunit")
        self.assertEqual([t["asserts"] for t in out["tests"]], [21, 4, 27])
        with open(out["written"][0]) as f:
            text = f.read()
        self.assertIn("EXTENDS CfUnit.FB_TestSuite", text)
        self.assertIn("METHOD PRIVATE Test_S2", text)
        self.assertIn("AssertEquals_INT(Expected := 1, Actual := uut.iBox", text)
        self.assertIn("TEST_FINISHED();", text)
        self.assertIn("CfUnit.RUN();", text)

    def test_tcunit_files(self):
        d, out = self.gen("tcunit")
        import xml.dom.minidom
        for p in out["written"]:
            xml.dom.minidom.parse(p)
        with open(out["written"][0]) as f:
            self.assertIn("EXTENDS TcUnit.FB_TestSuite", f.read())

    def test_plain_openplc_passes_checker(self):
        d, out = self.gen("plain", "--openplc", "--with-source")
        with open(out["written"][0]) as f:
            res = openplc_check.check(f.read())
        self.assertTrue(res["ok"], res["errors"])
        regs = {l["name"]: l["modbus"] for l in res["located"]}
        self.assertEqual(regs["nPass"], "holding register 1024")

    def test_program_refused(self):
        p = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, "make_tests.py"), "--program", PAL, "--entry",
                            "PLC_PRG", "--scenarios", PAL_SCEN, "--framework", "cfunit", "-o", tempfile.mkdtemp()],
                           capture_output=True, text=True, timeout=120)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("FUNCTION_BLOCK", p.stderr)

    def test_end_if_semicolon_rule(self):
        st = ("PROGRAM prog0\n  VAR\n    x : INT;\n  END_VAR\n  IF x > 1 THEN\n    x := 0;\n  END_IF\nEND_PROGRAM\n"
              "CONFIGURATION Config0\n  RESOURCE Res0 ON PLC\n    TASK Main(INTERVAL := T#20ms,PRIORITY := 0);\n"
              "    PROGRAM Inst0 WITH Main : prog0;\n  END_RESOURCE\nEND_CONFIGURATION\n")
        self.assertIn("semicolon", {e["rule"] for e in openplc_check.check(st)["errors"]})
        self.assertTrue(openplc_check.check(st.replace("END_IF\n", "END_IF;\n"))["ok"])


@unittest.skipUnless(os.environ.get("RUNGSTONE_COMPILE") == "1",
                     "set RUNGSTONE_COMPILE=1 with the rungstone-matiec Docker image built (tools/matiec)")
class GeneratedTestsCompile(unittest.TestCase):
    def test_plain_tests_compile(self):
        d = tempfile.mkdtemp()
        out = json.loads(run("make_tests.py", "--program", PAL, "--entry", "FB_LayerCounter", "--scenarios", PAL_SCEN,
                             "--framework", "plain", "--openplc", "--with-source", "-o", d))
        p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "compile_openplc.py"), out["written"][0]],
                           capture_output=True, text=True, timeout=600)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
