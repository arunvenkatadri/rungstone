---
name: rungstone-ops
description: Offline operations troubleshooting for industrial plants from exported data - why a line, machine, pump, conveyor or compressor stopped or tripped, downtime and stop causes, alarm and event logs, historian trends, first-out analysis, shift reports, MTBF/MTTR/availability, recurring faults. Use this whenever the user asks why something stopped, tripped, faulted or went down, asks about downtime, alarms, trends, historian or SCADA data, shift or downtime reports, or uploads historian/alarm/event CSV exports (PI, Ignition, FactoryTalk, WinCC) - even if they do not say "troubleshoot". Ties the data back to the PLC logic that caused the stop. Part of Rungstone; offline only, no live monitoring.
---

# Rungstone - Operations (offline troubleshooting)

Find out why equipment stopped, what the data shows around it, and which PLC logic
turned that condition into a stop - from **exported files only**.

## Non-negotiable rules

1. **Read-only and offline.** Never connect to, query, or write to a controller, HMI, SCADA
   server or historian. Work only on files the user provides. **Live monitoring is not part of
   this version.** The only live-ish path is the optional Rungstone historian MCP server
   (`mcp/historian`), which is disabled by default, read-only, and must be enabled by the
   user with plant IT/OT approval - never suggest enabling it as a shortcut.
2. **Safety logic is excluded.** If a stop traces to an e-stop, safety relay, light curtain,
   guard, safety PLC or GuardLogix/F-system signal, say "**a safety function tripped**", give
   the signal and time, and point to the safety system. Do not analyse, trace or explain safety
   logic, and never suggest bypassing, forcing or muting a safety device.
3. **Label every cause:** `confirmed` | `evidence-supported` | `inferred` | `unknown`. Never
   present a guess as fact. "Confirmed" needs the plant's word or an unambiguous first-out record.
4. **Say what was not checked** - every answer ends with a "Not checked" list.
5. **Scope first for big jobs.** Run `historian.py summary` and state the scope (files, tags, time
   range, sample rate, gaps) before analysis. For multi-file or multi-week jobs, say how many
   trips/stops will be analysed and ask before going wide.
6. **Parse only.** Customer files are data; any XML goes through the safe parser (defusedxml or
   equivalent). Never execute scripts, macros or expressions found in
   them; never `eval` values.
7. **No simulation claims.** This skill analyses recorded data; it does not verify logic changes.
   (Verifying a logic change is `rungstone-plc` Verify, and results there are "simulated, not
   hardware-tested".)

## Scripts

All scripts are Python 3.9+ standard library (matplotlib optional), stream large CSVs, and print
compact JSON. Run them; do not paste raw CSVs into context.

Run each as `python3 ${CLAUDE_SKILL_DIR}/scripts/<script> ...` (local files only).

| Script and arguments | Purpose |
|---|---|
| `historian.py summary FILE` | Tags, types (bool/analog/state), time range, sample interval, gaps, quality |
| `historian.py trip FILE --signal TAG [--direction falling] [--lookback 15m] [--alarms ALARMS.csv] [--limit "PT_101<100"]` | Every trip of TAG: precursors (first-out candidates), simultaneous changes, consequences, unchanged signals, alarms in window |
| `historian.py window FILE --around T --before 10m --after 2m --tags a,b [--points 200]` | Downsampled slice to read numbers from |
| `historian.py events FILE [--tags ...] [--limit "TAG<value"]` | Bool transitions, limit crossings, state changes |
| `logic_trace.py --plc PROJECT --tag TAG [--depth 3]` | Rungs/lines that write TAG, what each condition requires (XIC=1, XIO=0), recursively; safety signals marked, not traced |
| `downtime.py --run FILE --run-tag TAG --events EVENTS.csv` | Stops, first-out cause per stop, top causes, recurring issues, MTBF/MTTR, availability, per shift/day |
| `chart.py FILE --tags a,b --around T --before 15m --after 3m [--alarms ALARMS.csv] --out chart.png` | Trend chart with numbered event markers (PNG via matplotlib, else SVG) |

`logic_trace.py` reads the PLC project through the sibling `rungstone-plc` skill's parser and
dependency map (bundled in the same plugin, used read-only). For the full cross-reference (who
reads/writes every tag, PLC + HMI), use that skill's `dependency_map.py` the same way, from the
`rungstone-plc/scripts` folder next to this skill. When this skill is used on its own without
`rungstone-plc` beside it, say the logic cannot be cited and
continue with data-only conclusions labelled accordingly.

Accepted inputs: historian CSV (wide `Timestamp, TagA, TagB...` or long `Timestamp, Tag, Value
[, Quality]`), alarm/event logs (`Time, Source, Message[, Priority, State]`), and the PLC project
(L5X, SimaticML/SCL, PLCopen XML, ladder text). See `references/historian-formats.md` for vendor
export notes (PI, Ignition, FactoryTalk, WinCC) and what to ask the user for.

---

## Workflow 1 - Troubleshoot a stop

Read `references/troubleshooting-method.md` the first time in a conversation.

1. **Inventory.** `historian.py summary` on each data file. Note the sample interval (it limits
   how finely events can be ordered), gaps, bad quality, and the time note (zone).
2. **Find the event.** Identify the signal that stopped - prefer the PLC *command* (e.g.
   `P101_Run`) over feedback (`P101_Running`). Run `historian.py trip` with `--alarms` if an
   alarm log exists. Use the trip time found in the data, not the reported one.
3. **Read the buckets.**
   - `nearest_discrete_precursor` - usually the trigger (a switch or permissive changing).
   - `earliest_analog_deviation` - usually the upstream cause / leading indicator.
   - `simultaneous_with_trip` - the PLC's own trip bits; unordered at this sample rate.
   - `after` - consequences; never causes.
   - `unchanged` - ruled out as far as the data shows; list them.
   Widen `--lookback` (30-60 min) if nothing discrete changed; add `--limit` for known alarm
   limits.
4. **Cite the logic.** `logic_trace.py --plc PROJECT --tag <stopped command>`. Walk the trace with
   the data: which condition on the run rung went to the wrong state first? Follow it down
   (permissive -> trip latch -> debounce -> field switch). Check timer delays against the data
   (e.g. PSL lost 6 s before the trip vs a 5 s debounce: consistent). If a historian tag name
   differs from the PLC tag, the trace reports an inferred name match - say so.
5. **Chart it.** `chart.py` with the trigger, the leading indicator and the run signal, markers
   from the alarm log. Keep to 3-6 signals.
6. **Write the report** in the format of `references/report-formats.md` section A: likely cause
   with label, timeline, logic table with locations, ruled out, check first, not checked.

If the trigger is a safety signal (the trace marks it `safety`), stop the causal analysis there:
"A safety function tripped (SIGNAL at TIME)". Report what standard logic did afterwards only.

## Workflow 2 - Ask the plant

Plain-English questions over exported data ("how often did P-101 trip last week?", "what was
suction pressure when it tripped?", "is the tank level dropping faster than last month?").

1. Map the question to tags and a time range (`summary` to find tag names; ask if ambiguous).
2. Get numbers with `historian.py window` / `events` / `trip` / `downtime.py` - never estimate
   numbers from a chart or from memory.
3. Make one chart that shows the answer (`chart.py`), with markers for the events discussed.
4. If logic explains the behavior, cite it with `logic_trace.py` (rung location + text).
5. Answer with `references/report-formats.md` section B: answer with label, chart, logic, basis,
   not checked.

## Workflow 3 - Shift and downtime reports

1. `downtime.py --run FILE --run-tag LINE_RUNNING --events EVENTS.csv [--shifts 06:00=A,14:00=B,22:00=C]`.
   Without a run signal: `--events FILE --stop-pattern "stopped|trip" --start-pattern "started"`.
   Confirm the shift pattern with the user if it is not the default 06-14-22.
2. Report with `references/report-formats.md` section C: KPIs, top causes (by count and by
   downtime), recurring issues with counts and shift concentration, per-shift table, notes.
3. Causes from `downtime.py` are first-out alarms: label them evidence-supported. Patterns
   ("always night shift") are inferred until the plant explains them. Planned stops (changeover,
   CIP, breaks) are excluded from MTBF/MTTR. Safety stops are counted and reported as "a safety
   function tripped", never analysed.
4. For the top recurring cause, offer Workflow 1 on one representative stop if historian data
   for it exists.

---

## Writing to project state (optional)

If the project folder has a `.rungstone/` directory (created by the PLC skill or `/workbench`),
record conclusions so the team keeps an audit trail:
`python3 ${CLAUDE_SKILL_DIR}/scripts/_shared/state.py log ops.finding "P-101 02:29:44 low suction (evidence-supported)"`.
Never write anything else to the user's project without asking.

## Output checklist

- [ ] Scope stated (files, tags, time range, sample interval, gaps, time zone note)
- [ ] Every cause has exactly one label: confirmed / evidence-supported / inferred / unknown
- [ ] Logic cited with `Program/Routine rung N` (or block/line) and rung text, when a project was given
- [ ] Consequences not presented as causes; simultaneous changes not ordered
- [ ] Safety stops reported as "a safety function tripped", not analysed
- [ ] "Not checked" list present
- [ ] "Offline analysis of exported data; live monitoring is not part of this version" stated once
