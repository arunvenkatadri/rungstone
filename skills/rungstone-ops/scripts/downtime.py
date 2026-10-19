"""
downtime.py - stops, causes, MTBF/MTTR and shift breakdown from exported data.

Stops come from a run signal (wide or long historian CSV) or, if no run signal
is available, from an event log using --stop-pattern / --start-pattern.  The
candidate cause of each stop is the FIRST alarm/event that became active in
the window [stop - cause_window, stop + grace]; consequential events such as
"Line stopped" are skipped (--ignore-pattern).  A cause found this way is
"evidence-supported", never "confirmed"; stops with no event are "unknown".

Usage:
  python3 downtime.py --run FILE --run-tag Line1_Running [--running-value 1]
                      [--events alarms.csv] [--shifts 06:00=A,14:00=B,22:00=C]
                      [--cause-window 5m] [--min-stop 60s] [--planned-pattern REGEX]
  python3 downtime.py --events log.csv --stop-pattern "stopped|trip" --start-pattern "started"
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ops_csv as oc  # noqa: E402

DEFAULT_IGNORE = r"(?i)\b(line|machine|unit|pump|conveyor)\s*\d*\s*(stopped|started|running|is running|stop$)|^p-?\d+ stopped$|started from hmi"
DEFAULT_PLANNED = r"(?i)(planned|changeover|change-over|break|lunch|shift change|cip|clean(ing)?|scheduled|maintenance window|pm\b)"
SAFETY_RE = re.compile(r"(?i)(e-?stop|emergency|safety|light ?curtain|guard door|interlock open)")


def parse_shifts(s):
    out = []
    for part in s.split(","):
        hhmm, _, name = part.partition("=")
        h, m = hhmm.split(":")
        out.append((int(h) * 3600 + int(m) * 60, name or hhmm))
    out.sort()
    return out


def shift_of(t, shifts):
    """Return (shift name, shift date) - a shift that crosses midnight belongs to the day it started."""
    sod = t % 86400
    day = t - sod
    cur = None
    for start, name in shifts:
        if sod >= start:
            cur = (start, name)
    if cur is None:  # before the first shift start: belongs to yesterday's last shift
        cur = shifts[-1]
        day -= 86400
    return cur[1], oc.fmt_time(day)[:10]


def stops_from_run(path, tag, running_value):
    info = oc.sniff(path)
    tp = oc.TimeParser()
    samples = []
    for t, name, k, v, q in oc.iter_samples(path, info, [tag], None, None, tp):
        if k == "none":
            continue
        samples.append((t, 1 if (v == running_value or (isinstance(v, str) and v.lower() in oc.TRUE)) else 0))
    if not samples:
        raise SystemExit("run tag %s not found in %s" % (tag, path))
    samples.sort()
    stops = []
    down = None
    for t, run in samples:
        if not run and down is None:
            down = t
        elif run and down is not None:
            stops.append([down, t])
            down = None
    if down is not None:
        stops.append([down, None])
    return stops, samples[0][0], samples[-1][0], tp


def stops_from_events(events, stop_re, start_re):
    stops = []
    down = None
    for e in events:
        txt = "%s %s" % (e["source"] or "", e["message"] or "")
        if down is None and stop_re.search(txt):
            down = e["t"]
        elif down is not None and start_re.search(txt):
            stops.append([down, e["t"]])
            down = None
    if down is not None:
        stops.append([down, None])
    return stops


def active(e):
    st = (e.get("state") or "").upper()
    return st in ("", "ACT", "ACTIVE", "IN", "ALARM", "EVT", "EVENT", "TRUE", "1", "ON", "RAISED", "COMING", "C", "UNACK")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", help="historian CSV containing the run signal")
    ap.add_argument("--run-tag")
    ap.add_argument("--running-value", type=float, default=1.0)
    ap.add_argument("--events", help="alarm/event log CSV")
    ap.add_argument("--stop-pattern", help="events-only mode: regex marking a stop")
    ap.add_argument("--start-pattern", help="events-only mode: regex marking a restart")
    ap.add_argument("--shifts", default="06:00=A,14:00=B,22:00=C")
    ap.add_argument("--first-out-window", default="30s",
                    help="the earliest event this close before the stop is the first-out cause")
    ap.add_argument("--cause-window", default="5m", help="fallback: nearest event this far before the stop")
    ap.add_argument("--grace", default="5s", help="events up to this long AFTER the stop still count as cause")
    ap.add_argument("--min-stop", default="0s", help="ignore stops shorter than this")
    ap.add_argument("--planned-pattern", default=DEFAULT_PLANNED)
    ap.add_argument("--ignore-pattern", default=DEFAULT_IGNORE, help="events never treated as causes")
    ap.add_argument("--max-stops", type=int, default=200)
    a = ap.parse_args(argv)

    shifts = parse_shifts(a.shifts)
    events = []
    etp = oc.TimeParser()
    if a.events:
        events = sorted(oc.iter_events(a.events, tp=etp), key=lambda e: e["t"])
    if a.run:
        if not a.run_tag:
            raise SystemExit("--run-tag is required with --run")
        stops, t_first, t_last, tp = stops_from_run(a.run, a.run_tag, a.running_value)
        source = "run signal %s" % a.run_tag
    elif a.events and a.stop_pattern and a.start_pattern:
        stops = stops_from_events(events, re.compile(a.stop_pattern, re.I), re.compile(a.start_pattern, re.I))
        t_first, t_last, tp = events[0]["t"], events[-1]["t"], etp
        source = "event log patterns"
    else:
        raise SystemExit("give --run/--run-tag, or --events with --stop-pattern and --start-pattern")

    cw, grace, min_stop = oc.parse_duration(a.cause_window), oc.parse_duration(a.grace), oc.parse_duration(a.min_stop)
    fo = oc.parse_duration(a.first_out_window)
    ign = re.compile(a.ignore_pattern)
    planned_re = re.compile(a.planned_pattern)
    rows = []
    for s, e in stops:
        dur = (e - s) if e is not None else (t_last - s)
        if dur < min_stop:
            continue
        cands = [ev for ev in events if s - cw <= ev["t"] <= s + grace and active(ev)
                 and not ign.search("%s %s" % (ev["source"] or "", ev["message"] or ""))]
        close = [ev for ev in cands if ev["t"] >= s - fo]
        if close:
            cause, basis = close[0], "evidence-supported (first-out: earliest event within %gs before the stop)" % fo
        elif cands:
            cause, basis = cands[-1], "inferred (no event within %gs; nearest event %gs before the stop)" % (fo, s - cands[-1]["t"])
        else:
            cause, basis = None, "unknown"
        shift, sdate = shift_of(s, shifts)
        txt = ("%s" % (cause["message"] or cause["source"])) if cause else None
        cat = "unknown"
        if cause:
            cat = "planned" if planned_re.search(txt or "") else "safety" if SAFETY_RE.search(txt or "") else "unplanned"
        rows.append({"start": oc.fmt_time(s), "end": oc.fmt_time(e) if e is not None else None,
                     "duration_s": round(dur, 1), "open_at_end_of_data": e is None or None, "shift": shift,
                     "shift_date": sdate, "cause": txt or "no event found in window",
                     "cause_source": cause["source"] if cause else None,
                     "cause_lead_s": round(s - cause["t"], 1) if cause else None,
                     "cause_basis": basis,
                     "category": cat,
                     "other_events": [ev["message"] for ev in cands if ev is not cause][:3] or None, "_s": s, "_d": dur})

    def agg(key_fn, subset):
        res = {}
        for r in subset:
            k = key_fn(r)
            x = res.setdefault(k, {"count": 0, "downtime_s": 0.0})
            x["count"] += 1
            x["downtime_s"] += r["_d"]
        return res

    unplanned = [r for r in rows if r["category"] != "planned"]
    by_cause = agg(lambda r: r["cause"], unplanned)
    top_count = sorted(by_cause.items(), key=lambda kv: (-kv[1]["count"], -kv[1]["downtime_s"]))[:10]
    top_dur = sorted(by_cause.items(), key=lambda kv: -kv[1]["downtime_s"])[:10]
    per_shift = {}
    for r in rows:
        x = per_shift.setdefault(r["shift"], {"stops": 0, "unplanned_stops": 0, "downtime_s": 0.0, "unplanned_downtime_s": 0.0, "causes": {}})
        x["stops"] += 1
        x["downtime_s"] += r["_d"]
        if r["category"] != "planned":
            x["unplanned_stops"] += 1
            x["unplanned_downtime_s"] += r["_d"]
            x["causes"][r["cause"]] = x["causes"].get(r["cause"], 0) + 1
    for x in per_shift.values():
        x["top_cause"] = max(x["causes"].items(), key=lambda kv: kv[1])[0] if x["causes"] else None
        x["downtime_s"] = round(x["downtime_s"], 1)
        x["unplanned_downtime_s"] = round(x["unplanned_downtime_s"], 1)
    per_day = {}
    for r in rows:
        x = per_day.setdefault(r["shift_date"], {"stops": 0, "downtime_s": 0.0})
        x["stops"] += 1
        x["downtime_s"] = round(x["downtime_s"] + r["_d"], 1)
    recurring = []
    for cause, x in by_cause.items():
        if cause == "no event found in window":
            continue
        shifts_hit = {}
        for r in unplanned:
            if r["cause"] == cause:
                shifts_hit[r["shift"]] = shifts_hit.get(r["shift"], 0) + 1
        if x["count"] >= 3 or any(v >= 2 for v in shifts_hit.values()):
            dom = max(shifts_hit.items(), key=lambda kv: kv[1])
            recurring.append({"cause": cause, "count": x["count"], "by_shift": shifts_hit,
                              "pattern": ("concentrated on shift %s (%d of %d)" % (dom[0], dom[1], x["count"])
                                          if dom[1] >= max(2, 0.6 * x["count"]) else "spread across shifts")})
    recurring.sort(key=lambda r: -r["count"])
    span = (t_last - t_first) if t_last is not None and t_first is not None else 0.0
    down_total = sum(r["_d"] for r in rows)
    down_unpl = sum(r["_d"] for r in unplanned)
    run_time = max(span - down_total, 0.0)
    n_unpl = len(unplanned)
    for r in rows:
        del r["_s"], r["_d"]
        for k in [k for k, v in r.items() if v is None]:
            del r[k]
    out = {
        "tool": "rungstone/downtime", "stops_from": source,
        "period": {"start": oc.fmt_time(t_first), "end": oc.fmt_time(t_last), "hours": round(span / 3600.0, 2)},
        "totals": {"stops": len(rows), "unplanned_stops": n_unpl, "planned_stops": len(rows) - n_unpl,
                   "downtime_h": round(down_total / 3600.0, 2), "unplanned_downtime_h": round(down_unpl / 3600.0, 2),
                   "availability_pct": round(100.0 * (span - down_total) / span, 2) if span else None,
                   "mtbf_h": round(run_time / n_unpl / 3600.0, 2) if n_unpl else None,
                   "mttr_min": round(down_unpl / n_unpl / 60.0, 1) if n_unpl else None,
                   "stops_with_no_cause_event": sum(1 for r in rows if r["category"] == "unknown"),
                   "safety_stops": sum(1 for r in rows if r["category"] == "safety")},
        "top_causes_by_count": [dict(cause=k, count=v["count"], downtime_min=round(v["downtime_s"] / 60.0, 1)) for k, v in top_count],
        "top_causes_by_downtime": [dict(cause=k, count=v["count"], downtime_min=round(v["downtime_s"] / 60.0, 1)) for k, v in top_dur],
        "recurring": recurring, "per_shift": per_shift, "per_day": per_day, "stops": rows[:a.max_stops],
        "definitions": {"shifts": a.shifts, "mtbf": "run time / unplanned stops (planned stops excluded)",
                        "mttr": "mean duration of unplanned stops", "availability": "(period - all downtime) / period",
                        "cause": "earliest active alarm/event within the first-out window before each stop (evidence-supported), else the nearest within the cause window (inferred); never confirmed without the plant"},
        "notes": ["Safety stops (e-stop, guard, light curtain) are counted and reported as 'a safety function tripped'; "
                  "their cause must be investigated with the safety system, not inferred here.",
                  oc.zone_note(tp)],
    }
    sys.stdout.write(json.dumps(out, separators=(",", ":"), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
