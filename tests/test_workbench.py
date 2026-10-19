"""Tests for shared/workbench (server.py, static.py). Run: python3 -m unittest tests.test_workbench"""

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared", "workbench"))
sys.path.insert(0, os.path.join(ROOT, "shared", "py"))
import server  # noqa: E402
import state  # noqa: E402
import static  # noqa: E402


def make_state():
    tmp = tempfile.mkdtemp(prefix="rungstone-wb-")
    d = os.path.join(tmp, state.STATE_DIR)
    os.makedirs(d)
    state.init(d, "Test Line")
    state.queue_add(d, "high", "rung 3", "ACC read by HMI", "add DINT tag")
    state.queue_add(d, "low", "rung 5", "rename", "rename tag")
    state.save(d, "dependencies.json", {"summary": {"tags": 1, "references": 2, "conflicts": 0, "open_questions": 0},
                                        "conflicts": [], "open_questions": [], "tags": [],
                                        "excluded_safety": [{"kind": "program", "name": "SafetyProgram",
                                                             "reason": "Class=Safety", "confidence": "vendor-flagged"}]})
    os.makedirs(os.path.join(d, "reports"))
    with open(os.path.join(d, "reports", "understand-main.md"), "w") as f:
        f.write("# Summary\n\n<script>alert(1)</script> **bold**\n")
    return tmp, d


class WorkbenchServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp, self.d = make_state()
        self.srv = server.Workbench(self.d, "127.0.0.1", 0)
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()
        self.base = self.srv.url.rstrip("/")

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, headers=dict({"Content-Type": "application/json"}, **(headers or {})),
                                   method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_index_embeds_csrf_and_no_external_refs(self):
        code, html = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn(self.srv.csrf, html)
        self.assertNotRegex(html, r'(src|href)\s*=\s*["\']https?://')
        self.assertNotIn("@import", html)

    def test_state(self):
        code, body = self.req("/api/state")
        self.assertEqual(code, 200)
        st = json.loads(body)
        self.assertEqual(st["project"]["name"], "Test Line")
        self.assertEqual(len(st["queue"]), 2)
        self.assertTrue(st["export"]["blocked"])
        self.assertEqual(st["excluded_safety"][0]["name"], "SafetyProgram")
        self.assertEqual(st["label"], "simulated, not hardware-tested")
        self.assertIn("understand-main.md", st["reports"])

    def test_approve_with_csrf_unblocks_export_and_logs(self):
        code, body = self.req("/api/queue/decide", {"ids": ["R1"], "action": "approve"}, {"X-Rungstone-CSRF": self.srv.csrf})
        self.assertEqual(code, 200, body)
        self.assertEqual(json.loads(body)["updated"], ["R1"])
        self.assertFalse(json.loads(body)["export"]["blocked"])
        with open(os.path.join(self.d, "decisions.log")) as f:
            self.assertIn("queue.approved", f.read())

    def test_post_without_csrf_rejected(self):
        code, _ = self.req("/api/queue/decide", {"ids": ["R1"], "action": "approve"})
        self.assertEqual(code, 403)
        code, _ = self.req("/api/queue/decide", {"ids": ["R1"], "action": "approve"}, {"X-Rungstone-CSRF": "wrong"})
        self.assertEqual(code, 403)
        self.assertEqual(state.queue(self.d)["items"][0]["status"], "open")

    def test_cross_origin_rejected(self):
        code, _ = self.req("/api/queue/decide", {"ids": ["R1"], "action": "approve"},
                           {"X-Rungstone-CSRF": self.srv.csrf, "Origin": "http://evil.example"})
        self.assertEqual(code, 403)

    def test_reject_requires_note(self):
        code, body = self.req("/api/queue/decide", {"ids": ["R2"], "action": "reject"}, {"X-Rungstone-CSRF": self.srv.csrf})
        self.assertEqual(code, 400)
        self.assertIn("note", body)
        code, _ = self.req("/api/queue/decide", {"ids": ["R2"], "action": "reject", "note": "not needed"},
                           {"X-Rungstone-CSRF": self.srv.csrf})
        self.assertEqual(code, 200)
        self.assertEqual(state.queue(self.d)["items"][1]["status"], "rejected")

    def test_edit_replaces_fix(self):
        code, _ = self.req("/api/queue/decide", {"ids": ["R2"], "action": "edit", "fix": "use Tank1_Level"},
                           {"X-Rungstone-CSRF": self.srv.csrf})
        self.assertEqual(code, 200)
        item = state.queue(self.d)["items"][1]
        self.assertEqual((item["status"], item["proposed_fix"], item["previous_fix"]), ("edited", "use Tank1_Level", "rename tag"))

    def test_bad_host_header_rejected(self):
        code, _ = self.req("/api/state", headers={"Host": "attacker.example"})
        self.assertEqual(code, 403)


class BindTest(unittest.TestCase):
    def test_non_loopback_refused(self):
        tmp, d = make_state()
        try:
            for host in ("0.0.0.0", "192.168.1.10", "", "::"):
                with self.assertRaises(ValueError):
                    server.Workbench(d, host, 0)
            self.assertEqual(server.main(["--state-dir", d, "--host", "0.0.0.0"]), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class StaticTest(unittest.TestCase):
    def test_static_embeds_state_and_is_self_contained(self):
        tmp, d = make_state()
        try:
            out = os.path.join(tmp, "wb.html")
            self.assertEqual(static.main(["--state-dir", d, "-o", out]), 0)
            with open(out, encoding="utf-8") as fh:
                html = fh.read()
            self.assertIn('"mode": "static"', html)
            self.assertIn("ACC read by HMI", html)
            self.assertIn("SafetyProgram", html)
            self.assertNotRegex(html, r'<(script|link|img|iframe)[^>]+(src|href)\s*=\s*["\']?https?://')
            self.assertNotRegex(html, r'(src|href)\s*=\s*["\']https?://')
            # embedded report HTML must not be able to close the script element
            boot = html.split("var BOOT = ", 1)[1].split(";\nvar LABEL", 1)[0]
            self.assertNotIn("</script>", boot)
            self.assertIn("\\u003cscript>", boot)
            self.assertEqual(json.loads(boot)["state"]["export"]["blocked"], True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_static_from_project_folder(self):
        tmp, d = make_state()
        try:
            out = os.path.join(tmp, "wb.html")
            self.assertEqual(static.main(["--state-dir", tmp, "-o", out]), 0)
            with open(out) as fh:
                self.assertTrue(re.search(r"Test Line", fh.read()))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
