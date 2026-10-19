#!/usr/bin/env python3
"""Deterministic checks of the rungstone-ops scripts on the ops fixtures.

Run from the repo root: python3 evals/rungstone-ops/smoke_test.py
Exits non-zero on the first failed check.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
S = os.path.join(ROOT, "skills", "rungstone-ops", "scripts")
F = os.path.join(ROOT, "fixtures", "ops")
HIST = os.path.join(F, "pump_station", "historian_2025-06-12.csv")
ALM = os.path.join(F, "pump_station", "alarms_2025-06-12.csv")
L5X = os.path.join(F, "pump_station", "Pump_Station.L5X")


def run(script, *args):
    r = subprocess.run([sys.executable, "-I", os.path.join(S, script)] + list(args), capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("%s failed: %s" % (script, r.stderr[-800:]))
    return json.loads(r.stdout)


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


s = run("historian.py", "summary", HIST)
check(s["signals"] == 15 and s["sample_interval_s"] == 2.0, "summary: 15 signals at 2 s")

t = run("historian.py", "trip", HIST, "--signal", "P101_Run", "--alarms", ALM)["trips"][0]
check(t["trip_time"] == "2025-06-12T02:29:44", "trip found at 02:29:44")
check(t["nearest_discrete_precursor"]["tag"] == "PSL_101", "first-out discrete precursor is PSL_101")
check(t["earliest_analog_deviation"]["tag"] in ("LT_100", "PT_101"), "leading indicator is tank level / suction pressure")
check([e["tag"] for e in t["simultaneous_with_trip"]] == ["P101_LowSuct_Trip"], "low-suction latch simultaneous with trip")
check({"P101_OL", "PSH_102", "SR_EStop_OK"} <= set(t["unchanged"]), "overload, PSH, safety relay unchanged")
check(not any(e["tag"] in ("P101_Running",) for e in t["before"]), "running feedback is not a precursor")

lt = run("logic_trace.py", "--plc", L5X, "--tag", "P101_Run")["traces"][0]
locs = json.dumps(lt)
for loc in ("P101_Control rung 4", "P101_Control rung 0", "P101_Control rung 2", "P101_Control rung 1"):
    check(loc in locs, "logic_trace cites " + loc)
check('"safety"' in locs and "SR_EStop_OK" in locs, "safety relay status marked safety, not traced")

d = run("downtime.py", "--run", os.path.join(F, "downtime", "line1_run_week.csv"), "--run-tag", "Line1_Running",
        "--events", os.path.join(F, "downtime", "line1_events_week.csv"))
check(d["totals"]["stops"] == 24 and d["totals"]["unplanned_stops"] == 20, "downtime: 24 stops, 20 unplanned")
check(d["top_causes_by_count"][0]["cause"].startswith("P-101 tripped: low suction"), "top cause low suction")
check(d["recurring"][0]["by_shift"] == {"C": 6}, "low suction concentrated on shift C")
check(d["totals"]["safety_stops"] == 2, "two safety stops counted")

with tempfile.TemporaryDirectory() as tmp:
    c = run("chart.py", HIST, "--tags", "PT_101,LT_100,P101_Run", "--around", "2025-06-12T02:29:44", "--before", "10m",
            "--after", "2m", "--alarms", ALM, "--out", os.path.join(tmp, "c.svg"), "--svg")
    check(os.path.getsize(c["chart"]) > 5000 and len(c["markers"]) >= 3, "chart.py SVG with markers")
print("all ops checks passed")
