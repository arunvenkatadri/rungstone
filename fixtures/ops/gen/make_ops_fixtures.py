#!/usr/bin/env python3
"""Regenerate the synthetic ops fixtures (seeded, deterministic).

Writes:
  fixtures/ops/pump_station/historian_2025-06-12.csv   wide CSV, 2 s samples, 02:00-03:00
  fixtures/ops/pump_station/alarms_2025-06-12.csv      alarm/event log for the same hour
  fixtures/ops/downtime/line1_run_week.csv             long CSV (Timestamp,Tag,Value) run transitions, 7 days
  fixtures/ops/downtime/line1_events_week.csv          alarm/event log, 7 days, US date format

All data is synthetic and original to this repository.
"""
import csv
import datetime as dt
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.dirname(HERE)


def pump_hour():
    rnd = random.Random(101)
    t0 = dt.datetime(2025, 6, 12, 2, 0, 0)
    rows = []
    run = 1
    trip = 0
    psl = 1
    deb = 0.0
    running = 1
    run_hist = []
    restart_at = 2640
    reset_at = 2600
    level = 65.0
    pt101 = 180.0
    for s in range(0, 3601):
        # supply tank level: steady, then falls (upstream supply stopped), then refills after the trip
        if s < 1200:
            level = 65.0 + rnd.gauss(0, 0.15)
        elif run and s < 2100:
            level = 65.0 - (s - 1200) * 0.066 + rnd.gauss(0, 0.15)
        else:
            level = min(60.0, level + 0.03) + rnd.gauss(0, 0.05)
        # suction pressure follows level, plus friction loss while running
        static = 60.0 + level * 2.0
        loss = 10.0 if running else 0.0
        pt101 = static - loss + rnd.gauss(0, 1.2)
        if s >= 1500 and running:
            pt101 -= min(40.0, (s - 1500) * 0.08)  # vortexing / NPSH loss as the tank empties
        # PSL-101 with 5 kPa hysteresis, setpoint 80 kPa
        if psl and pt101 < 80.0:
            psl = 0
        elif not psl and pt101 > 85.0:
            psl = 1
        # PLC logic (1 s scan approximation of the routine)
        reset = 1 if reset_at <= s < reset_at + 2 else 0
        start = 1 if restart_at <= s < restart_at + 2 else 0
        if run and not psl:
            deb += 1.0
        else:
            deb = 0.0
        if deb > 5.0:
            trip = 1
        if reset and psl:
            trip = 0
        permissive = 1 if not trip else 0
        run = 1 if ((start or run) and permissive) else 0
        run_hist.append(run)
        running = run_hist[s - 2] if s >= 2 else run  # MCC aux contact follows the command ~2 s later
        flow = (42.0 * max(0.0, min(1.0, (pt101 - 40.0) / 120.0)) + rnd.gauss(0, 0.3)) if running else max(0.0, rnd.gauss(0, 0.05))
        amps = (24.0 + flow * 0.17 + rnd.gauss(0, 0.2)) if running else 0.0
        pt102 = (pt101 + 430.0 + flow * 0.5 + rnd.gauss(0, 2.0)) if running else (pt101 + 5.0 + rnd.gauss(0, 1.0))
        if s % 2 == 0:
            rows.append([(t0 + dt.timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%S"),
                         run, running, 0, psl, 0, 1, trip,
                         "%.1f" % pt101, "%.0f" % pt102, "%.1f" % max(flow, 0.0), "%.1f" % amps, "%.1f" % level,
                         start, 0, reset])
    hdr = ["Timestamp", "P101_Run", "P101_Running", "P101_OL", "PSL_101", "PSH_102", "SR_EStop_OK",
           "P101_LowSuct_Trip", "PT_101", "PT_102", "FT_101", "P101_Amps", "LT_100", "HMI_Start_PB",
           "HMI_Stop_PB", "Fault_Reset"]
    with open(os.path.join(OUT, "pump_station", "historian_2025-06-12.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(hdr)
        w.writerows(rows)

    def find(col, pred):
        i = hdr.index(col)
        for r in rows:
            if pred(r[i]):
                return r[0]
        return None
    lvl_low = find("LT_100", lambda v: float(v) < 30.0)
    pt_low = find("PT_101", lambda v: float(v) < 100.0)
    psl_lo = find("PSL_101", lambda v: v == 0)
    trip_at = find("P101_LowSuct_Trip", lambda v: v == 1)
    alarms = [
        ("2025-06-12T02:05:31", "C3_FILTER_DP", "Compressor C-3 inlet filter dP high", "Low", "ACT"),
        ("2025-06-12T02:06:02", "C3_FILTER_DP", "Compressor C-3 inlet filter dP high", "Low", "ACK"),
        (lvl_low, "LT_100", "T-100 supply tank level low (<30 %)", "Medium", "ACT"),
        (pt_low, "PT_101", "P-101 suction pressure low warning (<100 kPa)", "Medium", "ACT"),
        (psl_lo, "PSL_101", "P-101 suction pressure low (PSL-101)", "High", "ACT"),
        (trip_at, "P101_FAULT", "P-101 tripped: low suction pressure", "High", "ACT"),
        (trip_at, "P101_RUN", "P-101 stopped", "Event", "EVT"),
        ("2025-06-12T02:35:10", "P101_FAULT", "P-101 tripped: low suction pressure", "High", "ACK"),
        ("2025-06-12T02:35:11", "PSL_101", "P-101 suction pressure low (PSL-101)", "High", "ACK"),
        ("2025-06-12T02:35:12", "LT_100", "T-100 supply tank level low (<30 %)", "Medium", "ACK"),
        ("2025-06-12T02:43:20", "PT_101", "P-101 suction pressure low warning (<100 kPa)", "Medium", "RTN"),
        ("2025-06-12T02:43:20", "P101_FAULT", "P-101 tripped: low suction pressure", "High", "RTN"),
        ("2025-06-12T02:44:00", "P101_RUN", "P-101 started from HMI", "Event", "EVT"),
    ]
    with open(os.path.join(OUT, "pump_station", "alarms_2025-06-12.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Timestamp", "Source", "Message", "Priority", "State"])
        for a in sorted(alarms, key=lambda r: r[0]):
            w.writerow(a)
    return trip_at


def downtime_week():
    rnd = random.Random(7)
    start = dt.datetime(2025, 6, 2, 6, 0, 0)
    end = start + dt.timedelta(days=7)
    causes = [
        ("P101_FAULT", "P-101 tripped: low suction pressure", "High", 6, (900, 2400), "night"),
        ("CV2_JAM", "Conveyor CV-2 jam at transfer", "Medium", 5, (180, 900), None),
        ("P101_OL", "P-101 motor overload tripped", "High", 2, (1800, 3600), None),
        ("RIO2_COMM", "Remote I/O rack 2 connection lost", "High", 3, (120, 600), None),
        ("SAFETY_SR1", "E-stop pressed at station ES-3 (safety relay SR-1 open)", "High", 2, (300, 900), None),
        ("PLANNED", "Planned changeover started", "Event", 4, (1800, 2700), "day"),
        (None, None, None, 2, (240, 600), None),
    ]
    stops = []
    for src, msg, pri, n, (dmin, dmax), pref in causes:
        for _ in range(n):
            while True:
                day = rnd.randrange(7)
                if pref == "night":
                    hour = rnd.choice([22, 23, 0, 1, 2, 3, 4])
                elif pref == "day":
                    hour = rnd.choice([9, 10, 11, 13])
                else:
                    hour = rnd.randrange(24)
                t = start + dt.timedelta(days=day, hours=(hour - 6) % 24, minutes=rnd.randrange(60), seconds=rnd.randrange(60))
                dur = rnd.randrange(dmin, dmax)
                if t + dt.timedelta(seconds=dur) < end and all(abs((t - s[0]).total_seconds()) > 7200 for s in stops):
                    break
            stops.append((t, dur, src, msg, pri))
    stops.sort()
    run_rows = [[start.strftime("%Y-%m-%d %H:%M:%S"), "Line1_Running", 1]]
    ev_rows = []
    fmt = lambda t: t.strftime("%m/%d/%Y %H:%M:%S.") + "%03d" % (t.microsecond // 1000)
    for t, dur, src, msg, pri in stops:
        lead = dt.timedelta(seconds=rnd.choice([1, 2, 3, 4, 6]))
        if src:
            ev_rows.append([fmt(t - lead), src, msg, pri, "ACT" if pri != "Event" else "EVT"])
            # a consequential alarm that appears after the stop (must not be picked as the cause)
            ev_rows.append([fmt(t + dt.timedelta(seconds=3)), "LINE1", "Line 1 stopped", "Event", "EVT"])
            if pri != "Event":
                ev_rows.append([fmt(t + dt.timedelta(seconds=dur - 30)), src, msg, pri, "RTN"])
        else:
            ev_rows.append([fmt(t + dt.timedelta(seconds=3)), "LINE1", "Line 1 stopped", "Event", "EVT"])
        run_rows.append([t.strftime("%Y-%m-%d %H:%M:%S"), "Line1_Running", 0])
        back = t + dt.timedelta(seconds=dur)
        run_rows.append([back.strftime("%Y-%m-%d %H:%M:%S"), "Line1_Running", 1])
        ev_rows.append([fmt(back), "LINE1", "Line 1 started", "Event", "EVT"])
    run_rows.append([end.strftime("%Y-%m-%d %H:%M:%S"), "Line1_Running", 1])  # end of export period
    # background noise alarms that do not stop the line
    for _ in range(25):
        t = start + dt.timedelta(seconds=rnd.randrange(7 * 86400))
        ev_rows.append([fmt(t), "TT_210", "Bearing temperature TT-210 high warning", "Low", "ACT"])
    ev_rows.sort(key=lambda r: dt.datetime.strptime(r[0], "%m/%d/%Y %H:%M:%S.%f"))
    with open(os.path.join(OUT, "downtime", "line1_run_week.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Timestamp", "Tag", "Value"])
        w.writerows(run_rows)
    with open(os.path.join(OUT, "downtime", "line1_events_week.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Time", "Source", "Message", "Priority", "State"])
        w.writerows(ev_rows)
    return len(stops)


if __name__ == "__main__":
    print("pump trip at", pump_hour())
    print("stops generated:", downtime_week())
