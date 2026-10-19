---
name: rungstone-hmi-scada
description: Understand, retarget, migrate, verify and clean up HMI and SCADA projects alongside the PLC code they talk to. Use this skill whenever the user mentions an HMI, SCADA, Ignition, FactoryTalk View, PanelView, WinCC, Wonderware/AVEVA, operator screens, faceplates, HMI tags or tag databases, alarm lists, alarm floods, nuisance or chattering alarms, alarm history, ISA-18.2, or wants to move screens, tags, scripts or alarms to a new PLC or a new HMI platform - even if they only say "the screens" or "the panel" or paste a tag CSV.
---

# Rungstone - HMI/SCADA

Rungstone works on **exported files** from HMI/SCADA projects (FactoryTalk View ME/SE, Ignition, WinCC) and
connects them to the PLC project analysed by the sibling skill `rungstone-plc`.

## Non-negotiable rules

1. **Read-only.** Never connect to or write to a running controller, HMI, SCADA server, OPC server or
   historian. Outputs are files the engineer imports into the vendor tool. Do not offer to "push",
   "deploy" or "download" anything.
2. **Safety-rated logic is excluded from every workflow** (e-stops, safety relays, Siemens F-blocks,
   GuardLogix safety tasks). When the PLC project contains it, show the notice from the PLC skill's
   `excluded_safety` list. HMI objects bound to safety-flagged tags are listed but never retargeted
   silently; an HMI **write** to a safety tag is always a high conflict.
3. **Never claim behavior was verified unless a simulation actually ran.** Label every result
   "simulated, not hardware-tested". If nothing ran, say "scenario plan only - not run".
4. **Label every meaning** as `confirmed`, `comment` (caption/description/PLC comment), `io_map`, `inferred`
   (say from what) or `unknown`. Never present a guess as fact. Position-based label association is inferred.
5. **The engineer approves every flagged change before export.** Retarget/migrate output is a draft; review
   items go into `.rungstone/review_queue.json`.
6. **Show scope and a cost estimate before any large job** (more than ~20 screens, ~2,000 tags, or alarm
   history above ~50 MB): count files/objects with `parse_hmi.py` first, give the estimate (use
   `rungstone-plc/scripts/estimate.py` for the PLC side) and wait for the go-ahead.
7. **Parse only.** Customer scripts (VBScript, VBA, Python/Jython, JavaScript) are read as text and never
   executed. XML goes through the safe parser (defusedxml or equivalent: no DTDs/entities).

## Scripts

All in `${CLAUDE_SKILL_DIR}/scripts/`, Python 3.9+, standard library (openpyxl optional for .xlsx). Each
prints compact JSON; add `--md file` for a Markdown report. Run them; do not paste raw exports into context.

| Script | Use for |
|---|---|
| `parse_hmi.py FILES_OR_DIRS -o inv.json` | Inventory: screens -> objects (type, label, bindings, actions), scripts (reads/writes/side effects), alarms, tags, aggregated PLC references |
| `verify_bindings.py --hmi ... --plc ... [--target T] [--simulate] --md v.md` | Every binding resolves? + full PLC/HMI/SCADA consistency check; optional scenarios through the simulator |
| `retarget.py --mapping mapping.csv --hmi ... --out DIR [--device OLD=NEW] --md r.md` | Rebind HMI files to the translated PLC; writes copies, never originals; review items to the queue |
| `migrate_tags.py --hmi ... --to ignition-json\|wincc-csv\|factorytalk-csv --out DIR [--mapping ...] [--screens]` | Tag DB + alarms to another platform (DRAFT_* files), script stubs, screen wireframes |
| `alarm_analysis.py history.csv --md a.md [--operators N]` | ISA-18.2 KPIs, chattering/fleeting/stale/flood/consequential, bad actors, priority suggestions |

PLC side: when the user supplies the PLC project, these scripts use the sibling skill at
`${CLAUDE_SKILL_DIR}/../rungstone-plc/scripts` (parse_project.py, dependency_map.py, simulate.py). If that
folder is missing, say the `rungstone-plc` skill is needed for PLC resolution and fall back to a
`.rungstone/tagmap.json` (`verify_bindings.py --tagmap`) or HMI-only analysis.

## Project state

Shared with the other Rungstone skills in `.rungstone/` in the project folder (plain files, git friendly):
`mapping.csv` (old -> new, produced by rungstone-plc Translate) drives Retarget and Migrate;
`tagmap.json` holds signal meanings with their basis; `review_queue.json` gets every rebind that needs
judgement; `verify_results.json` gets simulation runs; `decisions.log` is the audit trail.
`${CLAUDE_SKILL_DIR}/scripts/_shared/state.py` manages it (`state.py status`, `state.py queue list --open`,
`state.py queue approve R3`). If `.rungstone/` does not exist and the work produces review items, run
`state.py init` first.

## Which reference to read

| Situation | Read |
|---|---|
| FactoryTalk View / PanelView files | `references/factorytalk-view.md` |
| Ignition tags, views, scripts, alarm journal | `references/ignition.md` |
| WinCC (TIA panels, Unified, V7/V8) | `references/wincc.md` |
| Alarm cleanup, priorities, response text | `references/alarm-management.md` |
| Writing any deliverable | `references/output-formats.md` (exact formats) |

## Workflow 1 - Understand HMI/SCADA

1. `parse_hmi.py <files> -o inv.json`. Read the JSON, not the raw exports.
2. If the PLC project is available, run `verify_bindings.py --hmi ... --plc ...` so you know what each tag
   really does in logic (use the PLC skill's dependency map for "who else writes it").
3. Write the deliverable in the Understand format: summary, per-screen object table (what each button and
   indicator does, PLC tag, basis), scripts in plain English (triggers, reads, writes, side effects like
   database writes), alarm list, open questions. Momentary buttons: say what happens if the operator holds it
   or comms drop. Multistate captions are the operator's meaning of each value - compare them with the PLC.

## Workflow 2 - Retarget (rebind to the translated PLC)

1. Needs `mapping.csv` from rungstone-plc Translate (columns `old_address, old_symbol, ..., old_type,
   new_symbol, new_address, new_type, basis, notes`, or a simple `old,new`). If it does not exist, say so and
   offer to run Translate or build the mapping first.
2. `retarget.py --mapping .rungstone/mapping.csv --hmi <files> --out rebound/ --target <t> [--device OLD=NEW]`.
3. Report rebinds, everything unmapped (do not guess a target for those), manual edits (script strings that
   need quoting changes), and the review items added. Timer/counter members are rebound by meaning
   (e.g. `.ACC` -> a new DINT tag, because IEC `ET` is TIME; `.DN` -> `.Q`) and always reviewed.
4. Remind: import the rebound copies into a test copy of the HMI project; nothing is final until approved.

## Workflow 3 - Migrate to another platform

1. `migrate_tags.py --hmi <files> --to <target> --out migrated/ [--mapping ...] [--screens]`.
2. Tag database and alarms come out as `DRAFT_*` files; scripts as stubs with the original code and the
   references they touch - port them with the user, explaining each change; screens only as
   **draft layout descriptions** (Markdown + HTML wireframe). Say clearly that screens are not migrated.
3. Flag inferred data types, priority remaps, driver address syntax, and WinCC's Word-bit trigger rule.

## Workflow 4 - Verify bindings

1. `verify_bindings.py --hmi ... --plc ... --md v.md` -> every binding resolved / broken / member_invalid /
   not_plc, plus conflicts.
2. With a PLC program: `--simulate` builds button, indicator and alarm scenarios and runs them through
   `rungstone-plc/scripts/simulate.py`. Report each as `ran` or `not run` with the reason (broken binding,
   unsupported instruction, simulator missing). Label: **simulated, not hardware-tested**.

## Workflow 5 - Consistency check across PLC + HMI + SCADA

Run `verify_bindings.py` with every HMI and SCADA export plus the PLC project (and `--target` if a
migration is planned). Report label mismatches (HMI caption vs PLC comment vs I/O map - contradictions such
as high/low or open/closed are high severity), broken links, competing writers (HMI writes a tag the PLC
also drives), read-never-written tags, HMI writes to inputs/read-only/safety tags, and members that change
meaning after translation (e.g. Timer `.ACC` has no direct IEC equivalent - propose the HMI rebind to a new
DINT tag). Put each finding in the conflicts table with evidence, then open questions.

## Workflow 6 - Alarm cleanup from alarm history

1. `alarm_analysis.py history.csv --md alarms.md [--operators N]` (ask how many operator positions share
   the alarms; default 1).
2. Report KPIs against ISA-18.2 guidance, nuisance alarms (chattering, fleeting) with evidence, floods with
   their driver (one chattering alarm vs a cascade), consequential pairs, standing alarms, bad actors.
3. Suggest priorities from consequence and time to respond using the matrix in
   `references/alarm-management.md`, and draft operator response text. With the PLC project, cite the logic
   that raises each alarm (rung/line) as the cause. Everything is a **suggestion for the alarm philosophy
   owner to approve** - mark inferred causes as inferred. Safety-related alarms: list for the safety team,
   do not re-prioritize.

## Limits

- Display formats are read from exports; binary-only sources (FactoryTalk `.MER`, WinCC V7 `.PDL`) cannot be
  parsed - ask for an export or screenshots plus object lists.
- Generic XML (Vision windows, WinCC Openness screens) is scanned for references; object roles are inferred.
- Scripts are never executed; their behavior is described from reading them.
- No live monitoring; this version analyses exported files only.
