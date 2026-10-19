# Contributing to Rungstone

All tooling is Python 3.9+ standard library and runs locally on the files in this repository.

| Task | Command (run from the repository root) |
|---|---|
| Unit tests | `python3 -m unittest discover -s tests -p "test_*.py"` |
| Copy `shared/` into each skill's `scripts/_shared/` | `python3 tools/sync_shared.py` (add `--check` to only report stale copies) |
| Build standalone `.skill` packages into `dist/` | `python3 tools/build_skills.py` |
| Validate the plugin and marketplace manifests | `claude plugin validate . --strict` |

Rules for changes:

- Keep every skill self-contained: edit `shared/`, then run the sync, never edit `scripts/_shared/` directly.
- Keep `SKILL.md` files under 500 lines and every file under 256 KiB.
- New fixtures must be original content written for this repository.
- Scripts must never open network connections, read credentials or environment variables, or execute customer files.
