# Contributing to Rungstone

All tooling is Python 3.9+ standard library and works only on the files in this repository.

- **Unit tests** live in `tests/`. Run them with Python's built-in `unittest` test discovery from the repository root.
- **Shared code** lives in `shared/`. `tools/sync_shared.py` copies it into each skill's `scripts/_shared/` folder
  (its `--check` option only reports stale copies). Never edit `scripts/_shared/` directly.
- **Standalone `.skill` packages** are built into `dist/` by `tools/build_skills.py`.
- **Manifests** are checked with the Claude Code plugin validator in strict mode.
- **OpenPLC compile check (optional, needs Docker).** `tools/matiec/Dockerfile` builds OpenPLC's own copy of the
  MatIEC compiler into an image named `rungstone-matiec`. `tools/compile_openplc.py` then compiles OpenPLC
  translations with `iec2c` and `gcc` inside that image, with networking off. This is the one exception to the
  standard-library rule, it is for maintainers only, and the skills never call it. The matching test runs when
  `RUNGSTONE_COMPILE=1` is set.

Rules for changes:

- Keep every skill self-contained, `SKILL.md` files under 500 lines, and every file under 256 KiB.
- New fixtures must be original content written for this repository.
- Scripts must never open network connections, read credentials or environment variables, or execute customer files.
