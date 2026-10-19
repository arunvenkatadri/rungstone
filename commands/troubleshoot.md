---
description: Find why a line, machine, or pump stopped from exported historian/alarm data plus the PLC logic (offline)
argument-hint: "<historian CSV> [alarm/event log] [PLC project] [time or signal, e.g. 'P-101 trip at 14:32']"
---
Use the **rungstone-ops** skill, Troubleshoot-a-stop workflow, on: $ARGUMENTS

Pick the relevant signals and time window, run the trip/first-out analysis, pull the logic that writes the stopped output via the dependency map, and report: likely cause (labelled confirmed / evidence-supported / inferred / unknown), evidence, logic cited with locations, what to check first, and what was not checked. This works on exported data only - there is no live monitoring.
