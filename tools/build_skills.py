#!/usr/bin/env python3
"""Build standalone .skill packages (zip) for each skill into dist/.

Each skill is self-contained (scripts/_shared is vendored by sync_shared.py),
so the same folder works inside the plugin and as an uploaded .skill.
Checks: exactly one SKILL.md, frontmatter name/description rules, shared
copies in sync, no file over 256 KiB, no caches or OS junk.
"""
import os
import re
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCLUDE_DIRS = {"__pycache__", "node_modules", "evals"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db"}
MAX_BYTES = 256 * 1024


def frontmatter(path):
    text = open(path, encoding="utf-8").read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not m:
        raise SystemExit("%s: no frontmatter" % path)
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, text.count("\n") + 1


def check(skill_dir):
    errs = []
    fm, lines = frontmatter(os.path.join(skill_dir, "SKILL.md"))
    name = fm.get("name", "")
    desc = fm.get("description", "")
    if name != os.path.basename(skill_dir):
        errs.append("name %r != folder" % name)
    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", name) or len(name) > 64:
        errs.append("bad name %r" % name)
    if not desc or len(desc) > 1024 or "<" in desc or ">" in desc:
        errs.append("description length %d / angle brackets" % len(desc))
    if lines > 500:
        errs.append("SKILL.md has %d lines (> 500)" % lines)
    for dp, dns, fns in os.walk(skill_dir):
        dns[:] = [d for d in dns if d not in EXCLUDE_DIRS]
        for fn in fns:
            p = os.path.join(dp, fn)
            if fn == "SKILL.md" and dp != skill_dir:
                errs.append("nested SKILL.md: %s" % p)
            if os.path.islink(p):
                errs.append("symlink: %s" % p)
            if fn not in EXCLUDE_FILES and not fn.endswith(".pyc") and os.path.getsize(p) > MAX_BYTES:
                errs.append("file > 256 KiB: %s" % p)
    return errs, len(desc), lines


def main():
    if subprocess.call([sys.executable, os.path.join(ROOT, "tools", "sync_shared.py"), "--check"]) != 0:
        raise SystemExit("run tools/sync_shared.py first")
    out = os.path.join(ROOT, "dist")
    os.makedirs(out, exist_ok=True)
    ok = True
    for name in sorted(os.listdir(os.path.join(ROOT, "skills"))):
        sd = os.path.join(ROOT, "skills", name)
        if not os.path.isfile(os.path.join(sd, "SKILL.md")):
            continue
        errs, dlen, lines = check(sd)
        if errs:
            ok = False
            print("FAIL %s:\n  %s" % (name, "\n  ".join(errs)))
            continue
        target = os.path.join(out, name + ".skill")
        n = 0
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            for dp, dns, fns in os.walk(sd):
                dns[:] = sorted(d for d in dns if d not in EXCLUDE_DIRS)
                for fn in sorted(fns):
                    if fn in EXCLUDE_FILES or fn.endswith(".pyc"):
                        continue
                    p = os.path.join(dp, fn)
                    z.write(p, os.path.relpath(p, os.path.dirname(sd)))
                    n += 1
        print("ok   %s -> dist/%s.skill (%d files, %d KiB, SKILL.md %d lines, description %d chars)" % (
            name, name, n, os.path.getsize(target) // 1024, lines, dlen))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
