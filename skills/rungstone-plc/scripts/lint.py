"""
lint.py - static analysis of PLC code: likely bugs and risky patterns, each with where and why.

Reads anything parse_project.py reads (or its parsed.json). Findings are things to check, not
verdicts: static analysis sees only the files supplied and can't know what the HMI, comms or
other controllers write.

Structured Text (CODESYS, TwinCAT, IEC, SCL):
  fb_call_conditional  timer/edge/counter instance called inside IF/CASE: it freezes instead of resetting
  fb_called_twice      the same timer/edge/counter instance called more than once per scan
  fb_never_called      a timer/edge/counter instance whose outputs are read but which is never called
  real_equality        = or <> on REAL/LREAL values
  division_unguarded   division or MOD by a variable never checked against zero in the POU
  loop_scan_risk       WHILE/REPEAT loops (scan time and watchdog)
  constant_condition   IF/WHILE on TRUE/FALSE
  case_without_else    CASE with no ELSE branch
  self_assignment      x := x;
  empty_branch         IF/ELSIF/ELSE with an empty body
  unused_variable      local variable declared and never used
Ladder (Logix, SLC/PLC-5 text):
  latch_never_unlatched  OTL with no OTU, no other writer: only a restart or download clears it
  oneshot_storage_shared the same ONS/OSR/OSF storage bit used by two instructions
  timer_shared           the same TIMER in two timer instructions
  rto_without_reset      RTO with no RES: the accumulator never clears
  rung_disabled          AFI on a rung: the rung never executes
From the dependency map (all languages):
  competing_writers    an output written in more than one place (last write wins)
  read_never_written   read but never written by the code supplied (HMI? comms? missing?)
  declared_unused      tag declared and never referenced

Usage:
  python3 lint.py FILES|parsed.json [--md report.md] [-o out.json]
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dependency_map as dm  # noqa: E402
import parse_project as pp  # noqa: E402
from portability import strip_code  # noqa: E402

SEV_ORDER = {"high": 0, "medium": 1, "low": 2}
STATEFUL_FB = {"TON", "TOF", "TP", "R_TRIG", "F_TRIG", "CTU", "CTD", "CTUD", "TONR", "RTO", "SR", "RS",
               "TON_TIME", "TOF_TIME", "TP_TIME", "IEC_TIMER", "TON_LTIME", "TOF_LTIME"}
REAL_TYPES = {"REAL", "LREAL"}
KEYWORDS = {"IF", "THEN", "ELSE", "ELSIF", "END_IF", "CASE", "OF", "END_CASE", "FOR", "TO", "BY", "DO", "END_FOR",
            "WHILE", "END_WHILE", "REPEAT", "UNTIL", "END_REPEAT", "AND", "OR", "XOR", "NOT", "MOD", "TRUE", "FALSE",
            "RETURN", "EXIT", "CONTINUE"}

TITLES = {
    "fb_call_conditional": "Timer/edge/counter called only inside a condition",
    "fb_called_twice": "Timer/edge/counter instance called more than once per scan",
    "fb_never_called": "Timer/edge/counter outputs read, but the instance is never called",
    "real_equality": "Exact comparison of REAL values",
    "division_unguarded": "Division by a value never checked for zero",
    "loop_scan_risk": "WHILE/REPEAT loop in cyclic code",
    "constant_condition": "Condition is always TRUE or always FALSE",
    "case_without_else": "CASE without ELSE",
    "self_assignment": "Variable assigned to itself",
    "empty_branch": "Empty IF/ELSE branch",
    "unused_variable": "Local variable never used",
    "overwritten_assignment": "Assignment overwritten later in the same scan",
    "set_never_cleared": "Set to TRUE and never cleared",
    "user_fb_call_conditional": "Block with internal timers/edges called only inside a condition",
    "latch_never_unlatched": "Latched (OTL) and never unlatched",
    "oneshot_storage_shared": "One-shot storage bit shared between instructions",
    "timer_shared": "Same TIMER used by more than one timer instruction",
    "rto_without_reset": "Retentive timer (RTO) never reset",
    "rung_disabled": "Rung disabled with AFI",
    "ladder_division_unguarded": "DIV by a register never checked for zero",
    "written_never_read": "Written but never read",
    "competing_writers": "Output written in more than one place",
    "read_never_written": "Read but never written by the code supplied",
    "declared_unused": "Tag declared but never used",
}
WHY = {
    "fb_call_conditional": ("When the condition goes false the instance is no longer called, so it keeps its last "
                            "state (a TON keeps Q and ET) instead of resetting.",
                            "Call it every scan and move the condition onto IN / CLK / CU."),
    "fb_called_twice": ("Each call advances the instance; two calls per scan give two different views of the same "
                        "timer or edge.", "Call it once per scan and read its outputs elsewhere."),
    "fb_never_called": ("An instance that is never called never changes: Q and ET stay at their initial values.",
                        "Call the instance every scan, or remove the reads."),
    "real_equality": ("Floating-point values rarely compare exactly equal after arithmetic.",
                      "Compare with a tolerance: ABS(a - b) < 0.001, or use >= / <=."),
    "division_unguarded": ("A zero divisor faults the controller or yields an undefined value, depending on the runtime.",
                           "Check the divisor first: IF d <> 0 THEN ... END_IF."),
    "loop_scan_risk": ("A loop whose exit depends on runtime data can run long or forever and trip the watchdog.",
                       "Bound it (FOR, or a counter limit), or spread the work over several scans."),
    "constant_condition": ("Usually debug code left in place: one branch never runs.", "Remove the dead branch or restore the real condition."),
    "case_without_else": ("An unexpected value (after a download, or a bad HMI write) does nothing, silently.",
                          "Add ELSE that moves to a safe state or raises an alarm."),
    "self_assignment": ("Does nothing; often a typo for another variable.", "Check which variable was meant."),
    "empty_branch": ("Does nothing; often unfinished code.", "Remove it or finish it."),
    "unused_variable": ("Clutter that hides intent.", "Remove it, or use it where intended."),
    "overwritten_assignment": ("Both assignments run every scan and nothing reads the variable in between, so the first "
                               "one has no effect.", "Combine them into one assignment, or make one conditional."),
    "set_never_cleared": ("Every assignment sets it TRUE, so once set it stays set until a restart or download "
                          "(a fault that can never be reset).", "Add the reset condition, e.g. on a reset button with the cause gone."),
    "user_fb_call_conditional": ("The block has timers or edge detectors inside; when it isn't called they freeze, "
                                 "and an edge input held TRUE across calls fires only once.",
                                 "Call it every scan and pass the condition as an input."),
    "latch_never_unlatched": ("Once set it stays set until a restart or download; no logic clears it.",
                              "Add the OTU (reset) condition, or confirm it is cleared from outside (HMI, comms)."),
    "oneshot_storage_shared": ("Two one-shots sharing a storage bit interfere: one can swallow the other's edge.",
                               "Give each ONS/OSR/OSF its own storage bit."),
    "timer_shared": ("Two instructions driving the same timer fight over its accumulator and done bit.",
                     "Use one timer per instruction."),
    "rto_without_reset": ("A retentive timer only clears with RES; without it, it stays done after the first time.",
                          "Add RES(timer) on the reset condition."),
    "rung_disabled": ("AFI (always false) disables the rung; often left from commissioning.",
                      "Remove AFI if the rung should run, or delete the rung."),
    "ladder_division_unguarded": ("Dividing by zero sets the math overflow bit and faults some controllers; the "
                                  "result is undefined.", "Guard the rung with NEQ <divisor> 0."),
    "written_never_read": ("Computed but never used: dead logic, or the code that should use it is missing.",
                           "Find the intended reader, or remove the calculation."),
    "competing_writers": ("Only the last write in the scan wins, so the earlier logic has no effect on the output.",
                          "Combine the conditions into one assignment, or use latch/unlatch on purpose."),
    "read_never_written": ("Normally set from the HMI, SCADA or comms; if not, it never changes.",
                           "Confirm who writes it."),
    "declared_unused": ("Clutter, or logic that was removed.", "Remove it if nothing outside the PLC uses it."),
}


def finding(rule, sev, where, detail):
    why, fix = WHY[rule]
    return {"rule": rule, "severity": sev, "title": TITLES[rule], "where": where, "detail": detail, "why": why, "fix": fix}


# --------------------------------------------------------------------------
# Structured Text
# --------------------------------------------------------------------------

def var_types(r):
    out = {}
    for sec in r.get("interface") or []:
        for m in sec.get("members") or []:
            if m.get("name"):
                out[m["name"].lower()] = ((m.get("data_type") or "").upper(), sec.get("section", ""))
    return out


def display_names(r):
    return {m["name"].lower(): m["name"] for sec in r.get("interface") or [] for m in sec.get("members") or [] if m.get("name")}


def base_type(t):
    t = t.split(".")[-1].strip()
    return re.sub(r"\s*\(.*$", "", t)


def lint_st(r, extra_types, stateful_fbs=()):
    out = []
    rid = r.get("id") or r.get("name")
    lines = r.get("lines") or []
    code = strip_code([l.get("text") or "" for l in lines])
    nums = [l.get("number") for l in lines]
    types = dict(extra_types)
    types.update(var_types(r))
    body = "\n".join(code)
    siemens = (r.get("language") == "SCL")

    def loc(i):
        return "%s line %s" % (rid, nums[i])

    def clean(name):
        return name.lstrip("#").strip('"').lower()

    stateful = {n for n, (t, _) in types.items() if base_type(t) in STATEFUL_FB}
    user_stateful = {n for n, (t, _) in types.items() if base_type(t) in stateful_fbs}
    reals = {n for n, (t, _) in types.items() if base_type(t) in REAL_TYPES}

    # nesting of IF / CASE for conditional FB calls
    depth = []
    calls = {}
    for i, ln in enumerate(code):
        for m in re.finditer(r"\b(IF|CASE|END_IF|END_CASE|ELSIF)\b|(#?[A-Za-z_]\w*)\s*\(", ln, re.I):
            kw = (m.group(1) or "").upper()
            if kw in ("IF", "CASE"):
                depth.append(kw)
            elif kw in ("END_IF", "END_CASE"):
                if depth:
                    depth.pop()
            elif m.group(2):
                n = clean(m.group(2))
                if n in user_stateful and depth:
                    out.append(finding("user_fb_call_conditional", "medium", loc(i),
                                       "%s (%s) is called inside %s." % (m.group(2), types[n][0], depth[-1])))
                if n in stateful:
                    calls.setdefault(n, []).append(i)
                    if depth:
                        out.append(finding("fb_call_conditional", "high", loc(i),
                                           "%s (%s) is called inside %s." % (m.group(2), base_type(types[n][0]), depth[-1])))
    for n, idxs in calls.items():
        if len(idxs) > 1:
            out.append(finding("fb_called_twice", "medium", loc(idxs[1]),
                               "%s is called on lines %s." % (n, ", ".join(str(nums[i]) for i in idxs))))
    for n in stateful:
        if n not in calls and re.search(r"(?<![\w.])#?%s\s*\.\s*(Q|ET|CV|QU|QD)\b" % re.escape(n), body, re.I):
            i = next(j for j, ln in enumerate(code) if re.search(r"(?<![\w.])#?%s\s*\." % re.escape(n), ln, re.I))
            out.append(finding("fb_never_called", "high", loc(i), "%s outputs are read but %s is never called." % (n, n)))

    for i, ln in enumerate(code):
        for m in re.finditer(r"(#?[\w.]+|\d+\.\d+)\s*(=|<>)\s*(#?[\w.]+|\d+\.\d+)", ln):
            if ln[max(0, m.start() - 1):m.start() + 1].endswith(":") or ln[m.end(1):m.end(1) + 3].lstrip().startswith(":="):
                continue
            a, b = clean(m.group(1)), clean(m.group(3))
            if a in reals or b in reals or re.fullmatch(r"\d+\.\d+", a) or re.fullmatch(r"\d+\.\d+", b):
                out.append(finding("real_equality", "medium", loc(i), "%s %s %s" % (m.group(1), m.group(2), m.group(3))))
        for m in re.finditer(r"(?:/|\bMOD\b)\s*(#?[A-Za-z_][\w.]*)", ln, re.I):
            d = clean(m.group(1))
            if d.upper() in KEYWORDS:
                continue
            guard = re.compile(r"(?<![\w.])#?%s\s*(<>|>|<|=)\s*0(?![.\d])|\b0\s*(<>|<|>)\s*#?%s(?![\w.])" % (
                re.escape(d), re.escape(d)), re.I)
            if not guard.search(body):
                out.append(finding("division_unguarded", "medium", loc(i), "Divides by %s." % m.group(1)))
        if re.search(r"^\s*(WHILE|REPEAT)\b", ln, re.I):
            out.append(finding("loop_scan_risk", "medium", loc(i), ln.strip()))
        if re.search(r"\b(IF|ELSIF|WHILE)\s+(TRUE|FALSE)\s+(THEN|DO)\b", ln, re.I):
            out.append(finding("constant_condition", "medium", loc(i), ln.strip()))
        sm = re.match(r"^\s*(#?[\w.\[\]]+)\s*:=\s*(#?[\w.\[\]]+)\s*;\s*$", ln)
        if sm and clean(sm.group(1)) == clean(sm.group(2)):
            out.append(finding("self_assignment", "low", loc(i), ln.strip()))

    # top-level assignments overwritten later with no read in between; BOOLs only ever set TRUE
    depth_n, top_assign, assigns, shown = 0, {}, {}, {}
    for i, ln in enumerate(code):
        am = re.match(r"^\s*(#?[A-Za-z_][\w.]*)\s*:=\s*(.*)$", ln)
        if am:
            n, rhs = clean(am.group(1)), am.group(2)
            shown.setdefault(n, am.group(1).lstrip("#"))
            assigns.setdefault(n, []).append(rhs.strip().rstrip(";").strip().upper())
            if depth_n == 0 and n in top_assign:
                prev = top_assign[n]
                between = "\n".join(code[prev + 1:i]) + "\n" + rhs
                if not re.search(r"(?<![\w.])#?%s(?![\w])" % re.escape(n), between, re.I) and \
                        not re.search(r"(?<![\w.])#?%s(?![\w])" % re.escape(n),
                                      code[prev].split(":=", 1)[1] if ":=" in code[prev] else "", re.I):
                    out.append(finding("overwritten_assignment", "high", loc(i),
                                       "%s is assigned on line %s and again on line %s." % (am.group(1), nums[prev], nums[i])))
            if depth_n == 0:
                top_assign[n] = i
        for m in re.finditer(r"\b(IF|CASE|FOR|WHILE|REPEAT|END_IF|END_CASE|END_FOR|END_WHILE|END_REPEAT)\b", ln, re.I):
            depth_n += -1 if m.group(1).upper().startswith("END_") else 1
            depth_n = max(depth_n, 0)
    for n, rhss in assigns.items():
        t = types.get(n, ("", ""))[0]
        if base_type(t) == "BOOL" and rhss and all(x == "TRUE" for x in rhss) and n not in reals:
            i = next(j for j, ln in enumerate(code) if re.match(r"^\s*#?%s\s*:=" % re.escape(n), ln, re.I))
            out.append(finding("set_never_cleared", "medium", loc(i), "%s is only ever assigned TRUE." % shown[n]))

    # CASE without ELSE, empty branches
    for i, ln in enumerate(code):
        if re.search(r"^\s*CASE\b", ln, re.I):
            depth_c, has_else = 0, False
            for j in range(i, len(code)):
                for m in re.finditer(r"\b(CASE|END_CASE|ELSE|IF|END_IF)\b", code[j], re.I):
                    k = m.group(1).upper()
                    if k in ("CASE", "IF"):
                        depth_c += 1
                    elif k in ("END_CASE", "END_IF"):
                        depth_c -= 1
                    elif k == "ELSE" and depth_c == 1:
                        has_else = True
                if depth_c == 0:
                    break
            if not has_else:
                out.append(finding("case_without_else", "low", loc(i), ln.strip()))
    stmt = [(i, l.strip()) for i, l in enumerate(code) if l.strip()]
    for k in range(len(stmt) - 1):
        i, a = stmt[k]
        b = stmt[k + 1][1]
        if re.search(r"\b(THEN|ELSE)\s*$", a, re.I) and re.match(r"^(END_IF|ELSE|ELSIF)\b", b, re.I):
            out.append(finding("empty_branch", "low", loc(i), "%s / %s" % (a, b)))

    # unused locals
    for n, (t, sec) in types.items():
        if n in extra_types:
            continue
        if not (sec.upper().startswith("VAR") and sec.upper() in ("VAR", "VAR_TEMP", "VAR_STAT", "LOCALVARS", "TEMPVARS")
                or sec in ("localVars", "tempVars")):
            continue
        if not re.search(r"(?<![\w])#?%s(?![\w])" % re.escape(n), body, re.I):
            out.append(finding("unused_variable", "low", rid, "%s : %s" % (display_names(r).get(n, n), t or "?")))
    return out


# --------------------------------------------------------------------------
# Ladder
# --------------------------------------------------------------------------

LAD_RE = re.compile(r"\b(OTL|OTU|ONS|OSR|OSF|TON|TOF|RTO|RES|AFI)\b(?:\s*\(\s*([^,)\s]*)|\s+(?!EOR\b)([^\s(]+))?", re.I)
DIV_RE = re.compile(r"\bDIV\s*(?:\(\s*([^,]+?)\s*,\s*([^,]+?)\s*,|\s+(\S+)\s+(\S+)\s)", re.I)


def lint_ladder(projects):
    out = []
    uses = {}      # (instr, tag) -> [where]
    divs = []
    all_text = "\n".join(rung.get("text") or "" for pr in projects for r in pr.get("routines", []) for rung in r.get("rungs") or [])
    for pr in projects:
        for r in pr.get("routines", []):
            rid = "%s/%s" % (r["program"], r["name"]) if r.get("program") else r.get("name")
            for rung in r.get("rungs") or []:
                where = "%s rung %s" % (rid, rung.get("number"))
                for m in DIV_RE.finditer(rung.get("text") or ""):
                    divisor = (m.group(2) or m.group(4) or "").strip()
                    if divisor and not re.fullmatch(r"-?[\d.]+", divisor):
                        divs.append((where, divisor))
                for m in LAD_RE.finditer(rung.get("text") or ""):
                    ins = m.group(1).upper()
                    tag = (m.group(2) if m.group(2) is not None else m.group(3)) or ""
                    if ins == "AFI":
                        out.append(finding("rung_disabled", "medium", where, rung.get("text")[:120]))
                        continue
                    if tag:
                        uses.setdefault((ins, tag.lower()), []).append((where, tag))
    def has(ins, tag):
        return (ins, tag) in uses
    for where, d in divs:
        e = re.escape(d)
        guard = re.compile(r"\b(NEQ|GRT|LES|EQU|GEQ|LEQ)\s*(\(\s*%s\s*,\s*0\s*\)|\s+%s\s+0\b|\(\s*0\s*,\s*%s\s*\)|\s+0\s+%s\b)" % (e, e, e, e), re.I)
        if not guard.search(all_text):
            out.append(finding("ladder_division_unguarded", "medium", where, "DIV by %s." % d))
    for (ins, tag), ws in sorted(uses.items()):
        disp = ws[0][1]
        if ins == "OTL" and not has("OTU", tag):
            out.append(finding("latch_never_unlatched", "medium", ws[0][0], "%s is latched in %s and never unlatched." % (
                disp, ", ".join(w for w, _ in ws))))
        if ins in ("ONS", "OSR", "OSF"):
            all_ws = [w for k in ("ONS", "OSR", "OSF") for w, _ in uses.get((k, tag), [])]
            if len(all_ws) > 1 and ins == min(k for k in ("ONS", "OSR", "OSF") if (k, tag) in uses):
                out.append(finding("oneshot_storage_shared", "high", all_ws[1], "%s is the storage bit in %s." % (disp, ", ".join(all_ws))))
        if ins in ("TON", "TOF", "RTO"):
            all_ws = [w for k in ("TON", "TOF", "RTO") for w, _ in uses.get((k, tag), [])]
            if len(all_ws) > 1 and ins == min(k for k in ("TON", "TOF", "RTO") if (k, tag) in uses):
                out.append(finding("timer_shared", "high", all_ws[1], "%s is used in %s." % (disp, ", ".join(all_ws))))
        if ins == "RTO" and not has("RES", tag):
            out.append(finding("rto_without_reset", "medium", ws[0][0], "%s has no RES." % disp))
    return out


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def analyse(projects):
    findings = []
    globals_ = {}
    for pr in projects:
        for t in pr.get("tags", []):
            if t.get("name") and t.get("data_type"):
                globals_[t["name"].split(".")[-1].lower()] = (t["data_type"].upper(), "global")
    stateful_fbs = set()
    for pr in projects:
        for r in pr.get("routines", []):
            if (r.get("kind") or "").lower() in ("functionblock", "function_block") and \
                    any(base_type(t) in STATEFUL_FB for t, _ in var_types(r).values()):
                stateful_fbs.add(r["name"].upper())
    for pr in projects:
        by_parent = {}
        for r in pr.get("routines", []):
            if r.get("lines"):
                by_parent.setdefault(r.get("program") or r.get("name"), []).append(r)
        for r in pr.get("routines", []):
            if not r.get("lines"):
                continue
            parent = r.get("program")
            extra = dict(globals_)
            if parent:  # methods/actions see the parent POU's variables
                for p in pr["routines"]:
                    if p.get("name") == parent and not p.get("program"):
                        extra.update(var_types(p))
            findings += lint_st(r, extra, stateful_fbs)
    findings += lint_ladder(projects)

    # Cross-reference findings only for controller/global tags and SLC data files: inside ST POUs, several
    # writers are normal (state machines) and inputs are written by the caller.
    dep = dm.analyse(projects, [])
    declared, outputs = set(), set()
    for pr in projects:
        for t in pr.get("tags", []):
            n = (t.get("name") or "").lower()
            declared.add(n)
            if re.search(r"(:O\b|:O\.|^%Q|\bO:)", t.get("alias_for") or t.get("address") or "", re.I):
                outputs.add(n)
    slc_data = re.compile(r"^[NFBL]\d+:\d+", re.I)

    def global_tag(name):
        n = (name or "").lower()
        return n in declared or bool(slc_data.match(n))
    for c in dep["conflicts"]:
        if c["type"] == "competing_writers" and "coils" in c["detail"]:
            findings.append(finding("competing_writers", "high", ", ".join(c["evidence"][:4]), "%s: %s" % (c["tag"], c["detail"])))
        elif c["type"] == "read_never_written" and global_tag(c["tag"]):
            findings.append(finding("read_never_written", "low", ", ".join(c["evidence"][:4]), c["tag"]))
    for t in dep["tags"]:
        if not global_tag(t.get("tag")) or "plc" not in (t.get("systems") or ["plc"]):
            continue
        if t.get("writers") and not t.get("readers") and t["tag"].lower() not in outputs \
                and not all(re.search(r"\[(ONS|OSR|OSF)\]", w) for w in t["writers"]) \
                and not re.match(r"^(O:|%Q|Local:\d+:O)", t["tag"], re.I):
            findings.append(finding("written_never_read", "low", t["writers"][0], t["tag"]))
        if not t.get("readers") and not t.get("writers") \
                and not (t.get("data_type") or "").upper().startswith(("AXIS", "MODULE")):
            findings.append(finding("declared_unused", "low", t.get("tag"), "%s : %s" % (t.get("tag"), t.get("data_type") or "?")))

    seen, uniq = set(), []
    for f in findings:
        k = (f["rule"], f["where"], f["detail"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    uniq.sort(key=lambda f: (SEV_ORDER[f["severity"]], f["rule"], f["where"]))
    excluded = [e for pr in projects for e in pr.get("excluded_safety", [])]
    counts = {s: sum(1 for f in uniq if f["severity"] == s) for s in ("high", "medium", "low")}
    return {"tool": "rungstone/lint", "summary": counts, "findings": uniq,
            "not_assessed": [{"name": e.get("name"), "reason": e.get("reason")} for e in excluded],
            "limits": ["Static analysis of the files supplied: values written by the HMI, SCADA, comms or other "
                       "controllers are invisible, and indirect addressing or pointers can hide readers and writers.",
                       "Findings are things to check, not proof of a bug. Nothing was run."]}


def cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def to_markdown(res, title):
    s = res["summary"]
    out = ["# Static analysis: %s" % title, "",
           "> Static analysis of the files supplied. Findings are things to check, not proof of a bug; nothing was run.", ""]
    if res["not_assessed"]:
        out += ["**Not assessed (safety logic, excluded):** " + ", ".join(x["name"] for x in res["not_assessed"]), ""]
    out += ["%d high, %d medium, %d low." % (s["high"], s["medium"], s["low"]), "",
            "| # | Severity | Finding | Where | Detail | Fix |", "|---|---|---|---|---|---|"]
    for i, f in enumerate(res["findings"], 1):
        out.append("| %d | %s | %s | %s | %s | %s |" % (i, f["severity"], cell(f["title"]), cell(f["where"]),
                                                       cell(f["detail"]), cell(f["fix"])))
    out += ["", "## Why these matter", ""]
    for rule in sorted({f["rule"] for f in res["findings"]}, key=lambda r: list(TITLES).index(r)):
        out.append("- **%s.** %s" % (TITLES[rule], WHY[rule][0]))
    out += ["", "## Limits", ""] + ["- " + l for l in res["limits"]]
    return "\n".join(out) + "\n"


def load_projects(files, allow):
    return dm.load_projects(files, allow)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--md")
    ap.add_argument("--title")
    ap.add_argument("-o", "--out")
    ap.add_argument("--allow-suspected", action="append", default=[])
    a = ap.parse_args(argv)
    res = analyse(load_projects(a.files, a.allow_suspected))
    if a.md:
        os.makedirs(os.path.dirname(os.path.abspath(a.md)), exist_ok=True)
        with open(a.md, "w", encoding="utf-8") as f:
            f.write(to_markdown(res, a.title or ", ".join(os.path.basename(x) for x in a.files)))
    text = json.dumps(res, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(json.dumps({"written": a.out, "md": a.md, "summary": res["summary"]}))
    else:
        print(text)


if __name__ == "__main__":
    main()
