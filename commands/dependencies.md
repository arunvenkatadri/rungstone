---
description: Map every tag across PLC, HMI and SCADA files - readers, writers, and conflicts (label mismatch, competing writers, read never written, broken links)
argument-hint: "<PLC files> [--hmi HMI/SCADA files] [--target siemens-scl|rockwell|iec-st]"
---
Use the **rungstone-plc** skill, Workflow 4 (Dependency map), on: $ARGUMENTS

If no files were named, use the source marked in use in the workbench (`state.py status` → `selected`); if none is chosen, ask which file to use.

Run dependency_map.py with every PLC and HMI/SCADA file provided (and --target / --mapping when a translation exists), save the JSON to .rungstone/dependencies.json and the Markdown to .rungstone/reports/dependencies.md, then present conflicts most severe first with a one-sentence consequence each, followed by open questions.
