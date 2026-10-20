"""Tests for rungstone-plc scripts and plugin hooks, run against fixtures/.

python3 -m unittest discover -s tests -p 'test_*.py'
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "rungstone-plc", "scripts")
FIX = os.path.join(ROOT, "fixtures", "plc")
HOOKS = os.path.join(ROOT, "hooks", "scripts")


def run(script, *args, stdin=None, cwd=None):
    p = subprocess.run([sys.executable, "-I", script] + list(args), input=stdin, capture_output=True, text=True,
                       cwd=cwd or ROOT, timeout=120)
    return p.returncode, p.stdout, p.stderr


def parse(path, *extra):
    code, out, err = run(os.path.join(SCRIPTS, "parse_project.py"), path, *extra)
    assert code == 0, err
    return json.loads(out)["projects"][0]


def depmap(plc, hmi=(), *extra):
    args = ["--plc"] + list(plc)
    if hmi:
        args += ["--hmi"] + list(hmi)
    code, out, err = run(os.path.join(SCRIPTS, "dependency_map.py"), *(args + list(extra)))
    assert code == 0, err
    return json.loads(out)


class ParseTests(unittest.TestCase):
    def test_l5x_strips_data_and_keeps_logic(self):
        pr = parse(os.path.join(FIX, "timers_hmi", "Fill_Station.L5X"))
        blob = json.dumps(pr)
        self.assertNotIn("DataValueMember", blob)
        self.assertNotIn("30000", blob)  # preset lives only in tag data
        rungs = pr["routines"][0]["rungs"]
        self.assertEqual(len(rungs), 7)
        self.assertIn("TON(Fill_Timer", rungs[1]["text"])
        mod = [m for m in pr["io_modules"] if m["name"] == "DI_Card"][0]
        self.assertEqual(mod["io_comments"][0]["comment"], "Start pushbutton (NO)")

    def test_guardlogix_safety_excluded(self):
        pr = parse(os.path.join(FIX, "estop", "Conveyor_Cell.L5X"))
        names = {e["name"]: e for e in pr["excluded_safety"]}
        self.assertEqual(names["SafetyProgram"]["confidence"], "vendor-flagged")
        self.assertEqual(names["MainProgram/R20_EStop_Chain"]["confidence"], "suspected")
        blob = json.dumps(pr["routines"])
        self.assertNotIn("DCS(", blob)
        self.assertNotIn("EStop_PB_1", blob)
        conv = [r for r in pr["routines"] if r["name"] == "Conveyor"][0]
        self.assertEqual(conv["rungs"][0]["safety_refs"], ["S_EStop_OK"])

    def test_vendor_flagged_cannot_be_released(self):
        pr = parse(os.path.join(FIX, "estop", "Conveyor_Cell.L5X"), "--allow-suspected", "R20_EStop_Chain",
                   "--allow-suspected", "SafetyProgram")
        names = [e["name"] for e in pr["excluded_safety"]]
        self.assertIn("SafetyProgram", names)
        self.assertNotIn("MainProgram/R20_EStop_Chain", names)

    def test_slc_text_and_plcopen(self):
        pr = parse(os.path.join(FIX, "two_tank", "two_tank_slc.txt"))
        self.assertEqual(len(pr["routines"][0]["rungs"]), 7)
        self.assertIn("SLC 5/04", pr["routines"][0]["description"])
        addrs = sorted(t["name"] for t in pr["tags"])
        self.assertEqual(addrs, sorted(["I:1/14", "O:2/15", "I:1/15", "I:3.0", "N7:0", "I:1/0", "N7:1", "N7:2",
                                        "O:4.0", "I:1/1", "O:6.0"]))
        po = parse(os.path.join(FIX, "unwritten_bit", "Mixer_PLCopen.xml"))
        self.assertEqual(po["routines"][0]["language"], "ST")

    def test_dtd_refused(self):
        with tempfile.NamedTemporaryFile("w", suffix=".L5X", delete=False) as f:
            f.write('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><RSLogix5000Content>&a;</RSLogix5000Content>')
        try:
            pr = parse(f.name)
            self.assertEqual(pr["format"], "refused-xml")
        finally:
            os.unlink(f.name)


class DependencyMapTests(unittest.TestCase):
    def test_timer_acc_flagged_for_iec(self):
        res = depmap([os.path.join(FIX, "timers_hmi", "Fill_Station.L5X")],
                     [os.path.join(FIX, "timers_hmi", "FTView_Fill_tags.csv")], "--target", "siemens-scl")
        by = {(c["type"], c["tag"]): c for c in res["conflicts"]}
        acc = by[("translation_break", "Fill_Timer.ACC")]
        self.assertEqual(acc["severity"], "high")
        self.assertIn("ET", acc["detail"])
        self.assertIn(("translation_break", "Fill_Timer.TT"), by)
        self.assertIn(("broken_link", "Tank_Ful"), by)
        self.assertTrue(any(c["type"] == "label_mismatch" and c["tag"] == "Batch_Count" for c in res["conflicts"]))
        self.assertTrue(any("HMI_Reset_Count" in q["tag"] for q in res["open_questions"]))

    def test_read_never_written(self):
        res = depmap([os.path.join(FIX, "unwritten_bit", "Mixer_PLCopen.xml")])
        self.assertTrue(any(c["type"] == "read_never_written" and c["tag"].endswith("Bypass_Mode") for c in res["conflicts"]))
        self.assertTrue(any(q["tag"].endswith("Bypass_Mode") for q in res["open_questions"]))
        # physical inputs are not open questions
        self.assertFalse(any("Start_PB" in q["tag"] for q in res["open_questions"]))

    def test_two_tank_registers(self):
        res = depmap([os.path.join(FIX, "two_tank", "two_tank_slc.txt")])
        rnw = sorted(c["tag"] for c in res["conflicts"] if c["type"] == "read_never_written")
        self.assertEqual(rnw, ["N7:1", "N7:2"])

    def test_safety_tags_not_analysed(self):
        res = depmap([os.path.join(FIX, "estop", "Conveyor_Cell.L5X")])
        self.assertEqual(res["summary"]["excluded_safety_items"], 2)
        self.assertFalse(any(c["tag"] == "S_EStop_OK" for c in res["conflicts"]))


class EstimateStateExportTests(unittest.TestCase):
    def test_estimate(self):
        code, out, err = run(os.path.join(SCRIPTS, "estimate.py"), os.path.join(FIX, "two_tank", "two_tank_slc.txt"), "--json")
        self.assertEqual(code, 0, err)
        d = json.loads(out)
        self.assertEqual([j["job"] for j in d["jobs"]], ["understand", "translate", "verify", "depmap"])
        self.assertFalse(d["confirm_before_running"])

    def test_state_sources(self):
        st = os.path.join(SCRIPTS, "_shared", "state.py")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run(st, "init", "--name", "t", "--target", "siemens-scl", cwd=d)[0], 0)
            src = os.path.join(d, "a.L5X")
            open(src, "w").write("<RSLogix5000Content/>")
            code, out, _ = run(st, "source", src, "--format", "l5x", cwd=d)
            self.assertEqual(code, 0, out)
            run(st, "source", src, "--format", "l5x", cwd=d)  # re-adding replaces, not duplicates
            proj = json.load(open(os.path.join(d, ".rungstone", "project.json")))
            self.assertEqual(proj["target"], "siemens-scl")
            self.assertEqual([(s["path"], s["format"], len(s["sha256"])) for s in proj["sources"]], [("a.L5X", "l5x", 64)])
            self.assertIn("source.added", open(os.path.join(d, ".rungstone", "decisions.log")).read())
            self.assertEqual(run(st, "source", os.path.join(d, "missing.xml"), cwd=d)[0], 1)
            self.assertEqual(json.loads(run(st, "status", cwd=d)[1])["selected"], ["a.L5X"])
            self.assertEqual(json.loads(run(st, "select", "--none", cwd=d)[1])["selected"], [])
            self.assertEqual(run(st, "select", "b.L5X", cwd=d)[0], 1)
            self.assertEqual(json.loads(run(st, "select", "a.L5X", cwd=d)[1])["selected"], ["a.L5X"])

    def test_export_gate(self):
        st = os.path.join(SCRIPTS, "_shared", "state.py")
        ex = os.path.join(SCRIPTS, "_shared", "export.py")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(run(st, "init", "--name", "t", cwd=d)[0], 0)
            run(st, "queue", "add", "--risk", "high", "--location", "rung 1", "--issue", "scaling", "--fix", "confirm", cwd=d)
            f = os.path.join(d, "out.scl")
            open(f, "w").write("// draft\n")
            code, out, _ = run(ex, f, cwd=d)
            self.assertEqual(code, 2)
            self.assertTrue(json.loads(out)["blocked"])
            self.assertEqual(run(st, "gate-export", cwd=d)[0], 2)
            run(st, "queue", "approve", "R1", "--note", "module confirmed 1746-NI4", cwd=d)
            code, out, _ = run(ex, f, cwd=d)
            self.assertEqual(code, 0, out)
            folder = json.loads(out)["folder"]
            self.assertTrue(os.path.exists(os.path.join(folder, "MANIFEST.md")))
            log = open(os.path.join(d, ".rungstone", "decisions.log")).read()
            self.assertIn("queue.approved", log)
            self.assertIn("export", log)


class HookTests(unittest.TestCase):
    def hook(self, name, payload):
        return run(os.path.join(HOOKS, name), stdin=json.dumps(payload))

    def test_network_guard(self):
        code, out, _ = self.hook("network_guard.py", {"tool_name": "Bash", "cwd": "/",
                                                      "tool_input": {"command": "python3 -c 'import snap7'"}})
        self.assertIn('"deny"', out)
        code, out, _ = self.hook("network_guard.py", {"tool_name": "Bash", "cwd": "/",
                                                      "tool_input": {"command": "python3 -I parse_project.py a.L5X"}})
        self.assertEqual(out.strip(), "")
        code, out, _ = self.hook("network_guard.py", {"tool_name": "mcp__kepware__write_tag", "tool_input": {}})
        self.assertIn('"deny"', out)
        code, out, _ = self.hook("network_guard.py", {"tool_name": "mcp__historian__query_range", "tool_input": {}})
        self.assertEqual(out.strip(), "")

    def test_network_guard_files(self):
        script = "from pycomm3 import LogixDriver\nwith LogixDriver('10.0.0.1') as plc:\n    plc.write('Run', 1)\n"
        out = self.hook("network_guard.py", {"tool_name": "Write", "cwd": "/",
                                             "tool_input": {"file_path": "/x/w.py", "content": script}})[1]
        self.assertIn('"deny"', out)
        self.assertNotIn("run it themselves", out)
        reader = "from pycomm3 import LogixDriver\nprint(LogixDriver)\n"
        p = {"tool_name": "Write", "tool_input": {"file_path": "/x/r.py", "content": reader}}
        with tempfile.TemporaryDirectory() as d:
            p["cwd"] = d
            self.assertEqual(self.hook("network_guard.py", p)[1].strip(), "")
            os.mkdir(os.path.join(d, ".rungstone"))
            self.assertIn('"deny"', self.hook("network_guard.py", p)[1])
        plain = {"tool_name": "Write", "cwd": "/", "tool_input": {"file_path": "/x/t.L5X", "content": "<RSLogix5000Content/>"}}
        self.assertEqual(self.hook("network_guard.py", plain)[1].strip(), "")

    def test_network_guard_scoped_patterns(self):
        with tempfile.TemporaryDirectory() as d:
            p = {"tool_name": "Bash", "cwd": d, "tool_input": {"command": "ssh user@10.0.0.5 ls"}}
            self.assertEqual(self.hook("network_guard.py", p)[1].strip(), "")
            os.mkdir(os.path.join(d, ".rungstone"))
            self.assertIn('"deny"', self.hook("network_guard.py", p)[1])
            for cmd in ("ls && scp a.L5X host:/tmp", "echo x | nc 10.0.0.5 502", "sudo ssh plc"):
                p["tool_input"]["command"] = cmd
                self.assertIn('"deny"', self.hook("network_guard.py", p)[1], cmd)
            for cmd in ('state.py queue add --text "SCP instruction on rung 1"', 'state.py log note "stop is NC wired"',
                        "grep -n 'SCP ' two_tank.txt"):
                p["tool_input"]["command"] = cmd
                self.assertEqual(self.hook("network_guard.py", p)[1].strip(), "", cmd)

    def test_export_gate_hook(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, ".rungstone"))
            json.dump({"items": [{"id": "R1", "risk": "high", "status": "open", "location": "rung 1"}]},
                      open(os.path.join(d, ".rungstone", "review_queue.json"), "w"))
            out = self.hook("export_gate.py", {"command_name": "rungstone:export", "cwd": d})[1]
            self.assertEqual(json.loads(out)["decision"], "block")
            out = self.hook("export_gate.py", {"command_name": "understand", "cwd": d})[1]
            self.assertEqual(out.strip(), "")

    def test_safety_scan(self):
        out = self.hook("safety_scan.py", {"tool_name": "Read",
                                           "tool_input": {"file_path": os.path.join(FIX, "estop", "Conveyor_Cell.L5X")}})[1]
        self.assertIn("additionalContext", out)
        out = self.hook("safety_scan.py", {"tool_name": "Write", "tool_input": {"file_path": "/tmp/x.scl",
                                                                                "content": "#a := #b AND #c;"}})[1]
        self.assertEqual(out.strip(), "")


if __name__ == "__main__":
    unittest.main()
