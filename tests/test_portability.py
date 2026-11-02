"""Tests for the lock-in report (portability.py) and the CODESYS/TwinCAT facts the parser keeps for it."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "rungstone-plc", "scripts")
FIX = os.path.join(ROOT, "fixtures", "plc")
PALLETIZER = os.path.join(FIX, "codesys_palletizer", "Palletizer_PLCopen.xml")
GRIPPER = os.path.join(FIX, "twincat_gripper", "FB_Gripper.TcPOU")


def run(script, *args):
    p = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, script)] + list(args),
                       capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def report(*files):
    return run("portability.py", *files)


def finding(res, rule):
    return next((f for f in res["findings"] if f["rule"] == rule), None)


class ParserCodesysTests(unittest.TestCase):
    def test_codesys_extras_kept(self):
        pr = run("parse_project.py", PALLETIZER)["projects"][0]
        by_id = {r["id"]: r for r in pr["routines"]}
        self.assertEqual(by_id["FB_Conveyor"]["extends"], ["FB_Station"])
        self.assertEqual(by_id["FB_Station"]["implements"], ["I_Station"])
        self.assertEqual(by_id["I_Station"]["kind"], "interface")
        self.assertEqual(by_id["FB_Station.IsFaulted.Get"]["kind"], "property")
        self.assertEqual(by_id["PRG_Lamps"]["language"], "CFC")
        self.assertIn("{attribute 'enable_dynamic_creation'}", by_id["FB_CycleLogger"]["pragmas"])
        types = [m["data_type"] for s in by_id["FB_CycleLogger"]["interface"] for m in s["members"]]
        self.assertIn("POINTER TO BYTE", types)
        kinds = {d["name"]: d.get("kind") for d in pr["data_types"]}
        self.assertEqual(kinds, {"E_CellState": "enum", "ST_Recipe": "struct", "U_StatusWord": "union"})
        recipe = [t for t in pr["tags"] if t["name"] == "stActive"][0]
        self.assertTrue(recipe["persistent"])
        self.assertNotIn("PRG_SafetyMonitor", by_id)

    def test_twincat_header_and_at_star(self):
        pr = run("parse_project.py", GRIPPER)["projects"][0]
        fb = [r for r in pr["routines"] if r["id"] == "FB_Gripper"][0]
        self.assertEqual(fb["extends"], ["FB_Device"])
        self.assertEqual(fb["implements"], ["I_Device"])
        addrs = {m["name"]: m.get("address") for s in fb["interface"] for m in s["members"]}
        self.assertEqual(addrs["bClosedSensor"], "%I*")
        self.assertEqual(addrs["bValve"], "%Q*")


class PortabilityTests(unittest.TestCase):
    def test_palletizer_findings_and_ratings(self):
        res = report(PALLETIZER)
        self.assertTrue(res["supported"])
        self.assertEqual(res["source_platform"], "codesys")
        self.assertEqual(res["targets"], ["openplc", "iec-st", "siemens-scl", "rockwell", "iec61499"])
        for rule in ("oop_extends", "oop_interface", "oop_method", "oop_property", "interface_vars", "motion",
                     "file_io", "dynamic_memory", "pointers", "clock", "try_catch", "union", "enum", "cfc",
                     "persistent", "tasks", "set_reset_ops", "bit_access", "util_lib", "attributes", "io_addresses"):
            self.assertIsNotNone(finding(res, rule), rule)
        motion = finding(res, "motion")
        self.assertEqual(motion["ratings"]["openplc"], "no_equivalent")
        self.assertEqual(motion["ratings"]["siemens-scl"], "rework")
        self.assertEqual(finding(res, "pointers")["ratings"]["rockwell"], "no_equivalent")
        self.assertEqual(finding(res, "enum")["ratings"]["openplc"], "portable")
        self.assertEqual(finding(res, "bit_access")["count"], 3)
        # THIS^ / SUPER^ are not pointer dereferences
        self.assertEqual([u["name"] for u in finding(res, "pointers")["units"]], ["FB_CycleLogger"])
        self.assertEqual(sorted(l["namespace"] for l in res["libraries"]), ["FILE", "Util"])

    def test_safety_not_assessed_and_plain_pou_moves(self):
        res = report(PALLETIZER)
        self.assertEqual([x["name"] for x in res["not_assessed"]], ["PRG_SafetyMonitor"])
        units = {u["name"]: u for u in res["units"]}
        self.assertNotIn("PRG_SafetyMonitor", units)
        self.assertEqual(set(units["FB_LayerCounter"]["ratings"].values()), {"portable"})
        self.assertEqual(units["FB_PickAxis"]["ratings"]["openplc"], "no_equivalent")
        s = res["summary"]["openplc"]
        self.assertGreater(s["hours"][1], s["hours"][0])
        self.assertIn("Dynamic memory (__NEW / __DELETE)", s["blockers"])

    def test_io_addresses_do_not_block_as_is(self):
        res = report(os.path.join(FIX, "unwritten_bit", "Mixer_PLCopen.xml"))
        self.assertEqual(res["summary"]["openplc"]["portable_line_pct"], 100)
        self.assertGreater(res["summary"]["openplc"]["hours"][1], 0)
        self.assertEqual(finding(res, "io_addresses")["count"], 6)

    def test_twincat(self):
        res = report(GRIPPER)
        self.assertEqual(res["source_platform"], "twincat")
        for rule in ("oop_extends", "oop_interface", "oop_method", "clock"):
            self.assertIsNotNone(finding(res, rule), rule)
        self.assertEqual(finding(res, "io_addresses")["count"], 2)

    def test_comments_and_strings_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "Plain.st")
            with open(p, "w") as f:
                f.write("FUNCTION_BLOCK FB_Plain\nVAR\n\tx : INT;\n\ts : STRING;\nEND_VAR\n"
                        "// p := __NEW(BYTE, 4); ADR(x)\n(* SysFileOpen(x)\n   MC_Power *)\n"
                        "s := 'ADR( __TRY THIS^';\nx := x + 1;\nEND_FUNCTION_BLOCK\n")
            res = report(p)
        self.assertEqual(res["source_platform"], "iec")
        self.assertNotIn("iec-st", res["targets"])
        self.assertEqual(res["findings"], [])
        self.assertEqual(res["summary"]["openplc"]["portable_line_pct"], 100)

    def test_rockwell_not_scored(self):
        res = report(os.path.join(FIX, "timers_hmi", "Fill_Station.L5X"))
        self.assertFalse(res["supported"])
        self.assertIn("not scored", res["message"])

    def test_markdown(self):
        with tempfile.TemporaryDirectory() as d:
            md = os.path.join(d, "r.md")
            run("portability.py", PALLETIZER, "--targets", "openplc,rockwell", "--md", md, "-o", os.path.join(d, "r.json"))
            with open(md) as f:
                text = f.read()
        self.assertIn("Not a quote", text)
        self.assertIn("| OpenPLC / Beremiz |", text)
        self.assertNotIn("Siemens S7-1500 SCL |", text)
        self.assertIn("PRG_SafetyMonitor", text)
        self.assertIn("## What this report does not see", text)


if __name__ == "__main__":
    unittest.main()
