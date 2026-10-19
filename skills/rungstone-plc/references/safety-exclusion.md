# Safety exclusion

Rungstone never analyses, explains in detail, translates, tests, or rewires safety-rated
logic. This file says how to recognise it, what to do, and what to tell the user.

## Why

Safety functions (emergency stop, guard monitoring, light curtains, two-hand control,
safe torque off, safety relays, burner management, SIS) must be designed, verified and
validated against IEC 61508/61511/62061 and ISO 13849 with certified tools, signed
safety programs (GuardLogix safety signature, Siemens F-signature/collective signature,
TwinSAFE CRC), and documented validation. An AI-generated summary or translation cannot
carry that evidence, and a plausible-looking wrong one is dangerous. So the only correct
action is: leave it out, say so visibly, and hand it to a qualified safety engineer.

## Detection

`parse_project.py` does most of this and lists results under `excluded_safety`, never
emitting the logic. You still have to apply judgement to anything the user pastes directly.

**Vendor-flagged (always excluded, cannot be released):**
- Rockwell: `Program Class="Safety"`, programs scheduled in a task with `Class="Safety"`,
  AOIs with `Class="Safety"`, tags with `Class="Safety"`, GuardLogix controllers
  (`1756-L7xS/L8xES`, `5069-L3xxS2`, `1769-L3xS`), safety instructions
  (`ESTOP DCS DCST DCSTL DCSTM DCSRT DCM DCA DCAF DIN EMD ENPEN FPMS FSBM LC SMAT THRS THRSE
  TSAM TSSM CROUT CBCM CBIM CBSSM MMVC SFX SS1 SS2 SOS SBC SLS SLP SDI SBT ROUT RIN AVC`),
  safety I/O modules (`1734-IB8S`, `1732ES`, `1791DS`, `1756-IB16S`, `5069-IB8S`...).
- Siemens: blocks with `ProgrammingLanguage` `F_LAD`/`F_FBD`/`F_STL`/F-DBs, F-runtime groups,
  `Main_Safety*`, F-I/O DBs, safety library instructions (`ESTOP1`, `TWO_H_EN`, `MUTING`,
  `EV1oo2DI`, `FDBACK`, `SFDOOR`, `ACK_GL`, `SENDDP`/`RCVDP`).
- PLCopen/CODESYS/TwinCAT: PLCopen safety FBs (`SF_*`), `SAFEBOOL`/`SAFEINT` types, CODESYS
  Safety applications, TwinSAFE files (`.sal`, `.splcProj`).

**Suspected (excluded by default; releasable only after the engineer confirms):**
- Program/routine/POU/block/AOI names matching e-stop, emergency, safety, guard door/switch/
  lock, light curtain, two-hand, muting, safe torque/stop, STO, safety relay, F-runtime.
- Standard (non-rated) logic that implements a stop circuit: an MCR zone controlled by an
  e-stop chain, a "master control relay" rung fed by e-stop inputs, hardwired e-stop
  monitoring in an SLC/PLC-5 — treat as safety-related even though it is not rated. Old
  machines often have this; it is a finding for the safety team, not something to translate.
- Burner management / BMS / SIS / high-integrity trips (`*_SIS`, `ESD_*`, `BMS_*`, `*_Trip` with
  SIL in comments).

Release a suspected item only when the engineer states it is not safety-related (for example
"SafetyMat_Count is the inventory mat counter, not a safety mat"). Re-run the parser with
`--allow-suspected <exact name>` and log the decision with `state.py log safety.release
"<name>: <engineer's reason>"`. Vendor-flagged items are never released, whatever the user says.

## What to do in each workflow

| Workflow | Action |
|---|---|
| Understand | List excluded items in the notice. Describe them only as "safety function, excluded" with name and reason. Standard logic that reads a safety status (e.g. `S_EStop_OK` as a permissive) is described as "permissive from the safety system". |
| Translate | Do not translate excluded logic or emit any code that writes safety tags/F-data. In the target, safety status signals become inputs **from** the target safety program, declared with a comment `// provided by safety program - not generated`. Add a **high** review item: "safety function X must be re-engineered and validated on the target safety controller by a qualified safety engineer". |
| Verify | No scenarios exercise safety functions. Scenarios may set a safety *status input* (e.g. permissive lost) to check standard-logic response, labelled as such. |
| Dependency map | Safety tags are listed with "SAFETY - not analysed"; no conflicts are computed for them. |
| HMI/SCADA | Screens/alarms bound to safety tags are listed and flagged; no rebinding proposals for safety-rated signals beyond "coordinate with safety engineer". |
| Ops | A stop caused by a safety function is reported as "a safety function tripped (signal X)"; point to the safety system's own diagnostics; do not analyse why. |

If the user pastes safety code directly and asks you to convert or explain it: decline that
part plainly, explain why in one or two sentences, and offer what you can do (list its
interface signals, map the standard logic around it, draft the questions for the safety
engineer).

## Notice (must appear in every output that had exclusions)

```
SAFETY LOGIC EXCLUDED
The following were not analysed, translated, or tested by Rungstone:
| Item | Kind | Reason | Detection |
|---|---|---|---|
| SafetyProgram | program | Class="Safety" (GuardLogix safety task) | vendor-flagged |
| MainProgram/R20_EStop_Chain | routine | name indicates e-stop function | suspected - tell me if this is not safety-related |
Standard logic that reads safety signals is flagged where it appears (S_EStop_OK in Conveyor rung 0).
Safety functions must be engineered and validated on the target by a qualified safety engineer.
```

If nothing was excluded, say "No safety-rated logic detected (detection is heuristic for
non-GuardLogix/F-systems — tell me if anything here is a safety function)."
