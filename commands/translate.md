---
description: Translate PLC code to S7-1500 SCL, Rockwell Logix L5X, or IEC Structured Text with a mapping table and a ranked review queue
argument-hint: "<files> to <siemens-scl | rockwell | iec-st>"
---
Use the **rungstone-plc** skill, Workflow 2 (Translate), on: $ARGUMENTS

If the target platform is not stated, ask (S7-1500 SCL, Rockwell Logix L5X, or IEC 61131-3 ST). Show the scope/cost estimate first for anything large. Work routine by routine against the shared tag map, write the target files with the DRAFT header, write .rungstone/mapping.csv, and add every translation risk to the review queue (high/medium/low, each with a proposed fix). End with the review queue and how to approve. Nothing is final until the engineer approves; nothing is ever downloaded to a controller.
