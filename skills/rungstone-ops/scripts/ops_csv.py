"""
ops_csv.py - streaming readers for historian, alarm and event exports.

Formats handled
  wide  : timestamp column + one column per tag           (PI DataLink, Ignition
          "wide" export, FactoryTalk Historian ExcelAdd-in, WinCC trend export)
  long  : timestamp, tag, value[, quality]                 (PI "Archive" export,
          Ignition tag history "tall", FactoryTalk Historian, most SQL dumps)
  events: timestamp + source/tag + message [+ priority, state]

Timestamps: ISO 8601 (with or without zone, 'T' or space, fractional seconds),
"MM/DD/YYYY hh:mm:ss[.fff] [AM|PM]", "DD.MM.YYYY hh:mm:ss[.fff]", "YYYY/MM/DD ...",
epoch seconds or milliseconds.  Times without a zone are kept as file time
(no conversion); zone-aware times are converted to UTC and flagged.

Nothing in a customer file is ever executed: values are only parsed as numbers,
booleans or strings.
"""

import csv
import datetime as _dt
import io
import os
import re

csv.field_size_limit(10 * 1024 * 1024)

TIME_HEADERS = re.compile(r"(?i)^(time|timestamp|date|datetime|date ?time|ts|t_stamp|time ?stamp|_time|event ?time|"
                          r"local ?time|utc ?time|occurrence ?time|alarm ?time)$")
TAG_HEADERS = re.compile(r"(?i)^(tag|tagname|tag ?name|tag ?path|name|point|pointname|point ?name|item|signal|"
                         r"variable|path|tagid)$")
VALUE_HEADERS = re.compile(r"(?i)^(value|val|v|floatvalue|intvalue|stringvalue|pv)$")
QUALITY_HEADERS = re.compile(r"(?i)^(quality|q|status|dataintegrity|quality ?code)$")
SOURCE_HEADERS = re.compile(r"(?i)^(source|tag|tagname|tag ?name|alarm|alarm ?name|name|point|object|area|"
                            r"source ?name|display ?path|tag ?path|condition)$")
MESSAGE_HEADERS = re.compile(r"(?i)^(message|msg|text|description|event|alarm ?text|alarm ?message|label|"
                             r"display ?text|condition ?text)$")
PRIORITY_HEADERS = re.compile(r"(?i)^(priority|severity|prio|class|alarm ?class|level)$")
STATE_HEADERS = re.compile(r"(?i)^(state|event ?type|status|transition|alarm ?state|action|event ?state)$")

TRUE = {"1", "true", "on", "yes", "running", "run", "open", "active", "t", "y", "1.0"}
FALSE = {"0", "false", "off", "no", "stopped", "stop", "closed", "inactive", "f", "n", "0.0"}

_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
    "%m/%d/%Y %H:%M:%S.%f", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M:%S.%f %p",
    "%m/%d/%Y %H:%M", "%d.%m.%Y %H:%M:%S.%f", "%d.%m.%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S",
    "%d-%b-%Y %H:%M:%S.%f", "%d-%b-%Y %H:%M:%S", "%d-%b-%y %H:%M:%S",
]
_EPOCH = _dt.datetime(1970, 1, 1)


class TimeParser(object):
    """Parses timestamps to float seconds on a single file-time axis."""

    def __init__(self):
        self.fmt = None
        self.zone_seen = False
        self.failures = 0

    def __call__(self, s):
        s = (s or "").strip().strip('"')
        if not s:
            return None
        if re.match(r"^\d{9,13}(\.\d+)?$", s):
            v = float(s)
            return v / 1000.0 if v > 1e11 else v
        zone = None
        m = re.search(r"(Z|[+-]\d{2}:?\d{2})$", s)
        if m and ("T" in s or "-" in s[:10]):
            zone = m.group(1)
            s = s[:m.start()].rstrip()
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)
        d = None
        if self.fmt:
            try:
                d = _dt.datetime.strptime(s, self.fmt)
            except ValueError:
                d = None
        if d is None:
            for f in _FORMATS:
                try:
                    d = _dt.datetime.strptime(s, f)
                    self.fmt = f
                    break
                except ValueError:
                    continue
        if d is None:
            self.failures += 1
            return None
        t = (d - _EPOCH).total_seconds()
        if zone:
            self.zone_seen = True
            if zone != "Z":
                sign = 1 if zone[0] == "+" else -1
                hh, mm = int(zone[1:3]), int(zone[-2:])
                t -= sign * (hh * 3600 + mm * 60)
        return t


def fmt_time(t, ms=False):
    if t is None:
        return None
    d = _EPOCH + _dt.timedelta(seconds=t)
    return d.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] if ms or abs(t - round(t)) > 1e-6 else d.strftime("%Y-%m-%dT%H:%M:%S")


_THOUSANDS = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$")
_DEC_COMMA = re.compile(r"^-?\d{1,3}(\.\d{3})*,\d+$|^-?\d+,\d+$")


def parse_value(s, decimal_comma=False):
    """Return (kind, value): kind in bool|num|str|none.

    decimal_comma: the file uses ',' as the decimal separator (typical for
    ';'-delimited European exports such as WinCC): "1.234,5" -> 1234.5.
    """
    if s is None:
        return "none", None
    v = s.strip()
    if v == "" or v.lower() in ("nan", "null", "none", "bad", "#n/a", "n/a", "shutdown", "no data", "i/o timeout"):
        return "none", None
    lv = v.lower()
    if lv in ("true", "on", "false", "off"):
        return "bool", 1.0 if lv in ("true", "on") else 0.0
    if decimal_comma and _DEC_COMMA.match(v):
        return "num", float(v.replace(".", "").replace(",", "."))
    if _THOUSANDS.match(v):
        return "num", float(v.replace(",", ""))
    try:
        return "num", float(v)
    except ValueError:
        return "str", v


def open_text(path):
    f = open(path, "rb")
    head = f.read(4)
    f.seek(0)
    enc = "utf-8-sig"
    if head.startswith(b"\xff\xfe") or head.startswith(b"\xfe\xff"):
        enc = "utf-16"
    return io.TextIOWrapper(f, encoding=enc, errors="replace", newline="")


def sniff(path):
    """Return dict describing the CSV layout (reads only the first ~64 KiB)."""
    with open_text(path) as f:
        sample = f.read(65536)
    lines = [l for l in sample.splitlines() if l.strip()]
    skip = 0
    while skip < len(lines) and (lines[skip].startswith(("#", ";", "//")) or "," not in lines[skip] and ";" not in lines[skip]
                                 and "\t" not in lines[skip]):
        skip += 1
    body = "\n".join(lines[skip:skip + 50])
    try:
        dialect = csv.Sniffer().sniff(body, delimiters=",;\t|")
        delim = dialect.delimiter
    except csv.Error:
        delim = ","
    rows = list(csv.reader(io.StringIO(body), delimiter=delim))
    if not rows:
        return {"layout": "empty", "delimiter": delim, "skip": skip, "header": []}
    header = [h.strip() for h in rows[0]]
    ti = next((i for i, h in enumerate(header) if TIME_HEADERS.match(h)), 0)
    tag_i = next((i for i, h in enumerate(header) if TAG_HEADERS.match(h) and i != ti), None)
    val_i = next((i for i, h in enumerate(header) if VALUE_HEADERS.match(h) and i != ti), None)
    q_i = next((i for i, h in enumerate(header) if QUALITY_HEADERS.match(h) and i not in (ti, tag_i, val_i)), None)
    msg_i = next((i for i, h in enumerate(header) if MESSAGE_HEADERS.match(h) and i != ti), None)
    info = {"delimiter": delim, "skip": skip, "header": header, "time_col": ti}
    if tag_i is not None and val_i is not None:
        info.update(layout="long", tag_col=tag_i, value_col=val_i, quality_col=q_i)
    elif msg_i is not None:
        src_i = next((i for i, h in enumerate(header) if SOURCE_HEADERS.match(h) and i not in (ti, msg_i)), None)
        pr_i = next((i for i, h in enumerate(header) if PRIORITY_HEADERS.match(h) and i not in (ti, msg_i, src_i)), None)
        st_i = next((i for i, h in enumerate(header) if STATE_HEADERS.match(h) and i not in (ti, msg_i, src_i, pr_i)), None)
        info.update(layout="events", source_col=src_i, message_col=msg_i, priority_col=pr_i, state_col=st_i)
    else:
        qcols = [i for i, h in enumerate(header) if re.search(r"(?i)(quality|_q$|\.q$|status$)", h) and i != ti]
        info.update(layout="wide", tag_cols=[i for i in range(len(header)) if i != ti and i not in qcols],
                    quality_cols=qcols)
    return info


def _rows(path, info):
    f = open_text(path)
    try:
        n = 0
        skipped = 0
        r = csv.reader(f, delimiter=info["delimiter"])
        for row in r:
            if not row or not any(c.strip() for c in row):
                continue
            if skipped < info["skip"]:
                skipped += 1
                continue
            n += 1
            if n == 1:
                continue  # header
            yield row
    finally:
        f.close()


def iter_samples(path, info=None, tags=None, start=None, end=None, tp=None):
    """Yield (t, tag, kind, value, quality) in file order; streaming."""
    info = info or sniff(path)
    tp = tp or TimeParser()
    want = set(t.lower() for t in tags) if tags else None
    dc = info.get("delimiter") == ";"
    hdr = info["header"]
    ti = info["time_col"]
    if info["layout"] == "wide":
        cols = [(i, hdr[i]) for i in info["tag_cols"] if want is None or hdr[i].lower() in want]
        qmap = {}
        for qi in info.get("quality_cols", []):
            base = re.sub(r"(?i)([ _.]?quality|_q|\.q|[ _.]?status)$", "", hdr[qi])
            qmap[base.lower()] = qi
        for row in _rows(path, info):
            if ti >= len(row):
                continue
            t = tp(row[ti])
            if t is None or (start is not None and t < start):
                continue
            if end is not None and t > end:
                break
            for i, name in cols:
                if i < len(row):
                    k, v = parse_value(row[i], dc)
                    qi = qmap.get(name.lower())
                    yield t, name, k, v, (row[qi].strip() if qi is not None and qi < len(row) else None)
    elif info["layout"] == "long":
        tg, vi, qi = info["tag_col"], info["value_col"], info.get("quality_col")
        for row in _rows(path, info):
            if max(ti, tg, vi) >= len(row):
                continue
            name = row[tg].strip()
            if want is not None and name.lower() not in want:
                continue
            t = tp(row[ti])
            if t is None or (start is not None and t < start) or (end is not None and t > end):
                continue  # long files are not always time-sorted
            k, v = parse_value(row[vi], dc)
            yield t, name, k, v, (row[qi].strip() if qi is not None and qi < len(row) else None)
    else:
        raise ValueError("%s looks like an event/alarm log, not a historian export" % os.path.basename(path))


def iter_events(path, info=None, start=None, end=None, tp=None):
    """Yield dicts {t, source, message, priority, state} from an alarm/event log."""
    info = info or sniff(path)
    if info["layout"] != "events":
        # tolerate a long-format log whose 'value' column carries the message
        if info["layout"] == "long":
            info = dict(info, layout="events", source_col=info["tag_col"], message_col=info["value_col"],
                        priority_col=None, state_col=None)
        else:
            raise ValueError("%s has no message/description column; not an event log" % os.path.basename(path))
    tp = tp or TimeParser()
    g = lambda row, i: row[i].strip() if i is not None and i < len(row) else None
    for row in _rows(path, info):
        t = tp(g(row, info["time_col"]))
        if t is None or (start is not None and t < start) or (end is not None and t > end):
            continue
        yield {"t": t, "source": g(row, info.get("source_col")), "message": g(row, info["message_col"]),
               "priority": g(row, info.get("priority_col")), "state": g(row, info.get("state_col"))}


def parse_time_arg(s, tp=None):
    """Parse a CLI time: absolute timestamp, or None."""
    if s is None:
        return None
    t = (tp or TimeParser())(s)
    if t is None:
        raise SystemExit("cannot parse time %r" % s)
    return t


def parse_duration(s):
    """'90', '90s', '15m', '2h', '1d' -> seconds."""
    if s is None:
        return None
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", str(s), re.I)
    if not m:
        raise SystemExit("cannot parse duration %r (use e.g. 90s, 15m, 2h)" % s)
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2).lower()]


def zone_note(tp):
    return ("timestamps carried a UTC offset and were converted to UTC" if tp.zone_seen
            else "times are as written in the file (no time zone in the data; confirm site local time vs UTC)")
