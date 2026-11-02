"""Tests for the IEC 61499 / Eclipse 4diac target: st_to_61499.py, iec61499_check.py, .fbt parsing and simulation."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import xml.dom.minidom

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "rungstone-plc", "scripts")
FIX = os.path.join(ROOT, "fixtures", "plc")
FILL = os.path.join(FIX, "iec61499_fill")


def run(script, *args, cwd=None, ok=True):
    p = subprocess.run([sys.executable, "-I", os.path.join(SCRIPTS, script)] + list(args), capture_output=True,
                       text=True, cwd=cwd or ROOT, timeout=120)
    if ok:
        assert p.returncode == 0, p.stderr + p.stdout
    return p


def generate(src, *extra):
    d = tempfile.mkdtemp()
    info = json.loads(run("st_to_61499.py", src, "-o", d, *extra).stdout)
    return d, info


def check(*files, ok=True):
    p = run("iec61499_check.py", *files, ok=ok)
    return json.loads(p.stdout)


class GenerateTests(unittest.TestCase):
    def test_fill_generates_clean_types(self):
        d, info = generate(os.path.join(FILL, "Fill_61499.st"))
        self.assertEqual(info["fb"], "Fill")
        self.assertIn("CYCLE", info["inputs"])
        self.assertEqual(info["warnings"], [])
        for p in info["written"]:
            xml.dom.minidom.parse(p)
        res = check(*info["written"])
        for r in res:
            self.assertTrue(r["ok"], r["errors"])
            self.assertFalse([w for w in r["warnings"] if "[sampling]" in w["message"]], r["warnings"])
        self.assertEqual({r["kind"] for r in res}, {"BasicFB", "Composite"})
        with open(os.path.join(d, "Fill_App.fbt")) as f:
            app = f.read()
        self.assertIn('Type="iec61499::events::E_CYCLE"', app)
        self.assertIn('Source="Cycle.EO" Destination="Logic.REQ"', app)

    def test_located_and_fb_instances_warned(self):
        d, info = generate(os.path.join(FIX, "two_tank", "openplc", "two_tank_openplc.st"))
        self.assertIn("Start_PB", info["inputs"])          # AT %IX -> data input
        self.assertIn("Master_Run", info["outputs"])       # AT %QX -> data output
        self.assertTrue(any("was %MW0" in w for w in info["warnings"]))
        d2, info2 = generate(os.path.join(FILL, "Fill_61131.st"))
        self.assertTrue(any("Fill_Timer : TON" in w for w in info2["warnings"]))
        res = check(os.path.join(d2, "Fill.fbt"), ok=False)
        self.assertFalse(res[0]["ok"])
        self.assertTrue(any("calls an FB instance" in e["message"] for e in res[0]["errors"]))

    def test_simulated_same_as_61131_source(self):
        d, info = generate(os.path.join(FILL, "Fill_61499.st"))
        p = run("simulate.py", "compare", "--source", "Fill_61131.st", "--target", os.path.join(d, "Fill.fbt"),
                "--target-entry", "Fill.REQ", "--mapping", "mapping.csv", "--scenarios", "scenarios.json", cwd=FILL)
        res = json.loads(p.stdout)
        self.assertEqual(res["summary"], {"match": 3, "differs": 0, "not run": 0})


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.d, info = generate(os.path.join(FILL, "Fill_61499.st"))
        with open(os.path.join(self.d, "Fill.fbt")) as f:
            self.good = f.read()

    def broken(self, old, new):
        self.assertIn(old, self.good)
        p = os.path.join(self.d, "Broken.fbt")
        with open(p, "w") as f:
            f.write(self.good.replace(old, new, 1))
        return check(p, ok=False)[0]

    def test_detects_errors(self):
        cases = [
            ('<ECAction Algorithm="REQ" Output="CNF"/>', '<ECAction Algorithm="RUN" Output="CNF"/>', "doesn't exist"),
            ('Condition="REQ" x="760"', 'Condition="GO" x="760"', "not an input event"),
            ('<ECAction Algorithm="REQ" Output="CNF"/>', '<ECAction Algorithm="REQ" Output="DONE"/>', "isn't an output event"),
            ("END_IF;\n", "END_IF\n", "needs ';'"),
            ("Fill_Valve := Filling;", "Fill_Valve := Filling + Ghost;", "Not declared"),
            ('<With Var="Start_PB"/>', '<With Var="Nope"/>', "not a data input"),
        ]
        for old, new, msg in cases:
            r = self.broken(old, new)
            self.assertFalse(r["ok"], old)
            self.assertTrue(any(msg in e["message"] for e in r["errors"]), (msg, r["errors"]))

    def test_sampling_warning(self):
        s = self.good
        start = s.index('<Event Name="REQ"')
        end = s.index("</Event>", start)
        p = os.path.join(self.d, "Stale.fbt")
        with open(p, "w") as f:
            f.write(s[:start] + s[start:end].replace('<With Var="Level_High"/>', "") + s[end:])
        r = check(p)[0]
        self.assertTrue(r["ok"])
        self.assertTrue(any("[sampling]" in w["message"] and "Level_High" in w["message"] for w in r["warnings"]))


class ParseTests(unittest.TestCase):
    def test_parse_fbt(self):
        d, info = generate(os.path.join(FILL, "Fill_61499.st"))
        pr = json.loads(run("parse_project.py", os.path.join(d, "Fill.fbt")).stdout)["projects"][0]
        self.assertEqual(pr["format"], "iec61499-fbt")
        ids = [r["id"] for r in pr["routines"]]
        self.assertEqual(ids, ["Fill", "Fill.initialize", "Fill.REQ"])
        fb = pr["routines"][0]
        self.assertEqual([e["name"] for e in fb["events"]], ["INIT", "REQ", "INITO", "CNF"])
        req = pr["routines"][2]
        self.assertFalse(any("ALGORITHM" in l["text"] for l in req["lines"]))
        app = json.loads(run("parse_project.py", os.path.join(d, "Fill_App.fbt")).stdout)["projects"][0]
        self.assertEqual(app["routines"][0]["kind"], "composite")
        self.assertIn("Cycle : iec61499::events::E_CYCLE", app["routines"][0]["networks"][0]["text"])

    def test_lint_and_hmi_rule(self):
        d, info = generate(os.path.join(FILL, "Fill_61499.st"))
        res = json.loads(run("lint.py", os.path.join(d, "Fill.fbt")).stdout)
        self.assertEqual([f for f in res["findings"] if f["severity"] == "high"], [])
        h = os.path.join(FIX, "timers_hmi")
        p = run("dependency_map.py", "--plc", "Fill_Station.L5X", "--hmi", "FTView_Fill_tags.csv", "--target", "iec61499", cwd=h)
        out = json.loads(p.stdout[p.stdout.index("{"):])
        self.assertTrue(any("communication FB" in c["detail"] for c in out["conflicts"]))

    def test_portability_column(self):
        res = json.loads(run("portability.py", os.path.join(FIX, "codesys_palletizer", "Palletizer_PLCopen.xml")).stdout)
        self.assertIn("iec61499", res["targets"])
        comms = [f for f in res["findings"] if f["rule"] == "motion"][0]
        self.assertEqual(comms["ratings"]["iec61499"], "no_equivalent")


if __name__ == "__main__":
    unittest.main()
