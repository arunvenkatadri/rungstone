"""
alarm_analysis.py - alarm history CSV -> ISA-18.2 / EEMUA 191 KPIs, nuisance
alarm detection and cleanup suggestions.

Columns are auto-detected (override with --time-col etc.):
  time      EventTime, Timestamp, Time, Date/Time, ActiveTime ...
  alarm     AlarmName, Alarm, Name, Source, DisplayPath, Tag ...
  state     State, Event, EventType, Transition, Status  (optional; without it
            every row is treated as an activation)
  priority  Priority, Severity, Class                     (optional)
  message   Message, Description, Text, Alarm Text, Label (optional)
  area      Area, Group, Unit                             (optional)
State words: ACTIVE/ALM/IN/ON/RAISED/SET/CAME IN/0 -> activation,
             CLEAR/RTN/NORMAL/OUT/OFF/RESET/WENT OUT/1 -> return to normal,
             ACK/ACKNOWLEDGED/2 -> acknowledge.

Detects (thresholds configurable):
  chattering        >= 3 activations of the same alarm within 60 s
  fleeting          active for less than 2 s before clearing
  stale / standing  active for more than 24 h (or still active > 24 h at end of data)
  flood             > 10 activations in a 10-minute window (ends below 5 / 10 min)
  consequential     alarm B activates within 2 s after alarm A in >= 80 % of
                    A's activations (>= 3 times) -> suppression / first-out candidate
  bad actors        top 10 alarms by share of all activations
  priority mix      vs ~80 % low / 15 % medium / 5 % high guidance

Every suggestion is labelled as a suggestion for the alarm philosophy owner
to approve; nothing changes the alarm system.

Usage:
  python3 alarm_analysis.py HISTORY.csv [--md report.md] [-o out.json]
         [--chatter-count 3 --chatter-window 60] [--fleeting 2] [--stale-hours 24]
         [--flood 10 --flood-window 600] [--operators 1]
"""

import argparse
import bisect
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hmi_common as hc  # noqa: E402

TIME_COLS = ("eventtime", "event time", "timestamp", "time stamp", "time", "date/time", "datetime", "activetime",
             "active time", "date time", "eventtimestamp", "event_time")
ALARM_COLS = ("alarmname", "alarm name", "alarm", "name", "source", "displaypath", "display path", "alarm id",
              "tag", "tagname", "tag name", "tagpath", "condition")
STATE_COLS = ("state", "event", "eventtype", "event type", "transition", "status", "alarmstate", "alarm state")
PRIO_COLS = ("priority", "severity", "class", "alarm class", "prio")
MSG_COLS = ("message", "description", "text", "alarm text", "label", "msg", "alarm message")
AREA_COLS = ("area", "group", "unit", "plant area", "alarm group")
TAG_COLS = ("tagpath", "tag path", "tag", "tagname", "tag name", "source")

ACT = re.compile(r"(?i)^(active|act|alm|alarm|in|on|raised|set|came in|active.*unack.*|activated|0|true|high|trip)$")
CLR = re.compile(r"(?i)^(clear|cleared|clr|rtn|return.*|normal|norm|out|off|reset|went out|inactive|1|false|"
                 r"active.*ack.*clear.*|cleared.*)$")
ACK = re.compile(r"(?i)^(ack|acked|acknowledged|acknowledge|2)$")

TIME_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%m/%d/%Y %H:%M:%S.%f", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M:%S.%f %p",
                "%d.%m.%Y %H:%M:%S.%f", "%d.%m.%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S")


class TimeParser(object):
    def __init__(self):
        self.fmt = None

    def __call__(self, s):
        s = s.strip()
        if not s:
            return None
        if re.fullmatch(r"\d{9,13}(\.\d+)?", s):
            v = float(s)
            return v / 1000.0 if v > 1e11 else v
        s2 = re.sub(r"(Z|[+-]\d\d:?\d\d)$", "", s)
        if self.fmt:
            try:
                return _epoch(dt.datetime.strptime(s2, self.fmt))
            except ValueError:
                pass
        for f in TIME_FORMATS:
            try:
                d = dt.datetime.strptime(s2, f)
                self.fmt = f
                return _epoch(d)
            except ValueError:
                continue
        return None


def _epoch(d):
    return (d - dt.datetime(1970, 1, 1)).total_seconds()


def fmt_t(t):
    return dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")


def pick(header, names, override=None):
    if override:
        o = override.strip().lower()
        return header.index(o) if o in header else None
    for n in names:
        if n in header:
            return header.index(n)
    return None


def prio_bucket(p):
    if p is None:
        return "unspecified"
    s = str(p).strip().lower()
    if re.fullmatch(r"\d+", s):
        v = int(s)
        # Ignition 0..4 (Diagnostic..Critical); FactoryTalk 1..1000 (urgent >= 751)
        if v <= 4:
            return ["diagnostic", "low", "medium", "high", "critical"][v]
        return "critical" if v >= 751 else "high" if v >= 501 else "medium" if v >= 251 else "low"
    for k, words in (("critical", ("critical", "urgent", "emergency", "1 - urgent")),
                     ("high", ("high", "errors", "error", "alarm", "major")),
                     ("medium", ("medium", "med", "warning", "warnings", "minor")),
                     ("low", ("low", "info", "information", "notice", "advisory")),
                     ("diagnostic", ("diagnostic", "diag", "system"))):
        if any(s == w or s.startswith(w) for w in words):
            return k
    return "unspecified"


def analyse(path, a):
    rows = hc.iter_csv(path)
    header = None
    skipped = 0
    for row in rows:
        low = [c.strip().lower() for c in row]
        if pick(low, TIME_COLS, a.time_col) is not None and pick(low, ALARM_COLS, a.alarm_col) is not None:
            header = low
            break
        skipped += 1
        if skipped > 30:
            break
    if header is None:
        raise SystemExit("could not find a header with a time column and an alarm column; pass --time-col/--alarm-col")
    ci_t, ci_a = pick(header, TIME_COLS, a.time_col), pick(header, ALARM_COLS, a.alarm_col)
    ci_s, ci_p = pick(header, STATE_COLS, a.state_col), pick(header, PRIO_COLS, a.priority_col)
    ci_m, ci_ar = pick(header, MSG_COLS), pick(header, AREA_COLS)
    ci_tag = pick([h if i != ci_a else "" for i, h in enumerate(header)], TAG_COLS)
    tp = TimeParser()
    names, idx = [], {}
    meta = {}
    events = []  # (t, alarm_index, kind) kind: 'A' activate 'C' clear 'K' ack
    bad_time = unknown_state = 0
    for row in rows:
        if len(row) <= max(ci_t, ci_a):
            continue
        t = tp(row[ci_t])
        if t is None:
            bad_time += 1
            continue
        name = row[ci_a].strip()
        if not name:
            continue
        if name not in idx:
            idx[name] = len(names)
            names.append(name)
            meta[name] = {}
        i = idx[name]
        m = meta[name]
        for key, ci in (("priority", ci_p), ("message", ci_m), ("area", ci_ar), ("tag", ci_tag)):
            if ci is not None and ci < len(row) and row[ci].strip() and key not in m:
                m[key] = row[ci].strip()
        if ci_s is None:
            kind = "A"
        else:
            st = row[ci_s].strip() if ci_s < len(row) else ""
            kind = "A" if ACT.match(st) else "C" if CLR.match(st) else "K" if ACK.match(st) else None
            if kind is None:
                unknown_state += 1
                continue
        events.append((t, i, kind))
    events.sort()
    if not events:
        raise SystemExit("no usable events")
    t0, t1 = events[0][0], events[-1][0]
    span_h = max((t1 - t0) / 3600.0, 1e-9)
    acts = [(t, i) for t, i, k in events if k == "A"]
    per = {}
    for t, i in acts:
        per.setdefault(i, []).append(t)

    # durations / stale / fleeting
    active_since = {}
    durations = {}
    ack_delay = {}
    for t, i, k in events:
        if k == "A":
            if i not in active_since:
                active_since[i] = t
        elif k == "C":
            if i in active_since:
                durations.setdefault(i, []).append(t - active_since.pop(i))
        elif k == "K" and i in active_since:
            ack_delay.setdefault(i, []).append(t - active_since[i])
    still_active = dict((i, t1 - ts) for i, ts in active_since.items())

    # chattering
    chatter = {}
    for i, ts in per.items():
        episodes, j, in_ep, count_in = 0, 0, False, 0
        flagged = set()
        for k in range(len(ts)):
            while ts[k] - ts[j] > a.chatter_window:
                j += 1
            if k - j + 1 >= a.chatter_count:
                if not in_ep:
                    episodes += 1
                    in_ep = True
                flagged.update(range(j, k + 1))
            elif in_ep and k - j + 1 < 2:
                in_ep = False
        if episodes:
            chatter[i] = {"episodes": episodes, "activations_in_chatter": len(flagged),
                          "share_of_own": round(len(flagged) / float(len(ts)), 3)}

    # floods (sliding window over all activations)
    times = [t for t, _ in acts]
    floods = []
    j = 0
    k = 0
    in_flood = None
    for k in range(len(times)):
        while times[k] - times[j] > a.flood_window:
            j += 1
        n = k - j + 1
        if n > a.flood and in_flood is None:
            in_flood = {"start": times[j], "peak": n, "first_idx": j}
        elif in_flood is not None:
            in_flood["peak"] = max(in_flood["peak"], n)
            if n < a.flood / 2.0:
                in_flood["end"] = times[k]
                in_flood["last_idx"] = k
                floods.append(in_flood)
                in_flood = None
    if in_flood is not None:
        in_flood["end"] = times[-1]
        in_flood["last_idx"] = len(times) - 1
        floods.append(in_flood)
    flood_out = []
    for f in floods:
        contrib = {}
        for t, i in acts[f["first_idx"]:f["last_idx"] + 1]:
            contrib[names[i]] = contrib.get(names[i], 0) + 1
        first = names[acts[f["first_idx"]][1]]
        flood_out.append({"start": fmt_t(f["start"]), "end": fmt_t(f["end"]),
                          "minutes": round((f["end"] - f["start"]) / 60.0, 1),
                          "activations": f["last_idx"] - f["first_idx"] + 1, "peak_per_window": f["peak"],
                          "first_alarm": first,
                          "top_contributors": sorted(contrib.items(), key=lambda kv: -kv[1])[:5]})
    chat_names = set(names[i] for i in chatter)
    for f in flood_out:
        top = f["top_contributors"][0] if f["top_contributors"] else None
        if top and top[0] in chat_names and top[1] > 0.5 * f["activations"]:
            f["driver"] = "chattering alarm %s (%d of %d) - fixing the chatter removes this flood" % (top[0], top[1], f["activations"])
        else:
            f["driver"] = "multi-alarm cascade starting with %s - review first-out / state-based suppression" % f["first_alarm"]
    flood_time = sum(f["end"] - f["start"] for f in floods)

    # 10-minute buckets
    buckets = {}
    for t in times:
        b = int((t - t0) // 600)
        buckets[b] = buckets.get(b, 0) + 1
    n_buckets = int((t1 - t0) // 600) + 1
    over = sum(1 for v in buckets.values() if v > a.flood)

    # consequential pairs
    cons = []
    by_alarm_times = dict((i, ts) for i, ts in per.items())
    for ia, ta in by_alarm_times.items():
        if len(ta) < 3:
            continue
        follow = {}
        for t in ta:
            lo = bisect.bisect_right(times, t)
            hi = bisect.bisect_right(times, t + a.consequential_window)
            seen = set()
            for tt, ib in acts[lo:hi]:
                if ib != ia and ib not in seen:
                    seen.add(ib)
                    follow[ib] = follow.get(ib, 0) + 1
        for ib, n in follow.items():
            if n >= 3 and n / float(len(ta)) >= 0.8 and ib not in chatter:
                cons.append({"leader": names[ia], "follower": names[ib], "times": n, "leader_activations": len(ta),
                             "share": round(n / float(len(ta)), 2),
                             "follower_share_explained": round(n / float(len(by_alarm_times.get(ib, [1]))), 2)})
    cons.sort(key=lambda c: -c["times"])

    # per alarm table
    total = len(acts)
    alarms = []
    for i, nm in enumerate(names):
        ts = per.get(i, [])
        d = durations.get(i, [])
        fleeting = sum(1 for x in d if x < a.fleeting)
        stale = [x for x in d if x > a.stale_hours * 3600]
        rec = {"alarm": nm, "activations": len(ts), "share": round(len(ts) / float(total), 4) if total else 0,
               "per_day": round(len(ts) / (span_h / 24.0), 1), "priority": meta[nm].get("priority"),
               "priority_bucket": prio_bucket(meta[nm].get("priority")), "message": meta[nm].get("message"),
               "area": meta[nm].get("area"), "tag": meta[nm].get("tag")}
        if d:
            srt = sorted(d)
            rec["median_active_s"] = round(srt[len(srt) // 2], 1)
        if fleeting:
            rec["fleeting"] = fleeting
        if stale:
            rec["stale_episodes"] = len(stale)
        if i in still_active and still_active[i] > a.stale_hours * 3600:
            rec["standing_at_end_h"] = round(still_active[i] / 3600.0, 1)
        if i in chatter:
            rec["chattering"] = chatter[i]
        ak = ack_delay.get(i)
        if ak:
            rec["median_ack_s"] = round(sorted(ak)[len(ak) // 2], 1)
        rec["suggestions"] = suggest(rec, a, cons)
        alarms.append(rec)
    alarms.sort(key=lambda r: -r["activations"])
    bad = [{"alarm": r["alarm"], "activations": r["activations"], "share": r["share"]} for r in alarms[:10]]

    pr = {}
    for t, i in acts:
        b = prio_bucket(meta[names[i]].get("priority"))
        pr[b] = pr.get(b, 0) + 1
    prio_mix = dict((k, round(v / float(total), 3)) for k, v in sorted(pr.items()))
    per_op_hour = total / span_h / max(a.operators, 1)
    peak10 = max(buckets.values()) if buckets else 0
    kpis = {
        "period": {"start": fmt_t(t0), "end": fmt_t(t1), "hours": round(span_h, 1)},
        "activations": total, "distinct_alarms": len(names), "operators": a.operators,
        "avg_per_operator_per_hour": round(per_op_hour, 2),
        "avg_per_operator_per_10min": round(per_op_hour / 6.0, 2),
        "isa_18_2_rate_target": "~6 per hour acceptable (~1 per 10 min); up to ~12 per hour maximum manageable",
        "rate_assessment": "acceptable" if per_op_hour <= 6 else "maximum manageable" if per_op_hour <= 12 else "likely overdemanding",
        "peak_10min": peak10,
        "pct_10min_windows_over_flood_threshold": round(100.0 * over / max(n_buckets, 1), 2),
        "isa_18_2_flood_target": "< ~1 % of 10-minute periods in flood",
        "pct_time_in_flood": round(100.0 * flood_time / max(t1 - t0, 1e-9), 2),
        "top10_share": round(sum(b["share"] for b in bad), 3),
        "isa_18_2_top10_target": "top 10 alarms < 1-5 % of total load",
        "chattering_alarms": len(chatter),
        "stale_or_standing_alarms": sum(1 for r in alarms if r.get("stale_episodes") or r.get("standing_at_end_h")),
        "priority_mix": prio_mix,
        "priority_guidance": "approximately 80 % low, 15 % medium, 5 % high; critical/urgent under 1 %",
    }
    nuisance = [r["alarm"] for r in alarms if r.get("chattering") or (r.get("fleeting", 0) >= max(3, 0.5 * r["activations"]))]
    return {"tool": "rungstone/alarm_analysis", "file": path,
            "columns": dict((k, header[v] if v is not None else None) for k, v in (
                ("time", ci_t), ("alarm", ci_a), ("state", ci_s), ("priority", ci_p), ("message", ci_m), ("area", ci_ar))),
            "kpis": kpis, "bad_actors": bad, "nuisance_alarms": nuisance,
            "chattering": [dict(alarm=names[i], **v) for i, v in sorted(chatter.items(), key=lambda kv: -kv[1]["episodes"])],
            "floods": flood_out, "consequential": cons[:20],
            "standing": [{"alarm": r["alarm"], "standing_at_end_h": r.get("standing_at_end_h"), "stale_episodes": r.get("stale_episodes")}
                         for r in alarms if r.get("stale_episodes") or r.get("standing_at_end_h")],
            "fleeting": [{"alarm": r["alarm"], "fleeting": r["fleeting"], "activations": r["activations"]}
                         for r in alarms if r.get("fleeting")],
            "alarms": alarms,
            "data_quality": {"rows_bad_time": bad_time, "rows_unknown_state": unknown_state,
                             "state_column": header[ci_s] if ci_s is not None else None},
            "note": "Suggestions are for the alarm philosophy owner / rationalization team to approve. Nothing was changed."}


def suggest(r, a, cons):
    out = []
    if r.get("chattering"):
        out.append("Chattering (%d episode(s), %d%% of its activations): add an on-delay (e.g. 2-5 s) and/or deadband "
                   "or hysteresis; check the instrument and setpoint. Nuisance alarm." % (
                       r["chattering"]["episodes"], round(100 * r["chattering"]["share_of_own"])))
    if r.get("fleeting") and r["fleeting"] >= max(3, 0.3 * r["activations"]):
        out.append("Fleeting (%d activations shorter than %g s): add an on-delay; if it needs no operator action, "
                   "remove the alarm or make it an event." % (r["fleeting"], a.fleeting))
    if r.get("standing_at_end_h") or r.get("stale_episodes"):
        out.append("Standing/stale (active > %g h): fix the cause or the instrument, or remove it from service under MOC; "
                   "shelving is a temporary measure only." % a.stale_hours)
    for c in cons:
        if c["follower"] == r["alarm"]:
            out.append("Consequential to %s (follows within %g s in %d%% of cases): candidate for state-based suppression "
                       "or a first-out group." % (c["leader"], a.consequential_window, round(100 * c["share"])))
    pb = r["priority_bucket"]
    if pb in ("high", "critical") and r["per_day"] > 5:
        out.append("Priority suggestion: %s-priority alarm occurring %.1f times/day is unlikely to warrant that priority; "
                   "re-rate from consequence severity and time to respond (likely medium or low)." % (pb, r["per_day"]))
    if pb == "unspecified":
        out.append("No priority recorded: assign one from the priority matrix (consequence x time to respond).")
    if not r.get("message"):
        out.append("No message text: write cause / consequence / operator action (response text template).")
    return out


def to_md(res):
    k = res["kpis"]
    L = ["# Alarm history analysis", "",
         "Period %s to %s (%.1f h), %d activations of %d distinct alarms, %d operator position(s)." % (
             k["period"]["start"], k["period"]["end"], k["period"]["hours"], k["activations"], k["distinct_alarms"], k["operators"]),
         "", "## KPIs vs ISA-18.2 guidance", "", "| KPI | Value | Guidance |", "|---|---|---|",
         "| Avg alarms / operator / hour | %s (%s) | %s |" % (k["avg_per_operator_per_hour"], k["rate_assessment"], k["isa_18_2_rate_target"]),
         "| Peak alarms in 10 min | %s | flood above 10 |" % k["peak_10min"],
         "| %% of 10-min periods in flood | %s %% | %s |" % (k["pct_10min_windows_over_flood_threshold"], k["isa_18_2_flood_target"]),
         "| %% of time in flood | %s %% | |" % k["pct_time_in_flood"],
         "| Top-10 share of load | %s %% | %s |" % (round(100 * k["top10_share"], 1), k["isa_18_2_top10_target"]),
         "| Chattering alarms | %s | 0 |" % k["chattering_alarms"],
         "| Stale / standing alarms | %s | 0 (fewer than 5 on any day) |" % k["stale_or_standing_alarms"],
         "| Priority mix | %s | %s |" % (", ".join("%s %d%%" % (p, round(100 * v)) for p, v in k["priority_mix"].items()), k["priority_guidance"]),
         "", "## Nuisance alarms", ""]
    L += ["- **%s**" % n for n in res["nuisance_alarms"]] or ["None detected."]
    L += ["", "## Bad actors (top 10)", "", "| # | Alarm | Activations | Share |", "|---|---|---|---|"]
    for n, b in enumerate(res["bad_actors"], 1):
        L.append("| %d | %s | %d | %.1f %% |" % (n, b["alarm"], b["activations"], 100 * b["share"]))
    if res["floods"]:
        L += ["", "## Floods", "", "| Start | End | Minutes | Activations | First alarm | Top contributors | Driver |", "|---|---|---|---|---|---|---|"]
        for f in res["floods"]:
            L.append("| %s | %s | %s | %d | %s | %s | %s |" % (f["start"], f["end"], f["minutes"], f["activations"], f["first_alarm"],
                                                             ", ".join("%s (%d)" % c for c in f["top_contributors"]), f["driver"]))
    if res["consequential"]:
        L += ["", "## Consequential / duplicate candidates", "", "| Leader | Follower | Times | Share of leader |", "|---|---|---|---|"]
        for c in res["consequential"]:
            L.append("| %s | %s | %d | %d %% |" % (c["leader"], c["follower"], c["times"], round(100 * c["share"])))
    if res["standing"]:
        L += ["", "## Standing / stale", ""] + ["- %s: %s" % (s["alarm"], ("still active %s h at end of data" % s["standing_at_end_h"])
                                                          if s.get("standing_at_end_h") else "%d stale episode(s)" % s["stale_episodes"])
                                               for s in res["standing"]]
    L += ["", "## Suggestions (for the alarm philosophy owner to approve)", "", "| Alarm | Priority | Suggestions |", "|---|---|---|"]
    for r in res["alarms"]:
        if r["suggestions"]:
            L.append("| %s | %s | %s |" % (r["alarm"], r.get("priority") or "-", "<br>".join(s.replace("|", "/") for s in r["suggestions"])))
    dq = res["data_quality"]
    L += ["", "_Data quality: %d rows with unparseable time, %d rows with unknown state. Analysis of exported history only; "
              "no alarm configuration was changed._" % (dq["rows_bad_time"], dq["rows_unknown_state"])]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("history")
    ap.add_argument("--time-col")
    ap.add_argument("--alarm-col")
    ap.add_argument("--state-col")
    ap.add_argument("--priority-col")
    ap.add_argument("--chatter-count", type=int, default=3)
    ap.add_argument("--chatter-window", type=float, default=60.0)
    ap.add_argument("--fleeting", type=float, default=2.0)
    ap.add_argument("--stale-hours", type=float, default=24.0)
    ap.add_argument("--flood", type=int, default=10)
    ap.add_argument("--flood-window", type=float, default=600.0)
    ap.add_argument("--consequential-window", type=float, default=2.0)
    ap.add_argument("--operators", type=int, default=1)
    ap.add_argument("--md")
    ap.add_argument("-o", "--output")
    ap.add_argument("--full", action="store_true", help="include every alarm in JSON (default: only alarms with findings)")
    a = ap.parse_args(argv)
    res = analyse(a.history, a)
    if not a.full:
        res["alarms"] = [r for r in res["alarms"] if r["suggestions"]] + []
    s = json.dumps(res, indent=2, ensure_ascii=False)
    if a.output:
        open(a.output, "w", encoding="utf-8").write(s)
    else:
        sys.stdout.write(s + "\n")
    if a.md:
        open(a.md, "w", encoding="utf-8").write(to_md(res))
    k = res["kpis"]
    sys.stderr.write("alarm_analysis: %d activations, %.2f/h/operator, %d chattering, %d floods, %d consequential, nuisance: %s\n" % (
        k["activations"], k["avg_per_operator_per_hour"], k["chattering_alarms"], len(res["floods"]), len(res["consequential"]),
        ", ".join(res["nuisance_alarms"]) or "none"))


if __name__ == "__main__":
    main()
