# Output formats - rungstone-hmi-scada

Use these exact structures. Every deliverable starts with the **notice block** and ends with **open
questions**. Every meaning carries a basis: `confirmed` (engineer said so this session), `comment` (caption,
description, PLC comment), `io_map` (I/O list / module point comment), `inferred` (deduced - say from what),
`unknown`.

## Notice block (top of every deliverable)

```
> **Rungstone - HMI/SCADA <workflow>** | Files: <list> | PLC project: <file or "not supplied">
> Read-only analysis of exported files. Nothing was connected to or written to a live HMI, SCADA or controller.
> Safety: <"No safety-rated logic detected" | "Excluded: <items> (reason) - not analysed">
> <Verify only:> Results are **simulated, not hardware-tested** | <or> Scenario plan only - **not run**.
```

## 1. Understand HMI/SCADA

```
## Summary            3-6 sentences: what the screens let the operator see and do.
## Screens
### <screen name> (<platform>, <file>)
| Object | Type | What it does | PLC tag | Basis |
|---|---|---|---|---|
| pbResetCount | momentary push button | Resets the batch counter while held (writes 1, 0 on release) | HMI_Reset_Count | comment |
| miTankLevel | multistate indicator | Shows "TANK LOW" when Tank_Full = 1 - CONTRADICTS PLC comment "high level" | Tank_Full | comment (conflict) |
## Scripts
| Script | Trigger | What it does (plain English) | Reads | Writes | Side effects |
## Alarms
| Alarm | Source tag | Condition | Priority | Message | Basis |
## Open questions for the plant
1. <question> - why it matters - where it comes from (object/script/alarm)
```

## 2. Retarget (output of retarget.py, explained)

```
## Retarget summary   N rebound, N unmapped, N manual, N review items; rebound copies in <out>/ (originals untouched)
## Rebinds            | Where | Old | New | Rule | Review id/risk |
## Could not be mapped  | Where | Reference | Reason | Proposed action |
## Needs manual edit  | Where | Old | Proposed | Why |
## Review queue items added   R<n> ... (status open until the engineer approves)
```

## 3. Migrate

```
## Migration summary  target, tag count, alarm count, script stubs, screen drafts; DRAFT files in <out>/
## Tag database       | Old tag | New tag | Type (old -> new, basis) | PLC reference (new) | Notes |
## Alarms             | Alarm | Trigger | Condition/limit | Priority old -> new | Message | Notes |
## Scripts            | Script | Status: stub only | Port notes |
## Screens            "Draft layout descriptions only - not importable." + link to DRAFT_screen_*.md/html
## Review items       (rebinds needing judgement, type guesses, priority remaps)
```

## 4. Verify bindings

```
## Binding check      N bindings: resolved N, broken N, member_invalid N, not_plc N
| Where | Reference | Status | Why |      (only non-resolved rows)
## Scenarios          heading must include "(simulated, not hardware-tested)" or "(plan only - not run)"
| Scenario | Stimulus | Expected (from PLC logic) | Observed | Verdict ran / not run | Reason |
```

Never write "verified", "passes" or "works" for a scenario that did not run. "ran" means the simulator
executed it; it is not a hardware test.

## 5. Consistency check (PLC + HMI + SCADA)

```
## Conflicts (most severe first)
| Severity | Type | Tag | Detail | Evidence |
types: label_mismatch, broken_link, competing_writers, read_never_written, hmi_writes_input,
       hmi_writes_readonly, hmi_writes_safety, translation_watch/translation_break, member_invalid
## Open questions
```

A label mismatch is always reported with both texts and both sources, e.g.
`HMI says 'Tank LOW level' (Fill_Overview:miTankLevel caption) but io module comment says 'Tank high level switch LSH-301' (high/low)`.

## 6. Alarm cleanup (from alarm history)

```
## Period and load    dates, activations, distinct alarms, operator positions assumed
## KPIs vs ISA-18.2   | KPI | Value | Guidance |  (rate, floods, peak, top-10 share, chattering, stale, priority mix)
## Nuisance alarms    chattering / fleeting list, each with evidence (episodes, counts)
## Bad actors         top 10 with share
## Floods             | Start | End | Activations | First alarm | Driver |
## Consequential pairs | Leader | Follower | Times | Share |
## Standing / stale
## Suggestions        | Alarm | Current priority | Suggested change | Rationale | Basis |   ("suggestion - for the alarm philosophy owner to approve")
## Response text drafts  (template from alarm-management.md, causes cited to PLC logic when the project is supplied)
```
