---
description: Generate test scenarios and run source vs target in the scan-cycle simulator (results are simulated, not hardware-tested)
argument-hint: "[source file] [target file] [mapping.csv]"
---
Use the **rungstone-plc** skill, Workflow 3 (Verify), on: $ARGUMENTS

If no files were named, use the source marked in use in the workbench (`state.py status` → `selected`); if none is chosen, ask which file to use.

Default to the source files in .rungstone/project.json, the latest translated target, and .rungstone/mapping.csv. Write the scenario plan first (normal cycle, faults, resets, power cycle, overflow, missing inputs, edge timing), then run simulate.py compare. Report match / differs / not run with reasons. Every result is labelled "simulated, not hardware-tested"; if nothing ran, say NOT RUN and give no verdicts.
