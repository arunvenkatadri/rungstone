#!/usr/bin/env python3
"""
historian.py - offline analysis of historian exports (CSV, wide or long).

Subcommands
  summary  FILE                         signals, types, time range, sample rate, gaps, quality
  window   FILE --start T --end T | --around T [--before 10m --after 2m]
           [--tags a,b] [--points 200]  downsampled slice for reading/charting
  events   FILE [--tags a,b] [--limit "PT_101<100" ...] [--start/--end]
                                        bool transitions, analog limit crossings, state changes
  trip     FILE --signal P101_Run [--direction falling|rising] [--level X]
           [--lookback 15m] [--baseline 30m] [--post 60s] [--alarms alarms.csv]
                                        for every trip: what changed before it (first-out
                                        candidates), at the same sample, and after it

Output is compact JSON on stdout.  Files are streamed; only the requested time
ranges are held in memory.  Results describe the data only: a change that
precedes a trip is evidence, not proof of cause.
"""

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ops_csv as oc  # noqa: E402


def out(obj):
    sys.stdout.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n")


def classify(values):
    nums = [v for k, v in values if k in ("num", "bool")]
    strs = [v for k, v in values if k == "str"]
    if strs and len(strs) >= len(nums):
        return "state"
    if not nums:
        return "empty"
    distinct = set(nums)
    if distinct <= {0.0, 1.0}:
        return "bool"
    if len(distinct) <= 8 and all(float(v).is_integer() for v in distinct):
        return "state"
    return "analog"


def load_series(path, tags=None, start=None, end=None, tp=None):
    info = oc.sniff(path)
    tp = tp or oc.TimeParser()
    series = {}
    for t, name, k, v, q in oc.iter_samples(path, info, tags, start, end, tp):
        if k == "none":
            continue
        series.setdefault(name, []).append((t, k, v))
    for s in series.values():
        s.sort(key=lambda x: x[0])
    return info, tp, series


def series_kind(s):
    return classify([(k, v) for _, k, v in s])


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else 0.5 * (xs[n // 2 - 1] + xs[n // 2])


# ---------------------------------------------------------------- summary

def cmd_summary(a):
    info = oc.sniff(a.file)
    tp = oc.TimeParser()
    stats = {}
    times = []
    t_first = t_last = None
    last_row_t = None
    gaps = []
    diffs = []
    for t, name, k, v, q in oc.iter_samples(a.file, info, None, None, None, tp):
        if t_first is None or t < t_first:
            t_first = t
        if t_last is None or t > t_last:
            t_last = t
        if last_row_t is None or t != last_row_t:
            if last_row_t is not None and t > last_row_t:
                d = t - last_row_t
                if len(diffs) < 2000:
                    diffs.append(d)
                med = median(diffs[:200]) if diffs else None
                if med and d > 5 * med and len(gaps) < 20:
                    gaps.append({"from": oc.fmt_time(last_row_t), "to": oc.fmt_time(t), "seconds": round(d, 3)})
            last_row_t = t
        st = stats.setdefault(name, {"n": 0, "none": 0, "distinct": set(), "kinds": {}, "min": None, "max": None,
                                     "sum": 0.0, "nums": 0, "transitions": 0, "last": None, "bad_quality": 0})
        if q and q.lower() not in ("good", "192", "0", "ok", "g"):
            st["bad_quality"] += 1
        if k == "none":
            st["none"] += 1
            continue
        st["n"] += 1
        st["kinds"][k] = st["kinds"].get(k, 0) + 1
        if len(st["distinct"]) <= 20:
            st["distinct"].add(v)
        if k in ("num", "bool"):
            st["nums"] += 1
            st["sum"] += v
            st["min"] = v if st["min"] is None else min(st["min"], v)
            st["max"] = v if st["max"] is None else max(st["max"], v)
        if st["last"] is not None and v != st["last"]:
            st["transitions"] += 1
        st["last"] = v
    sigs = []
    for name, st in stats.items():
        kinds = [(k, v) for v in st["distinct"] for k in [("str" if isinstance(v, str) else "num")]]
        kind = classify(kinds) if kinds else "empty"
        row = {"tag": name, "type": kind, "samples": st["n"]}
        if st["none"]:
            row["missing"] = st["none"]
        if st["bad_quality"]:
            row["bad_quality"] = st["bad_quality"]
        if kind == "analog":
            row.update(min=round(st["min"], 4), max=round(st["max"], 4), mean=round(st["sum"] / max(st["nums"], 1), 4))
        elif kind in ("bool", "state"):
            row["changes"] = st["transitions"]
            row["values"] = sorted(str(x) for x in st["distinct"])[:10]
        sigs.append(row)
    med = median(diffs) if diffs else None
    out({"file": os.path.basename(a.file), "layout": info["layout"], "start": oc.fmt_time(t_first),
         "end": oc.fmt_time(t_last), "duration_s": round(t_last - t_first, 3) if t_first is not None else None,
         "sample_interval_s": round(med, 3) if med else None, "signals": len(sigs), "gaps": gaps,
         "quality_columns": [info["header"][i] for i in info.get("quality_cols", [])] or
                            ([info["header"][info["quality_col"]]] if info.get("quality_col") is not None else []),
         "unparsed_timestamps": tp.failures, "time_note": oc.zone_note(tp), "tags": sigs})


# ---------------------------------------------------------------- window

def time_range(a, tp):
    if getattr(a, "around", None):
        c = oc.parse_time_arg(a.around, tp)
        return c - oc.parse_duration(a.before or "10m"), c + oc.parse_duration(a.after or "2m"), c
    return oc.parse_time_arg(a.start, tp), oc.parse_time_arg(a.end, tp), None


def downsample(s, kind, t0, points):
    if kind in ("bool", "state"):
        res = []
        last = object()
        for t, k, v in s:
            if v != last:
                res.append([round(t - t0, 3), v])
                last = v
        if s and res[-1][0] != round(s[-1][0] - t0, 3):
            res.append([round(s[-1][0] - t0, 3), s[-1][2]])
        return res
    if len(s) <= points:
        return [[round(t - t0, 3), round(v, 4)] for t, k, v in s]
    span = (s[-1][0] - s[0][0]) or 1.0
    width = span / points
    buckets = {}
    for t, k, v in s:
        b = int((t - s[0][0]) / width)
        e = buckets.setdefault(min(b, points - 1), [0.0, 0, v, v, t])
        e[0] += v
        e[1] += 1
        e[2] = min(e[2], v)
        e[3] = max(e[3], v)
    return [[round(s[0][0] + (b + 0.5) * width - t0, 3), round(e[0] / e[1], 4), round(e[2], 4), round(e[3], 4)]
            for b, e in sorted(buckets.items())]


def cmd_window(a):
    tp = oc.TimeParser()
    start, end, center = time_range(a, tp)
    tags = [t.strip() for t in a.tags.split(",")] if a.tags else None
    info, tp, series = load_series(a.file, tags, start, end, tp)
    t0 = start if start is not None else min((s[0][0] for s in series.values() if s), default=0)
    res = {}
    for name, s in series.items():
        kind = series_kind(s)
        res[name] = {"type": kind, "points": downsample(s, kind, t0, a.points)}
        if kind == "analog":
            vals = [v for _, _, v in s]
            res[name].update(min=round(min(vals), 4), max=round(max(vals), 4))
    out({"file": os.path.basename(a.file), "t0": oc.fmt_time(t0), "start": oc.fmt_time(start), "end": oc.fmt_time(end),
         "center": oc.fmt_time(center), "format": "points are [seconds_from_t0, value] (analog buckets: [t, mean, min, max])",
         "time_note": oc.zone_note(tp), "series": res,
         "missing_tags": sorted(set(tags or []) - set(series)) if tags else []})


# ---------------------------------------------------------------- events

def parse_limits(lims):
    res = []
    for l in lims or []:
        for op in ("<=", ">=", "<", ">"):
            if op in l:
                tag, val = l.split(op, 1)
                res.append((tag.strip(), op, float(val)))
                break
        else:
            raise SystemExit("bad --limit %r (use TAG<value or TAG>value)" % l)
    return res


def violates(v, op, lim):
    return {"<": v < lim, "<=": v <= lim, ">": v > lim, ">=": v >= lim}[op]


def find_events(series, limits, start=None, end=None):
    evs = []
    lim_by_tag = {}
    for tag, op, val in limits:
        lim_by_tag.setdefault(tag.lower(), []).append((op, val))
    for name, s in series.items():
        kind = series_kind(s)
        prev = None
        state = {}
        for t, k, v in s:
            if start is not None and t < start or end is not None and t > end:
                prev = v
                continue
            if kind in ("bool", "state") and prev is not None and v != prev:
                evs.append({"t": t, "tag": name, "type": "transition", "from": prev, "to": v,
                            "edge": "rising" if kind == "bool" and v > prev else "falling" if kind == "bool" else None})
            if kind == "analog":
                for op, lim in lim_by_tag.get(name.lower(), []):
                    bad = violates(v, op, lim)
                    was = state.get((op, lim))
                    if was is not None and bad != was:
                        evs.append({"t": t, "tag": name, "type": "limit", "limit": "%s%s%g" % (name, op, lim),
                                    "entered" if bad else "cleared": True, "value": round(v, 4)})
                    state[(op, lim)] = bad
            prev = v
    evs.sort(key=lambda e: (e["t"], e["tag"]))
    return evs


def cmd_events(a):
    tp = oc.TimeParser()
    start, end = oc.parse_time_arg(a.start, tp), oc.parse_time_arg(a.end, tp)
    tags = [t.strip() for t in a.tags.split(",")] if a.tags else None
    limits = parse_limits(a.limit)
    info, tp, series = load_series(a.file, tags, start, end, tp)
    evs = find_events(series, limits)
    total = len(evs)
    for e in evs:
        e["t"] = oc.fmt_time(e["t"])
        for k in [k for k, v in e.items() if v is None]:
            del e[k]
    out({"file": os.path.basename(a.file), "events": evs[:a.max], "total": total,
         "truncated": total > a.max, "time_note": oc.zone_note(tp)})


# ---------------------------------------------------------------- trip

def find_trips(s, direction, level):
    kind = series_kind(s)
    trips = []
    prev = None
    for t, k, v in s:
        if prev is not None:
            if kind == "analog" and level is not None:
                if direction == "falling" and prev >= level > v or direction == "rising" and prev <= level < v:
                    trips.append(t)
            elif v != prev:
                fall = isinstance(v, float) and isinstance(prev, float) and v < prev
                if (direction == "falling") == fall:
                    trips.append(t)
        prev = v
    return trips, kind


def value_at(s, t):
    last = None
    for ts, k, v in s:
        if ts > t:
            break
        last = v
    return last


def analog_deviation(s, trip, lookback, baseline):
    base = [v for t, k, v in s if trip - lookback - baseline <= t < trip - lookback]
    win = [(t, v) for t, k, v in s if trip - lookback <= t < trip]  # the trip sample may already hold consequences
    if len(base) < 5 or len(win) < 3:
        return None
    med = median(base)
    mad = median([abs(x - med) for x in base]) or 0.0
    sigma = 1.4826 * mad
    rng = max(base) - min(base)
    thr = max(4.0 * sigma, 0.05 * abs(med), 0.5 * rng, 1e-6 if med else 0.01)
    at_trip = win[-1][1]
    out_now = abs(at_trip - med) > thr
    onset = None
    if out_now:
        inband = 0
        for t, v in reversed(win):
            if abs(v - med) <= thr:
                inband += 1
                if inband >= 3:
                    break
            else:
                inband = 0
                onset = (t, v)
    peak = max(win, key=lambda tv: abs(tv[1] - med))
    if not out_now and abs(peak[1] - med) <= thr:
        return None
    return {"baseline": round(med, 4), "band": round(thr, 4), "at_trip": round(at_trip, 4),
            "change_pct": round(100.0 * (at_trip - med) / med, 1) if med else None,
            "onset_t": onset[0] if onset else None, "onset_value": round(onset[1], 4) if onset else None,
            "sustained": out_now, "peak_t": peak[0], "peak_value": round(peak[1], 4)}


def cmd_trip(a):
    tp = oc.TimeParser()
    info = oc.sniff(a.file)
    _, tp, sig = load_series(a.file, [a.signal], None, None, tp)
    key = next((k for k in sig if k.lower() == a.signal.lower()), None)
    if key is None:
        raise SystemExit("signal %s not found; run summary to list tags" % a.signal)
    trips, kind = find_trips(sig[key], a.direction, a.level)
    lookback, baseline, post = oc.parse_duration(a.lookback), oc.parse_duration(a.baseline), oc.parse_duration(a.post)
    limits = parse_limits(a.limit)
    results = []
    interval = None
    times = [t for t, _, _ in sig[key][:500]]
    if len(times) > 2:
        interval = median([b - a_ for a_, b in zip(times, times[1:]) if b > a_])
    tol = max(interval or 0.0, 0.001)
    for trip in trips[:a.max_trips]:
        _, _, series = load_series(a.file, None, trip - lookback - baseline, trip + post, oc.TimeParser())
        before, at, after, unchanged = [], [], [], []
        for name, s in sorted(series.items()):
            if name == key:
                continue
            sk = series_kind(s)
            found = False
            if sk in ("bool", "state"):
                prev = value_at(s, trip - lookback)
                for t, k, v in s:
                    if t < trip - lookback:
                        continue
                    if prev is not None and v != prev:
                        e = {"tag": name, "kind": "transition", "dt_s": round(t - trip, 3), "t": oc.fmt_time(t),
                             "detail": "%s -> %s" % (fmtv(prev), fmtv(v))}
                        (at if abs(t - trip) < 0.5 * tol else before if t < trip else after).append(e)
                        found = True
                    prev = v
            elif sk == "analog":
                dev = analog_deviation(s, trip, lookback, baseline)
                if dev:
                    t_on = dev["onset_t"] if dev["onset_t"] is not None else dev["peak_t"]
                    e = {"tag": name, "kind": "deviation", "dt_s": round(t_on - trip, 3), "t": oc.fmt_time(t_on),
                         "detail": "baseline %g (normal band +/-%g); %s %g; %g just before trip (%s%% vs baseline)" % (
                             dev["baseline"], dev["band"],
                             "left the band at onset with" if dev["sustained"] else "transient excursion, peak",
                             dev["onset_value"] if dev["onset_value"] is not None else dev["peak_value"], dev["at_trip"],
                             dev["change_pct"] if dev["change_pct"] is not None else "n/a")}
                    (at if abs(t_on - trip) < 0.5 * tol else before).append(e)
                    found = True
                post_vals = [v for t, k, v in s if trip < t <= trip + post]
                pre = value_at(s, trip)
                if post_vals and pre is not None:
                    big = max(post_vals, key=lambda v: abs(v - pre))
                    span = (max(v for _, _, v in s) - min(v for _, _, v in s)) or 1.0
                    if abs(big - pre) > 0.2 * span:
                        after.append({"tag": name, "kind": "response", "dt_s": None,
                                      "detail": "moved from %g to %g within %gs after trip" % (round(pre, 3), round(big, 3), post)})
                        found = True
                for tag, op, lim in limits:
                    if tag.lower() != name.lower():
                        continue
                    was = None
                    first = None
                    recross = 0
                    for t, k, v in s:
                        if t < trip - lookback:
                            was = violates(v, op, lim)
                            continue
                        if t > trip:
                            break
                        bad = violates(v, op, lim)
                        if was is not None and bad and not was:
                            if first is None:
                                first = (t, v)
                            else:
                                recross += 1
                        was = bad
                    if first:
                        t, v = first
                        e = {"tag": name, "kind": "limit", "dt_s": round(t - trip, 3), "t": oc.fmt_time(t),
                             "detail": "crossed %s%s%g (value %g)%s" % (name, op, lim, round(v, 3),
                                                                     ", re-crossed %d more time(s) (noise around the limit)" % recross if recross else "")}
                        (at if abs(t - trip) < 0.5 * tol else before).append(e)
                        found = True
            if not found:
                unchanged.append(name)
        before.sort(key=lambda e: e["dt_s"])
        discrete_before = [e for e in before if e["kind"] in ("transition", "limit")]
        analog_before = [e for e in before if e["kind"] == "deviation"]
        alarms = []
        if a.alarms:
            for ev in oc.iter_events(a.alarms, start=trip - lookback, end=trip + post):
                alarms.append({"dt_s": round(ev["t"] - trip, 3), "t": oc.fmt_time(ev["t"]), "source": ev["source"],
                               "message": ev["message"], "priority": ev["priority"], "state": ev["state"]})
        results.append({
            "trip_time": oc.fmt_time(trip), "signal": key, "direction": a.direction,
            "nearest_discrete_precursor": discrete_before[-1] if discrete_before else None,
            "earliest_discrete_change": discrete_before[0] if discrete_before else None,
            "earliest_analog_deviation": analog_before[0] if analog_before else None,
            "simultaneous_with_trip": at,
            "before": before, "after": after[:30], "unchanged": unchanged, "alarms_in_window": alarms,
        })
    out({"file": os.path.basename(a.file), "signal": key, "signal_type": kind, "trips_found": len(trips),
         "sample_interval_s": round(interval, 3) if interval else None,
         "lookback_s": lookback, "baseline_s": baseline, "post_s": post, "trips": results,
         "notes": ["Changes within one sample of the trip are listed under simultaneous_with_trip; their order cannot "
                   "be resolved at this sample rate (the PLC scan is much faster than the historian).",
                   "Ordering in time is evidence, not proof of cause. Confirm against the logic (logic_trace.py) "
                   "and with the plant.",
                   oc.zone_note(tp)]})


def fmtv(v):
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("summary")
    p.add_argument("file")
    p.set_defaults(fn=cmd_summary)
    p = sub.add_parser("window")
    p.add_argument("file")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--around")
    p.add_argument("--before")
    p.add_argument("--after")
    p.add_argument("--tags")
    p.add_argument("--points", type=int, default=200)
    p.set_defaults(fn=cmd_window)
    p = sub.add_parser("events")
    p.add_argument("file")
    p.add_argument("--tags")
    p.add_argument("--limit", action="append")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--max", type=int, default=500)
    p.set_defaults(fn=cmd_events)
    p = sub.add_parser("trip")
    p.add_argument("file")
    p.add_argument("--signal", required=True)
    p.add_argument("--direction", choices=["falling", "rising"], default="falling")
    p.add_argument("--level", type=float, help="analog signals: trip when crossing this level")
    p.add_argument("--lookback", default="15m")
    p.add_argument("--baseline", default="30m")
    p.add_argument("--post", default="60s")
    p.add_argument("--limit", action="append", help="extra analog limit to test, e.g. PT_101<100")
    p.add_argument("--alarms", help="alarm/event log CSV to merge into each trip window")
    p.add_argument("--max-trips", type=int, default=20)
    p.set_defaults(fn=cmd_trip)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
