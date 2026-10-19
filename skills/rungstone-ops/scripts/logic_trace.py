#!/usr/bin/env python3
"""
logic_trace.py - cite the PLC logic behind a signal.

Given a tag (e.g. the pump run output that dropped) and the PLC project, list
every rung / ST line / network that writes it, the conditions on each of those
rungs (with what each condition requires: XIC = must be 1, XIO = must be 0,
compares, timers), and then - recursively, to --depth levels - what writes
those conditions.  Physical inputs, block inputs and safety signals end the
recursion.  Safety-related signals are marked and never traced into: the trace
says "a safety function tripped / permissive from the safety system" and stops.

Uses the sibling rungstone-plc skill (parse_project.py, dependency_map.py) in
read-only mode.  Historian tag names that do not match a PLC tag exactly are
matched by normalised name and reported as an inferred match.

Usage:
  python3 logic_trace.py --plc Pump_Station.L5X --tag P101_Run [--depth 3]
  python3 logic_trace.py --plc parsed.json --tag "P-101 Run"
"""

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLC_SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "..", "rungstone-plc", "scripts"))
if not os.path.isdir(PLC_SCRIPTS):
    sys.stdout.write(json.dumps({"error": "rungstone-plc skill not found next to rungstone-ops (expected %s). Install the "
                                          "Rungstone plugin or the rungstone-plc skill to trace logic." % PLC_SCRIPTS}) + "\n")
    sys.exit(2)
sys.path.insert(0, PLC_SCRIPTS)
import parse_project as pp  # noqa: E402
import dependency_map as dm  # noqa: E402

REQUIRES = {"XIC": "must be 1 (TRUE)", "XIO": "must be 0 (FALSE)", "Contact": "must be 1 (TRUE)",
            "contact": "must be 1 (TRUE)", "EQU": "compare =", "NEQ": "compare <>", "GRT": "compare >",
            "GEQ": "compare >=", "LES": "compare <", "LEQ": "compare <=", "LIM": "limit test", "CMP": "expression"}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def item_index(projects):
    """where-string -> {text, comment, safety_refs, language}"""
    idx = {}
    for pr in projects:
        for r in pr.get("routines", []):
            for kind, key in (("rung", "rungs"), ("line", "lines"), ("network", "networks")):
                for x in r.get(key, []):
                    idx["%s %s %s" % (r["id"], kind, x.get("number"))] = {
                        "routine": r["id"], "kind": kind, "number": x.get("number"), "text": x.get("text"),
                        "comment": x.get("comment"), "safety_refs": x.get("safety_refs"), "file": os.path.basename(pr["file"]),
                        "language": r.get("language")}
    return idx


def collect(projects):
    sym = dm.build_index(projects)
    refs = []
    for c in dm.code_refs(projects):
        path_, prefixes = dm.code_token_path(c["token"], c["siemens"], c["prefixes"])
        base, member = sym.resolve(path_, prefixes)
        refs.append({"where": c["where"], "token": c["token"], "access": c["access"], "instr": c["instr"],
                     "base": base, "member": member})
    return sym, refs


def resolve_tag(name, sym, refs):
    key = dm.norm_path(name)
    bases = set(r["base"] for r in refs) | set(sym.sym)
    if key in bases:
        return key, "exact"
    for b in bases:
        if b.split(".")[-1] == key:
            return b, "exact (scoped)"
    n = norm(name)
    cands = [b for b in bases if norm(b.split(".")[-1]) == n or norm((sym.sym.get(b) or {}).get("display")) == n]
    if len(cands) == 1:
        return cands[0], "inferred (normalised name match)"
    return None, "not found"


def info_for(base, sym):
    i = sym.sym.get(base, {})
    disp = i.get("display") or base
    out = {"tag": disp}
    labels = i.get("labels") or []
    if labels:
        out["description"] = labels[0]["text"]
        src = labels[0]["source"]
        out["description_basis"] = "io_map" if src.startswith("io") else "comment"
        out["description_source"] = src
        if len(labels) > 1:
            out["other_descriptions"] = [l["text"] for l in labels[1:4]]
    if i.get("alias_for"):
        out["alias_for"] = i["alias_for"]
    if i.get("data_type"):
        out["data_type"] = i["data_type"]
    if i.get("physical_input"):
        out["physical_input"] = True
    name = disp.split(".")[-1]
    if i.get("safety") or pp.SAFETY_NAME_RE.search(name):
        out["safety"] = ("safety-related signal (%s). Not traced: if this dropped, a safety function tripped - "
                         "investigate with the safety system and its documentation, not this tool."
                         % (i.get("safety") or "name/description indicates e-stop/safety"))
    return out


def trace(base, sym, refs, items, depth, seen):
    writers = {}
    for r in refs:
        if r["base"] == base and r["access"] in ("write", "unknown"):
            writers.setdefault(r["where"], []).append(r)
    res = []
    for where, ws in sorted(writers.items()):
        it = items.get(where, {})
        conds = []
        here = [r for r in refs if r["where"] == where and r["access"] == "read"]
        for r in here:
            c = info_for(r["base"], sym)
            c["operand"] = r["token"]
            c["instruction"] = r["instr"]
            if r["instr"] in REQUIRES:
                c["requires"] = REQUIRES[r["instr"]]
            if r["base"] == base:
                c["note"] = "seal-in / self-reference"
            elif r["base"] in seen:
                c["note"] = "traced elsewhere in this result"
            elif depth > 0 and r["base"] not in seen and not c.get("physical_input") and not c.get("safety"):
                seen.add(r["base"])
                sub = trace(r["base"], sym, refs, items, depth - 1, seen)
                if sub:
                    c["written_by"] = sub
                elif not sym.sym.get(r["base"], {}).get("physical_input"):
                    c["written_by"] = []
                    c["note"] = "no writer in the supplied PLC files (HMI/SCADA, another controller, or I/O not mapped?)"
            conds.append(c)
        res.append({k: v for k, v in {
            "location": where, "file": it.get("file"), "comment": it.get("comment"), "logic": it.get("text"),
            "writes_with": sorted(set(w["instr"] for w in ws if w["instr"])),
            "conditions": conds,
            "safety_refs": it.get("safety_refs"),
            "branches": "rung has parallel branches - conditions inside [ , ] are OR-ed" if "[" in (it.get("text") or "") or " BST " in (it.get("text") or "") else None,
        }.items() if v not in (None, [], "")})
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plc", nargs="+", required=True)
    ap.add_argument("--tag", required=True, action="append", help="tag to trace (repeatable)")
    ap.add_argument("--depth", type=int, default=3)
    ap.add_argument("--allow-suspected", action="append", default=[])
    a = ap.parse_args(argv)
    projects = dm.load_projects(a.plc, a.allow_suspected)
    sym, refs = collect(projects)
    items = item_index(projects)
    out = {"tool": "rungstone/logic_trace", "traces": [],
           "excluded_safety": [dict(e, file=os.path.basename(p["file"])) for p in projects for e in p.get("excluded_safety", [])],
           "notes": ["Static read of the supplied project; the controller may run a different version. Confirm the "
                     "project matches what was running at the time of the event.",
                     "Safety logic is excluded and never traced."]}
    for tag in a.tag:
        base, how = resolve_tag(tag, sym, refs)
        if base is None:
            out["traces"].append({"requested": tag, "match": how,
                                  "hint": "no PLC tag matches; check the historian-to-PLC tag naming"})
            continue
        t = info_for(base, sym)
        t.update(requested=tag, match=how)
        if t.get("safety"):
            t["writers"] = []
        else:
            t["writers"] = trace(base, sym, refs, items, a.depth, {base})
            if not t["writers"]:
                t["note"] = "nothing in the supplied PLC code writes this tag"
        t["read_in"] = sorted(set(r["where"] for r in refs if r["base"] == base and r["access"] == "read"))
        out["traces"].append(t)
    sys.stdout.write(json.dumps(out, separators=(",", ":"), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
