#!/usr/bin/env python3
"""
migrate_tags.py - migrate an HMI/SCADA tag database, alarms and scripts to a
target platform as DRAFT files; screens as draft layout descriptions only.

Targets (--to):
  ignition-json    Ignition tag export JSON (folders, OPC tags, tag alarms)
  wincc-csv        WinCC (TIA) HMI tag CSV + discrete/analog alarm CSV
  factorytalk-csv  FactoryTalk View-style tag CSV + alarm CSV

Optional --mapping mapping.csv rebinds PLC references to the new PLC (same
rules as retarget.py: Timer.ACC -> new DINT tag etc.); every rebind that
needs engineering judgement is listed in the report.

Every output file is named DRAFT_* and a README_DRAFT.md explains what must
be checked before import.  Scripts are NOT translated automatically: each
gets a stub with the original code, the references it touches and the
rebinds, ready for a reviewed port.  Screens are wireframes (HTML + Markdown)
showing object positions, labels and bindings - not importable screens.

Usage:
  python3 migrate_tags.py --hmi FILE_OR_DIR... --to ignition-json --out migrated/
         [--mapping mapping.csv --target siemens-scl] [--device NAME] [--opc-server NAME]
         [--screens] [-o report.json]
"""

import argparse
import csv
import html
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hmi_common as hc  # noqa: E402
import parse_hmi  # noqa: E402
import retarget  # noqa: E402

CANON = [(r"^(bool|boolean|digital|d|bit)$", "bool"), (r"^(int2|int|int16|short|word|sint|byte|usint|uint)$", "int16"),
         (r"^(int4|dint|int32|long|dword|udint|integer|a|analog)$", "int32"), (r"^(int8|lint|int64)$", "int64"),
         (r"^(float4|real|float|single|float32)$", "float32"), (r"^(float8|lreal|double|float64)$", "float64"),
         (r"^(string|wstring|s|text)$", "string"), (r"^(datetime|date_and_time|dtl|ldt)$", "datetime")]
TARGET_TYPES = {
    "ignition-json": {"bool": "Boolean", "int16": "Int2", "int32": "Int4", "int64": "Int8", "float32": "Float4",
                      "float64": "Float8", "string": "String", "datetime": "DateTime", "unknown": "Int4"},
    "wincc-csv": {"bool": "Bool", "int16": "Int", "int32": "DInt", "int64": "LInt", "float32": "Real", "float64": "LReal",
                  "string": "WString", "datetime": "DTL", "unknown": "DInt"},
    "factorytalk-csv": {"bool": "D", "int16": "A", "int32": "A", "int64": "A", "float32": "A", "float64": "A",
                        "string": "S", "datetime": "S", "unknown": "A"},
}
MEMBER_TYPES = {"acc": "int32", "pre": "int32", "dn": "bool", "en": "bool", "tt": "bool", "cu": "bool", "cd": "bool",
                "ov": "bool", "un": "bool", "q": "bool", "et": "int32", "pt": "int32", "cv": "int32", "pv": "int32"}
PRIO = {"ignition-json": {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low", "diagnostic": "Diagnostic"},
        "wincc-csv": {"critical": "Errors", "high": "Errors", "medium": "Warnings", "low": "Warnings", "diagnostic": "System"},
        "factorytalk-csv": {"critical": "1000", "high": "750", "medium": "500", "low": "250", "diagnostic": "1"}}


def canon_type(dt, path):
    d = (dt or "").strip().lower()
    for rx, c in CANON:
        if re.match(rx, d):
            return c
    mem = (path or "").lower().rsplit(".", 1)[-1] if "." in (path or "") else ""
    if mem in MEMBER_TYPES:
        return MEMBER_TYPES[mem]
    if re.search(r"/\d+$", path or ""):
        return "bool"
    return "unknown"


def prio_bucket(p):
    s = (str(p) if p is not None else "").lower()
    for k in ("critical", "urgent", "high", "error", "medium", "warning", "low", "diagnostic"):
        if k in s:
            return {"urgent": "critical", "error": "high", "warning": "medium"}.get(k, k)
    return "medium"


def ign_safe(name):
    return re.sub(r"[^\w -]", "_", name).strip() or "Tag"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hmi", nargs="+", required=True)
    ap.add_argument("--to", required=True, choices=sorted(TARGET_TYPES))
    ap.add_argument("--out", required=True)
    ap.add_argument("--mapping")
    ap.add_argument("--target", choices=["siemens-scl", "iec-st", "rockwell"], default="siemens-scl")
    ap.add_argument("--device", default="NEW_PLC", help="target device / connection / shortcut name")
    ap.add_argument("--opc-server", default="Ignition OPC UA Server")
    ap.add_argument("--screens", action="store_true", help="also write draft screen wireframes")
    ap.add_argument("-o", "--output")
    args = ap.parse_args(argv)
    inv = parse_hmi.parse_paths(args.hmi)
    rb = retarget.Rebinder(hc.load_mapping(args.mapping), args.target, {}) if args.mapping else None
    os.makedirs(args.out, exist_ok=True)
    report = {"tool": "rungstone/migrate_tags", "to": args.to, "out": args.out, "draft": True, "tags": [], "alarms": [],
              "rebinds": [], "unmapped": [], "review": [], "files": [], "warnings": inv["warnings"]}

    def plc_path(ref):
        if not ref:
            return None, None
        if rb is None:
            return ref["path"], None
        fake = {"path": ref["path"], "device": ref.get("device"), "kind": "plain", "raw": ref["path"]}
        new, info = rb.new_ref(fake)
        if new is None:
            report["unmapped"].append({"ref": ref["path"], "reason": info})
            return ref["path"], None
        report["rebinds"].append({"old": ref["path"], "new": new, "rule": info["rule"]})
        if info.get("review"):
            report["review"].append(dict(info["review"], old=ref["path"], new=new))
        return new, info.get("review", {}).get("new_type")

    tags = []
    seen = set()
    for t in inv["tags"]:
        key = (t.get("name") or "").lower()
        if key in seen:
            continue
        seen.add(key)
        ref = t.get("plc_ref")
        new, new_type = plc_path(ref)
        ct = canon_type(new_type or t.get("data_type"), ref["path"] if ref else None)
        alarms = [a for a in inv["alarms"] if (a.get("tag") or "").lower() == key or
                  (a.get("plc_ref") and ref and a["plc_ref"].get("norm") == ref.get("norm"))]
        tags.append({"name": t.get("name"), "plc": new, "type": ct, "description": t.get("description"),
                     "read_only": t.get("access") == "read", "memory": ref is None, "alarms": alarms})
        report["tags"].append({"name": t.get("name"), "plc": new, "type": TARGET_TYPES[args.to][ct],
                               "type_basis": "declared" if t.get("data_type") else "inferred from member/address"})
    orphan_alarms = [a for a in inv["alarms"] if not any(a in t["alarms"] for t in tags)]

    if args.to == "ignition-json":
        root = {"name": "", "tagType": "Provider", "tags": []}
        folders = {}
        for t in tags:
            parts = re.split(r"[\\/]", t["name"])
            parent = root
            for f in parts[:-1]:
                k = id(parent), f
                if k not in folders:
                    node = {"name": ign_safe(f), "tagType": "Folder", "tags": []}
                    parent["tags"].append(node)
                    folders[k] = node
                parent = folders[k]
            tag = {"name": ign_safe(parts[-1]), "tagType": "AtomicTag", "dataType": TARGET_TYPES[args.to][t["type"]]}
            if t["memory"]:
                tag["valueSource"] = "memory"
            else:
                tag.update({"valueSource": "opc", "opcServer": args.opc_server,
                            "opcItemPath": "ns=1;s=[%s]%s" % (args.device, t["plc"])})
            if t["description"]:
                tag["documentation"] = t["description"]
            if t["read_only"]:
                tag["readOnly"] = True
            if t["alarms"]:
                tag["alarms"] = []
                for a in t["alarms"]:
                    sp = a.get("setpoint")
                    tag["alarms"].append({"name": a.get("name") or "Alarm", "mode": "Equality" if sp in (None, 1, 1.0, "1") else "AboveValue",
                                          "setpointA": float(sp) if sp not in (None, "") and re.match(r"^-?[\d.]+$", str(sp)) else 1.0,
                                          "priority": PRIO[args.to][prio_bucket(a.get("priority"))],
                                          "label": a.get("message") or a.get("name"), "notes": a.get("notes") or ""})
                    report["alarms"].append({"alarm": a.get("name"), "tag": t["name"]})
            parent["tags"].append(tag)
        p = os.path.join(args.out, "DRAFT_tags_ignition.json")
        json.dump(root, open(p, "w", encoding="utf-8"), indent=2)
        report["files"].append(p)
    elif args.to == "wincc-csv":
        p = os.path.join(args.out, "DRAFT_HMITags.csv")
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["Name", "Path", "Connection", "PLC tag", "DataType", "Access Method", "Address", "Acquisition cycle", "Comment"])
            for t in tags:
                absolute = bool(t["plc"] and re.match(r"^%?[IQM][XBWD]?\d", t["plc"]))
                w.writerow([re.sub(r"[\\/]", "_", t["name"]), "Default tag table", "" if t["memory"] else args.device,
                            "" if (t["memory"] or absolute) else t["plc"], TARGET_TYPES[args.to][t["type"]],
                            "" if t["memory"] else ("Absolute access" if absolute else "Symbolic access"),
                            t["plc"] if absolute else "", "1 s", t["description"] or ""])
        report["files"].append(p)
        p = os.path.join(args.out, "DRAFT_Alarms.csv")
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["ID", "Name", "Alarm text", "Alarm class", "Trigger tag", "Trigger bit / limit", "Info text", "Rungstone note"])
            n = 0
            for t in tags:
                for a in t["alarms"]:
                    n += 1
                    w.writerow([n, a.get("name"), a.get("message"), PRIO[args.to][prio_bucket(a.get("priority"))],
                                re.sub(r"[\\/]", "_", t["name"]), a.get("trigger_bit") or a.get("setpoint") or "",
                                a.get("notes") or "", "BOOL trigger: WinCC discrete alarms need a Word/Int trigger tag + bit - pack in PLC or use analog alarm"])
                    report["alarms"].append({"alarm": a.get("name"), "tag": t["name"]})
            for a in orphan_alarms:
                n += 1
                w.writerow([n, a.get("name"), a.get("message"), PRIO[args.to][prio_bucket(a.get("priority"))], a.get("tag") or "",
                            a.get("trigger_bit") or a.get("setpoint") or "", a.get("notes") or "", "trigger tag not in tag database"])
                report["alarms"].append({"alarm": a.get("name"), "tag": a.get("tag")})
        report["files"].append(p)
    else:
        p = os.path.join(args.out, "DRAFT_tags_factorytalk.csv")
        with open(p, "w", newline="", encoding="utf-8") as f:
            f.write(";DRAFT generated by Rungstone - review before import. Format simplified; use the Tag Import and Export Wizard layout of your FactoryTalk version.\n")
            w = csv.writer(f)
            w.writerow([";Tag Type", "Tag Name", "Tag Description", "Read Only", "Data Source", "Address"])
            for t in tags:
                w.writerow([TARGET_TYPES[args.to][t["type"]], t["name"].replace("/", "\\"), t["description"] or "",
                            "T" if t["read_only"] else "F", "M" if t["memory"] else "D",
                            "" if t["memory"] else "{[%s]%s}" % (args.device, t["plc"])])
        report["files"].append(p)
        p = os.path.join(args.out, "DRAFT_alarms_factorytalk.csv")
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Alarm", "Tag", "Condition", "Setpoint", "Severity", "Message", "Notes"])
            for t in tags:
                for a in t["alarms"]:
                    w.writerow([a.get("name"), t["name"], a.get("condition") or "", a.get("setpoint") or "",
                                PRIO[args.to][prio_bucket(a.get("priority"))], a.get("message") or "", a.get("notes") or ""])
                    report["alarms"].append({"alarm": a.get("name"), "tag": t["name"]})
            for a in orphan_alarms:
                w.writerow([a.get("name"), a.get("tag") or "", a.get("condition") or "", a.get("setpoint") or "",
                            PRIO[args.to][prio_bucket(a.get("priority"))], a.get("message") or "", "trigger tag not in tag database"])
        report["files"].append(p)

    # scripts: stubs only
    if inv["scripts"]:
        sd = os.path.join(args.out, "DRAFT_scripts")
        os.makedirs(sd, exist_ok=True)
        for s in inv["scripts"]:
            src = hc.read_text(s["file"]).splitlines()
            body = "\n".join(src[s["line"] - 1:s["line"] - 1 + s["lines"]])
            refs = s.get("plc_reads", []) + s.get("plc_writes", [])
            lines = ["DRAFT - script port required (Rungstone does not translate scripts automatically).",
                     "Source: %s line %d (%s, %s)" % (s["file"], s["line"], s["name"], s["language"]),
                     "Target: %s" % args.to, "Reads: %s" % (", ".join(s["reads"]) or "-"), "Writes: %s" % (", ".join(s["writes"]) or "-"),
                     "Side effects to re-implement: %s" % (", ".join(s["side_effects"]) or "none"), "PLC rebinds:"]
            for r in refs:
                new, _ = plc_path(r)
                lines.append("  %s -> %s" % (r["path"], new))
            lines += ["", "---- original (not executed) ----", body]
            fn = os.path.join(sd, "%s__%s.txt" % (re.sub(r"\W+", "_", os.path.basename(s["file"])), re.sub(r"\W+", "_", s["name"])))
            open(fn, "w", encoding="utf-8").write("\n".join(lines) + "\n")
            report["files"].append(fn)

    if args.screens:
        for sc in inv["screens"]:
            base = os.path.join(args.out, "DRAFT_screen_%s" % re.sub(r"\W+", "_", sc["name"]))
            md = ["# DRAFT layout: %s" % sc["name"], "",
                  "Source: %s (%s). **Draft layout description only - not an importable screen.** Rebuild it in the target "
                  "editor using the target's faceplates and the site HMI style guide." % (sc["file"], sc["platform"]), "",
                  "| Object | Type | Role | Label | Position | Bindings (rebound) | Action |", "|---|---|---|---|---|---|---|"]
            boxes = []
            for o in sc["objects"]:
                binds = []
                for b in o.get("bindings", []):
                    for r in b.get("refs", []):
                        new, _ = plc_path(r)
                        binds.append("%s %s" % ("W" if r.get("access") == "write" else "R", new))
                pos = o.get("position") or {}
                x = pos.get("left", pos.get("x"))
                y = pos.get("top", pos.get("y"))
                act = "; ".join(a.get("kind", "") + (" " + (a.get("display") or a.get("page") or "") if a.get("kind") == "navigate" else "")
                                for a in o.get("actions", []))
                md.append("| %s | %s | %s | %s | %s | %s | %s |" % (o.get("name"), o.get("type"), o.get("role"), o.get("label") or "",
                                                                  "%s,%s" % (x, y) if x is not None else "", "; ".join(binds), act))
                if x is not None and y is not None:
                    boxes.append((float(x), float(y), float(pos.get("width", 120)), float(pos.get("height", 28)), o, binds))
            open(base + ".md", "w", encoding="utf-8").write("\n".join(md) + "\n")
            h = ["<!doctype html><meta charset=utf-8><title>DRAFT %s</title>" % html.escape(sc["name"]),
                 "<style>body{font:13px system-ui;background:#1b1d21;color:#ddd}.c{position:relative;width:1300px;height:820px;"
                 "border:1px dashed #666;margin:12px}.b{position:absolute;border:1px solid #d9a441;font:11px monospace;overflow:hidden;"
                 "padding:2px;background:#25282d}.w{border-color:#e06c75}h1{font-size:15px;color:#d9a441;margin:12px}</style>",
                 "<h1>DRAFT LAYOUT - %s - not an importable screen</h1><div class=c>" % html.escape(sc["name"])]
            for x, y, w_, h_, o, binds in boxes:
                h.append('<div class="b%s" style="left:%dpx;top:%dpx;width:%dpx;height:%dpx" title="%s">%s<br>%s</div>' % (
                    " w" if any(b.startswith("W") for b in binds) else "", x, y, w_, h_, html.escape("; ".join(binds)),
                    html.escape(o.get("label") or o.get("name") or ""), html.escape(o.get("type") or "")))
            h.append("</div>")
            open(base + ".html", "w", encoding="utf-8").write("\n".join(h))
            report["files"] += [base + ".md", base + ".html"]

    readme = ["# DRAFT migration output (Rungstone)", "",
              "Target: %s. Generated from: %s." % (args.to, ", ".join(sorted(set(f["file"] for f in inv["files"])))), "",
              "These files are drafts. Before importing into the target project:", "",
              "1. Review every rebind marked for review (%d) and every unmapped reference (%d) in the report." % (len(report["review"]), len(report["unmapped"])),
              "2. Check data types inferred from members/addresses (column type_basis in the report).",
              "3. Check OPC item path / connection syntax against the target driver (e.g. Ignition Siemens driver addressing differs from TIA symbols).",
              "4. Alarm priorities were mapped by bucket; re-rate them against the site alarm philosophy.",
              "5. Scripts are stubs: port them by hand and review; screens are wireframes only.",
              "6. Import into a test copy of the HMI/SCADA project first. Never deploy untested to a running system.", ""]
    p = os.path.join(args.out, "README_DRAFT.md")
    open(p, "w", encoding="utf-8").write("\n".join(readme))
    report["files"].append(p)
    s = json.dumps(report, indent=2, ensure_ascii=False)
    if args.output:
        open(args.output, "w", encoding="utf-8").write(s)
    else:
        sys.stdout.write(s + "\n")
    sys.stderr.write("migrate_tags: %d tags, %d alarms, %d rebinds (%d need review), %d unmapped -> %s\n" % (
        len(report["tags"]), len(report["alarms"]), len(report["rebinds"]), len(report["review"]), len(report["unmapped"]), args.out))


if __name__ == "__main__":
    main()
