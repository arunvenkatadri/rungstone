"""Tests for mcp/historian/server.py. Run: python3 -m unittest discover -s tests -p 'test_mcp_historian.py'"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "mcp", "historian", "server.py")


def make_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE history (tagname TEXT, t_stamp TEXT, value REAL, quality INTEGER)")
    con.execute("CREATE TABLE tags (tagname TEXT, description TEXT)")
    rows = []
    for i in range(600):
        t = "2026-10-19T08:%02d:%02d" % (i // 60, i % 60)
        rows.append(("Pump1_Run", t, 0.0 if i >= 400 else 1.0, 192))
        rows.append(("Pump1_Amps", t, 95.0 if i == 398 else 40.0 + (i % 5), 192))
    con.executemany("INSERT INTO history VALUES (?,?,?,?)", rows)
    con.executemany("INSERT INTO tags VALUES (?,?)", [("Pump1_Run", "Pump 1 running"), ("Pump1_Amps", "Pump 1 motor current")])
    con.commit()
    con.close()


class HistorianTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rungstone-hist-")
        self.db = os.path.join(self.tmp, "hist.db")
        make_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, enabled=True, **over):
        cfg = {"enabled": enabled, "backend": "sqlite",
               "sqlite": {"path": self.db, "table": "history",
                          "columns": {"tag": "tagname", "time": "t_stamp", "value": "value", "quality": "quality"},
                          "tags_table": {"table": "tags", "name": "tagname", "description": "description"}},
               "limits": {"max_points": 100}}
        cfg.update(over)
        p = os.path.join(self.tmp, "cfg.json")
        with open(p, "w") as f:
            json.dump(cfg, f)
        return p

    def run_server(self, messages, cfg_path):
        env = dict(os.environ)
        env.pop("RUNGSTONE_HISTORIAN_CONFIG", None)
        if cfg_path:
            env["RUNGSTONE_HISTORIAN_CONFIG"] = cfg_path
        inp = "".join(json.dumps(m) + "\n" for m in messages)
        p = subprocess.run([sys.executable, "-I", SERVER], input=inp, capture_output=True, text=True, env=env, timeout=20)
        out = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        return p.returncode, out, p.stderr

    def session(self, calls, cfg=None):
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        for i, (name, args) in enumerate(calls):
            msgs.append({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": name, "arguments": args}})
        code, out, err = self.run_server(msgs, cfg or self.config())
        self.assertEqual(code, 0, err)
        return out

    def test_refuses_without_config(self):
        code, out, err = self.run_server([{"jsonrpc": "2.0", "id": 1, "method": "initialize"}], None)
        self.assertEqual(code, 3)
        self.assertEqual(out, [])
        self.assertIn("disabled", err)

    def test_refuses_when_disabled(self):
        code, out, err = self.run_server([{"jsonrpc": "2.0", "id": 1, "method": "initialize"}], self.config(enabled=False))
        self.assertEqual(code, 3)
        self.assertEqual(out, [])
        self.assertIn("disabled", err)

    def test_initialize_and_list(self):
        out = self.session([])
        self.assertEqual(len(out), 2)  # the notification gets no response
        init = out[0]["result"]
        self.assertEqual(init["protocolVersion"], "2025-06-18")
        self.assertIn("tools", init["capabilities"])
        names = sorted(t["name"] for t in out[1]["result"]["tools"])
        self.assertEqual(names, ["latest", "list_tags", "query_range", "search_tags"])

    def test_protocol_negotiation_fallback(self):
        code, out, _ = self.run_server([{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {"protocolVersion": "1999-01-01"}}], self.config())
        self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")

    def test_tools(self):
        out = self.session([("list_tags", {}), ("search_tags", {"pattern": "amps"}), ("latest", {"tag": "Pump1_Run"}),
                            ("query_range", {"tags": ["Pump1_Run", "Pump1_Amps"], "start": "2026-10-19T08:00:00Z",
                                             "end": "2026-10-19T08:09:59Z", "max_points": 50})])
        res = dict((m["id"], json.loads(m["result"]["content"][0]["text"])) for m in out if m["id"] >= 10)
        self.assertEqual([t["tag"] for t in res[10]["tags"]], ["Pump1_Amps", "Pump1_Run"])
        self.assertEqual(res[11]["tags"][0]["description"], "Pump 1 motor current")
        self.assertEqual(res[12]["value"], 0.0)
        amps = res[13]["series"]["Pump1_Amps"]
        self.assertEqual(amps["count_raw"], 600)
        self.assertTrue(amps["downsampled"])
        self.assertLessEqual(len(amps["points"]), 52)
        self.assertIn(95.0, [p[1] for p in amps["points"]], "min/max decimation must keep the current spike")
        self.assertTrue(res[13]["read_only"])

    def test_bad_inputs_are_tool_errors(self):
        out = self.session([("query_range", {"tags": ["x"], "start": "nonsense", "end": "2026-10-19T09:00:00"}),
                            ("drop_table", {"name": "history"}),
                            ("latest", {"tag": "Pump1_Run'; DROP TABLE history; --"})])
        res = dict((m["id"], m["result"]) for m in out if m["id"] >= 10)
        self.assertTrue(res[10]["isError"])
        self.assertTrue(res[11]["isError"])
        self.assertIn("no samples", res[12]["content"][0]["text"])  # treated as a literal tag name
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM history").fetchone()[0], 1200)
        con.close()

    def test_bad_identifier_in_config_rejected(self):
        cfg = self.config(sqlite={"path": self.db, "table": "history; DROP TABLE history",
                                  "columns": {"tag": "tagname", "time": "t_stamp", "value": "value"}})
        out = self.session([("list_tags", {})], cfg)
        self.assertTrue(out[-1]["result"]["isError"])
        self.assertIn("invalid identifier", out[-1]["result"]["content"][0]["text"])

    def test_no_write_path(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("rungstone_historian_server", SERVER)
        hs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hs)
        with open(self.config()) as fh:
            cfg = json.load(fh)
        b = hs.SqliteBackend(cfg["sqlite"], hs.DEFAULT_LIMITS)
        with self.assertRaises(sqlite3.Error):
            b.conn.execute("DELETE FROM history")
        with self.assertRaises(sqlite3.Error):
            b.conn.execute("CREATE TABLE x (a)")
        with open(SERVER) as f:
            src = f.read()
        for word in ("INSERT", "UPDATE", "DELETE", "DROP", "write_value", "set_value", "call_method"):
            self.assertIsNone(re.search(r'"[^"\n]*\b%s\b' % word, src), word)
        self.assertNotIn("mode=rw", src)
        self.assertEqual(sorted(t["name"] for t in hs.TOOLS), ["latest", "list_tags", "query_range", "search_tags"])

    def test_csv_backend(self):
        d = os.path.join(self.tmp, "csv")
        os.makedirs(d)
        with open(os.path.join(d, "a.csv"), "w") as f:
            f.write("timestamp,Pump1_Run,Pump1_Amps\n2026-10-19 08:00:00,1,40\n2026-10-19 08:00:01,0,96\n")
        cfg = self.config(backend="csv_dir", csv_dir={"path": d, "format": "wide", "time_column": "timestamp"})
        out = self.session([("list_tags", {}), ("latest", {"tag": "Pump1_Amps"})], cfg)
        res = dict((m["id"], json.loads(m["result"]["content"][0]["text"])) for m in out if m["id"] >= 10)
        self.assertEqual([t["tag"] for t in res[10]["tags"]], ["Pump1_Amps", "Pump1_Run"])
        self.assertEqual(res[11]["value"], 96.0)

    def test_not_registered_in_plugin_mcp_json(self):
        p = os.path.join(ROOT, ".mcp.json")
        if os.path.exists(p):
            with open(p) as f:
                self.assertNotIn("historian", f.read())


if __name__ == "__main__":
    unittest.main()
