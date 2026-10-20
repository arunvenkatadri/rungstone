---
description: Open the Rungstone workbench (beta) - local review UI for estimates, review queue (approve/edit/reject), mapping, verification, conflicts
argument-hint: "[project folder]"
---
Open the Rungstone workbench for the project folder: $ARGUMENTS (if empty, use the current folder). The workbench is in beta: tell the user so, and that every approval can also be made in chat.

1. Make sure `.rungstone/` exists there (if not, run `python3 "${CLAUDE_PLUGIN_ROOT}/skills/rungstone-plc/scripts/_shared/state.py" init` in that folder).
2. Start the localhost-only server in the background: `python3 "${CLAUDE_PLUGIN_ROOT}/skills/rungstone-plc/scripts/_shared/workbench/server.py" --state-dir <folder>/.rungstone` and give the user the URL it prints. It binds to 127.0.0.1 only and never connects anywhere.
3. If a local server cannot be used (no browser, remote session), generate the single-file fallback instead: `python3 "${CLAUDE_PLUGIN_ROOT}/skills/rungstone-plc/scripts/_shared/workbench/static.py" --state-dir <folder>/.rungstone -o <folder>/.rungstone/workbench.html` and tell the user approvals then happen in chat ("approve R1, R3; reject R2: reason").
