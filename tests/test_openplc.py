"""Tests for the OpenPLC / Beremiz target: openplc_check.py, st_to_plcopen.py, CONFIGURATION parsing,
simulator compare against an OpenPLC translation, and the Modbus check in dependency_map.py."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.dom.minidom

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "rungstone-plc", "scripts")
FIX = os.path.join(ROOT, "fixtures", "plc")
TWO_TANK = os.path.join(FIX, "two_tank")
OPENPLC_ST = os.path.join(TWO_TANK, "openplc", "two_tank_openplc.st")
sys.path.insert(0, SCRIPTS)
import openplc_check  # noqa: E402

BAD = """FUNCTION_BLOCK FB_X EXTENDS FB_Base
VAR
  bIn AT %IX0.9 : BOOL;
  p : POINTER TO INT;
END_VAR
// a line comment
x S= y;
w.3 := TRUE;
END_FUNCTION_BLOCK

PROGRAM main
VAR RETAIN
  a AT %MX0.0 : BOOL;
  b AT %QW2 : BOOL;
  c AT %IW1 : INT;
  d AT %IW1 : INT;
  e AT %I* : BOOL;
END_VAR
{attribute 'hide'}
END_PROGRAM

CONFIGURATION Cfg
  RESOURCE R1 ON PLC
    TASK T1(INTERVAL := T#20ms, PRIORITY := 0);
    PROGRAM i0 WITH T2 : nothere;
  END_RESOURCE
END_CONFIGURATION
"""


def run(script, *args, cwd=None, ok=True):
    p = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, script)] + list(args),
                       capture_output=True, text=True, cwd=cwd or ROOT, timeout=120)
    if ok:
        assert p.returncode == 0, p.stderr + p.stdout
    return p


def compare(target, mapping):
    p = run("simulate.py", "compare", "--source", "two_tank_slc.txt", "--target", target, "--mapping", mapping,
            "--scenarios", os.path.join("sim", "scenarios.json"), cwd=TWO_TANK)
    return {r["id"]: r for r in json.loads(p.stdout)["runs"]}


class CheckTests(unittest.TestCase):
    def test_fixture_is_clean(self):
        with open(OPENPLC_ST) as f:
            res = openplc_check.check(f.read())
        self.assertTrue(res["ok"], res["errors"])
        self.assertEqual(res["warnings"], [])
        self.assertEqual(res["instances"][0]["program"], "prog_TwoTank")
        regs = {l["name"]: l["modbus"] for l in res["located"]}
        self.assertEqual(regs["Master_Run"], "coil 0")
        self.assertEqual(regs["Level_Cutoff"], "holding register 1024")
        self.assertEqual(regs["Level_Raw"], "input register 0")

    def test_bad_file_errors(self):
        res = openplc_check.check(BAD)
        self.assertFalse(res["ok"])
        rules = {e["rule"] for e in res["errors"]}
        for r in ("oop", "located_in_fb", "address", "pointer", "comment", "operator", "bit_access", "address_type",
                  "duplicate_address", "pragma", "configuration"):
            self.assertIn(r, rules)
        msgs = " ".join(e["message"] for e in res["errors"])
        self.assertIn("no %MX memory", msgs)
        self.assertIn("bit must be 0-7", msgs)
        self.assertIn("TwinCAT-style", msgs)
        self.assertEqual({w["rule"] for w in res["warnings"]}, {"retain", "unused_program", "header"})

    def test_strings_and_comments_not_flagged(self):
        with open(OPENPLC_ST) as f:
            text = f.read()
        text = text.replace("Master_Run := (Start_PB",
                                               "(* x S= y; w.3 POINTER TO {pragma} *)\n  Master_Run := (Start_PB")
        res = openplc_check.check(text)
        self.assertTrue(res["ok"], res["errors"])

    def test_exit_code(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.st")
            with open(p, "w") as f:
                f.write(BAD)
            self.assertEqual(run("openplc_check.py", p, ok=False).returncode, 1)
        self.assertEqual(run("openplc_check.py", OPENPLC_ST).returncode, 0)

    def test_modbus_of(self):
        m = openplc_check.modbus_of
        self.assertEqual(m("%QX1.2"), "coil 10")
        self.assertEqual(m("%IX0.7"), "discrete input 7")
        self.assertEqual(m("%QW5"), "holding register 5")
        self.assertEqual(m("%MD3"), "holding registers 2054-2055")
        self.assertEqual(m("%ML1"), "holding registers 4100-4103")
        self.assertIsNone(m("%ID0"))
        self.assertIsNone(m("%MX0.0"))


class ConvertAndSimulateTests(unittest.TestCase):
    def test_configuration_parsed(self):
        p = run("parse_project.py", OPENPLC_ST)
        pr = json.loads(p.stdout)["projects"][0]
        self.assertEqual([r["id"] for r in pr["routines"]], ["prog_TwoTank"])
        self.assertEqual(pr["tasks"], [{"name": "Main", "interval": "T#10ms", "priority": "0", "programs": ["prog_TwoTank"]}])

    def test_plcopen_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "plc.xml")
            info = json.loads(run("st_to_plcopen.py", OPENPLC_ST, "-o", out).stdout)
            self.assertEqual(info["pous"], ["prog_TwoTank"])
            xml.dom.minidom.parse(out)
            pr = json.loads(run("parse_project.py", out).stdout)["projects"][0]
            self.assertEqual(pr["format"], "plcopen-xml")
            r = pr["routines"][0]
            addrs = {m["name"]: m.get("address") for s in r["interface"] for m in s["members"]}
            self.assertEqual(addrs["Level_Cutoff"], "%MW0")
            self.assertEqual(pr["tasks"][0]["programs"], ["prog_TwoTank"])
            runs = compare(out, os.path.join("openplc", "mapping.csv"))
        self.assertEqual(runs["S1"]["verdict"], "match")

    def test_struct_enum_and_initial_values(self):
        st = ("TYPE\n  E_Mode : (Idle, Run, Fault);\n  ST_Rec : STRUCT\n    iLayers : INT := 4; (* layers *)\n"
              "  END_STRUCT;\nEND_TYPE\n\nFUNCTION_BLOCK FB_A\nVAR_INPUT\n  x : INT := 3;\nEND_VAR\n"
              "VAR\n  arr : ARRAY[0..2] OF INT := [1, 2, 3];\nEND_VAR\nx := x + 1;\nEND_FUNCTION_BLOCK\n")
        with tempfile.TemporaryDirectory() as d:
            src, out = os.path.join(d, "a.st"), os.path.join(d, "plc.xml")
            with open(src, "w") as f:
                f.write(st)
            info = json.loads(run("st_to_plcopen.py", src, "-o", out).stdout)
            with open(out) as f:
                x = f.read()
        self.assertEqual(sorted(info["types"]), ["E_Mode", "ST_Rec"])
        self.assertIn('<value name="Fault"/>', x)
        self.assertIn('<simpleValue value="3"/>', x)
        self.assertEqual(len(info["warnings"]), 1)  # array initialiser not exported

    def test_simulator_compare(self):
        runs = compare(os.path.join("openplc", "two_tank_openplc.st"), os.path.join("openplc", "mapping.csv"))
        for s in ("S1", "S2", "S3"):
            self.assertEqual(runs[s]["verdict"], "match", runs[s])
        # wire break: SLC shows -250 (visible fault), OpenPLC input clamps at 0
        self.assertEqual(runs["S4"]["verdict"], "differs")
        div = runs["S4"]["first_divergence"]
        self.assertEqual((div["signal"], div["source"], div["target"]), ("N7:0", -250, 0))


class HmiModbusTests(unittest.TestCase):
    def test_hmi_tags_need_modbus_addresses(self):
        d = os.path.join(FIX, "timers_hmi")
        p = run("dependency_map.py", "--plc", "Fill_Station.L5X", "--hmi", "FTView_Fill_tags.csv", "--target", "openplc",
                "--mapping", os.path.join("sim", "mapping.csv"), cwd=d)
        res = json.loads(p.stdout[p.stdout.index("{"):])
        mb = {c["tag"]: c for c in res["conflicts"] if "Modbus" in c["detail"]}
        self.assertEqual(mb["Fill_Valve"]["severity"], "medium")
        self.assertIn("coil 0", mb["Fill_Valve"]["detail"])
        self.assertEqual(mb["Batch_Count"]["severity"], "high")

    def test_mapping_keyed_by_symbol(self):
        d = os.path.join(FIX, "timers_hmi")
        with tempfile.TemporaryDirectory() as t:
            m = os.path.join(t, "mapping.csv")
            with open(os.path.join(d, "sim", "mapping.csv")) as f, open(m, "w") as g:
                rows = f.read().splitlines()
                g.write(rows[0] + "\n" + "\n".join("," + r.split(",", 1)[1] for r in rows[1:]) + "\n")
            p = run("dependency_map.py", "--plc", "Fill_Station.L5X", "--hmi", "FTView_Fill_tags.csv", "--mapping", m, cwd=d)
        res = json.loads(p.stdout[p.stdout.index("{"):])
        missing = [c["tag"] for c in res["conflicts"] if "missing from the old->new mapping" in c["detail"]]
        self.assertNotIn("Fill_Valve", missing)

    def test_target_listed_everywhere(self):
        sys.path.insert(0, os.path.join(ROOT, "shared", "workbench"))
        import server  # noqa: E402
        self.assertIn("openplc", server.TARGETS)
        with open(os.path.join(ROOT, "shared", "workbench", "app.html")) as f:
            self.assertIn('"openplc"', f.read())
        p = run("estimate.py", OPENPLC_ST, "--target", "openplc", "--json")
        self.assertIn('"job": "translate"', p.stdout)


if __name__ == "__main__":
    unittest.main()
