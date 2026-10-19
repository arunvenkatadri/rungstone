"""Tests for skills/rungstone-plc/scripts/simulate.py (stdlib unittest).

Run: python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "skills", "rungstone-plc", "scripts"))
import simulate as sim  # noqa: E402

FIX = os.path.join(ROOT, "fixtures", "plc")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def prog(self, name, text, entry=None):
        p = os.path.join(self.tmp, name)
        with open(p, "w") as f:
            f.write(text)
        return sim.Program(p, entry)

    def run_sc(self, prog, events, observe, duration=1000, initial=None, scan=10, rename=None):
        sc = {"id": "T", "scan_ms": scan, "duration_ms": duration, "initial": initial or {}, "events": events,
              "observe": observe}
        trace, err = sim.run_scenario(prog, sc, rename)
        if err:
            raise AssertionError("not run: %s at %s" % (err.reason, err.where))
        return trace

    @staticmethod
    def at(trace, name, t, scan=10):
        return trace[name][int(t // scan)]


class Ladder(Base):
    def test_seal_in_slc(self):
        p = self.prog("seal.txt", "0 SOR BST XIC I:1/14 NXB XIC O:2/15 BND XIC I:1/15 OTE O:2/15 EOR\n")
        t = self.run_sc(p, [{"at_ms": 100, "set": {"I:1/14": 1}}, {"at_ms": 200, "set": {"I:1/14": 0}},
                            {"at_ms": 500, "set": {"I:1/15": 0}}], ["O:2/15"], initial={"I:1/15": 1})
        self.assertEqual(self.at(t, "O:2/15", 50), 0)
        self.assertEqual(self.at(t, "O:2/15", 300), 1)  # sealed in after start released
        self.assertEqual(self.at(t, "O:2/15", 600), 0)  # stop drops it

    def test_branch_or_logix_text(self):
        p = self.prog("br.txt", "[XIC(A) ,XIC(B) ]OTE(C);\n")
        t = self.run_sc(p, [{"at_ms": 100, "set": {"A": 1}}, {"at_ms": 200, "set": {"A": 0, "B": 1}},
                            {"at_ms": 300, "set": {"B": 0}}], ["C"], duration=400)
        self.assertEqual([self.at(t, "C", x) for x in (50, 150, 250, 350)], [0, 1, 1, 0])

    def test_ons_one_scan(self):
        p = self.prog("ons.txt", "XIC(A)ONS(S)OTE(P);\n")
        t = self.run_sc(p, [{"at_ms": 100, "set": {"A": 1}}], ["P"], duration=300)
        self.assertEqual(sum(t["P"]), 1)
        self.assertEqual(self.at(t, "P", 100), 1)

    def test_ton_tof_rto(self):
        p = self.prog("tmr.txt", "XIC(A)TON(T1,100,0);\nXIC(B)TOF(T2,100,0);\nXIC(C)RTO(T3,100,0);\n")
        t = self.run_sc(p, [{"at_ms": 0, "set": {"A": 1, "B": 1, "C": 1}}, {"at_ms": 50, "set": {"B": 0, "C": 0}},
                            {"at_ms": 100, "set": {"C": 1}}],
                        ["T1.DN", "T1.ACC", "T2.DN", "T3.DN", "T3.ACC"], duration=400)
        self.assertEqual(self.at(t, "T1.DN", 90), 0)
        self.assertEqual(self.at(t, "T1.DN", 100), 1)
        self.assertEqual(self.at(t, "T1.ACC", 300), 100)  # ACC clamps at PRE
        self.assertEqual(self.at(t, "T2.DN", 140), 1)     # TOF still timing after B fell at 50
        self.assertEqual(self.at(t, "T2.DN", 160), 0)
        self.assertEqual(self.at(t, "T3.ACC", 90), 40)    # retained while rung false
        self.assertEqual(self.at(t, "T3.DN", 150), 0)
        self.assertEqual(self.at(t, "T3.DN", 160), 1)

    def test_slc_timer_time_base(self):
        p = self.prog("tb.txt", "0 SOR XIC I:1/0 TON T4:0 0.01 50 0 EOR\n")
        t = self.run_sc(p, [{"at_ms": 0, "set": {"I:1/0": 1}}], ["T4:0.ACC", "T4:0/DN"], duration=600)
        self.assertEqual(self.at(t, "T4:0.ACC", 250), 25)  # 0.01 s base
        self.assertEqual(self.at(t, "T4:0/DN", 490), 0)
        self.assertEqual(self.at(t, "T4:0/DN", 500), 1)

    def test_ctu_counts_edges(self):
        p = self.prog("ctu.txt", "XIC(A)CTU(C1,3,0);\n")
        ev = []
        for i in range(4):
            ev += [{"at_ms": 100 * i + 10, "set": {"A": 1}}, {"at_ms": 100 * i + 60, "set": {"A": 0}}]
        t = self.run_sc(p, ev, ["C1.ACC", "C1.DN"], duration=500)
        self.assertEqual(t["C1.ACC"][-1], 4)
        self.assertEqual(self.at(t, "C1.DN", 150), 0)
        self.assertEqual(self.at(t, "C1.DN", 250), 1)

    def test_slc_int_overflow_wraps(self):
        p = self.prog("ovf.txt", "0 SOR ADD N7:0 1 N7:0 EOR\n")
        t = self.run_sc(p, [], ["N7:0"], duration=10, initial={"N7:0": 32766})
        self.assertEqual(t["N7:0"], [32767, -32768])

    def test_div_rounding_modes(self):
        p = self.prog("div.txt", "0 SOR DIV N7:0 N7:1 N7:2 EOR\n")
        t = self.run_sc(p, [], ["N7:2"], duration=0, initial={"N7:0": 7, "N7:1": 2})
        self.assertEqual(t["N7:2"][-1], 4)  # SLC rounds 3.5 -> 4
        q = self.prog("div.st", "PROGRAM P\nVAR\n a : INT := 7;\n b : INT := 2;\n c : INT;\n r : REAL := 7.0;\n d : DINT;\nEND_VAR\n"
                               "c := a / b;\nd := r / b;\nEND_PROGRAM\n")
        t = self.run_sc(q, [], ["c", "d"], duration=0)
        self.assertEqual(t["c"][-1], 3)  # IEC truncates
        self.assertEqual(t["d"][-1], 4)  # REAL 3.5 stored to DINT rounds (ties to even -> 4)
        p.div_mode = "logix"
        s = sim.Sim(p)
        self.assertEqual(s.divide(7, 2, "DINT"), 3)      # Logix INT/INT truncates
        self.assertEqual(s.divide(7.0, 2, "DINT"), 4)    # REAL operand with integer dest rounds

    def test_unsupported_is_not_run(self):
        p = self.prog("pid.txt", "XIC(A)PID(P1,PV,0,CV,0,0,0);\n")
        sc = {"id": "U", "scan_ms": 10, "duration_ms": 100, "initial": {}, "events": [], "observe": ["CV"]}
        trace, err = sim.run_scenario(p, sc)
        self.assertIsNone(trace)
        self.assertIn("PID", err.reason)

    def test_safety_jsr_not_run(self):
        p = sim.Program(os.path.join(FIX, "estop", "Conveyor_Cell.L5X"))
        sc = {"id": "S", "scan_ms": 10, "duration_ms": 50, "initial": {}, "events": [], "observe": ["Conv_Run"]}
        trace, err = sim.run_scenario(p, sc)
        self.assertIsNone(trace)
        self.assertIn("excluded as safety", err.reason)

    def test_unknown_preset_not_run(self):
        p = self.prog("q.txt", "XIC(A)TON(T1,?,?);\n")
        sc = {"id": "Q", "scan_ms": 10, "duration_ms": 50, "initial": {"A": 1}, "events": [], "observe": ["T1.DN"]}
        trace, err = sim.run_scenario(p, sc)
        self.assertIn("preset", err.reason)


class StructuredText(Base):
    def test_st_case_timer_scaling(self):
        p = self.prog("st.st", """PROGRAM P
VAR
    state : INT;
    out : INT;
    go : BOOL;
    t1 : TON;
    raw : INT;
    pct : INT;
END_VAR
CASE state OF
    0: IF go THEN state := 1; END_IF;
    1, 2: out := 10;
          IF t1.Q THEN state := 5; END_IF;
    3..9: out := 99;
ELSE
    out := -1;
END_CASE;
t1(IN := state = 1, PT := T#200MS);
pct := SCALE_X(MIN := 0, VALUE := NORM_X(MIN := 0, VALUE := raw, MAX := 27648), MAX := 1000);
END_PROGRAM
""")
        t = self.run_sc(p, [{"at_ms": 100, "set": {"go": 1}}], ["state", "out", "pct"], duration=500,
                        initial={"raw": 13824})
        self.assertEqual(self.at(t, "state", 150), 1)
        self.assertEqual(self.at(t, "out", 150), 10)
        self.assertEqual(self.at(t, "state", 330), 5)
        self.assertEqual(self.at(t, "out", 400), 99)
        self.assertEqual(t["pct"][-1], 500)

    def test_power_cycle_retain(self):
        p = self.prog("ret.st", "PROGRAM P\nVAR RETAIN\n kept : INT;\nEND_VAR\nVAR\n lost : INT;\n go : BOOL;\nEND_VAR\n"
                               "IF go THEN kept := 5; lost := 5; END_IF;\nEND_PROGRAM\n")
        t = self.run_sc(p, [{"at_ms": 0, "set": {"go": 1}}, {"at_ms": 50, "set": {"go": 0}}, {"at_ms": 100, "power_cycle": True}],
                        ["kept", "lost"], duration=200)
        self.assertEqual(t["kept"][-1], 5)
        self.assertEqual(t["lost"][-1], 0)
        q = self.prog("ret.txt", "XIC(A)OTL(L);\nXIC(A)OTE(E);\n")
        t = self.run_sc(q, [{"at_ms": 100, "power_cycle": True}], ["L", "E"], duration=200, initial={"A": 1})
        self.assertEqual(t["L"][-1], 1)  # Rockwell keeps latched bits
        t = self.run_sc(q, [{"at_ms": 0, "set": {"A": 1}}, {"at_ms": 50, "set": {"A": 0}}, {"at_ms": 100, "power_cycle": True}],
                        ["L", "E"], duration=200)
        self.assertEqual(t["L"][-1], 1)
        self.assertEqual(t["E"][-1], 0)


class Compare(Base):
    def compare(self, src, tgt, mapping, scen):
        class A(object):
            pass
        a = A()
        a.source, a.target, a.mapping, a.scenarios = src, tgt, mapping, scen
        a.tolerance, a.source_entry, a.target_entry, a.state_dir = 1, None, None, None
        return sim.cmd_compare(a)

    def test_two_tank_scaling_caught_and_fixed(self):
        d = os.path.join(FIX, "two_tank")
        naive = self.compare(os.path.join(d, "two_tank_slc.txt"), os.path.join(d, "sim", "FB_TwoTank.scl"),
                             os.path.join(d, "sim", "mapping.csv"), os.path.join(d, "sim", "scenarios.json"))
        self.assertEqual(naive["label"], sim.LABEL)
        v = dict((r["id"], r["verdict"]) for r in naive["runs"])
        self.assertEqual(v["S1"], "differs")
        self.assertEqual(naive["runs"][0]["first_divergence"]["signal"], "N7:0")
        fixed = self.compare(os.path.join(d, "two_tank_slc.txt"), os.path.join(d, "sim", "FB_TwoTank.scl"),
                             os.path.join(d, "sim", "mapping_scaled.csv"), os.path.join(d, "sim", "scenarios.json"))
        self.assertEqual(fixed["summary"], {"match": 4, "differs": 0, "not run": 0})
        self.assertTrue(all(r["label"] == sim.LABEL for r in fixed["runs"]))

    def test_fill_station_power_cycle_differs(self):
        d = os.path.join(FIX, "timers_hmi")
        res = self.compare(os.path.join(d, "Fill_Station.L5X"), os.path.join(d, "sim", "Fill_Station.st"),
                           os.path.join(d, "sim", "mapping.csv"), os.path.join(d, "sim", "scenarios.json"))
        v = dict((r["id"], r["verdict"]) for r in res["runs"])
        self.assertEqual(v, {"S1": "match", "S2": "match", "S3": "match", "S4": "differs", "S5": "match"})
        s4 = [r for r in res["runs"] if r["id"] == "S4"][0]
        self.assertEqual(s4["first_divergence"]["t_ms"], 8000)

    def test_unmapped_signal_not_run(self):
        d = os.path.join(FIX, "timers_hmi")
        sc = os.path.join(self.tmp, "s.json")
        with open(sc, "w") as f:
            json.dump({"scenarios": [{"id": "X", "observe": ["Nope"], "duration_ms": 10}]}, f)
        res = self.compare(os.path.join(d, "Fill_Station.L5X"), os.path.join(d, "sim", "Fill_Station.st"),
                           os.path.join(d, "sim", "mapping.csv"), sc)
        self.assertEqual(res["runs"][0]["verdict"], "not run")
        self.assertIn("no mapping", res["runs"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
