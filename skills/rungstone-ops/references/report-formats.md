# Report formats (rungstone-ops)

Use these templates exactly. Keep reports short; put detail in tables.
Every report ends with the "Not checked" list and the offline notice.

---

## A. Troubleshoot report ("why did X stop?")

```markdown
# Why P-101 stopped - 2025-06-12 02:29:44

**Likely cause (evidence-supported):** low suction pressure. PSL-101 opened at 02:29:38;
after the 5 s debounce the low-suction trip latched and removed the run permissive at 02:29:44.
**Upstream (inferred):** supply tank T-100 level fell from 65 % to 27 % over the 9 minutes before
the trip; suction pressure followed it down.

## Timeline
| Time | dt to trip | Signal / alarm | What happened | Source |
|---|---|---|---|---|
| 02:20:48 | -8 m 56 s | LT_100 | Tank level starts falling from its 65 % baseline | historian |
| 02:21:04 | -8 m 40 s | PT_101 | Suction pressure leaves its 180 kPa baseline | historian |
| 02:28:08 | -1 m 36 s | PT_101 alarm | Suction pressure low warning (<100 kPa) | alarm log |
| 02:29:38 | -6 s | PSL_101 | Low suction switch opens (1 -> 0) | historian |
| 02:29:44 | 0 | P101_LowSuct_Trip / P101_Run | Trip latched, run command drops (same sample) | historian |
| 02:29:46 | +2 s | P101_Running, FT_101 | Feedback drops, flow to zero (consequence) | historian |

## Logic that caused the stop
| Location | Logic | Why it matters |
|---|---|---|
| Pumps/P101_Control rung 4 | `[XIC(HMI_Start_PB) ,XIC(P101_Run) ]XIO(HMI_Stop_PB)XIC(P101_Permissive)OTE(P101_Run);` | Run needs `P101_Permissive` = 1 |
| Pumps/P101_Control rung 0 | `...XIO(P101_LowSuct_Trip)OTE(P101_Permissive);` | Permissive drops when the low-suction trip bit turns on |
| Pumps/P101_Control rungs 1-2 | `XIC(P101_Run)XIO(PSL_101)TON(PSL_Debounce,...)` then `XIC(PSL_Debounce.DN)OTL(P101_LowSuct_Trip)` | 5 s of lost PSL-101 while running latches the trip |

## Ruled out (no change in the window)
P101_OL (overload), PSH_102 (high discharge pressure), SR_EStop_OK (safety relay status), HMI_Stop_PB.

## Check first
1. Why T-100 level fell: upstream supply/feed, inlet valve, level control - the root of the chain.
2. PSL-101 setpoint vs PT-101 (switch opened at ~80 kPa; confirm against the instrument datasheet).
3. Whether a T-100 low-level permissive should stop P-101 before suction is lost (process decision).

## Not checked
- Internal timer accumulator and permissive bits that are not historized (link inferred from the logic).
- Whether the exported PLC project matches what was running at 02:29 (export dated 2025-06-12 08:15).
- Upstream equipment that fills T-100 (no data supplied).
- Live state: this is an offline analysis of exported data; live monitoring is not part of this version.
```

Rules:
- Exactly one label on each cause line: **confirmed / evidence-supported / inferred / unknown**.
- Timeline `Source` column: historian, alarm log, event log, PLC logic (inferred), user.
- Consequences go in the timeline after 0 and are never offered as causes.
- If the first-out is a safety signal, the "Likely cause" line reads: *"A safety function tripped
  (SIGNAL at TIME). Investigate with the safety system and its documentation; this tool does not
  analyse safety logic."* and the logic table stops at the standard-logic boundary.

## B. Ask-the-plant answer

```markdown
**Answer:** one or two sentences, with numbers and a label (evidence-supported / inferred / unknown).

![chart title](path to chart.png or .svg)
One line saying what the chart shows and what the numbered markers are.

**Logic that explains it:** `Program/Routine rung N` - rung text - one sentence.
**Based on:** files, time range, tags. **Not checked:** list.
```

## C. Shift / downtime report

```markdown
# Line 1 downtime - 2025-06-02 06:00 to 2025-06-09 06:00 (168 h)

| Metric | Value |
|---|---|
| Stops (unplanned / planned) | 24 (20 / 4) |
| Downtime (unplanned) | 8.1 h (5.6 h) |
| Availability | 95.2 % |
| MTBF / MTTR (unplanned) | 8.0 h / 16.8 min |
| Stops with no cause event | 2 |
| Safety stops | 2 |

## Top causes (unplanned)
| # | Cause | Stops | Downtime | Basis |
|---|---|---|---|---|
| 1 | P-101 tripped: low suction pressure | 6 | 137 min | evidence-supported (first-out alarm) |

## Recurring issues
- **P-101 low suction - 6 of 6 on night shift C.** What that suggests (inferred) and what to check.

## By shift
| Shift | Stops | Unplanned downtime | Top cause |
|---|---|---|---|

## Notes
- Cause = earliest alarm within 30 s before each stop (first-out); stops with no event are "unknown".
- Safety stops are counted, not analysed: "a safety function tripped" - see the safety system.
- Definitions: MTBF = run time / unplanned stops; MTTR = mean unplanned stop duration.
- Offline analysis of exported data; live monitoring is not part of this version.
```

Rules:
- Totals come from `downtime.py`; do not recompute by hand.
- Say which shift definition was used.
- "Recurring" claims need counts; a pattern (e.g. night shift) is **inferred** until the plant confirms why.
