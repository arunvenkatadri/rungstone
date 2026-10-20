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

    def test_sources_add_select_remove(self):
        h = {"X-Rungstone-CSRF": self.srv.csrf}
        os.makedirs(os.path.join(self.tmp, "plc"))
        for n in ("Line3.L5X", "Line4.L5X"):
            with open(os.path.join(self.tmp, "plc", n), "w") as f:
                f.write("<RSLogix5000Content/>")
        with open(os.path.join(self.tmp, "notes.docx"), "w") as f:
            f.write("x")
        files = [c["path"] for c in json.loads(self.req("/api/candidates")[1])["files"]]
        self.assertEqual(files, [os.path.join("plc", "Line3.L5X"), os.path.join("plc", "Line4.L5X")])
        self.assertEqual(self.req("/api/source/add", {"path": "plc/Line3.L5X"})[0], 403)  # no token
        self.assertEqual(self.req("/api/source/add", {"path": "plc/Line3.L5X"}, h)[0], 200)
        proj = state.load(self.d, "project.json")
        self.assertEqual(proj["sources"][0]["format"], "l5x")
        self.assertEqual(state.selected_sources(proj), ["plc/Line3.L5X"])  # first source is in use
        self.assertEqual(self.req("/api/source/add", {"path": "plc/Line4.L5X", "use": True}, h)[0], 200)
        self.assertEqual(state.selected_sources(state.load(self.d, "project.json")), ["plc/Line4.L5X"])  # one at a time
        code, body = self.req("/api/source/select", {"path": "plc/Line3.L5X"}, h)
        self.assertEqual((code, json.loads(body)["selected"]), (200, ["plc/Line3.L5X"]))
        self.assertEqual(self.req("/api/source/select", {"path": "nope.L5X"}, h)[0], 404)
        self.assertEqual(self.req("/api/source/remove", {"path": "plc/Line3.L5X"}, h)[0], 200)
        proj = state.load(self.d, "project.json")
        self.assertEqual(([s["path"] for s in proj["sources"]], state.selected_sources(proj)), (["plc/Line4.L5X"], []))
        log = open(os.path.join(self.d, "decisions.log")).read()
        for word in ("source.added", "source.selected", "source.removed"):
            self.assertIn(word, log)

    def test_sniff_platform(self):
        fx = os.path.join(ROOT, "fixtures", "plc")
        cases = {os.path.join(fx, "estop", "Conveyor_Cell.L5X"): ("rockwell-logix", "detected"),
                 os.path.join(fx, "two_tank", "two_tank_slc.txt"): ("rockwell-slc500", "inferred"),
                 os.path.join(fx, "two_tank", "sim", "FB_TwoTank.scl"): ("siemens-tia", "detected"),
                 os.path.join(fx, "unwritten_bit", "Mixer_PLCopen.xml"): ("codesys", "detected"),
                 os.path.join(fx, "timers_hmi", "FTView_Fill_tags.csv"): (None, "unknown")}
        for f, want in cases.items():
            r = server.sniff_platform(f)
            self.assertEqual((r["platform"], r["confidence"]), want, f)
        self.assertIn("1756-L83ES", server.sniff_platform(os.path.join(fx, "estop", "Conveyor_Cell.L5X"))["detail"])
        samples = {"plc5.txt": ("// PLC-5/40, rack 1771-IBD\n0 SOR XIC I:001/00 OTE O:002/00 EOR\n", "rockwell-plc5"),
                   "ml.txt": ("// MicroLogix 1400 1766-L32BXB\n0 SOR XIC I:0/0 OTE O:0/0 EOR\n", "rockwell-micrologix"),
                   "bare.txt": ("0 SOR XIC B3:0/0 OTE B3:0/1 EOR\n", "rockwell-slc500"),
                   "fb.xml": ('<Document><Engineering version="V19" /><SW.Blocks.FB ID="0"/></Document>', "siemens-tia"),
                   "x.TcPOU": ('<TcPlcObject Version="1.1.0.1"><POU Name="P"/></TcPlcObject>', "beckhoff-twincat")}
        for name, (text, plat) in samples.items():
            p = os.path.join(self.tmp, name)
            with open(p, "w") as f:
                f.write(text)
            self.assertEqual(server.sniff_platform(p)["platform"], plat, name)
        self.assertEqual(server.sniff_platform(os.path.join(self.tmp, "bare.txt"))["confidence"], "unsure")

    def test_target_and_platform_override(self):
        h = {"X-Rungstone-CSRF": self.srv.csrf}
        self.assertEqual(self.req("/api/project/target", {"target": "siemens-scl"})[0], 403)  # no token
        self.assertEqual(self.req("/api/project/target", {"target": "s7-200"}, h)[0], 400)
        self.assertEqual(self.req("/api/project/target", {"target": "rockwell"}, h)[0], 200)
        self.assertEqual(state.load(self.d, "project.json")["target"], "rockwell")
        with open(os.path.join(self.tmp, "a.txt"), "w") as f:
            f.write("0 SOR XIC B3:0/0 OTE B3:0/1 EOR\n")
        self.req("/api/source/add", {"path": "a.txt"}, h)
        st = json.loads(self.req("/api/state")[1])
        self.assertEqual(st["sniffed"]["a.txt"]["confidence"], "unsure")
        self.assertEqual(self.req("/api/source/platform", {"path": "a.txt", "platform": "nope"}, h)[0], 400)
        self.assertEqual(self.req("/api/source/platform", {"path": "a.txt", "platform": "rockwell-plc5"}, h)[0], 200)
        self.assertEqual(state.load(self.d, "project.json")["sources"][0]["platform"], "rockwell-plc5")
        self.assertEqual(self.req("/api/source/platform", {"path": "a.txt", "platform": ""}, h)[0], 200)
        self.assertNotIn("platform", state.load(self.d, "project.json")["sources"][0])
        log = open(os.path.join(self.d, "decisions.log")).read()
        self.assertIn("project.target", log)
        self.assertIn("source.platform", log)

    def test_source_add_refuses_paths_outside_project(self):
        h = {"X-Rungstone-CSRF": self.srv.csrf}
        outside = tempfile.NamedTemporaryFile(suffix=".L5X", delete=False)
        outside.close()
        try:
            for p in (outside.name, "../" + os.path.basename(outside.name), "/etc/hosts", "", ".rungstone/../../x"):
                self.assertEqual(self.req("/api/source/add", {"path": p}, h)[0], 400, p)
            os.symlink(outside.name, os.path.join(self.tmp, "link.L5X"))
            self.assertEqual(self.req("/api/source/add", {"path": "link.L5X"}, h)[0], 400)
        finally:
            os.unlink(outside.name)
        self.assertEqual(state.load(self.d, "project.json")["sources"], [])

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
