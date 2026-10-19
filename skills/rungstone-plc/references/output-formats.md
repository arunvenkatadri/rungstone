# Output formats (rungstone-plc)

Use these exactly. Engineers compare outputs across jobs and paste them into tickets;
consistent headings and columns matter more than prose.

## Common blocks

**Scope & estimate** (before any job that `estimate.py` marks CONFIRM, or > ~20 routines):
paste the estimator table, then one line: "Proceed with all of it, or narrow the scope
(e.g. one program / routine list)?" Wait for the answer.

**Safety notice** — the block from `safety-exclusion.md`, at the top of every output,
whenever anything was excluded. Otherwise the one-line "no safety-rated logic detected"
note.

**Meaning basis** — every stated meaning of a signal carries one of:

| Basis | Use when |
|---|---|
| `confirmed` | the engineer told you in this session |
| `comment` | a rung, tag, declaration or network comment says it (quote it) |
| `io_map` | an I/O list / module point comment / tag-table comment says it |
| `inferred` | deduced from use; add the reason ("drives O:2/15 seal-in, so likely master run") |
| `unknown` | no evidence; say so and add an open question |

Never upgrade `inferred` to fact in later prose. Write "probably", "likely" for inferred
statements in narrative text.

**Location references** — `Program/Routine rung N`, `Routine line N`, `Block network N`,
`file.csv row N`. Rung numbers are the source's own numbering.

## 1. Understand

```
# <Project or routine> — what it does
<Safety notice>

## Summary
3–8 sentences: machine purpose, main modes, what starts/stops it, key interlocks. Mark inferences.

## Machine sequence
| Step | What happens | Triggered by | Rungs/lines | Basis |
|---|---|---|---|---|
| 1 | Operator presses Start; master run seals in | I:1/14 rising, I:1/15 closed | file 2 rung 0 | comment |

## Signal table
| Address / tag | Meaning | How we know | Used in |
|---|---|---|---|
| I:1/14 | Start pushbutton (NO) | comment "Master start / stop" + user statement → confirmed | rung 0 |
| N7:1 | Low-level cutoff for tank 1 | inferred (compared with N7:0 in GRT/LEQ, rungs 2–3) | rungs 2, 3 |

## Open questions for the plant
1. <question> — why it matters — where (location).
```
Order open questions by impact. Always include: signals read but never written
(from `dependency_map.py`), unknown meanings, ambiguous NC/NO wiring, analog ranges,
anything you inferred that changes behavior if wrong.

## 2. Translate

Deliverables (write files; do not paste huge code blocks into chat — show a short excerpt):
1. Target code file(s): `.scl` (S7-1500), `.L5X` partial export (Logix), `.st` (IEC), each with
   the DRAFT header from the platform reference.
2. `mapping.csv` in `.rungstone/` (and a copy next to the code) with columns:
   `old_address,old_symbol,old_description,old_type,new_symbol,new_address,new_type,basis,notes`
   — one row per source signal, including internal bits/registers, timers, counters.
   Every source signal appears exactly once. Unused or excluded signals get a row with
   `notes` = "not used" / "safety - excluded".
3. Review queue items in `.rungstone/review_queue.json` (via `state.py queue add`).

Chat summary format:

```
# Translation: <source> → <target>   (DRAFT — nothing is final until you approve the review queue)
<Safety notice>
Files: out/FB_TwoTank.scl, out/TwoTank_tags.csv, .rungstone/mapping.csv

## Mapping (excerpt or full if ≤ 40 rows)
| Old | Meaning (basis) | New symbol | New address | Type old→new | Notes |
|---|---|---|---|---|---|

## Review queue
| ID | Risk | Location | Issue | Proposed fix | Status |
|---|---|---|---|---|---|
| R1 | high | rung 1 | Analog scaling: SLC 1746-NI4 4–20 mA raw 3277..16384 → S7 0..27648 (inferred module) | NORM_X(0,raw,27648) → SCALE_X(0,·,1000); confirm module + range | open |

Approve with e.g. "approve R2 R3", "edit R1: use 0..32767", "reject R4: keep 16-bit". High-risk items block export.
```

Rank items high → medium → low, then by source order. Each item: one issue, one concrete
proposed fix (code or setting), the source location. Use `translation-pitfalls.md` for
default risk levels.

## 3. Verify

Scenario plan first (always), then results.

```
# Verification plan: <source> vs <target>
<Safety notice>
| ID | Category | Preconditions | Stimulus (time → action) | Observe | Expected (source behavior) |
|---|---|---|---|---|---|
| S1 | normal | tanks full, I:1/15=1 | 0 ms Start=1; 200 ms Start=0 | O:2/15, O:4.0 | master runs; valve 1 opens |
```
Categories to cover: normal cycle, each fault/permissive loss, reset, power cycle (mid-cycle),
overflow/limits, missing or stuck inputs, edge timing (one-shots, timer boundaries).
Write the plan as `scenarios.json` (schema `scenario.schema.json`) when a simulator will run it.

Results (only when `simulate.py compare` or another simulator actually ran):

```
# Verification results — simulated, not hardware-tested
| ID | Verdict | Detail |
|---|---|---|
| S1 | match | 500 scans, all observed signals equal |
| S4 | differs | first at 3010 ms: Fill_Timer elapsed source 3000, target 0 (power cycle: TON not retained) |
| S6 | not run | PID instruction at rung 9 not supported by simulator |
Simulator approximations: <list from simulate.py output>
```
If nothing ran: title the section "Verification plan — NOT RUN (no simulator available)" and
give no verdicts at all. Never write "verified", "passes", or "equivalent" without a run.

## 4. Dependency map

Use `dependency_map.py --md` output as the base, then add interpretation:

```
# Dependency map: <files>
<Safety notice>
Summary line (counts by conflict type).

## Conflicts (most severe first)
| Severity | Type | Tag | Detail | Evidence |
## Open questions
## Cross-reference (every tag: readers, writers, systems)
```
Conflict types: `label_mismatch`, `competing_writers`, `read_never_written`, `broken_link`,
`translation_break` / `translation_watch`. For each high conflict add one sentence of
plain-English consequence ("the HMI shows elapsed fill time from Fill_Timer.ACC; after
translation that value no longer exists — rebind to Fill_Elapsed_ms").

## Export

`/rungstone:export` (or `export.py`) copies approved deliverables to `export/<timestamp>/`
with a `MANIFEST.md` listing files, sha256, approved/rejected review items, open medium/low
items, and the verification label. Refused while any high-risk item is open.
