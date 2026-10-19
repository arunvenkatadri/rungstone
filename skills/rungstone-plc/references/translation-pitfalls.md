# Translation pitfalls

Where cross-vendor translation silently changes machine behavior. Read before every
Translate and Verify job. Each pitfall gives: what differs, how to detect it in the source,
what to do in the target, and the default review-queue risk. Raise the risk if the signal
drives motion, heat, pressure, chemical dosing, or anything near people.

Risk scale used in the review queue:
- **high** — can change what the machine physically does, when it does it, or how it fails
  (timing, scaling, fault/power-up behavior, interlocks, motion, PID). Blocks export until decided.
- **medium** — semantics differ but the difference shows up in testing or only in edge cases
  (rounding, counter limits, HMI rebinds, naming of status bits).
- **low** — cosmetic or documentation (naming, comments, structure).

## Contents
1. Scan order and execution model
2. Immediate I/O
3. PID algorithm forms
4. Motion
5. Analog raw ranges and scaling
6. Timers (ACC vs ET, time bases, reset behavior)
7. One-shots and edges
8. Counters
9. Data types, overflow, rounding
10. Retentive memory and power cycle
11. First-scan bits and startup
12. Fault behavior
13. Protocols (Modbus, DH+, EtherNet/IP, Profinet)
14. Program-control constructs (MCR, JMP, indirect addressing, subroutines)
15. Strings, arrays, bit order

---

## 1. Scan order and execution model — high

- **Rockwell Logix:** I/O updates **asynchronously** at the module RPI; an input can change
  between two rungs of one scan. Tasks preempt by priority; continuous task runs in the gaps.
- **SLC/PLC-5, S7, CODESYS/TwinCAT:** synchronous process image — inputs frozen for the whole
  scan, outputs written at the end (per task/OB in S7 with process image partitions).
- Source logic that reads the same input in several rungs may rely on (or suffer from)
  asynchronous updates; translated to a process image, it becomes consistent. Usually safer,
  but note it. The reverse (S7 -> Logix) can break logic that assumes a frozen input: buffer
  inputs at the top of the routine (`MOV`/`COP` to a local image) and flag it.
- **Rung order matters:** OTE on the same bit in two rungs -> last one wins (dependency map
  flags these). Seal-in and "last-write-wins" patterns must keep their relative order.
- **Periodic tasks / cyclic OBs:** keep the rate and priority. PID and integrators must stay in
  a fixed-rate task (OB3x on S7-1500), not OB1.
- Detection: `tasks` in parse output; rungs writing the same tag; inputs read in many rungs.

## 2. Immediate I/O — high

- SLC `IIN`/`IOT`, PLC-5 `IIN`/`IOT`, Logix `IOT` (immediate output), S7 peripheral access
  `%IW64:P` / `%QW64:P`, CODESYS/TwinCAT have no general equivalent (task-level I/O).
- Translate to the target's peripheral access when it exists; otherwise move the logic to a
  faster task and say the immediacy is lost. Always a review item.

## 3. PID algorithm forms — high (never auto-tune, never claim equivalence)

| Source | Form | Notes |
|---|---|---|
| SLC/MicroLogix `PID` | ISA-ish dependent, Kc/Ti(min/rep)/Td(min); integer PV/CV 0–16383 (or scaled) | control block PD or N file (23 words) |
| Logix `PID` (ladder) | independent **or** dependent (`.PE`), Kp/Ki (1/s or 1/min per `.MO`)/Kd | `.SP .PV .CV .KP .KI .KD .MAXO .MINO .DB .SWM .MO` |
| Logix `PIDE` (FBD) | velocity form, PCMD/OCMD/... modes, autotune | lots of mode logic |
| S7-1500 `PID_Compact` | proportional-integral-derivative with own tuning, Gain/Ti(s)/Td(s), pretuning/fine tuning | call in a cyclic interrupt OB |
| S7-300 `FB41 CONT_C` | GAIN, TI, TD (TIME) | |
| IEC/CODESYS/TwinCAT | library-specific (`PID` in Util, `FB_BasicPID`, `Tc2_ControllerToolbox`) | |

Do: carry over setpoints, limits, deadband, manual/auto handshake, output clamping, and
anti-windup intent; convert gain units with the formula shown; put the loop in a fixed-rate
task; queue a **high** item: "loop requires re-tuning/verification at commissioning".
The simulator reports PID as "not run". Never present translated gains as equivalent.

## 4. Motion — high (stub, do not translate behavior)

Logix motion (`MSO MSF MAJ MAM MAS MAH MAG MCD MAPC MCCM...` with AXIS/MOTION_INSTRUCTION
tags), Kinetix/CIP Motion, Siemens Technology Objects (`MC_Power`, `MC_Home`,
`MC_MoveAbsolute`, `MC_MoveJog`...), PLCopen Motion (`MC_*` in CODESYS/TwinCAT `Tc2_MC2`).
Concepts map roughly (PLCopen MC is the common ground) but axis configuration, units,
homing, error handling and handshakes are drive- and configuration-specific. Generate the
call skeleton with TODOs and a high review item per axis. Simulator: "not run".

## 5. Analog raw ranges and scaling — high, always ask

Typical raw counts (always confirm the actual module catalog number and configured range):

| Platform / module | 0–10 V | 4–20 mA | Notes |
|---|---|---|---|
| SLC 1746-NI4 | 0..16383 | 3277..16384 | NO4I out: 0..32764 = 0–21 mA |
| MicroLogix embedded / 1762-IF4 | 0..4095 or 0..32767 (config) | 0..4095 / 6241..31206 (raw/prop) | data format setting |
| ControlLogix 1756-IF8 etc. | **engineering units configured in the module** | | the tag may already be in EU — check module config |
| CompactLogix 1769-IF4 | raw/proportional 0..32767 or EU | | |
| S7-1500 / S7-1200 | 0..27648 | 0..27648 (live zero inside module) | overrange to 32511; 32767 = overflow / wire break |
| Beckhoff EL30xx/EL31xx | 0..32767 | 0..32767 | 0x7FFF overrange |
| Generic 12-bit | 0..4095 | 819..4095 | |

Detection: `SCP`, `SCL` (SLC), `CPT` with constants, `NORM_X`/`SCALE_X`, divisions by
16383/4095/27648/32767, any analog input word (`I:3.0`, `Local:3:I.Ch0Data`, `%IW..`).
Do: translate the *engineering* intent (0–100 %, 0–10 bar) using the target raw range,
keep the source raw range in the mapping table notes, add underrange/wire-break handling
where the source had it (or note it was missing), and queue a **high** item listing: source
module/range (or "unknown"), target module/range (inferred), the formula before/after, and
the request to confirm. Integer scaled values (e.g. 0..1000 = 0.1 %) — keep the same scaled
integer if HMI/SCADA reads it, or flag the HMI rebind.

## 6. Timers — high when HMI/SCADA or other logic reads members, else medium

| | SLC/PLC-5 | Logix | IEC / S7 / CODESYS |
|---|---|---|---|
| Preset/accum | `.PRE/.ACC` INT in **time-base** units (1.0 s, 0.01 s; MicroLogix 0.001) | `.PRE/.ACC` DINT ms | `PT`/`ET` TIME (ms) |
| Status | `/EN /TT /DN` | `.EN .TT .DN` | `Q` only (`IN` is an input) |
| TON reset | rung false -> ACC=0, DN=0 | same | `IN` false -> ET=0, Q=0 |
| ACC at done | stops at PRE | stops at PRE | ET stops at PT |
| Retentive | `RTO` + `RES` | `RTO` + `RES` | `TONR` (S7-1500, TwinCAT) with `R`; not in base IEC |
| Not scanned | timer freezes (rung not evaluated) | freezes; JSR not called -> freezes | instance not called -> ET not updated; Q keeps last value |
| Preset change while timing | takes effect immediately | same | PT read every call; usually same |

Pitfalls:
- **HMI/SCADA reads `.ACC` (elapsed time display) or `.TT`:** no direct IEC equivalent. ET is a
  TIME; HMI numeric objects usually need a DINT. Create an explicit `xxx_Elapsed_ms : DInt :=
  TIME_TO_DINT(tmr.ET)` (and `xxx_Timing := tmr.IN AND NOT tmr.Q` for `.TT`) and queue an HMI
  rebind. `dependency_map.py --target` flags these.
- **HMI writes `.PRE`:** create a DINT setpoint tag (retentive), convert with `DINT_TO_TIME`,
  clamp to sane limits; rebind HMI.
- **SLC time base:** `TON T4:0 0.01 500 0` = 5.00 s; `1.0 100` = 100 s. Wrong base = 100x error.
- **Timer used for its `.DN` in other rungs before the TON rung executes** (scan-order effect)
  — IEC instance call position must preserve it.
- **Timer inside a conditional subroutine/IF:** IEC instance not called keeps ET — call
  unconditionally with `IN := condition`.
- TOF, RTO and pulse semantics: Logix TOF `DN` is true while rung true and during delay; IEC
  TOF `Q` same. RTO -> `TONR` or `ET` accumulator logic; reset by `RES` -> `R` input.

## 7. One-shots and edges — high if they gate counts/latches

- `ONS(storage)`: rung-out true for one scan on false->true; the storage bit must be unique.
  `OSR(storage, output)` / `OSF` (falling) write an output bit and pass the rung condition.
- Logix sets ONS storage on **prescan** (so no pulse on the first scan after run); SLC similar.
  IEC `R_TRIG` memory initializes FALSE -> a TRUE input on the first scan **does** produce a
  pulse. Flag where a first-scan pulse would count, latch, or start something.
- Shared storage bits (same storage used by two ONS) = existing bug: report, don't copy blindly.
- `R_TRIG` inside IF/ELSE misses edges when not called: call every scan.
- Siemens LAD `P`/`N` contacts need a unique edge memory bit (often `%M` — retentivity matters).

## 8. Counters — medium (high if count drives a recipe/batch)

- SLC: `.ACC` INT -32768..32767; overflow sets `/OV` and **wraps**; `CTU` and `CTD` can share
  one counter address (up/down) -> IEC `CTUD`.
- Logix: DINT; counts on rung false->true; continues past PRE (`DN` stays true); `OV/UN` on wrap.
- IEC `CTU`: counts while `CV < PVmax`, stops at the type limit (no wrap), `Q := CV >= PV`;
  S7 `CTU` CV is Int — use `CTU_DINT` for Logix DINT counts. `.CU/.CD` memory bits do not exist.
- `RES` -> `R` input; preset loads -> `LD` on CTD/CTUD.

## 9. Data types, overflow, rounding — medium to high

- Width: SLC N = 16-bit; Logix DINT default; S7 Int is 16-bit — translate SLC N7 to Int only
  if HMI/comms need 16-bit, otherwise DInt and say so.
- **Overflow:** SLC sets `S:5/0` minor fault and the overflow trap; if the trap bit is not cleared
  by end of scan the processor **major faults**. Logix sets `S:V` and continues (wraps). S7
  integer arithmetic wraps silently (ENO false in LAD/FBD). Source logic that clears `S:5/0`
  is handling overflow on purpose — translate the intent.
- **Integer division:** SLC `DIV` with an integer destination **rounds** (the unrounded quotient
  goes to `S:13/S:14`). Logix `DIV` with integer operands truncates, but rounds when a REAL
  operand feeds an integer destination. IEC/SCL `/` on integers **truncates**. SLC `7/2` -> 4 vs IEC 3.
  This is how `simulate.py` models it. Confirm against the controller's instruction reference
  for the firmware in use, and put every integer DIV in the review queue (medium).
- **REAL -> INT:** Logix MOV rounds to nearest; IEC `REAL_TO_INT` rounds (implementation
  dependent at .5), `TRUNC` truncates. State which you used.
- Signed/unsigned and BCD (`TOD`/`FRD`) conversions — flag.
- Floating compare `EQU` on REAL — note fragility.

## 10. Retentive memory and power cycle — high

| Platform | Default on power cycle |
|---|---|
| Logix | **All tag values retained**; prescan clears OTE outputs, resets TON/ONS states; OTL bits stay set |
| SLC/PLC-5 | Data table retained (battery/capacitor); OTE outputs non-retentive; OTL retained |
| S7-1500 | **Non-retentive by default**; only members marked Retain (DB/FB/tag table %M range) keep values |
| CODESYS/TwinCAT | `VAR` reinitialized; `VAR RETAIN`/`PERSISTENT` keep values (hardware/runtime dependent) |

Every latched state, sequence step, counter, totalizer, setpoint, recipe value and mode
selector in a Rockwell source needs an explicit retain decision on S7/IEC targets, and a
"what should happen after power loss" question to the plant when not obvious (restart in
the middle of a sequence is a classic hazard). Default proposal: retain setpoints/recipes/
totalizers/counters; **do not** retain run commands and sequence steps unless the source
logic depended on it — and put both in the review queue (high).

## 11. First-scan bits and startup — medium

- SLC `S:1/15` first pass, Logix `S:FS`, S7 `OB100` (or the `FirstScan` flag of OB1 start info on
  some CPUs), CODESYS/TwinCAT: none standard — use a `bFirstScan` pattern (`VAR bInit : BOOL :=
  TRUE; END_VAR` cleared at end of first cycle) or TwinCAT `_TaskInfo[].FirstCycle`.
- Logix power-up handler program, SLC fault/power-up routines -> OB100 + explicit init code.

## 12. Fault behavior — high

- Array index out of range: Logix major fault (controller stops unless a fault routine clears
  it); SLC major fault; S7-1500 error OB/`GET_ERR_ID` handling (CPU reaction depends on model
  and firmware); CODESYS/TwinCAT runtime exception (stop) unless checked (`CheckBounds`).
  Generated code must bounds-check every computed index and say so.
- Module/connection failure: Logix inputs hold last state or go to zero per module config and a
  connection fault is visible via `GSV`/module status; S7 OB82/OB86 + value status; outputs on
  fault/program mode go off/hold/substitute value per module config. State the source module
  config if known, propose explicit diagnostics handling, and queue an item.
- Watchdog/cycle overrun: Logix task watchdog major fault; S7 OB80 then STOP if exceeded twice.
- User fault routines (SLC `S:29`, Logix MajorFaultProgram) translate to error OBs / explicit
  handlers — never silently drop them.

## 13. Protocols — high (anything that crosses a network)

- **Modbus:** 1-based register numbers in docs (`40001`) vs 0-based offsets on the wire; 32-bit
  values span two registers with vendor-specific word order (swap often needed); coils vs
  discrete inputs vs holding vs input registers. S7-1500: `MB_CLIENT`/`MB_SERVER`; Logix: AOIs
  or MSG over a gateway/module; CODESYS/TwinCAT: libraries. Map register-by-register in the
  mapping table, keep word order explicit.
- **DH+ / DH-485 / RIO:** no equivalent on S7 or IEC targets; requires a gateway or network
  redesign. List every `MSG` with its path and data as a review item; never pretend it works.
- **EtherNet/IP:** produced/consumed tags (RPI-based, Logix-to-Logix) and `MSG` (CIP data
  table read/write, PCCC for SLC). S7/IEC targets need an EtherNet/IP adapter/scanner (module,
  firmware feature, or gateway) — confirm availability; never assume.
- **Profinet:** S7 native (I-Device for controller-to-controller). Logix targets need a gateway
  or a supported Profinet module — confirm.
- **OPC UA:** S7-1500 and TwinCAT servers expose symbols; Logix needs an external server.
  HMI/SCADA connections change — run the dependency map with `--target`.
- Comms status bits (`MSG.DN/.ER`, `TCON` STATUS, connection faults) gate logic — translate
  the intent (stale-data handling), not the bit names.

## 14. Program-control constructs — medium to high

- **MCR zones:** outputs (OTE) inside a false MCR zone are forced off, but OTL/OTU and timers
  behave differently (TON resets). No IEC equivalent — expand to explicit conditions on every
  output in the zone. High if the MCR was part of a stop circuit (then it is safety-related:
  see safety-exclusion.md).
- **JMP/LBL:** skipped rungs are not evaluated (timers freeze, OTEs hold). Translate to IF
  blocks and keep the hold semantics explicitly.
- **TND / AFI / NOP:** temporary end and always-false — usually debugging leftovers; ask.
- **Indirect/indexed addressing:** SLC `N7:[N7:0]`, `#N7:0` file instructions, Logix arrays with
  computed index, S7 pointers/ANY/VARIANT, IEC pointers — translate to arrays with bounds checks;
  the dependency map cannot see these accesses (say so).
- **Subroutines with parameters:** SLC `JSR` input/return parameters, Logix `SBR/RET` params —
  translate to FB/FC interfaces.
- **File/sequencer instructions** (`FFL/FFU/LFL/LFU/BSL/BSR/SQO/SQI/SQL/FAL/FSC/COP/FLL`):
  re-implement with arrays and loops; the CONTROL structure (`.POS .LEN .DN .EN .ER .UL`) has no
  equivalent. Medium unless they drive sequencing (then high). Simulator: "not run".

## 15. Strings, arrays, bit order

- Logix STRING = 82 chars + `.LEN` (DINT); S7 `String[n]` max 254 with header bytes;
  IEC `STRING(n)`. ASCII/`MSG` string handling differs everywhere.
- Arrays: Logix 0-based only; S7 `Array[lo..hi]`; IEC any bounds. Keep 0-based for traceability.
- Bit-of-word access: Logix `DINT.5`, SLC `B3:0/5`, S7 `"Word".%X5` (slice access) on
  optimized data — and S7 is big-endian for `%M`/absolute overlays (see siemens-scl.md § 5).
- Packed BOOL arrays used for HMI/comms (bit 0 = first alarm) — keep packing explicit.
