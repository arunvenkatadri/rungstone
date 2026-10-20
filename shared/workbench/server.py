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
  GET  /api/candidates   project files that look like PLC/HMI/historian exports
  GET  /api/version      a fingerprint of .rungstone/ and recorded outputs; the page
                         polls it and reloads /api/state when it changes
  POST /api/source/add   {"path": "...", "format": "...", "use": true}  record a source
                         file (and, with "use", make it the one the workflow runs on)
  POST /api/source/remove {"path": "..."}
  POST /api/source/select {"path": "..."}  the one source the workflows run on
  POST /api/project/target {"target": "siemens-scl|rockwell|iec-st"}
  POST /api/source/platform {"path": "...", "platform": "..."|""}  engineer's
                         override of the detected source platform ("" = use detection)
                         (all POSTs need the token)

Source paths must resolve inside the project folder (the parent of .rungstone/);
the server only hashes them and reads their first 16 KB as text to name the
platform (sniff_platform); it never parses, imports or runs them.

Every decision is written through state.queue_decide(), which appends to
decisions.log.  The server never executes anything from project files and
never opens outbound connections.
"""

import argparse
import csv
import json
import os
import re
import hashlib
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
SOURCE_FORMATS = {".l5x": "l5x", ".l5k": "l5k", ".scl": "siemens-scl", ".st": "iec-st", ".xml": "xml",
                  ".tcpou": "twincat", ".tcgvl": "twincat", ".tcdut": "twincat", ".txt": "text",
                  ".csv": "csv", ".xlsx": "xlsx", ".json": "json", ".awl": "siemens-stl", ".exp": "codesys-export"}
SKIP_DIRS = {".rungstone", ".git", "node_modules", "__pycache__", "export", ".venv", "venv"}
MAX_CANDIDATES = 500
TARGETS = ("siemens-scl", "rockwell", "iec-st")
PARSED_MAX = 64 * 1024 * 1024


SNIFF_BYTES = 16 * 1024
SOURCE_PLATFORMS = ("rockwell-logix", "rockwell-slc500", "rockwell-plc5", "rockwell-micrologix", "siemens-tia",
                    "siemens-step7", "codesys", "beckhoff-twincat", "iec-st", "other")
_RX = re.compile


def sniff_platform(path):
    """Name the source platform from the file's first bytes. Returns {platform, detail, confidence, evidence}."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(SNIFF_BYTES).decode("utf-8", "replace")
    except OSError:
        return {"platform": None, "confidence": "unknown", "evidence": "file could not be read"}
    ext = os.path.splitext(path)[1].lower()

    def attr(name):
        m = _RX(r'\b%s="([^"]{1,60})"' % name).search(head)
        return m.group(1) if m else None
    if "<RSLogix5000Content" in head:
        proc, rev = attr("ProcessorType"), attr("SoftwareRevision")
        return {"platform": "rockwell-logix", "confidence": "detected",
                "detail": ", ".join(x for x in (proc, "exported from Studio 5000 v" + rev if rev else None) if x),
                "evidence": "RSLogix5000Content header" + (" with ProcessorType" if proc else "")}
    if "<TcPlcObject" in head:
        return {"platform": "beckhoff-twincat", "confidence": "detected", "evidence": "TcPlcObject header"}
    if "plcopen.org" in head:
        prod = attr("productName")
        return {"platform": "codesys" if prod and "codesys" in prod.lower() else "iec-st", "confidence": "detected",
                "detail": "PLCopen XML" + (" from " + prod if prod else ""), "evidence": "PLCopen namespace"}
    if "<Document" in head and ("SW.Blocks" in head or "<Engineering" in head):
        ver = _RX(r'<Engineering version="([^"]+)"').search(head)
        return {"platform": "siemens-tia", "confidence": "detected", "detail": "SimaticML" + (", TIA " + ver.group(1) if ver else ""),
                "evidence": "SimaticML document"}
    if ext in (".scl", ".awl") or _RX(r"(?im)^\s*(FUNCTION_BLOCK|ORGANIZATION_BLOCK|DATA_BLOCK)\s+\"").search(head):
        return {"platform": "siemens-step7" if ext == ".awl" else "siemens-tia", "confidence": "detected",
                "evidence": "Siemens source (%s)" % (ext or "block header")}
    if _RX(r"\bSOR\b.*\bEOR\b").search(head) or _RX(r"(?m)^\s*\d+\s+SOR\b").search(head):
        for rx, plat, why in ((r"\b17(61|62|63|64|66)-", "rockwell-micrologix", "176x MicroLogix catalog numbers"),
                              (r"\bMicroLogix\b", "rockwell-micrologix", "MicroLogix in comments"),
                              (r"\b1771-|\bPLC-?5\b", "rockwell-plc5", "PLC-5 / 1771 in comments"),
                              (r"\b1746-|\b1747-|\bSLC\s*5/0\d|\bSLC\s*500\b", "rockwell-slc500", "SLC / 1746 in comments")):
            m = _RX(rx, re.I).search(head)
            if m:
                return {"platform": plat, "confidence": "inferred", "evidence": why + " (" + m.group(0).strip() + ")"}
        return {"platform": "rockwell-slc500", "confidence": "unsure",
                "evidence": "RSLogix 500-style ladder text with no rack or processor hints; SLC 500, PLC-5 and MicroLogix look the same"}
    if ext == ".st" or _RX(r"(?im)^\s*(FUNCTION_BLOCK|PROGRAM)\s+\w").search(head):
        return {"platform": "iec-st", "confidence": "inferred", "evidence": "IEC Structured Text keywords"}
    return {"platform": None, "confidence": "unknown", "evidence": "not a recognised PLC export"}


def parsed_platforms(d):
    """Platform facts per parsed file (format, controller, software revision) from .rungstone/parsed.json."""
    p = os.path.join(d, "parsed.json")
    if not os.path.isfile(p) or os.path.getsize(p) > PARSED_MAX:
        return []
    try:
        data = state.load(d, "parsed.json") or {}
    except (ValueError, OSError):
        return []
    root = project_root(d)
    out = []
    for pr in data.get("projects") or []:
        f = pr.get("file") or ""
        rel = os.path.relpath(os.path.realpath(os.path.join(root, f)), root)
        out.append({"file": rel, "format": pr.get("format"), "controller": pr.get("controller"),
                    "software": (pr.get("target") or {}).get("software_revision"), "stats": pr.get("stats")})
    return out


def project_root(d):
    return os.path.realpath(os.path.dirname(os.path.abspath(d)))


def resolve_in_project(d, rel):
    """Real path of rel if it is a file inside the project folder, else None."""
    if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
        return None
    root = project_root(d)
    p = os.path.realpath(os.path.join(root, rel.strip()))
    if os.path.commonpath([root, p]) != root or not os.path.isfile(p):
        return None
    return p


def candidates(d):
    root = project_root(d)
    have = set(s.get("path") for s in (state.load(d, "project.json", {}) or {}).get("sources", []))
    out = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith("."))
        if cur[len(root):].count(os.sep) >= 6:
            dirs[:] = []
        for fn in sorted(files):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in SOURCE_FORMATS or fn.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(cur, fn), root)
            if rel not in have:
                out.append({"path": rel, "format": SOURCE_FORMATS[ext]})
            if len(out) >= MAX_CANDIDATES:
                return out
    return out


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


def sniff_sources(d):
    out = {}
    for src in (state.load(d, "project.json", {}) or {}).get("sources", []):
        p = resolve_in_project(d, src.get("path"))
        if p:
            out[src["path"]] = sniff_platform(p)
    return out


OUTPUT_MAX = 400 * 1024


def outputs(d):
    """Generated files recorded with state.py output, with their text for display (never executed)."""
    out = []
    for o in (state.load(d, "project.json", {}) or {}).get("outputs", []):
        p = resolve_in_project(d, o.get("path"))
        item = dict(o, exists=bool(p))
        if p:
            size = os.path.getsize(p)
            item["size"] = size
            item["changed_since_recorded"] = state.sha256(p) != o.get("sha256")
            if size <= OUTPUT_MAX:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    item["text"] = fh.read()
        out.append(item)
    return out


def version(d):
    """Cheap change fingerprint: names, sizes and mtimes of state files, reports and outputs."""
    parts = []
    for base in (d, os.path.join(d, "reports")):
        if os.path.isdir(base):
            for fn in sorted(os.listdir(base)):
                fp = os.path.join(base, fn)
                if os.path.isfile(fp):
                    st = os.stat(fp)
                    parts.append("%s:%d:%d" % (fn, st.st_size, st.st_mtime_ns))
    for o in (state.load(d, "project.json", {}) or {}).get("outputs", []):
        p = resolve_in_project(d, o.get("path"))
        if p:
            st = os.stat(p)
            parts.append("%s:%d:%d" % (o["path"], st.st_size, st.st_mtime_ns))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


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
        "platforms": parsed_platforms(d),
        "sniffed": sniff_sources(d),
        "outputs": outputs(d),
        "version": version(d),
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
        if path == "/api/version":
            return self._json(200, {"version": version(self.server.state_dir)})
        if path == "/api/candidates":
            return self._json(200, {"files": candidates(self.server.state_dir)})
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
        if self.path == "/api/queue/decide":
            return self._decide(body)
        if self.path in ("/api/source/add", "/api/source/remove"):
            return self._source(body, self.path.endswith("add"))
        if self.path == "/api/source/select":
            return self._select(body)
        if self.path == "/api/project/target":
            return self._target(body)
        if self.path == "/api/source/platform":
            return self._platform(body)
        return self._json(404, {"error": "not found"})

    def _source(self, body, add):
        d = self.server.state_dir
        rel = body.get("path") if isinstance(body, dict) else None
        if add:
            p = resolve_in_project(d, rel)
            if p is None:
                return self._json(400, {"error": "not a file inside the project folder"})
            fmt = (body.get("format") or "").strip()[:40] or SOURCE_FORMATS.get(os.path.splitext(p)[1].lower())
            rel = os.path.relpath(p, project_root(d))
            with self.server.lock:
                state.add_source(d, p, fmt)
                state.log(d, "source.added", rel + " (workbench)")
                if body.get("use") is True:
                    state.set_selected(d, [rel])
                    state.log(d, "source.selected", rel + " (workbench)")
        else:
            if not isinstance(rel, str) or not rel:
                return self._json(400, {"error": "path required"})
            with self.server.lock:
                if not state.remove_source(d, rel):
                    return self._json(404, {"error": "not a recorded source"})
                state.log(d, "source.removed", rel + " (workbench)")
        return self._json(200, {"sources": (state.load(d, "project.json", {}) or {}).get("sources", [])})

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


    def _platform(self, body):
        d = self.server.state_dir
        rel, plat = (body.get("path"), body.get("platform")) if isinstance(body, dict) else (None, None)
        if plat not in SOURCE_PLATFORMS + ("",):
            return self._json(400, {"error": "unknown platform"})
        with self.server.lock:
            proj = state.load(d, "project.json")
            hit = [x for x in proj["sources"] if x["path"] == rel]
            if not hit:
                return self._json(404, {"error": "not a recorded source"})
            if plat:
                hit[0]["platform"] = plat
            else:
                hit[0].pop("platform", None)
            state.save(d, "project.json", proj)
            state.log(d, "source.platform", "%s -> %s (workbench)" % (rel, plat or "detected"))
        return self._json(200, {"path": rel, "platform": plat or None})

    def _target(self, body):
        d = self.server.state_dir
        t = body.get("target") if isinstance(body, dict) else None
        if t not in TARGETS:
            return self._json(400, {"error": "target must be one of " + ", ".join(TARGETS)})
        with self.server.lock:
            proj = state.load(d, "project.json")
            proj["target"] = t
            state.save(d, "project.json", proj)
            state.log(d, "project.target", t + " (workbench)")
        return self._json(200, {"target": t})

    def _select(self, body):
        d = self.server.state_dir
        rel = body.get("path") if isinstance(body, dict) else None
        if not isinstance(rel, str) or not rel:
            return self._json(400, {"error": "path required"})
        with self.server.lock:
            if state.set_selected(d, [rel]):
                return self._json(404, {"error": "not a recorded source"})
            state.log(d, "source.selected", rel + " (workbench)")
        return self._json(200, {"selected": state.selected_sources(state.load(d, "project.json", {}))})


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
