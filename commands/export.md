---
description: Package approved deliverables for the vendor IDE with a manifest (blocked while any high-risk review item is open)
argument-hint: "[files to export; default: the latest translated files in out/]"
---
Export approved Rungstone deliverables: $ARGUMENTS

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/skills/rungstone-plc/scripts/_shared/state.py" gate-export`. If it reports blocked, list the open high-risk items and stop - the engineer must approve, edit or reject them first.
2. Otherwise run `python3 "${CLAUDE_PLUGIN_ROOT}/skills/rungstone-plc/scripts/_shared/export.py" <files>` and show the export folder, the MANIFEST summary, and any open medium/low items.
3. Remind the user: import the files into the vendor IDE, compile, and test offline. Rungstone never downloads to a controller.
