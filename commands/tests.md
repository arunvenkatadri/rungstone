---
description: Generate unit tests for a function block (CfUnit for CODESYS, TcUnit for TwinCAT, or plain IEC for any runtime) with expected values from the simulator
argument-hint: "<file> <function block> [cfunit | tcunit | plain] [scenarios.json]"
---
Use the **rungstone-plc** skill, Workflow 8 (Unit tests), on: $ARGUMENTS

If no function block was named, list the function blocks in the file and ask. If the framework isn't stated, use cfunit for CODESYS exports, tcunit for TwinCAT, plain otherwise. If no scenarios file was given, write one first (normal cycle, each input edge, resets, limits) using only the FB's inputs and outputs, show it, and then generate. Write the tests to `out/tests/` and record them with `state.py output out/tests/* --workflow tests`.
