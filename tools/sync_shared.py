"""Copy shared/py, shared/schemas and shared/workbench into every skill's scripts/_shared/.

Skills must be self-contained so they also work as standalone .skill
packages (claude.ai, Cowork) where the plugin's shared/ folder is absent.
Run after editing anything in shared/; `--check` exits 1 if a copy is stale.
"""
import filecmp
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = [(os.path.join(ROOT, "shared", "py"), ""), (os.path.join(ROOT, "shared", "schemas"), "schemas"),
       (os.path.join(ROOT, "shared", "workbench"), "workbench")]


def targets():
    sk = os.path.join(ROOT, "skills")
    for name in sorted(os.listdir(sk)):
        scripts = os.path.join(sk, name, "scripts")
        if os.path.isdir(scripts):
            yield os.path.join(scripts, "_shared")


def main():
    check = "--check" in sys.argv
    stale = []
    for dst_root in targets():
        for src, sub in SRC:
            if not os.path.isdir(src):
                continue
            dst = os.path.join(dst_root, sub)
            os.makedirs(dst, exist_ok=True)
            for fn in sorted(os.listdir(src)):
                s, t = os.path.join(src, fn), os.path.join(dst, fn)
                if os.path.isfile(s) and not fn.endswith(".pyc"):
                    if not os.path.exists(t) or not filecmp.cmp(s, t, shallow=False):
                        stale.append(os.path.relpath(t, ROOT))
                        if not check:
                            shutil.copy2(s, t)
        init = os.path.join(dst_root, "__init__.py")
        if not os.path.exists(init) and not check:
            open(init, "w").write('"""Vendored copy of shared/py - do not edit here; edit shared/ and run tools/sync_shared.py."""\n')
    if check and stale:
        print("stale shared copies:\n  " + "\n  ".join(stale))
        return 1
    print("%s %d file(s)" % ("would update" if check else "synced", len(stale)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
