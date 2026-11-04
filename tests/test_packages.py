"""Tests for the non-Claude packages: tools/build_codex.py and tools/build_grok.py."""
import os
import subprocess
import sys
import unittest
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build(tool):
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", tool)], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    return p.stdout


class PackageTests(unittest.TestCase):
    def test_grok_package(self):
        build("build_grok.py")
        out = os.path.join(ROOT, "dist", "grok")
        zips = [f for f in os.listdir(out) if f.endswith(".zip")]
        self.assertEqual(len(zips), 1)
        names = zipfile.ZipFile(os.path.join(out, zips[0])).namelist()
        for skill in ("rungstone-plc", "rungstone-hmi-scada", "rungstone-ops"):
            self.assertIn("rungstone/skills/%s/SKILL.md" % skill, names)
        self.assertIn("rungstone/GROK_BOT.md", names)
        self.assertFalse([n for n in names if "/hooks/" in n or n.endswith(".mcp.json")])
        with open(os.path.join(out, "rungstone", "skills", "rungstone-plc", "scripts", "_shared", "workbench", "server.py")) as f:
            self.assertIn('DEFAULT_APP = "grok"', f.read())
        with open(os.path.join(out, "rungstone", "skills", "rungstone-plc", "SKILL.md")) as f:
            text = f.read()
        self.assertNotIn("CLAUDE_SKILL_DIR", text)
        self.assertIn("<skill-dir>", text)

    def test_codex_package_still_codex(self):
        build("build_codex.py")
        with open(os.path.join(ROOT, "dist", "codex", "rungstone", "skills", "rungstone-plc", "scripts", "_shared",
                               "workbench", "server.py")) as f:
            self.assertIn('DEFAULT_APP = "codex"', f.read())


if __name__ == "__main__":
    unittest.main()
