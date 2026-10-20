---
description: Understand, retarget, migrate, or verify HMI/SCADA screens, tags, scripts, and alarms (FactoryTalk View, Ignition, WinCC, PanelView)
argument-hint: "<understand | retarget | migrate | verify | consistency> <files>"
---
Use the **rungstone-hmi-scada** skill on: $ARGUMENTS

If no files were named, use the source marked in use in the workbench (`state.py status` → `selected`); if none is chosen, ask which file to use.

Pick the workflow from the first word (understand, retarget, migrate, verify, consistency); if none is given, ask. Retarget uses .rungstone/mapping.csv from the PLC translation. Screens are only ever produced as draft layouts. Never execute HMI/SCADA scripts - parse them as text.
