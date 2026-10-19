"""
verify_bindings.py - check that every HMI/SCADA binding resolves to a real
PLC tag, and run the PLC + HMI + SCADA consistency check.

Checks per binding:
  resolved            base tag exists and the member (if any) is valid
  broken              tag not found in the PLC project
  member_invalid      base exists but the member does not (e.g. Timer1.ACCUM)
  not_plc             HMI-internal / memory tag (no PLC reference) - informational
Conflicts (consistency check across PLC + HMI + SCADA):
  label_mismatch      HMI caption/description contradicts or disagrees with the
                      PLC comment / I/O map
  hmi_writes_input    HMI writes a physical input or block input (overwritten every scan)
  hmi_writes_readonly HMI writes a tag that is Constant or ExternalAccess Read Only
  hmi_writes_safety   HMI writes a safety-flagged tag (never acceptable)
  + everything dependency_map.py reports (competing writers, read never
    written, broken links, translation watch) when the rungstone-plc skill is
    installed alongside.

Optional --simulate: builds button/indicator/alarm scenarios and runs them
through rungstone-plc/scripts/simulate.py.  If the simulator is missing or a
scenario cannot run, the result is "not run" with the reason.  Results are
always labelled "simulated, not hardware-tested".

Usage:
  python3 verify_bindings.py --hmi FILE_OR_DIR... --plc PLC_FILE... [--target siemens-scl]
          [--simulate --program PLC_FILE] [-o out.json] [--md out.md] [--state DIR]
  python3 verify_bindings.py --hmi ... --tagmap .rungstone/tagmap.json   (no PLC files)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hmi_common as hc  # noqa: E402
import parse_hmi  # noqa: E402

LABEL = "simulated, not hardware-tested"
TIMER_M = {"acc", "pre", "dn", "en", "tt"}
COUNTER_M = {"acc", "pre", "dn", "cu", "cd", "ov", "un"}
CONTROL_M = {"pos", "len", "dn", "en", "er", "ul", "em", "eu", "in", "fd"}
IEC_TIMER_M = {"et", "pt", "q", "in"}
IEC_COUNTER_M = {"cv", "pv", "q", "qu", "qd", "cu", "cd", "r", "ld"}


def hmi_bindings(inv):
    """Flatten inventory into binding records."""
    out = []
    for sc in inv["screens"]:
        for o in sc["objects"]:
            for b in o.get("bindings", []):
                if b.get("unresolved"):
                    out.append({"where": "%s:%s" % (sc["name"], o.get("name")), "file": sc["file"], "label": o.get("label"),
                                "property": b.get("property"), "ignition_path": b.get("ignition_path"),
                                "access": b.get("access", "read"), "status_hint": "ignition_unresolved"})
                for r in b.get("refs", []):
                    out.append({"where": "%s:%s" % (sc["name"], o.get("name")), "file": sc["file"], "label": o.get("label"),
                                "label_source": o.get("label_source"), "property": b.get("property"),
                                "device": r.get("device"), "path": r["path"], "norm": r.get("norm") or hc.norm_path(r["path"]),
                                "access": r.get("access", "read")})
            for a in o.get("actions", []):
                for r in a.get("plc_writes", []) or []:
                    out.append({"where": "%s:%s (script)" % (sc["name"], o.get("name")), "file": sc["file"],
                                "label": o.get("label"), "device": r.get("device"), "path": r["path"], "norm": r["norm"],
                                "access": "write"})
    for t in inv["tags"]:
        if t.get("plc_ref"):
            r = t["plc_ref"]
            out.append({"where": "tag %s" % t["name"], "file": t["file"], "label": t.get("description"),
                        "label_source": "tag description", "device": r.get("device"), "path": r["path"],
                        "norm": r.get("norm") or hc.norm_path(r["path"]),
                        "access": "may_write" if t.get("access") == "read/write" else "read"})
        elif t.get("value_source") in ("memory", "expression", "derived", "reference", "query"):
            out.append({"where": "tag %s" % t["name"], "file": t["file"], "status_hint": "not_plc", "access": "read",
                        "label": t.get("description")})
    for a in inv["alarms"]:
        if a.get("plc_ref"):
            r = a["plc_ref"]
            out.append({"where": "alarm %s" % a.get("name"), "file": a["file"], "label": a.get("message"),
                        "label_source": "alarm message", "device": r.get("device"), "path": r["path"],
                        "norm": r.get("norm") or hc.norm_path(r["path"]), "access": "read"})
    for s in inv["scripts"]:
        for key, acc in (("plc_reads", "read"), ("plc_writes", "write")):
            for r in s.get(key, []):
                out.append({"where": "script %s:%s" % (os.path.basename(s["file"]), s["name"]), "file": s["file"],
                            "device": r.get("device"), "path": r["path"], "norm": r["norm"], "access": acc})
    return out


class PlcModel(object):
    """PLC symbols from the rungstone-plc skill (preferred) or a tagmap.json."""

    def __init__(self, plc_files=None, tagmap=None, allow=None):
        self.projects, self.idx, self.udts, self.source = [], None, {}, None
        self.sym = {}
        if plc_files and hc.dm:
            self.projects = hc.dm.load_projects(plc_files, allow or [])
            self.idx = hc.dm.build_index(self.projects)
            self.sym = self.idx.sym
            for pr in self.projects:
                for dt in pr.get("data_types", []):
                    self.udts[dt["name"].upper()] = set(m["name"].lower().split(".")[0] for m in dt.get("members", []))
            self.source = "plc project via rungstone-plc"
        elif plc_files and not hc.dm:
            raise SystemExit("PLC files given but the rungstone-plc skill is not installed next to this skill; "
                             "supply the tag map with --tagmap .rungstone/tagmap.json instead, or install rungstone-plc.")
        elif tagmap:
            data = json.load(open(tagmap, encoding="utf-8"))
            for k, v in (data.get("tags") or {}).items():
                labels = []
                if v.get("meaning"):
                    labels.append({"source": "tagmap (%s)" % v.get("basis", "unknown"), "text": v["meaning"]})
                self.sym[hc.norm_path(k)] = {"display": v.get("display") or k, "data_type": v.get("data_type"),
                                             "labels": labels, "safety": v.get("safety"),
                                             "physical_input": v.get("physical_input")}
            self.source = "tagmap.json"

    def resolve(self, norm):
        parts = norm.split(".")
        for start in range(len(parts)):
            cand = ".".join(parts[start:])
            if self.idx is not None:
                b, m = self.idx.resolve(cand, [""])
                if b in self.sym or hc.SLC_RE.fullmatch(b) or b.upper().startswith(("I:", "O:")):
                    return b, m
            else:
                for n in range(len(parts), start, -1):
                    c = ".".join(parts[start:n])
                    if c in self.sym:
                        return c, ".".join(parts[n:])
                if hc.SLC_RE.fullmatch(cand):
                    b, m = hc.split_member(hc.norm_slc(cand))
                    return b, m
        return None, None


def member_ok(info, base, member, udts):
    if not member:
        return True, None
    m0 = re.split(r"[.\[]", member)[0].lower()
    if re.fullmatch(r"\d+", m0):
        return True, None  # bit of an integer
    dt = (info.get("data_type") or "").upper()
    kind = hc.kind_of(dt, base)
    sets = {"timer": TIMER_M, "counter": COUNTER_M, "control": CONTROL_M, "iec_timer": IEC_TIMER_M, "iec_counter": IEC_COUNTER_M}
    if kind in sets:
        return (m0 in sets[kind]), kind
    if dt in udts:
        return (m0 in udts[dt]), "udt " + dt
    return True, None  # unknown type: cannot judge


def verify(inv, model, target=None):
    bindings = hmi_bindings(inv)
    results, conflicts = [], []

    def conflict(ctype, sev, tag, detail, evidence):
        conflicts.append({"type": ctype, "severity": sev, "tag": tag, "detail": detail, "evidence": sorted(set(evidence))[:12]})

    for b in bindings:
        rec = {"where": b["where"], "path": b.get("path") or b.get("ignition_path"), "access": b["access"],
               "label": b.get("label")}
        if b.get("status_hint") == "not_plc":
            rec["status"] = "not_plc"
            results.append(rec)
            continue
        if b.get("status_hint") == "ignition_unresolved":
            rec["status"] = "broken"
            rec["why"] = "Ignition tag path not found in the supplied tag exports"
            results.append(rec)
            continue
        if model is None or not model.sym and model.idx is None:
            rec["status"] = "unverified"
            rec["why"] = "no PLC project or tagmap supplied"
            results.append(rec)
            continue
        base, member = model.resolve(b["norm"])
        info = model.sym.get(base, {}) if base else {}
        if base is None or (base not in model.sym and not hc.SLC_RE.fullmatch(base or "") and
                            not (base or "").upper().startswith(("I:", "O:"))):
            rec["status"] = "broken"
            rec["why"] = "no PLC tag named '%s'" % b["path"]
            results.append(rec)
            continue
        ok, kind = member_ok(info, base, member, model.udts)
        rec["plc_tag"] = info.get("display") or base
        if member:
            rec["member"] = member
        if not ok:
            rec["status"] = "member_invalid"
            rec["why"] = "'%s' is not a member of %s (%s)" % (member, rec["plc_tag"], kind)
        else:
            rec["status"] = "resolved"
        results.append(rec)
        if b["access"] == "write":
            if info.get("safety"):
                conflict("hmi_writes_safety", "high", rec["plc_tag"], "HMI/SCADA writes a safety-flagged tag. Remove the write; "
                         "safety functions must not be commanded from standard HMI.", [b["where"]])
            if info.get("physical_input") or base.upper().startswith("I:") or re.search(r":i(\.|$)", base):
                conflict("hmi_writes_input", "high", rec["plc_tag"], "HMI writes a physical/block input; the value is overwritten every scan.",
                         [b["where"]])
            if info.get("constant"):
                conflict("hmi_writes_readonly", "high", rec["plc_tag"], "HMI writes a Constant tag; the write will be rejected.", [b["where"]])
        # label consistency vs PLC comment / I/O map
        if b.get("label"):
            for lab in info.get("labels", []):
                opp = hc.contradiction(b["label"], lab["text"])
                if opp:
                    conflict("label_mismatch", "high", rec["plc_tag"] + ("." + member.upper() if member else ""),
                             "HMI says '%s' (%s, %s) but %s says '%s' (%s)." % (b["label"], b["where"], b.get("label_source") or "label",
                                                                               lab["source"], lab["text"], opp),
                             [b["where"], lab["source"]])
                elif not member and hc.jaccard(b["label"], lab["text"]) < 0.2 and not re.match(r"^\d+=", b["label"]):
                    conflict("label_mismatch", "medium", rec["plc_tag"],
                             "HMI label '%s' (%s) does not match %s '%s'." % (b["label"], b["where"], lab["source"], lab["text"]),
                             [b["where"], lab["source"]])

    plc_side = None
    if model is not None and model.projects and hc.dm:
        hrefs = [{"device": b.get("device"), "path": b["path"], "access": b["access"], "label": None, "where": b["where"],
                  "file": b["file"], "hmi_name": None} for b in bindings if b.get("path")]
        plc_side = hc.dm.analyse(model.projects, hrefs, target, None)
        seen = set((c["type"], c["tag"].lower()) for c in conflicts)
        writers_by_tag = dict((t["tag"].lower(), t.get("writers", [])) for t in plc_side.get("tags", []))
        hmi_write_members = {}
        for b in bindings:
            if b["access"] == "write" and b.get("path"):
                base, mem = model.resolve(b["norm"])
                if base:
                    hmi_write_members.setdefault((model.sym.get(base, {}).get("display") or base).lower(), set()).add(
                        (mem or "").split(".")[0].lower())
        for c in plc_side["conflicts"]:
            if c["type"] == "label_mismatch":
                continue  # our own label check above is binding-aware
            if c["type"] == "competing_writers" and "HMI/SCADA writes" in c["detail"]:
                # A timer/counter instruction owns ACC/DN/EN/TT (CU/CD/OV/UN), not the preset: an HMI write to
                # .PRE alongside TON/CTU is the normal setpoint pattern, not a fight.
                mems = hmi_write_members.get(c["tag"].lower(), set())
                plc_writers = [w for w in writers_by_tag.get(c["tag"].lower(), []) if "[hmi]" not in w]
                timer_only = plc_writers and all(re.search(r"\[(TON|TOF|RTO|CTU|CTD|TONR|TOFR|RTOR|CTUD)\]", w) for w in plc_writers)
                if timer_only and mems and mems <= {"pre"}:
                    continue
            if (c["type"], c["tag"].lower()) not in seen:
                conflicts.append(c)
    sev = {"high": 0, "medium": 1, "low": 2, "info": 3}
    dedup = {}
    for c in conflicts:
        dedup.setdefault((c["type"], c["tag"].lower(), c["detail"]), c)
    conflicts = sorted(dedup.values(), key=lambda c: (sev.get(c["severity"], 9), c["type"], c["tag"]))
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    out = {"tool": "rungstone/verify_bindings", "plc_source": model.source if model else None,
           "summary": {"bindings": len(results), "by_status": counts, "conflicts": len(conflicts)},
           "bindings": results, "conflicts": conflicts}
    if plc_side:
        out["open_questions"] = plc_side.get("open_questions", [])
        out["excluded_safety"] = plc_side.get("excluded_safety", [])
    return out


# --------------------------------------------------------------------------
# Scenarios through simulate.py
# --------------------------------------------------------------------------

def build_scenarios(inv):
    scen = []
    indicators = sorted(set(r["path"] for sc in inv["screens"] for o in sc["objects"] if o.get("role") == "indicator"
                            for b in o.get("bindings", []) for r in b.get("refs", [])))
    for sc in inv["screens"]:
        for o in sc["objects"]:
            for a in o.get("actions", []):
                targets = a.get("targets") or [r["path"] for r in a.get("plc_writes", []) or []]
                for t in targets:
                    kind = a.get("kind", "")
                    if "momentary" in kind or a.get("kind") == "script" or "set 1" in kind:
                        steps = [{"at_ms": 100, "set": {t: 1}}, {"at_ms": 400, "set": {t: 0}}]
                    elif "toggle" in kind:
                        steps = [{"at_ms": 100, "set": {t: 1}}, {"at_ms": 1500, "set": {t: 0}}]
                    elif "numeric" in kind:
                        v = a.get("maximum") or a.get("minimum") or 10
                        steps = [{"at_ms": 100, "set": {t: float(v) if "." in str(v) else int(float(v))}}]
                    else:
                        continue
                    scen.append({"name": "HMI %s: %s -> %s" % (o.get("name"), kind.split(" (")[0], t), "category": "hmi-button",
                                 "scan_ms": 10, "duration_ms": 2000, "events": steps, "observe": sorted(set([t] + indicators))})
    for al in inv["alarms"]:
        r = al.get("plc_ref")
        if r:
            scen.append({"name": "Alarm %s on %s" % (al.get("name"), r["path"]), "category": "hmi-alarm", "scan_ms": 10,
                         "duration_ms": 1000, "events": [], "observe": [r["path"]],
                         "note": "observe whether the PLC logic can drive the alarm source under a normal cycle"})
    return scen


def run_sim(program, scenarios):
    sim = os.path.join(hc.PLC_SCRIPTS, "simulate.py")
    if not os.path.exists(sim):
        return [{"scenario": s["name"], "verdict": "not run", "reason": "simulator not installed (rungstone-plc/scripts/simulate.py missing)"}
                for s in scenarios]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"scenarios": scenarios}, f)
        sp = f.name
    try:
        cp = subprocess.run([sys.executable, "-I", sim, "run", "--program", program, "--scenarios", sp],
                            capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return [{"scenario": s["name"], "verdict": "not run", "reason": "simulator timed out"} for s in scenarios]
    finally:
        os.unlink(sp)
    if cp.returncode not in (0,):
        return [{"scenario": s["name"], "verdict": "not run", "reason": "simulator error: %s" % (cp.stderr.strip()[-300:] or cp.returncode)}
                for s in scenarios]
    try:
        data = json.loads(cp.stdout)
    except ValueError:
        return [{"scenario": s["name"], "verdict": "not run", "reason": "simulator output was not JSON"} for s in scenarios]
    res = data.get("results") or data.get("scenarios") or []
    out = []
    for i, s in enumerate(scenarios):
        r = res[i] if i < len(res) else {}
        out.append({"scenario": s["name"], "verdict": r.get("verdict") or r.get("status") or "not run",
                    "reason": r.get("reason"), "observed_final": r.get("final"), "changes": r.get("changes"),
                    "label": LABEL})
    return out


def to_md(res):
    L = ["# HMI/SCADA binding verification", "",
         "Bindings: %d (%s). Conflicts: %d. PLC source: %s." % (
             res["summary"]["bindings"], ", ".join("%s %d" % kv for kv in sorted(res["summary"]["by_status"].items())),
             res["summary"]["conflicts"], res.get("plc_source") or "none")]
    if res.get("excluded_safety"):
        L += ["", "**Safety logic excluded (not analysed):** " + "; ".join("%s (%s, %s)" % (e["name"], e["reason"], e["confidence"])
                                                                     for e in res["excluded_safety"])]
    L += ["", "## Conflicts", "", "| Severity | Type | Tag | Detail | Evidence |", "|---|---|---|---|---|"]
    for c in res["conflicts"]:
        L.append("| %s | %s | %s | %s | %s |" % (c["severity"], c["type"], c["tag"], c["detail"].replace("|", "/"), "; ".join(c["evidence"])))
    if not res["conflicts"]:
        L.append("| - | - | - | none | - |")
    L += ["", "## Bindings not resolved", "", "| Where | Reference | Status | Why |", "|---|---|---|---|"]
    bad = [b for b in res["bindings"] if b["status"] not in ("resolved", "not_plc")]
    for b in bad:
        L.append("| %s | %s | %s | %s |" % (b["where"], b.get("path"), b["status"], b.get("why", "")))
    if not bad:
        L.append("| - | - | all resolved | - |")
    if res.get("open_questions"):
        L += ["", "## Open questions", ""] + ["%d. %s" % (i, q["question"]) for i, q in enumerate(res["open_questions"], 1)]
    if res.get("simulation"):
        L += ["", "## Scenarios (%s)" % LABEL, "", "| Scenario | Verdict | Reason |", "|---|---|---|"]
        for s in res["simulation"]:
            L.append("| %s | %s | %s |" % (s["scenario"], s["verdict"], s.get("reason") or ""))
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hmi", nargs="+", required=True)
    ap.add_argument("--plc", nargs="*", default=[])
    ap.add_argument("--tagmap")
    ap.add_argument("--target", choices=["siemens-scl", "iec-st", "rockwell"])
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--program", help="PLC program file to simulate (defaults to the first --plc file)")
    ap.add_argument("--state", help="project folder containing .rungstone/ (results appended to verify_results.json)")
    ap.add_argument("-o", "--output")
    ap.add_argument("--md")
    args = ap.parse_args(argv)
    inv = parse_hmi.parse_paths(args.hmi)
    model = PlcModel(args.plc, args.tagmap) if (args.plc or args.tagmap) else None
    res = verify(inv, model, args.target)
    res["inventory_summary"] = inv["summary"]
    if args.simulate:
        prog = args.program or (args.plc[0] if args.plc else None)
        scen = build_scenarios(inv)
        broken = set(hc.norm_path(b["path"]) for b in res["bindings"] if b["status"] in ("broken", "member_invalid") and b.get("path"))
        runnable = [x for x in scen if not any(hc.norm_path(t) in broken for e in x["events"] for t in e.get("set", {}))
                    and not (x["category"] == "hmi-alarm" and hc.norm_path(x["observe"][0]) in broken)]
        skipped = [{"scenario": x["name"], "verdict": "not run", "reason": "binding is broken - nothing in the PLC to drive",
                    "label": LABEL} for x in scen if x not in runnable]
        scen = runnable
        res["simulation"] = skipped + run_sim(prog, scen) if prog else [{"scenario": s["name"], "verdict": "not run",
                                                               "reason": "no PLC program given"} for s in scen]
        res["simulation_label"] = LABEL
        d = hc.find_state_dir(args.state)
        if d:
            from _shared import state
            vr = state.load(d, "verify_results.json", {"label": LABEL, "runs": []})
            vr.setdefault("runs", []).append({"source": "rungstone-hmi-scada/verify_bindings", "at": state.now(),
                                              "label": LABEL, "results": res["simulation"]})
            state.save(d, "verify_results.json", vr)
    s = json.dumps(res, indent=2, ensure_ascii=False)
    if args.output:
        open(args.output, "w", encoding="utf-8").write(s)
    else:
        sys.stdout.write(s + "\n")
    if args.md:
        open(args.md, "w", encoding="utf-8").write(to_md(res))
    sys.stderr.write("verify_bindings: %d bindings %s, %d conflicts\n" % (
        res["summary"]["bindings"], res["summary"]["by_status"], res["summary"]["conflicts"]))


if __name__ == "__main__":
    main()
