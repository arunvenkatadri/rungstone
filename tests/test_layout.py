"""Plugin layout checks that mirror the directory's blocking rules."""
import json
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP = {".git", "__pycache__", "dist", ".rungstone"}


def files():
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in SKIP and not d.endswith("-workspace")]
        for fn in fns:
            yield os.path.join(dp, fn)


class LayoutTests(unittest.TestCase):
    def test_manifest(self):
        m = json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
        self.assertEqual(m["name"], "rungstone")
        self.assertEqual(m["displayName"], "Rungstone")
        self.assertRegex(m["version"], r"^\d+\.\d+\.\d+$")
        self.assertTrue(m.get("license"))
        # historian server is registered but off until the user sets its config in userConfig
        self.assertEqual(list(m.get("mcpServers", {})), ["rungstone-historian"])
        self.assertFalse(os.path.exists(os.path.join(ROOT, ".mcp.json")))

    def test_repo_limits(self):
        fs = [f for f in files() if not f.endswith(".pyc")]
        self.assertLess(len(fs), 512, "directory holds plugins with > 512 files for review")
        for f in fs:
            self.assertNotIn(os.path.basename(f), (".DS_Store", "Thumbs.db"), f)
            self.assertFalse(os.path.islink(f), f)
            self.assertLessEqual(os.path.getsize(f), 256 * 1024, "%s > 256 KiB" % f)

    def test_readme_and_license(self):
        words = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read().split()
        self.assertGreaterEqual(len(words), 40)
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "LICENSE")))

    def test_commands_frontmatter(self):
        cdir = os.path.join(ROOT, "commands")
        names = sorted(f[:-3] for f in os.listdir(cdir) if f.endswith(".md"))
        self.assertEqual(names, sorted(["understand", "translate", "verify", "dependencies", "hmi", "alarms",
                                        "troubleshoot", "report", "workbench", "export"]))
        for n in names:
            t = open(os.path.join(cdir, n + ".md"), encoding="utf-8").read()
            self.assertTrue(re.match(r"^---\ndescription: .+\n", t), n)

    def test_skills(self):
        sdir = os.path.join(ROOT, "skills")
        for name in os.listdir(sdir):
            p = os.path.join(sdir, name, "SKILL.md")
            self.assertTrue(os.path.isfile(p), p)
            t = open(p, encoding="utf-8").read()
            m = re.match(r"^---\nname: (\S+)\ndescription: (.+?)\n(?:\w[\w-]*:.*\n)*---\n", t)
            self.assertTrue(m, name)
            self.assertEqual(m.group(1), name)
            self.assertLessEqual(len(m.group(2)), 1024)
            self.assertNotRegex(m.group(2), r"[<>]")
            self.assertLess(t.count("\n"), 500, name)
            self.assertIn("Non-negotiable rules", t, name)
            self.assertIn("simulated, not hardware-tested", " ".join(t.split()), name)

    def test_shared_in_sync(self):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "sync_shared.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_hooks_json(self):
        h = json.load(open(os.path.join(ROOT, "hooks", "hooks.json")))
        for ev, groups in h["hooks"].items():
            for g in groups:
                for hk in g["hooks"]:
                    script = re.search(r"hooks/scripts/(\w+\.py)", hk["command"]).group(1)
                    self.assertTrue(os.path.isfile(os.path.join(ROOT, "hooks", "scripts", script)), script)


if __name__ == "__main__":
    unittest.main()
