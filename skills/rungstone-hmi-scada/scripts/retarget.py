"""
retarget.py - rebind HMI/SCADA references to the new PLC tags using the
mapping.csv produced by the rungstone-plc Translate workflow.

  * originals are never modified; rebound copies are written to --out
  * FactoryTalk {[shortcut]tag}, [shortcut]tag, OPC "ns=..;s=[dev]tag",
    Siemens "DB".member and SLC addresses are recognised
  * replacement is format-aware: XML attribute escaping, JSON re-serialised
    (strings only), CSV rewritten cell by cell, scripts edited only when the
    new reference needs no quoting changes (otherwise flagged for manual edit)
  * timer/counter members are re-mapped by meaning, e.g. Logix Timer.ACC ->
    a new DINT tag <new>_ACC_ms (IEC ET is TIME), Timer.DN -> <new>.Q; each
    such rebind is added to the review queue (.rungstone/review_queue.json)
  * Ignition tag paths ([default]Folder/Tag) are HMI-internal and left alone;
    rebind the Ignition tag export's OPC item paths instead

Usage:
  python3 retarget.py --mapping .rungstone/mapping.csv --hmi FILE_OR_DIR... --out rebound/
         [--target siemens-scl|iec-st|rockwell] [--device OLD=NEW ...] [--state DIR] [--no-queue]
         [--md report.md] [-o report.json]
"""

import argparse
import csv
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hmi_common as hc  # noqa: E402

TEXT_EXT = (".xml", ".json", ".csv", ".vbs", ".vba", ".bas", ".vb", ".py", ".jy", ".js", ".txt")
SCRIPT_EXT = (".vbs", ".vba", ".bas", ".vb", ".py", ".jy", ".js", ".txt")


class Rebinder(object):
    def __init__(self, mapping, target, devices):
        self.mapping = mapping
        self.family = hc.target_family(target)
        self.devices = devices
        self.rebinds, self.unmapped, self.internal, self.manual, self.reviews = [], [], [], [], []

    def new_ref(self, ref):
        """Return (new_path, info) or (None, reason)."""
        key = hc.norm_path(ref["path"])
        row = self.mapping.get(key)
        if row:
            return row["new"], {"basis": row.get("basis") or "mapping row", "rule": "exact"}
        base, member = hc.split_member(key)
        row = self.mapping.get(hc.norm_path(base))
        if not row:
            return None, "not in mapping"
        new_base = row["new"]
        if not member:
            return new_base, {"basis": row.get("basis") or "mapping row", "rule": "exact"}
        kind = hc.kind_of(row.get("old_type"), base)
        m0 = member.split(".")[0]
        rule = hc.REBIND.get((kind, m0, self.family)) if kind else None
        if rule:
            newm, suffix, ntype, risk, why = rule
            if newm:
                new = "%s.%s" % (new_base, newm)
            else:
                new = new_base + suffix
            why = why.replace("<new>", new_base)
            return new, {"basis": row.get("basis") or "mapping row", "rule": "%s .%s" % (kind, m0.upper()),
                         "review": {"risk": risk, "issue": why, "new_type": ntype,
                                    "fix": ("Create %s (%s) in the PLC and keep it updated in logic; HMI rebound to it." % (new, ntype))
                                    if not newm else "Confirm %s is exposed to the HMI (multi-instance / DB member must be HMI accessible)." % new}}
        return "%s.%s" % (new_base, member), {"basis": row.get("basis") or "mapping row", "rule": "member carried over",
                                               "review": {"risk": "medium", "new_type": None,
                                                          "issue": "Member .%s carried over unchanged; it may not exist on the target type." % member,
                                                          "fix": "Confirm %s.%s exists on the target or map it explicitly in mapping.csv." % (new_base, member)}}

    def render(self, ref, new):
        dev = ref.get("device")
        if dev and dev in self.devices:
            dev = self.devices[dev]
        raw = ref["raw"]
        if ref["kind"] == "factorytalk":
            return "{%s[%s]%s}" % ("::" if raw.startswith("{::") else "", dev, new)
        if ref["kind"] == "opc":
            prefix = re.match(r"ns=\d+;s=", raw).group(0)
            return prefix + ("[%s]" % dev if dev else "") + new
        if ref["kind"] == "shortcut":
            return "%s[%s]%s" % ("::" if raw.startswith("::") else "", dev, new)
        return new

    def process(self, text, where_fn, esc, script=False, siemens="auto"):
        refs = hc.find_refs(text, siemens)
        out, last = [], 0
        for r in refs:
            if r["kind"] == "shortcut" and "/" in r["path"]:
                self.internal.append({"where": where_fn(r["span"][0]), "ref": r["raw"]})
                continue
            new, info = self.new_ref(r)
            loc = where_fn(r["span"][0])
            if new is None:
                self.unmapped.append({"where": loc, "ref": r["raw"], "reason": info})
                continue
            rendered = self.render(r, new)
            if script and '"' in rendered and '"' not in r["raw"]:
                self.manual.append({"where": loc, "old": r["raw"], "new": rendered,
                                    "reason": "new reference contains quotes; edit the script string by hand"})
                continue
            out.append(text[last:r["span"][0]])
            out.append(esc(rendered))
            last = r["span"][1]
            rec = {"where": loc, "old": r["raw"], "new": rendered, "rule": info["rule"], "basis": info["basis"]}
            if info.get("review"):
                rec["review"] = info["review"]
                self.reviews.append({"location": loc, "old": r["raw"], "new": rendered, **info["review"]})
            self.rebinds.append(rec)
        out.append(text[last:])
        return "".join(out)


def xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def line_fn(fname, text):
    def where(pos):
        return "%s line %d" % (fname, text.count("\n", 0, pos) + 1)
    return where


def retarget_file(rb, src, dst):
    ext = os.path.splitext(src)[1].lower()
    fname = os.path.basename(src)
    text = hc.read_text(src)
    if ext == ".json":
        data = json.loads(text)
        counter = {"n": 0}

        def walk(o, path):
            if isinstance(o, dict):
                return dict((k, walk(v, path + "/" + k)) for k, v in o.items())
            if isinstance(o, list):
                return [walk(v, "%s[%d]" % (path, i)) for i, v in enumerate(o)]
            if isinstance(o, str) and o:
                return rb.process(o, lambda pos, p=path: "%s %s" % (fname, p), lambda s: s,
                                  script=path.endswith("/script"))
            return o

        new = walk(data, "")
        out = json.dumps(new, indent=2, ensure_ascii=False) + "\n"
    elif ext == ".csv":
        rows, delim = hc.sniff_rows(text)
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=delim, lineterminator="\n")
        for i, row in enumerate(rows, start=1):
            w.writerow([rb.process(c, lambda pos, i=i, j=j: "%s row %d col %d" % (fname, i, j + 1), lambda s: s)
                        for j, c in enumerate(row)])
        out = buf.getvalue()
    elif ext in SCRIPT_EXT:
        out = rb.process(text, line_fn(fname, text), lambda s: s, script=True, siemens=False)
    else:
        out = rb.process(text, line_fn(fname, text), xml_escape, siemens=False)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8", newline="") as f:
        f.write(out)


def gather(paths):
    for p in paths:
        if os.path.isdir(p):
            for dp, _, fns in os.walk(p):
                for fn in sorted(fns):
                    if fn.lower().endswith(TEXT_EXT) and not fn.startswith("."):
                        full = os.path.join(dp, fn)
                        yield full, os.path.relpath(full, p)
        else:
            yield p, os.path.basename(p)


def to_md(rep):
    L = ["# HMI/SCADA retarget report", "",
         "%d references rebound, %d unmapped, %d need manual edit, %d review items. Target family: %s. "
         "Rebound copies in `%s` (originals untouched)." % (len(rep["rebinds"]), len(rep["unmapped"]), len(rep["manual"]),
                                                           len(rep["review_items"]), rep["target_family"], rep["out"]),
         "", "## Rebinds", "", "| Where | Old | New | Rule | Review |", "|---|---|---|---|---|"]
    for r in rep["rebinds"]:
        L.append("| %s | `%s` | `%s` | %s | %s |" % (r["where"], r["old"], r["new"], r["rule"],
                                                   (r.get("review_id") or "") + (" " + r["review"]["risk"] if r.get("review") else "")))
    L += ["", "## Could not be mapped (left unchanged)", "", "| Where | Reference | Reason |", "|---|---|---|"]
    for u in rep["unmapped"]:
        L.append("| %s | `%s` | %s |" % (u["where"], u["ref"], u["reason"]))
    if not rep["unmapped"]:
        L.append("| - | - | none |")
    if rep["manual"]:
        L += ["", "## Needs a manual edit", "", "| Where | Old | Proposed | Why |", "|---|---|---|---|"]
        for m in rep["manual"]:
            L.append("| %s | `%s` | `%s` | %s |" % (m["where"], m["old"], m["new"], m["reason"]))
    if rep["internal"]:
        L += ["", "## HMI-internal paths (not PLC references, unchanged)", ""]
        L += ["- %s: `%s`" % (i["where"], i["ref"]) for i in rep["internal"]]
    L += ["", "Nothing is final until the engineer approves the review items. Re-import the rebound files into the HMI "
              "project in a test copy; do not deploy to a running HMI/SCADA without testing."]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mapping", required=True)
    ap.add_argument("--hmi", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--target", choices=["siemens-scl", "iec-st", "rockwell"], default="siemens-scl")
    ap.add_argument("--device", action="append", default=[], metavar="OLD=NEW", help="rename an HMI device/shortcut")
    ap.add_argument("--state", help="project folder holding .rungstone/ (default: search upward)")
    ap.add_argument("--no-queue", action="store_true", help="do not add review items to the review queue")
    ap.add_argument("--md")
    ap.add_argument("-o", "--output")
    args = ap.parse_args(argv)
    mapping = hc.load_mapping(args.mapping)
    devices = dict(d.split("=", 1) for d in args.device if "=" in d)
    rb = Rebinder(mapping, args.target, devices)
    written = []
    out_abs = os.path.abspath(args.out)
    for src, rel in gather(args.hmi):
        dst = os.path.join(args.out, rel)
        if os.path.abspath(dst) == os.path.abspath(src) or os.path.abspath(src).startswith(out_abs + os.sep):
            sys.exit("refusing to overwrite an original: %s" % src)
        if src.lower().endswith((".xlsx", ".xlsm")):
            rb.manual.append({"where": src, "old": "", "new": "", "reason": "workbooks are not rewritten; export the sheet to CSV"})
            continue
        try:
            retarget_file(rb, src, dst)
            written.append(dst)
        except (ValueError, UnicodeDecodeError) as e:
            rb.manual.append({"where": src, "old": "", "new": "", "reason": "could not process: %s" % e})
    review_ids = []
    if not args.no_queue and rb.reviews:
        d = hc.find_state_dir(args.state)
        if d:
            from _shared import state
            existing = set((i.get("location"), i.get("issue")) for i in state.queue(d)["items"])
            for r in rb.reviews:
                issue = "HMI rebind %s -> %s: %s" % (r["old"], r["new"], r["issue"])
                if (r["location"], issue) in existing:
                    continue
                item = state.queue_add(d, r["risk"], r["location"], issue, r["fix"], workflow="hmi-retarget")
                review_ids.append(item["id"])
                for rec in rb.rebinds:
                    if rec["where"] == r["location"] and rec["new"] == r["new"]:
                        rec["review_id"] = item["id"]
            state.log(d, "hmi.retarget", "%d rebinds, %d unmapped, %d review items" % (len(rb.rebinds), len(rb.unmapped), len(review_ids)))
    rep = {"tool": "rungstone/retarget", "target_family": rb.family, "out": args.out, "files_written": written,
           "rebinds": rb.rebinds, "unmapped": rb.unmapped, "manual": rb.manual, "internal": rb.internal,
           "review_items": rb.reviews, "review_queue_ids": review_ids,
           "note": "Draft rebinds. The engineer approves every flagged change before export."}
    s = json.dumps(rep, indent=2, ensure_ascii=False)
    if args.output:
        open(args.output, "w", encoding="utf-8").write(s)
    else:
        sys.stdout.write(s + "\n")
    if args.md:
        open(args.md, "w", encoding="utf-8").write(to_md(rep))
    sys.stderr.write("retarget: %d rebound, %d unmapped, %d manual, %d review items -> %s\n" % (
        len(rb.rebinds), len(rb.unmapped), len(rb.manual), len(rb.reviews), args.out))


if __name__ == "__main__":
    main()
