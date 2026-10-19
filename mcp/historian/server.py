"""
Rungstone historian MCP server - OPTIONAL, READ-ONLY.

Registered by the plugin but DISABLED by default: until the user sets the
"Historian connector config" plugin setting to a JSON config with
"enabled": true (after plant IT/OT approval), it serves no tools and connects
to nothing.

Transport: MCP stdio (newline-delimited JSON-RPC 2.0).  Standard library only.

Backends (all read-only):
  sqlite   - file opened with URI mode=ro and PRAGMA query_only=ON
  csv_dir  - folder of exported CSV files (long or wide format)
  odbc     - SQL historian tables via pyodbc (if installed); readonly=True connection
  opcua    - OPC UA values/history via asyncua (if installed); reads only

Credentials: only what the user enters in the plugin settings (Claude Code
userConfig, sensitive values kept in the secure store), passed to this process
by Claude Code. The config file must not contain credentials.

Tools: list_tags, search_tags, latest, query_range.
The model never supplies SQL.  Queries are parameterised SELECTs built from
the configured table/column mapping; identifiers are validated.
"""

import csv
import datetime as dt
import fnmatch
import glob
import json
import os
import re
import sqlite3
import sys

SERVER_NAME = "rungstone-historian"
SERVER_VERSION = "0.1.0"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
DEFAULT_LIMITS = {"max_points": 2000, "max_tags": 20, "max_rows_scanned": 1000000, "max_list": 500}


class ToolError(Exception):
    pass


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------

def parse_time(s):
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return dt.datetime.fromtimestamp(float(s), dt.timezone.utc)
    t = str(s).strip().replace("Z", "+00:00")
    if re.fullmatch(r"\d+(\.\d+)?", t):
        v = float(t)
        return dt.datetime.fromtimestamp(v / 1000.0 if v > 1e11 else v, dt.timezone.utc)
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%Y/%m/%d %H:%M:%S"):
        try:
            d = dt.datetime.fromisoformat(t) if fmt is None else dt.datetime.strptime(t, fmt)
            return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    raise ToolError("unrecognised time: %r (use ISO 8601, e.g. 2026-10-19T08:00:00)" % s)


def to_db_time(d, fmt):
    if fmt == "epoch_s":
        return d.timestamp()
    if fmt == "epoch_ms":
        return int(d.timestamp() * 1000)
    return d.astimezone(dt.timezone.utc).replace(tzinfo=None).isoformat(sep=" ") if fmt == "iso_space" else \
        d.astimezone(dt.timezone.utc).replace(tzinfo=None).isoformat()


def iso(d):
    return d.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def downsample(points, max_points):
    """Min/max decimation that keeps spikes (trips) visible."""
    if len(points) <= max_points:
        return points, False
    buckets = max(1, max_points // 2)
    size = len(points) / float(buckets)
    out = []
    for b in range(buckets):
        chunk = points[int(b * size):int((b + 1) * size)]
        if not chunk:
            continue
        numeric = [p for p in chunk if isinstance(p[1], float)]
        if not numeric:
            out.append(chunk[0])
            continue
        lo = min(numeric, key=lambda p: p[1])
        hi = max(numeric, key=lambda p: p[1])
        out.extend(sorted({lo, hi}, key=lambda p: p[0]) if lo is not hi else [lo])
    if out[-1] != points[-1]:
        out.append(points[-1])
    return out, True


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------

class SqlBackend(object):
    """Parameterised, read-only SELECTs over a configured history table."""

    placeholder = "?"

    def __init__(self, cfg, limits):
        self.limits = limits
        self.table = self._ident(cfg.get("table", "history"))
        cols = cfg.get("columns", {})
        self.c_tag = self._ident(cols.get("tag", "tag"))
        self.c_time = self._ident(cols.get("time", "t_stamp"))
        self.c_val = self._ident(cols.get("value", "value"))
        self.c_q = self._ident(cols["quality"]) if cols.get("quality") else None
        self.time_format = cfg.get("time_format", "iso")
        tt = cfg.get("tags_table")
        self.tags_table = None
        if tt:
            self.tags_table = (self._ident(tt["table"]), self._ident(tt.get("name", "name")),
                               self._ident(tt["description"]) if tt.get("description") else None)

    @staticmethod
    def _ident(name):
        if not isinstance(name, str) or not IDENT.match(name):
            raise ToolError("invalid identifier in config: %r" % (name,))
        return '"%s"' % name

    def _rows(self, sql, params):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            for r in batch:
                yield r

    def list_tags(self, limit):
        if self.tags_table:
            t, n, d = self.tags_table
            sql = "SELECT %s%s FROM %s ORDER BY %s LIMIT %d" % (n, (", " + d) if d else "", t, n, limit)
            return [{"tag": r[0], "description": r[1] if d else None} for r in self._rows(sql, [])]
        sql = "SELECT DISTINCT %s FROM %s ORDER BY %s LIMIT %d" % (self.c_tag, self.table, self.c_tag, limit)
        return [{"tag": r[0]} for r in self._rows(sql, [])]

    def search_tags(self, pattern, limit):
        like = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("*", "%").replace("?", "_")
        if "%" not in like and "_" not in like.replace("\\_", ""):
            like = "%" + like + "%"
        if self.tags_table:
            t, n, d = self.tags_table
            sql = "SELECT %s%s FROM %s WHERE %s LIKE ? ESCAPE '\\' ORDER BY %s LIMIT %d" % (
                n, (", " + d) if d else "", t, n, n, limit)
            return [{"tag": r[0], "description": r[1] if d else None} for r in self._rows(sql, [like])]
        sql = "SELECT DISTINCT %s FROM %s WHERE %s LIKE ? ESCAPE '\\' ORDER BY %s LIMIT %d" % (
            self.c_tag, self.table, self.c_tag, self.c_tag, limit)
        return [{"tag": r[0]} for r in self._rows(sql, [like])]

    def latest(self, tag):
        cols = "%s, %s%s" % (self.c_time, self.c_val, (", " + self.c_q) if self.c_q else "")
        sql = "SELECT %s FROM %s WHERE %s = ? ORDER BY %s DESC LIMIT 1" % (cols, self.table, self.c_tag, self.c_time)
        for r in self._rows(sql, [tag]):
            return {"tag": tag, "time": iso(parse_time(r[0])), "value": num(r[1]),
                    "quality": r[2] if self.c_q else None}
        return {"tag": tag, "error": "no samples"}

    def query_range(self, tag, start, end):
        cols = "%s, %s" % (self.c_time, self.c_val)
        sql = "SELECT %s FROM %s WHERE %s = ? AND %s >= ? AND %s <= ? ORDER BY %s LIMIT %d" % (
            cols, self.table, self.c_tag, self.c_time, self.c_time, self.c_time, self.limits["max_rows_scanned"])
        out = []
        for r in self._rows(sql, [tag, to_db_time(start, self.time_format), to_db_time(end, self.time_format)]):
            out.append((iso(parse_time(r[0])), num(r[1])))
        return out


class SqliteBackend(SqlBackend):
    def __init__(self, cfg, limits):
        SqlBackend.__init__(self, cfg, limits)
        path = os.path.abspath(cfg["path"])
        if not os.path.isfile(path):
            raise ToolError("sqlite file not found: %s" % path)
        uri = "file:%s?mode=ro" % path.replace("?", "%3f").replace("#", "%23")
        self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self.conn.execute("PRAGMA query_only = ON")


class OdbcBackend(SqlBackend):
    """Read-only ODBC (SQL historian tables).

    Connection details come only from what the user chose to share: either a
    system DSN named in the config file, or the ODBC connection string the user
    entered in the plugin's settings (Claude Code userConfig, stored in the
    platform's secure store and handed to this process by Claude Code as
    RUNGSTONE_ODBC_CONNECTION). Credentials are never put in the config file.
    """

    def __init__(self, cfg, limits):
        try:
            import pyodbc  # noqa: F401
        except ImportError:
            raise ToolError("backend not available: odbc needs the pyodbc package")
        SqlBackend.__init__(self, cfg, limits)
        reject_credentials(cfg, "odbc")
        shared = SHARED.get("odbc_connection") or ""
        dsn = str(cfg.get("dsn") or "")
        if shared:
            conn_str = shared
        elif re.match(r"^[A-Za-z0-9 _.-]{1,64}$", dsn):
            conn_str = "DSN=%s" % dsn
        else:
            raise ToolError("odbc needs a \"dsn\" in the config or a connection string in the plugin settings")
        self.conn = pyodbc.connect(conn_str, readonly=True, autocommit=False)  # read-only connection
        # T-SQL has no LIMIT; this backend targets ANSI/Postgres/SQLite-style dialects. Document per site.


class OpcUaBackend(object):
    """Read-only OPC UA: reads current values and raw history. Never writes.

    Anonymous unless the user entered an OPC UA username/password in the plugin
    settings (Claude Code userConfig). Use an account with read-only rights.
    """

    def __init__(self, cfg, limits):
        try:
            from asyncua.sync import Client  # noqa: F401
        except ImportError:
            raise ToolError("backend not available: opcua needs the asyncua package")
        reject_credentials(cfg, "opcua")
        self.limits = limits
        self.nodes = dict(cfg.get("nodes", {}))  # tag name -> NodeId string
        self.client = Client(cfg["endpoint"])
        if SHARED.get("opcua_username"):
            self.client.set_user(SHARED["opcua_username"])
            self.client.set_password(SHARED.get("opcua_password") or "")
        self.client.connect()

    def list_tags(self, limit):
        return [{"tag": t, "node": n} for t, n in sorted(self.nodes.items())[:limit]]

    def search_tags(self, pattern, limit):
        pat = pattern if any(c in pattern for c in "*?") else "*%s*" % pattern
        return [{"tag": t, "node": n} for t, n in sorted(self.nodes.items()) if fnmatch.fnmatch(t.lower(), pat.lower())][:limit]

    def _node(self, tag):
        if tag not in self.nodes:
            raise ToolError("tag %r is not in the configured node list" % tag)
        return self.client.get_node(self.nodes[tag])

    def latest(self, tag):
        dv = self._node(tag).read_data_value()
        return {"tag": tag, "time": iso(dv.SourceTimestamp or dt.datetime.now(dt.timezone.utc)),
                "value": num(dv.Value.Value), "quality": str(dv.StatusCode)}

    def query_range(self, tag, start, end):
        hist = self._node(tag).read_raw_history(start, end, numvalues=self.limits["max_rows_scanned"])
        return [(iso(h.SourceTimestamp), num(h.Value.Value)) for h in hist]


class CsvDirBackend(object):
    """Exported CSV files. Long format: time,tag,value[,quality]. Wide: time + one column per tag."""

    def __init__(self, cfg, limits):
        self.limits = limits
        self.dir = os.path.abspath(cfg["path"])
        if not os.path.isdir(self.dir):
            raise ToolError("csv directory not found: %s" % self.dir)
        self.fmt = cfg.get("format", "long")
        self.c_time = cfg.get("time_column", "timestamp")
        self.c_tag = cfg.get("tag_column", "tag")
        self.c_val = cfg.get("value_column", "value")
        self.c_q = cfg.get("quality_column")
        self.files = sorted(glob.glob(os.path.join(self.dir, "*.csv")))

    def _iter(self):
        scanned = 0
        for p in self.files:
            with open(p, newline="", encoding="utf-8-sig", errors="replace") as f:
                rd = csv.DictReader(f)
                for row in rd:
                    scanned += 1
                    if scanned > self.limits["max_rows_scanned"]:
                        return
                    if self.fmt == "long":
                        yield row.get(self.c_time), row.get(self.c_tag), row.get(self.c_val), row.get(self.c_q) if self.c_q else None
                    else:
                        t = row.get(self.c_time)
                        for k, v in row.items():
                            if k != self.c_time and v not in (None, ""):
                                yield t, k, v, None

    def _tags(self):
        if self.fmt == "wide":
            names = set()
            for p in self.files:
                with open(p, newline="", encoding="utf-8-sig", errors="replace") as f:
                    hdr = next(csv.reader(f), [])
                names.update(h for h in hdr if h != self.c_time)
            return sorted(names)
        return sorted(set(r[1] for r in self._iter() if r[1]))

    def list_tags(self, limit):
        return [{"tag": t} for t in self._tags()[:limit]]

    def search_tags(self, pattern, limit):
        pat = pattern if any(c in pattern for c in "*?") else "*%s*" % pattern
        return [{"tag": t} for t in self._tags() if fnmatch.fnmatch(t.lower(), pat.lower())][:limit]

    def latest(self, tag):
        best = None
        for t, k, v, q in self._iter():
            if k == tag and t:
                d = parse_time(t)
                if best is None or d >= best[0]:
                    best = (d, v, q)
        if best is None:
            return {"tag": tag, "error": "no samples"}
        return {"tag": tag, "time": iso(best[0]), "value": num(best[1]), "quality": best[2]}

    def query_range(self, tag, start, end):
        out = []
        for t, k, v, q in self._iter():
            if k == tag and t:
                d = parse_time(t)
                if start <= d <= end:
                    out.append((iso(d), num(v)))
        out.sort()
        return out


CREDENTIAL_KEYS = {"uid", "user", "username", "pwd", "password", "passwd", "secret", "token", "connection_string",
                   "private_key", "certificate"}
BACKENDS = {"sqlite": SqliteBackend, "csv_dir": CsvDirBackend, "odbc": OdbcBackend, "opcua": OpcUaBackend}

# Values the user chose to share through the plugin settings (Claude Code userConfig).
# Claude Code substitutes ${user_config.KEY} into this server's environment; nothing
# else in the environment is read.
SHARED = {}


def load_shared():
    """Read the three values Claude Code passes from the plugin settings.

    plugin.json maps them explicitly:
      RUNGSTONE_ODBC_CONNECTION  <- ${user_config.odbc_connection}   (sensitive)
      RUNGSTONE_OPCUA_USERNAME   <- ${user_config.opcua_username}
      RUNGSTONE_OPCUA_PASSWORD   <- ${user_config.opcua_password}    (sensitive)
    Nothing else in the environment is read.
    """
    values = {"odbc_connection": os.environ.get("RUNGSTONE_ODBC_CONNECTION", ""),
              "opcua_username": os.environ.get("RUNGSTONE_OPCUA_USERNAME", ""),
              "opcua_password": os.environ.get("RUNGSTONE_OPCUA_PASSWORD", "")}
    for key, v in values.items():
        if v and not v.startswith("${"):  # unsubstituted placeholder = not provided
            SHARED[key] = v


def reject_credentials(cfg, where):
    bad = [k for k in cfg if k.lower() in CREDENTIAL_KEYS]
    if bad:
        raise ToolError("%s config must not contain credentials (%s); enter them in the plugin settings instead"
                        % (where, ", ".join(bad)))

# ---------------------------------------------------------------------------
# MCP plumbing
# ---------------------------------------------------------------------------

TOOLS = [
    {"name": "list_tags", "description": "List historian tag names (read-only).",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 500}},
                     "additionalProperties": False}},
    {"name": "search_tags", "description": "Find tags by name pattern (* and ? wildcards; plain text matches anywhere).",
     "inputSchema": {"type": "object", "properties": {"pattern": {"type": "string", "minLength": 1, "maxLength": 200},
                                                     "limit": {"type": "integer", "minimum": 1, "maximum": 500}},
                     "required": ["pattern"], "additionalProperties": False}},
    {"name": "latest", "description": "Most recent sample of one tag (read-only).",
     "inputSchema": {"type": "object", "properties": {"tag": {"type": "string", "minLength": 1, "maxLength": 300}},
                     "required": ["tag"], "additionalProperties": False}},
    {"name": "query_range", "description": "Samples for one or more tags between start and end (ISO 8601). "
                                           "Large results are min/max downsampled so spikes and trips stay visible.",
     "inputSchema": {"type": "object", "properties": {
         "tags": {"type": "array", "items": {"type": "string", "maxLength": 300}, "minItems": 1, "maxItems": 20},
         "start": {"type": "string"}, "end": {"type": "string"},
         "max_points": {"type": "integer", "minimum": 10, "maximum": 20000}},
         "required": ["tags", "start", "end"], "additionalProperties": False}},
]


class Server(object):
    def __init__(self, cfg, disabled_reason=None):
        self.disabled = disabled_reason
        if disabled_reason:
            self.cfg, self.limits, self.backend_name, self._backend = {}, dict(DEFAULT_LIMITS), None, None
            return
        self.cfg = cfg
        self.limits = dict(DEFAULT_LIMITS, **cfg.get("limits", {}))
        name = cfg.get("backend")
        if name not in BACKENDS:
            raise ToolError("config 'backend' must be one of %s" % ", ".join(sorted(BACKENDS)))
        self.backend_name = name
        self._backend = None

    @property
    def backend(self):
        if self._backend is None:
            self._backend = BACKENDS[self.backend_name](self.cfg.get(self.backend_name, {}), self.limits)
        return self._backend

    def call(self, name, args):
        if self.disabled:
            raise ToolError("historian connector is disabled: %s" % self.disabled)
        args = args or {}
        lim = self.limits
        if name == "list_tags":
            n = min(int(args.get("limit", lim["max_list"])), lim["max_list"])
            return {"tags": self.backend.list_tags(n), "read_only": True}
        if name == "search_tags":
            pat = str(args.get("pattern", ""))
            if not pat:
                raise ToolError("pattern is required")
            n = min(int(args.get("limit", lim["max_list"])), lim["max_list"])
            return {"tags": self.backend.search_tags(pat, n), "read_only": True}
        if name == "latest":
            tag = str(args.get("tag", ""))
            if not tag:
                raise ToolError("tag is required")
            return self.backend.latest(tag)
        if name == "query_range":
            tags = args.get("tags")
            if isinstance(tags, str):
                tags = [tags]
            if not tags or not isinstance(tags, list):
                raise ToolError("tags must be a non-empty list")
            if len(tags) > lim["max_tags"]:
                raise ToolError("at most %d tags per query" % lim["max_tags"])
            start, end = parse_time(args.get("start")), parse_time(args.get("end"))
            if start is None or end is None or end < start:
                raise ToolError("start and end are required and end must be after start")
            mp = min(int(args.get("max_points", lim["max_points"])), lim["max_points"])
            series = {}
            for t in tags:
                pts = self.backend.query_range(str(t), start, end)
                ds, was = downsample(pts, mp)
                series[t] = {"points": [[a, b] for a, b in ds], "count_raw": len(pts), "downsampled": was}
            return {"start": iso(start), "end": iso(end), "series": series, "read_only": True}
        raise ToolError("unknown tool %r" % name)

    def handle(self, msg):
        mid = msg.get("id")
        method = msg.get("method")
        if method is None:
            return None  # a response from the client; ignore
        if mid is None:
            return None  # notification (e.g. notifications/initialized)
        try:
            if method == "initialize":
                req = (msg.get("params") or {}).get("protocolVersion")
                ver = req if req in PROTOCOLS else PROTOCOLS[0]
                return ok(mid, {"protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}},
                                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                                "instructions": (("Rungstone historian connector is DISABLED (%s). It offers no tools. "
                                                  "To enable it, with plant IT/OT approval, set 'Historian connector config' in "
                                                  "the Rungstone plugin settings to a config file with \"enabled\": true."
                                                  % self.disabled) if self.disabled else
                                                 "Read-only historian access. No write operations exist. "
                                                 "Use query_range around an event, then explain it with the PLC logic.")})
            if method == "ping":
                return ok(mid, {})
            if method == "tools/list":
                return ok(mid, {"tools": [] if self.disabled else TOOLS})
            if method == "tools/call":
                p = msg.get("params") or {}
                try:
                    res = self.call(p.get("name"), p.get("arguments"))
                    return ok(mid, {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
                                    "isError": False})
                except (ToolError, ValueError, sqlite3.Error) as e:
                    return ok(mid, {"content": [{"type": "text", "text": "error: %s" % e}], "isError": True})
            return err(mid, -32601, "method not found: %s" % method)
        except Exception as e:  # noqa: BLE001 - never crash the stdio loop
            return err(mid, -32603, "internal error: %s" % e)


def ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def load_config(argv=None):
    """Return (config, None) when enabled, or (None, reason) when the connector stays off."""
    argv = sys.argv[1:] if argv is None else argv
    path = argv[argv.index("--config") + 1] if "--config" in argv and argv.index("--config") + 1 < len(argv) else ""
    if not path or path.startswith("${"):
        return None, "no config file set"
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if cfg.get("enabled") is not True:
        return None, "\"enabled\" is not true in %s" % os.path.basename(path)
    return cfg, None


def main():
    load_shared()
    try:
        cfg, reason = load_config()
        srv = Server(cfg, reason)
    except (OSError, ValueError, ToolError) as e:
        srv = Server(None, "bad config: %s" % e)
    if srv.disabled:
        sys.stderr.write("rungstone-historian: disabled (%s); serving no tools\n" % srv.disabled)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = err(None, -32700, "parse error")
        else:
            if isinstance(msg, list):
                out = [r for r in (srv.handle(m) for m in msg if isinstance(m, dict)) if r] or None
            else:
                out = srv.handle(msg) if isinstance(msg, dict) else err(None, -32600, "invalid request")
        if out:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
