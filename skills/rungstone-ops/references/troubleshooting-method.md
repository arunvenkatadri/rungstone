# Troubleshooting method (offline, from exported data)

Read this before answering "why did it stop?". The goal is a short, honest answer:
the most likely triggering condition, the evidence for it, the logic that turns
that condition into a stop, what to check first, and what was not checked.

## Contents
1. Pick the event and the window
2. First-out logic: precursor, simultaneous, consequence
3. Cite the logic
4. Common stop causes and their signatures
5. Safety stops
6. Evidence labels
7. Traps

---

## 1. Pick the event and the window

- **The event** is the moment the thing of interest stopped: usually the PLC
  *command* (e.g. `P101_Run` 1 -> 0), not the feedback (`P101_Running`), because the
  command drops first and the feedback follows it by the starter/drive delay.
  If only feedback is logged, say so: the command could have stayed on (trip at the
  MCC, overload relay, contactor failure) and the PLC may not have stopped it at all.
- **Find it in the data**, do not trust a reported time: `historian.py trip` finds
  every 1 -> 0 of the signal. If the user said "around 2:30" and the data shows one
  trip at 02:29:44, use 02:29:44 and mention it.
- **Lookback**: start with 15 min (default). Slow process causes (tank draining,
  filter loading, temperature creep) need 30-60 min; fast electrical causes need
  only seconds. Widen the lookback when nothing discrete changed before the trip.
- **Baseline**: the period before the lookback (default 30 min) defines "normal" for
  each analog. If the process was not steady then (startup, grade change), choose
  another baseline with `--baseline` or say the baseline is unreliable.
- **Sample rate matters**: with 1-2 s historian samples you cannot order events that
  happen within one scan. Everything in `simultaneous_with_trip` is unordered. Event
  logs and SOE (sequence of events) records with ms timestamps order better than
  historian samples - prefer them for ordering when they exist.
- Historians often store on change with deadband (exception/compression). A flat line
  can mean "no change bigger than the deadband", not "no change".

## 2. First-out logic

`historian.py trip` sorts what it finds into three buckets:

| Bucket | Meaning | How to use it |
|---|---|---|
| `before` (and `nearest_discrete_precursor`) | Changed before the trip | Candidate causes. The nearest discrete change (a switch, a permissive bit, a limit crossing) is usually the *trigger*. The earliest analog deviation is usually the *leading indicator / upstream cause*. |
| `simultaneous_with_trip` | Changed in the same sample | Usually the internal trip bit (latch, permissive) - the PLC's own record of why. Cannot be ordered against the trip at this sample rate. |
| `after` | Changed after the trip | Consequences (flow to zero, amps to zero, pressure recovers). Never present these as causes. |
| `unchanged` | No change in the window | Ruled out *as far as the data shows* - list them, it is useful evidence ("overload, high discharge pressure and the safety relay did not change"). |

Build the causal chain from both ends and make sure they meet:
upstream cause (earliest analog deviation) -> trigger (nearest discrete precursor)
-> PLC trip bit (simultaneous) -> stop -> consequences. If the chain has a gap,
say where.

## 3. Cite the logic

Run `logic_trace.py --plc <project> --tag <stopped signal>`. It returns every rung
that writes the signal, what each condition requires (XIC = must be 1, XIO = must
be 0), and recursively what writes those conditions. Then:

1. Walk the trace with the data: for each condition on the run rung, did the data
   show it go to the wrong state at the trip? The first one that did is the path.
2. Quote the rungs as `Program/Routine rung N` with the rung text and comment. Keep
   the quote short: the run rung, the permissive rung, the trip/latch rung, the
   debounce/timer rung.
3. Note delays in the logic (debounce timers, `.PRE` values from the trace or the
   user) and check them against the data: PSL lost at 02:29:38, trip latched at
   02:29:44 = consistent with a 5 s debounce.
4. If the logic needs a signal that is not in the historian (internal bits, timer
   accumulators), say that the link is inferred from the logic, not observed.
5. Confirm the project version matches what was running (export date vs event
   date, online edits). If unknown, say so.

## 4. Common stop causes and their signatures

| Cause | Typical data signature | What to check first |
|---|---|---|
| Low suction pressure / loss of NPSH | Supply level or suction pressure sags for minutes, flow and amps drift down, PSL drops, debounce, trip; suction pressure jumps back when flow stops | Upstream supply (tank level, feed pump, valve lineup), strainer dP, PSL setpoint and transmitter agreement |
| Motor overload | Amps high (or rising) before the trip, overload input changes, often no process deviation | Load, mechanical binding, overload setting vs FLA, phase loss |
| High discharge pressure | Discharge pressure rises to PSH, downstream valve closes or blockage | Downstream valve positions, blocked line, PSH setpoint |
| Permissive loss (other equipment) | A run-permissive bit from another unit drops first | The other unit's own stop - repeat this method on it |
| Rungstone from a level/limit | A level/limit switch changes just before | Switch vs transmitter agreement, wiring, float stuck |
| Communications loss | Many signals freeze or go bad-quality at the same instant; I/O connection fault alarms; run drops without any process change | Network/switch logs, module status, RPI timeouts, cable/connectors |
| Operator stop | HMI stop bit (or local stop PB) changes just before | HMI audit trail, operator log |
| Power dip | Many unrelated motors stop together, controller may log a power-up | Utility/UPS logs |
| Safety function | Safety relay / safety status drops first | See section 5 |

## 5. Safety stops

If the first-out is an e-stop, safety relay, light curtain, guard switch, safety
PLC status, or any signal `logic_trace.py` marks as `safety`:

- State plainly: **"A safety function tripped (signal X at time T)."**
- Do not analyse, trace or explain the safety logic, and do not speculate about
  why the safety function acted or whether it should have. That belongs to the
  safety system owner with the safety documentation and validated tools.
- You may still report standard-logic facts around it (what the standard program
  did after the safety status dropped) and point to the safety system's own
  diagnostics and event log.
- Never suggest bypassing, jumpering, forcing or muting a safety device.

## 6. Evidence labels

Every cause statement carries exactly one label:

| Label | Use when |
|---|---|
| **confirmed** | The plant/engineer confirmed it in this conversation, or the system's own first-out/trip record names it unambiguously (and you say which record). |
| **evidence-supported** | The data shows the condition occurred before the stop and the logic shows it causes the stop. Most good answers end here. |
| **inferred** | Plausible from the logic or from typical behavior, but a link in the chain is not in the data (e.g. internal bit not historized, sample rate too coarse). |
| **unknown** | Not enough data. Say what data would settle it. |

## 7. Traps

- Correlation in time is not cause: the earliest deviation might be a coincidence.
  Check it is physically connected (tank -> suction pressure -> PSL -> trip).
- Alarm floods: the first alarm *displayed* is not always the first alarm *raised*;
  sort by timestamp, and prefer first-out/SOE records.
- Time zones and clock skew between historian, alarm server and PLC: line up a known
  event (the trip itself appears in both) and state the offset you applied.
- Interpolated historian values invent intermediate states: a bool interpolated to
  0.5, or a smooth ramp across a gap. Use raw/recorded values when you can.
- Missing data is not "normal": report gaps and bad-quality samples in the window.
- Do not stretch to a single cause when the data shows two independent ones.
