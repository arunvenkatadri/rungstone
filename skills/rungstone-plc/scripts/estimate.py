"""
estimate.py - estimate scope, tokens and cost for each Rungstone job
BEFORE anything runs.

Input: PLC exports (anything parse_project.py reads) or a parse_project.py
JSON.  Nothing is sent anywhere; this only measures the parsed project.

Per job (understand, translate, verify, dependency map) it reports input
tokens, output tokens, a low-high band, how many passes/chunks are needed,
and cost at the chosen model's list price.  Jobs above the "large" threshold
are marked CONFIRM so the user approves scope before work starts.

Token counts are estimates: PLC code is symbol-dense, so we assume ~3.0
characters per token for code and ~3.6 for prose/JSON declarations.  Real
counts vary by tokenizer and by how much of the reference material is
needed.  Treat the band, not the point value, as the answer.

Usage:
  python3 estimate.py FILE... [--jobs understand,translate,verify,depmap]
                      [--model claude-opus-5-5] [--price-in 4 --price-out 20]
                      [--target siemens-scl] [--json]
"""

import argparse
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_project as pp  # noqa: E402

# Anthropic list prices, USD per million tokens (input, output).
# Snapshot 2026-10; check https://www.anthropic.com/pricing before quoting.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.00, 50.00),
}
PRICES_AS_OF = "2026-10"

CODE_CPT = 3.0       # chars per token, PLC code
DECL_CPT = 3.6       # chars per token, declarations / JSON / prose
CHUNK_INPUT = 120000  # tokens of project material per chunk before we split
LARGE_TOKENS = 150000
LARGE_USD = 2.00
LARGE_ROUTINES = 40

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOB_REFS = {
    "understand": ["SKILL.md", "references/output-formats.md", "references/safety-exclusion.md"],
    "translate": ["SKILL.md", "references/output-formats.md", "references/safety-exclusion.md",
                  "references/translation-pitfalls.md"],
    "verify": ["SKILL.md", "references/output-formats.md", "references/translation-pitfalls.md"],
    "depmap": ["SKILL.md", "references/output-formats.md"],
}
FORMAT_REF = {"rockwell-l5x": "references/rockwell-l5x.md", "ladder-text": "references/rockwell-l5x.md",
              "siemens-simaticml": "references/siemens-scl.md", "siemens-scl": "references/siemens-scl.md",
              "plcopen-xml": "references/plcopen-xml.md", "beckhoff-twincat": "references/plcopen-xml.md",
              "iec-st": "references/plcopen-xml.md"}
TARGET_REF = {"siemens-scl": "references/siemens-scl.md", "rockwell": "references/rockwell-l5x.md",
              "iec-st": "references/plcopen-xml.md"}

RISKY = ("TON", "TOF", "RTO", "CTU", "CTD", "ONS", "OSR", "OSF", "PID", "PIDE", "MSG", "SCP", "SCL", "IOT", "IIN",
         "MAJ", "MAM", "MAS", "MSO", "MAH", "FFL", "FFU", "BSL", "BSR", "SQO", "COP", "CPS", "GSV", "SSV", "JMP",
         "MCR", "TND", "FAL", "AFI", "P_TRIG", "N_TRIG", "R_TRIG", "F_TRIG", "PID_COMPACT", "TCON", "TSEND",
         "TRCV", "MB_CLIENT", "MB_SERVER", "NORM_X", "SCALE_X")


def ref_tokens(paths):
    total = 0
    for p in paths:
        fp = os.path.join(SKILL_DIR, p)
        if os.path.exists(fp):
            total += os.path.getsize(fp) / DECL_CPT
        else:
            total += 3000  # reference not found; assume a typical file
    return int(total)


def measure(projects):
    code_chars = decl_chars = routines = items = 0
    risky = 0
    timers = 0
    signals = set()
    formats = set()
    excluded = 0
    import re
    word = re.compile(r"\b(%s)\b" % "|".join(RISKY), re.I)
    for pr in projects:
        formats.add(pr["format"])
        st = pr.get("stats") or pp.project_stats(dict({"routines": [], "tags": [], "data_types": [], "aois": [],
                                                       "excluded_safety": []}, **pr))
        code_chars += st["code_chars"]
        decl_chars += st["declaration_chars"]
        routines += st["routines"]
        items += st["rungs"] + st["st_lines"] + st["networks"]
        excluded += len(pr.get("excluded_safety", []))
        for t in pr.get("tags", []):
            signals.add(t.get("name"))
        for r in pr.get("routines", []):
            for key in ("rungs", "lines", "networks"):
                for x in r.get(key, []):
                    hits = word.findall(x.get("text") or "")
                    risky += len(hits)
                    timers += sum(1 for h in hits if h.upper() in ("TON", "TOF", "RTO", "CTU", "CTD"))
    return {"code_tokens": int(code_chars / CODE_CPT), "decl_tokens": int(decl_chars / DECL_CPT),
            "routines": routines, "items": items, "signals": len(signals), "risky_instructions": risky,
            "timers_counters": timers, "formats": sorted(formats), "excluded_safety_items": excluded}


def jobs_for(m, jobs, target):
    proj = m["code_tokens"] + m["decl_tokens"]
    fmt_refs = sorted(set(FORMAT_REF.get(f) for f in m["formats"] if FORMAT_REF.get(f)))
    out = []
    for job in jobs:
        refs = JOB_REFS[job] + fmt_refs + ([TARGET_REF[target]] if job == "translate" and target in TARGET_REF else [])
        ref_tok = ref_tokens(sorted(set(refs)))
        if job == "understand":
            material = proj
            output = 1500 + 120 * m["routines"] + 30 * m["items"] + 45 * m["signals"]
            note = "summary, sequence by rung, signal table, open questions"
        elif job == "translate":
            material = proj
            output = int(1.5 * m["code_tokens"]) + 50 * m["signals"] + 220 * m["risky_instructions"] + 1500
            note = "target code + mapping table + review queue (%d risky instructions)" % m["risky_instructions"]
        elif job == "verify":
            scen = 6 + 2 * m["routines"] + m["timers_counters"]
            material = proj + int(1.5 * m["code_tokens"])  # source + translated target
            output = 260 * scen + 800
            note = "~%d scenarios; plan only unless a simulator is connected" % scen
        else:  # depmap: the script does the heavy lifting locally; Claude reads its report
            material = 70 * m["signals"] + 400
            output = 35 * m["signals"] + 1200
            note = "script runs locally (no tokens); Claude reads and explains the report"
        passes = max(1, int(math.ceil(material / float(CHUNK_INPUT))))
        # each chunk re-reads the skill references; agentic overhead ~ +25% input for tool turns
        inp = int((material + ref_tok * passes) * 1.25)
        out.append({"job": job, "input_tokens": inp, "output_tokens": int(output), "passes": passes,
                    "reference_tokens_per_pass": ref_tok, "note": note})
    return out


def price(jobs, pin, pout):
    for j in jobs:
        mid = j["input_tokens"] * pin / 1e6 + j["output_tokens"] * pout / 1e6
        j["usd_mid"] = round(mid, 3)
        j["usd_low"] = round(mid * 0.7, 3)
        j["usd_high"] = round(mid * 1.8, 3)
    return jobs


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--jobs", default="understand,translate,verify,depmap")
    ap.add_argument("--model", default="claude-opus-5-5", help="one of: " + ", ".join(PRICES))
    ap.add_argument("--price-in", type=float, help="override USD per million input tokens")
    ap.add_argument("--price-out", type=float, help="override USD per million output tokens")
    ap.add_argument("--target", choices=["siemens-scl", "iec-st", "rockwell"])
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = ap.parse_args(argv)

    projects = []
    ctx = pp.Ctx()
    for f in args.files:
        if f.lower().endswith(".json"):
            try:
                d = json.loads(pp.read_text(f))
                if d.get("tool") == "rungstone/parse_project":
                    projects.extend(d["projects"])
                    continue
            except ValueError:
                pass
        pr = pp.parse_file(f, ctx)
        pr["stats"] = pp.project_stats(pr)
        projects.append(pr)

    pin, pout = PRICES.get(args.model, (None, None))
    if args.price_in is not None:
        pin = args.price_in
    if args.price_out is not None:
        pout = args.price_out
    if pin is None or pout is None:
        sys.exit("Unknown model %s; set prices with --price-in and --price-out." % args.model)

    m = measure(projects)
    jobs = price(jobs_for(m, [j.strip() for j in args.jobs.split(",") if j.strip()], args.target), pin, pout)
    total_in = sum(j["input_tokens"] for j in jobs)
    total_usd = sum(j["usd_mid"] for j in jobs)
    large = (total_in > LARGE_TOKENS or total_usd > LARGE_USD or m["routines"] > LARGE_ROUTINES)
    res = {"tool": "rungstone/estimate", "model": args.model, "price_per_mtok": {"input": pin, "output": pout},
           "prices_as_of": PRICES_AS_OF, "scope": m, "jobs": jobs,
           "total": {"input_tokens": total_in, "output_tokens": sum(j["output_tokens"] for j in jobs),
                     "usd_low": round(sum(j["usd_low"] for j in jobs), 2), "usd_mid": round(total_usd, 2),
                     "usd_high": round(sum(j["usd_high"] for j in jobs), 2)},
           "confirm_before_running": large,
           "assumptions": ["~%.1f chars/token for code, %.1f for declarations" % (CODE_CPT, DECL_CPT),
                           "+25%% input for tool-use turns; band is 0.7x-1.8x of the midpoint",
                           "projects over %d tokens of material are split into passes; references are re-read each pass" % CHUNK_INPUT,
                           "API list prices; claude.ai / Claude Code subscriptions bill as plan usage, not dollars",
                           "safety-excluded logic is not counted (it is never processed)"]}
    if args.json:
        print(json.dumps(res, indent=2))
        return
    s = m
    print("Scope: %d routines, %d rungs/lines/networks, %d tags, %d risky instructions, formats: %s"
          % (s["routines"], s["items"], s["signals"], s["risky_instructions"], ", ".join(s["formats"]) or "-"))
    if s["excluded_safety_items"]:
        print("Safety-excluded items (not counted, never processed): %d" % s["excluded_safety_items"])
    print("Project material: ~%d code tokens + ~%d declaration tokens" % (s["code_tokens"], s["decl_tokens"]))
    print("")
    print("| Job | Input tok | Output tok | Passes | Cost (USD, %s @ $%g/$%g per MTok) | Notes |" % (args.model, pin, pout))
    print("|---|---:|---:|---:|---|---|")
    for j in jobs:
        print("| %s | %s | %s | %d | $%.2f (%.2f-%.2f) | %s |" % (j["job"], format(j["input_tokens"], ","), format(j["output_tokens"], ","),
                                                            j["passes"], j["usd_mid"], j["usd_low"], j["usd_high"], j["note"]))
    t = res["total"]
    print("| **total** | %s | %s | | $%.2f (%.2f-%.2f) | |" % (format(t["input_tokens"], ","), format(t["output_tokens"], ","),
                                                       t["usd_mid"], t["usd_low"], t["usd_high"]))
    print("")
    print("Prices: Anthropic API list, as of %s - confirm at anthropic.com/pricing. Subscription plans count usage, not dollars." % PRICES_AS_OF)
    if large:
        print("CONFIRM: this is a large job. Show this estimate and get the user's go-ahead (or a narrower scope) before running.")


if __name__ == "__main__":
    main()
