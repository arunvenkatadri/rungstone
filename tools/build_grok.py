"""Build the Grok package (Grok Build CLI and Grok Bot) into dist/grok/.

Grok Build reads SKILL.md folders from ~/.grok/skills/ or <repo>/.grok/skills/. The package carries
the three skills adapted the same way as for Codex (see build_codex.py: plain <skill-dir> paths,
token-only estimates, no slash commands, no hooks or MCP server), with "Grok" as the host and the
workbench in Grok mode. Grok Bot has no skill upload: docs/grok-bot.md explains how its cloud
computer fetches this same zip, and is shipped inside it.

Output:
  dist/grok/rungstone/skills/<skill>/   the adapted skills
  dist/grok/rungstone-grok-<version>.zip
"""
import json
import os
import shutil
import subprocess
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_codex  # noqa: E402

ROOT = build_codex.ROOT
OUT = os.path.join(ROOT, "dist", "grok")
PKG = os.path.join(OUT, "rungstone")


def readme(version):
    return """# Rungstone for Grok

Version %s. Read-only toolkit for industrial controls engineers: understand, check, diff, translate
and verify PLC code (Rockwell, Siemens, CODESYS, Beckhoff, OpenPLC, IEC 61499), lock-in reports,
unit tests, HMI/SCADA rebinding and alarm cleanup, and finding why equipment stopped from
historian exports.

**Not for use on a production system without review and testing by a qualified engineer.**
Provided as is, without warranty (see LICENSE).

## Grok Build (CLI)

Copy the three skill folders into Grok's skills folder (or into `.grok/skills/` inside one project):

    mkdir -p ~/.grok/skills
    cp -R rungstone/skills/* ~/.grok/skills/

Then start `grok` in the folder with your exports and ask, for example, "explain what Line2.L5X
does" or "write a lock-in report for Palletizer_PLCopen.xml". The skills need python3 (3.9 or later).

## Grok Bot

See GROK_BOT.md: a bot brief and skills to paste in, and how the bot fetches this package.

## What it will not do

- It never connects to or writes to a controller; outputs are files for the vendor IDE.
- Safety-rated logic is detected and excluded, with a visible notice.
- Translations go through a review queue; export is blocked while high-risk items are open.
- Source-protected routines and blocks are never opened.

Grok sees your files: any file content or report Grok reads is processed by xAI under your plan.
Check your customer contracts, NDAs and export-control obligations first. The scripts themselves run
locally and make no network connections. Privacy: PRIVACY.md.

Full documentation: https://github.com/arunvenkatadri/rungstone
""" % version


def main():
    if subprocess.call([sys.executable, os.path.join(ROOT, "tools", "sync_shared.py"), "--check"]) != 0:
        sys.exit("shared/ copies are out of sync; run tools/sync_shared.py first")
    with open(os.path.join(ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
        version = json.load(f)["version"]
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(PKG)
    for name in build_codex.SKILLS:
        build_codex.copy_skill(name, os.path.join(PKG, "skills", name), host="Grok", app="grok")
    for fn in ("LICENSE", "PRIVACY.md"):
        shutil.copy2(os.path.join(ROOT, fn), os.path.join(PKG, fn))
    shutil.copy2(os.path.join(ROOT, "docs", "grok-bot.md"), os.path.join(PKG, "GROK_BOT.md"))
    with open(os.path.join(PKG, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme(version))

    errs = []
    for dp, _, fns in os.walk(PKG):
        for fn in fns:
            p = os.path.join(dp, fn)
            if os.path.getsize(p) > build_codex.MAX_BYTES:
                errs.append("file > 256 KiB: %s" % p)
            if fn.endswith((".md", ".py", ".html", ".json")):
                with open(p, encoding="utf-8", errors="replace") as fh:
                    t = fh.read()
                if "CLAUDE_SKILL_DIR" in t or "CLAUDE_PLUGIN_ROOT" in t or ("/rungstone:" in t and fn == "SKILL.md"):
                    errs.append("Claude-only reference left in %s" % p)
                if fn == "SKILL.md" and "Codex" in t:
                    errs.append("Codex wording left in %s" % p)
    if errs:
        sys.exit("\n".join(errs))

    zpath = os.path.join(OUT, "rungstone-grok-%s.zip" % version)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, dns, fns in os.walk(PKG):
            dns.sort()
            for fn in sorted(fns):
                p = os.path.join(dp, fn)
                z.write(p, os.path.relpath(p, OUT))
    n = sum(len(f) for _, _, f in os.walk(PKG))
    print("ok   rungstone %s -> %s (%d files, %d KiB zip)" % (version, os.path.relpath(zpath, ROOT), n,
                                                           os.path.getsize(zpath) // 1024))


if __name__ == "__main__":
    main()
