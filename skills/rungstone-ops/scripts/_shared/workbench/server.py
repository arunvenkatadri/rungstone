"""
server.py - Rungstone workbench: a local, loopback-only web UI over .rungstone/.

  python3 server.py [--state-dir DIR] [--port 0] [--host 127.0.0.1]

Binds to 127.0.0.1 only (any other address is refused), picks a free port by
default and prints {"url": ...} as JSON.  It serves one self-contained page
(app.html, no external resources) and a small JSON API:

  GET  /                 the app, with a per-run CSRF token embedded
  GET  /api/state        project, queue, mapping, verify results, deps, reports
  POST /api/queue/decide {"ids": [...], "action": "approve|reject|edit",
                          "note": "...", "fix": "..."}   (needs X-Rungstone-CSRF)

Every decision is written through state.queue_decide(), which appends to
decisions.log.  The server never executes anything from project files and
never opens outbound connections.
"""

import argparse
import csv
import json
import os
import hmac
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# state.py lives next to this folder (shared/py in the repo, _shared in a skill)
for cand in (os.path.dirname(HERE), os.path.join(os.path.dirname(HERE), "py")):
    if os.path.exists(os.path.join(cand, "state.py")) and cand not in sys.path:
        sys.path.insert(0, cand)
import state  # noqa: E402

LOOPBACK = ("127.0.0.1",)
MAX_BODY = 64 * 1024
REPORT_MAX = 400 * 1024
LABEL = "simulated, not hardware-tested"


def _collect_safety(obj, out, source):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "excluded_safety" and isinstance(v, list):
                for e in v:
                    if isinstance(e, dict):
                        item = dict(e)
                        item.setdefault("seen_in", source)
                        out.append(item)
            else:
                _collect_safety(v, out, source)
    elif isinstance(obj, list):
        for v in obj:
            _collect_safety(v, out, source)


def _read_json(d, name):
    try:
        return state.load(d, name)
    except (ValueError, OSError):
        return {"error": "could not read %s" % name}


def build_state(d):
    """Everything the UI needs, as one JSON-serialisable dict."""
    files = {}
    for name in ("project.json", "tagmap.json", "review_queue.json", "verify_results.json",
                 "dependencies.json", "estimate.json"):
        files[name] = _read_json(d, name)
    try:
        mapping = state.read_mapping(d)
    except (OSError, csv.Error):
        mapping = []
    reports = {}
    rdir = os.path.join(d, "reports")
    if os.path.isdir(rdir):
        for fn in sorted(os.listdir(rdir)):
            p = os.path.join(rdir, fn)
            if fn.lower().endswith(".md") and os.path.isfile(p) and os.path.getsize(p) <= REPORT_MAX:
                with open(p, encoding="utf-8", errors="replace") as f:
                    reports[fn] = f.read()
    log_tail = []
    lp = os.path.join(d, "decisions.log")
    if os.path.exists(lp):
        with open(lp, encoding="utf-8", errors="replace") as f:
            log_tail = [l.rstrip("\n") for l in f.readlines() if not l.startswith("#")][-200:]
    safety = []
    for name, data in files.items():
        _collect_safety(data, safety, name)
    queue = (files.get("review_queue.json") or {}).get("items", [])
    open_high = [i for i in queue if i.get("status") == "open" and i.get("risk") == "high"]
    return {
        "state_dir": d,
        "project": files["project.json"] or {},
        "tagmap": files["tagmap.json"] or {},
        "queue": queue,
        "mapping": mapping,
        "verify": files["verify_results.json"] or {},
        "dependencies": files["dependencies.json"] or {},
        "estimate": files["estimate.json"],
        "reports": reports,
        "decisions": log_tail,
        "excluded_safety": safety,
        "export": {"blocked": bool(open_high), "open_high": [i.get("id") for i in open_high]},
        "label": LABEL,
    }


def load_app():
    with open(os.path.join(HERE, "app.html"), encoding="utf-8") as f:
        return f.read()


def render_app(boot):
    # </script> inside JSON would end the script element early; escape '<'.
    js = json.dumps(boot, ensure_ascii=False).replace("<", "\\u003c")
    return load_app().replace("/*__BOOT__*/null", js, 1)


class Handler(BaseHTTPRequestHandler):
    server_version = "RungstoneWorkbench/1"

    def log_message(self, fmt, *args):  # keep stdout clean (URL JSON only)
        if self.server.verbose:
            sys.stderr.write("workbench: " + (fmt % args) + "\n")

    # -- helpers ----------------------------------------------------------
    def _host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, render_app({"mode": "live", "csrf": self.server.csrf}),
                              "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(200, build_state(self.server.state_dir))
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host"})
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        if origin and origin.lower() not in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port):
            return self._json(403, {"error": "cross-origin request refused"})
        if not hmac.compare_digest(self.headers.get("X-Rungstone-CSRF", ""), self.server.csrf):
            return self._json(403, {"error": "missing or bad CSRF token"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > MAX_BODY:
            return self._json(400, {"error": "bad body"})
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "invalid JSON"})
        if self.path != "/api/queue/decide":
            return self._json(404, {"error": "not found"})
        return self._decide(body)

    def _decide(self, body):
        ids = body.get("ids")
        action = body.get("action")
        note = (body.get("note") or "").strip() or None
        fix = (body.get("fix") or "").strip() or None
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            return self._json(400, {"error": "ids must be a non-empty list of strings"})
        if action == "approve":
            status = "approved"
        elif action == "reject":
            if not note:
                return self._json(400, {"error": "a note is required to reject"})
            status = "rejected"
        elif action == "edit":
            if not fix or len(ids) != 1:
                return self._json(400, {"error": "edit needs exactly one id and a new fix"})
            status = "edited"
        else:
            return self._json(400, {"error": "action must be approve, reject or edit"})
        with self.server.lock:
            done, missing = state.queue_decide(self.server.state_dir, set(ids), status, note, fix)
        return self._json(200, {"updated": done, "not_found": missing,
                                "export": build_state(self.server.state_dir)["export"]})


class Workbench(HTTPServer):
    def __init__(self, state_dir, host="127.0.0.1", port=0, verbose=False):
        if host not in LOOPBACK:
            raise ValueError("workbench binds to 127.0.0.1 only; refused host %r" % host)
        HTTPServer.__init__(self, (host, port), Handler)
        self.state_dir = state_dir
        self.csrf = os.urandom(32).hex()
        self.lock = threading.Lock()
        self.verbose = verbose

    @property
    def url(self):
        return "http://127.0.0.1:%d/" % self.server_address[1]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rungstone workbench (localhost only)")
    ap.add_argument("--state-dir", help=".rungstone folder (default: search upward from cwd)")
    ap.add_argument("--host", default="127.0.0.1", help="must be 127.0.0.1")
    ap.add_argument("--port", type=int, default=0, help="0 = pick a free port")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)
    d = args.state_dir or state.find_dir()
    if d and os.path.basename(os.path.normpath(d)) != state.STATE_DIR and \
            os.path.isdir(os.path.join(d, state.STATE_DIR)):
        d = os.path.join(d, state.STATE_DIR)
    if not d or not os.path.isdir(d):
        print(json.dumps({"error": "no .rungstone/ folder found; run state.py init first"}))
        return 1
    try:
        srv = Workbench(os.path.abspath(d), args.host, args.port, args.verbose)
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        return 2
    print(json.dumps({"url": srv.url, "state_dir": srv.state_dir, "bind": "127.0.0.1",
                      "stop": "Ctrl+C or kill the process"}), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
