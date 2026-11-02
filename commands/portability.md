---
description: Lock-in report - what ties a CODESYS, TwinCAT or IEC ST project to its platform, rated per target (OpenPLC, IEC ST, S7-1500, Logix) with rough hours
argument-hint: "[PLCopen XML / .TcPOU / .st files] [to openplc | iec-st | siemens-scl | rockwell]"
---
Use the **rungstone-plc** skill, Workflow 5 (Lock-in report), on: $ARGUMENTS

If no files were given, use the source marked in use in the workbench: run `state.py status` from the skill's `scripts/_shared/` folder and take its `selected` list. If none is chosen or there is no `.rungstone/`, look for CODESYS/TwinCAT exports in the current folder (PLCopen .xml, .TcPOU/.TcGVL/.TcDUT, .st) and ask which to use. If the user named targets, pass them with `--targets`. Follow the skill's start-of-job steps (state init, parse, safety notice), then produce the Lock-in output exactly as in references/output-formats.md.

When done, save the full answer as `.rungstone/reports/portability-<source name>.md` so the workbench shows it.
