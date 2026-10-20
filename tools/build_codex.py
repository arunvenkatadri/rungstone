"""Build the Codex / ChatGPT plugin package into dist/codex/.

The Codex directory takes skills-only plugins: no hooks and no local MCP
servers. So the package carries the three skills (with their scripts and
references), the icon, README, LICENSE and PRIVACY, and a plugin.json with
the OpenAI listing fields. The skills are adapted on the way:

  ${CLAUDE_SKILL_DIR}   -> <skill-dir> (explained at the top of each SKILL.md)
  Claude Code wording   -> Codex
  /rungstone: commands  -> plain requests (Codex plugins have no slash commands)
  cost estimates        -> token counts only (estimate.py --tokens-only)
  workbench             -> Codex mode (plain prompts instead of /rungstone: commands)

Output:
  dist/codex/rungstone/                      the plugin folder
  dist/codex/.agents/plugins/marketplace.json  a local marketplace for testing
  dist/codex/rungstone-codex-<version>.zip    the upload for the submission portal
"""
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "dist", "codex")
PLUGIN = os.path.join(OUT, "rungstone")
SKILLS = ("rungstone-plc", "rungstone-hmi-scada", "rungstone-ops")
EXCLUDE_DIRS = {"__pycache__", "evals"}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db"}
MAX_BYTES = 256 * 1024

SKILL_DIR_NOTE = ("> In this skill, `<skill-dir>` means the folder that contains this SKILL.md. "
                  "Run its scripts with `python3 <skill-dir>/scripts/...` from the user's project folder.\n\n")

LISTING = {
    "displayName": "Rungstone",
    "shortDescription": "PLC, HMI and trip analysis",
    "longDescription": (
        "Rungstone helps industrial controls engineers work with PLC, HMI/SCADA and historian exports.\n\n"
        "- Understand PLC code: a plain-English summary, the machine sequence by rung, and a signal table where "
        "every meaning is labelled confirmed, from comment, from I/O map, inferred or unknown.\n"
        "- Translate between Rockwell Logix, SLC 500, Siemens S7-1500 SCL and IEC 61131-3 Structured Text, with a "
        "mapping table and a ranked review queue the engineer approves before export.\n"
        "- Verify translations in a scan-cycle simulator (results are labelled simulated, not hardware-tested).\n"
        "- Map readers, writers and conflicts across PLC and HMI/SCADA, retarget HMI tags, and clean up alarms.\n"
        "- Find why a line or pump stopped from historian and alarm exports, and cite the PLC rung that caused it.\n\n"
        "Read-only by design: it never connects to or writes to a controller, excludes safety-rated logic with a "
        "visible notice, and never opens source-protected code. Not for use on a production system without review "
        "and testing by a qualified engineer. Provided as is, without warranty."),
    "developerName": "Arun Venkatadri",
    "category": "Developer Tools",
    "capabilities": ["Read", "Write"],
    "websiteURL": "https://github.com/arunvenkatadri/rungstone",
    "privacyPolicyURL": "https://github.com/arunvenkatadri/rungstone/blob/main/PRIVACY.md",
    "termsOfServiceURL": "https://github.com/arunvenkatadri/rungstone/blob/main/LICENSE",
    "defaultPrompt": [
        "Explain what this L5X does, rung by rung",
        "Translate this SLC 500 ladder to Siemens S7-1500 SCL",
        "Why did the pump trip? The historian and alarm exports are in this folder",
    ],
    "brandColor": "#F5A524",
    "composerIcon": "./assets/icon.png",
    "logo": "./assets/icon.png",
    "screenshots": ["./assets/workbench-review.png", "./assets/workbench-verify.png"],
}


def adapt_skill_md(text):
    text = text.replace("${CLAUDE_SKILL_DIR}", "<skill-dir>")
    text = text.replace("(when not running as a plugin, use the directory this SKILL.md is in)", "")
    text = text.replace("local workbench UI (beta) (Claude Code) or one self-contained HTML page (elsewhere)",
                        "local workbench UI (beta), or one self-contained HTML page when no browser is available")
    text = text.replace("- Workbench: in Claude Code, ", "- Workbench: ")
    text = text.replace("It (`/rungstone:workbench`) shows", "It shows")
    text = text.replace("(created by the PLC skill or `/workbench`)", "(created by the PLC skill or the workbench)")
    text = re.sub(r"(estimate\.py [^`\n]*?)--json", r"\1--tokens-only --json", text)
    text = text.replace("Claude Code", "Codex").replace("Claude", "Codex")
    m = re.match(r"^(---\n.*?\n---\n)", text, re.S)
    return m.group(1) + "\n" + SKILL_DIR_NOTE + text[m.end():].lstrip("\n")


def copy_skill(name):
    src = os.path.join(ROOT, "skills", name)
    dst = os.path.join(PLUGIN, "skills", name)
    for dp, dns, fns in os.walk(src):
        dns[:] = [d for d in dns if d not in EXCLUDE_DIRS]
        rel = os.path.relpath(dp, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for fn in fns:
            if fn in EXCLUDE_FILES or fn.endswith(".pyc"):
                continue
            s, d = os.path.join(dp, fn), os.path.join(dst, rel, fn)
            if fn == "SKILL.md" and rel == ".":
                with open(s, encoding="utf-8") as f:
                    text = adapt_skill_md(f.read())
                with open(d, "w", encoding="utf-8") as f:
                    f.write(text)
            elif fn == "server.py" and rel.endswith(os.path.join("_shared", "workbench")):
                with open(s, encoding="utf-8") as f:
                    text = f.read()
                assert 'DEFAULT_APP = "claude"' in text
                with open(d, "w", encoding="utf-8") as f:
                    f.write(text.replace('DEFAULT_APP = "claude"', 'DEFAULT_APP = "codex"', 1))
            else:
                shutil.copy2(s, d)


def readme(version):
    return """# Rungstone for Codex

Version %s. Read-only toolkit for industrial controls engineers: understand, translate and verify PLC
code, carry the work through to HMI/SCADA, and find why equipment stopped from historian exports.

**Not for use on a production system without review and testing by a qualified engineer.**
Provided as is, without warranty (see LICENSE).

- It never connects to or writes to a controller; outputs are files for the vendor IDE.
- Safety-rated logic is detected and excluded, with a visible notice.
- Translations go through a review queue; export is blocked while high-risk items are open.
- Source-protected routines and blocks are never opened.

Codex sees your files: any file content or report Codex reads is processed by OpenAI under your plan.
Check your customer contracts, NDAs and export-control obligations first. The scripts themselves run
locally and make no network connections. Privacy: PRIVACY.md.

Full documentation: https://github.com/arunvenkatadri/rungstone
""" % version


def main():
    if subprocess.call([sys.executable, os.path.join(ROOT, "tools", "sync_shared.py"), "--check"]) != 0:
        sys.exit("shared/ copies are out of sync; run tools/sync_shared.py first")
    with open(os.path.join(ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
        claude = json.load(f)
    version = claude["version"]
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(os.path.join(PLUGIN, "assets"))
    for name in SKILLS:
        copy_skill(name)
    shutil.copy2(os.path.join(ROOT, ".claude-plugin", "icon.png"), os.path.join(PLUGIN, "assets", "icon.png"))
    for shot in ("workbench-review.png", "workbench-verify.png"):
        shutil.copy2(os.path.join(ROOT, "docs", "images", shot), os.path.join(PLUGIN, "assets", shot))
    for fn in ("LICENSE", "PRIVACY.md"):
        shutil.copy2(os.path.join(ROOT, fn), os.path.join(PLUGIN, fn))
    with open(os.path.join(PLUGIN, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme(version))
    manifest = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        "name": "rungstone",
        "version": version,
        "description": claude["description"],
        "author": {"name": claude["author"]["name"]},
        "homepage": claude.get("homepage"),
        "repository": claude.get("repository"),
        "license": claude.get("license"),
        "keywords": claude.get("keywords", []),
        "extensions": {"com.openai": {"interface": LISTING}},
    }
    with open(os.path.join(PLUGIN, "plugin.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    os.makedirs(os.path.join(OUT, ".agents", "plugins"))
    with open(os.path.join(OUT, ".agents", "plugins", "marketplace.json"), "w", encoding="utf-8") as f:
        json.dump({"name": "rungstone-local", "interface": {"displayName": "Rungstone (local build)"},
                   "plugins": [{"name": "rungstone", "source": {"source": "local", "path": "./rungstone"},
                                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                                "category": LISTING["category"]}]}, f, indent=2)
        f.write("\n")

    errs = []
    for k in ("displayName", "shortDescription"):
        if len(LISTING[k]) > 30:
            errs.append("%s is %d chars (max 30)" % (k, len(LISTING[k])))
    if len(LISTING["longDescription"]) > 4000:
        errs.append("longDescription too long")
    for dp, _, fns in os.walk(PLUGIN):
        for fn in fns:
            p = os.path.join(dp, fn)
            if os.path.getsize(p) > MAX_BYTES and not fn.endswith(".png"):
                errs.append("file > 256 KiB: %s" % p)
            if fn.endswith((".md", ".py", ".html", ".json")):
                with open(p, encoding="utf-8", errors="replace") as fh:
                    t = fh.read()
                if "CLAUDE_SKILL_DIR" in t or "CLAUDE_PLUGIN_ROOT" in t or "/rungstone:" in t and fn == "SKILL.md":
                    errs.append("Claude-only reference left in %s" % p)
    if os.path.exists(os.path.join(PLUGIN, "hooks")):
        errs.append("hooks are not allowed in the Codex directory")
    if errs:
        sys.exit("\n".join(errs))

    zpath = os.path.join(OUT, "rungstone-codex-%s.zip" % version)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, dns, fns in os.walk(PLUGIN):
            dns.sort()
            for fn in sorted(fns):
                p = os.path.join(dp, fn)
                z.write(p, os.path.relpath(p, PLUGIN))
    n = sum(len(f) for _, _, f in os.walk(PLUGIN))
    print("ok   rungstone %s -> %s (%d files, %d KiB zip)" % (version, os.path.relpath(zpath, ROOT), n,
                                                           os.path.getsize(zpath) // 1024))


if __name__ == "__main__":
    main()
