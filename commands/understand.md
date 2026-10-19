---
description: Explain what PLC code does - summary, machine sequence by rung, signal table with evidence, open questions for the plant
argument-hint: "[files or folder, e.g. Line3.L5X io_list.csv]"
---
Use the **rungstone-plc** skill, Workflow 1 (Understand), on: $ARGUMENTS

If no files were given, look for PLC exports in the current folder (.L5X, SimaticML .xml, .scl, .st, PLCopen .xml, .TcPOU, ladder text, I/O CSV/XLSX) and ask which to use. Follow the skill's start-of-job steps (state init, parse, safety notice, estimate), then produce the Understand output exactly as in references/output-formats.md. Label every meaning confirmed / comment / io_map / inferred / unknown.
