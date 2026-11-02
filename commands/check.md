---
description: Static analysis of PLC code - likely bugs and risky patterns (timers called conditionally, double writers, unguarded division, latches never cleared...) with where, why and a fix
argument-hint: "[files: .L5X, PLCopen .xml, .TcPOU, .st, .scl, ladder text]"
---
Use the **rungstone-plc** skill, Workflow 6 (Check), on: $ARGUMENTS

If no files were given, use the source marked in use in the workbench (`state.py status` → `selected`); if none is chosen, look for PLC exports in the current folder and ask which to use. Follow the start-of-job steps (state init, parse, safety notice), run `lint.py`, read the code around each high and medium finding, and present the Check output from references/output-formats.md.

When done, save the full answer as `.rungstone/reports/check-<source name>.md` so the workbench shows it.
