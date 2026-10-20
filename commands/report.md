---
description: Shift and downtime reports from exported run/event/alarm data - top causes, recurring issues, MTBF/MTTR, per shift
argument-hint: "<run/event log CSV> [historian CSV] [shift definition, e.g. 06-14-22]"
---
Use the **rungstone-ops** skill, Shift and downtime reports workflow, on: $ARGUMENTS

If no files were named, use the source marked in use in the workbench (`state.py status` → `selected`); if none is chosen, ask which file to use.

Run downtime.py (and chart.py where a chart helps), then present the report in the skill's report format. State the data window and anything missing (gaps, unknown stop reasons).

When done, save the full answer as `.rungstone/reports/shift-<date>.md` or `downtime-<period>.md` (when `.rungstone/` exists) so the workbench shows it.
