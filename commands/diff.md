---
description: What changed between two exports of the same PLC project - per-routine diffs in plain English, the outputs each change writes, and what else reads them
argument-hint: "<old export> <new export>"
---
Use the **rungstone-plc** skill, Workflow 7 (Diff), on: $ARGUMENTS

Two files are needed: the older export first, then the newer one. If only one was given, ask for the other. Follow the start-of-job steps (state init, safety notice for both versions), run `diff_exports.py`, and present the Diff output from references/output-formats.md, starting with a plain-English summary of each change.

When done, save the full answer as `.rungstone/reports/diff-<new name>.md` so the workbench shows it.
