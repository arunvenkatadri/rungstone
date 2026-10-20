"""
state.py - Rungstone project state in .rungstone/ (plain files, git friendly).

  .rungstone/
    project.json         name, sources (path, sha256, format), target, created
    tagmap.json          shared tag map: one entry per signal, used by every skill
    mapping.csv          old -> new address/symbol table produced by Translate
    review_queue.json    flagged changes; engineer approves/edits/rejects each
    verify_results.json  simulation results ("simulated, not hardware-tested")
    dependencies.json    output of dependency_map.py
    decisions.log        append-only audit trail (TSV)

Meaning basis vocabulary (tagmap "basis"):
  confirmed  - the engineer confirmed it in this session
  comment    - taken from a rung/tag/declaration comment in the project
  io_map     - taken from an I/O list / module point comment
  inferred   - deduced from how the logic uses it (say why)
  unknown    - no evidence

CLI:
  state.py init [--name NAME] [--dir PATH]
  state.py status
  state.py queue list [--open] [--risk high]
  state.py queue add --risk high --location "rung 3" --issue "..." --fix "..." [--workflow translate]
  state.py queue approve ID [ID...] [--note "..."]
  state.py queue reject ID [ID...] --note "..."
  state.py queue edit ID --fix "new fix" [--note "..."]
  state.py gate-export          exit 2 (blocked) while any high-risk item is open
  state.py log ACTION DETAIL
"""

import argparse
import csv
import datetime as _dt
import hashlib
import json
import os
import sys

STATE_DIR = ".rungstone"
BASIS = ("confirmed", "comment", "io_map", "inferred", "unknown")
RISKS = ("high", "medium", "low")
STATUSES = ("open", "approved", "edited", "rejected")
MAPPING_COLUMNS = ["old_address", "old_symbol", "old_description", "old_type", "new_symbol", "new_address",
                   "new_type", "basis", "notes"]
LABEL = "simulated, not hardware-tested"


def now():
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def find_dir(start=None, create=False):
    cur = os.path.abspath(start or os.getcwd())
    while True:
        cand = os.path.join(cur, STATE_DIR)
        if os.path.isdir(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    if create:
        d = os.path.join(os.path.abspath(start or os.getcwd()), STATE_DIR)
        os.makedirs(d, exist_ok=True)
        return d
    return None


def _path(d, name):
    return os.path.join(d, name)


def load(d, name, default=None):
    p = _path(d, name)
    if not os.path.exists(p):
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save(d, name, data):
    p = _path(d, name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, p)


def log(d, action, detail="", item="", actor=None):
    actor = actor or "engineer"
    line = "\t".join([now(), actor, action, item, detail.replace("\t", " ").replace("\n", " ")])
    with open(_path(d, "decisions.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def init(d, name=None):
    proj = load(d, "project.json") or {"name": name or os.path.basename(os.path.dirname(d)), "created": now(),
                                       "sources": [], "target": None, "schema": "rungstone/project@1"}
    if name:
        proj["name"] = name
    save(d, "project.json", proj)
    for fname, default in (("tagmap.json", {"schema": "rungstone/tagmap@1", "tags": {}}),
                           ("review_queue.json", {"schema": "rungstone/review_queue@1", "items": []}),
                           ("verify_results.json", {"schema": "rungstone/verify_results@1", "label": LABEL, "runs": []}),
                           ("dependencies.json", {"schema": "rungstone/dependencies@1"})):
        if load(d, fname) is None:
            save(d, fname, default)
    if not os.path.exists(_path(d, "mapping.csv")):
        write_mapping(d, [])
    if not os.path.exists(_path(d, "decisions.log")):
        with open(_path(d, "decisions.log"), "w", encoding="utf-8") as f:
            f.write("# time\tactor\taction\titem\tdetail\n")
    log(d, "init", "state initialised")
    return proj


def add_source(d, path, fmt=None):
    proj = load(d, "project.json")
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(os.path.dirname(d)))
    entry = {"path": rel, "sha256": sha256(path), "format": fmt, "added": now()}
    proj["sources"] = [s for s in proj["sources"] if s["path"] != entry["path"]] + [entry]
    if not selected_sources(proj):
        proj["selected"] = [rel]  # the first source goes through the workflow until another is chosen
    save(d, "project.json", proj)


def selected_sources(proj):
    """Paths the workflows run on when the user names no files (recorded sources only)."""
    have = [s["path"] for s in proj.get("sources", [])]
    if "selected" not in proj:
        return have  # projects from before selection existed: all sources
    return [p for p in proj["selected"] if p in have]


def set_selected(d, paths):
    proj = load(d, "project.json")
    have = set(s["path"] for s in proj["sources"])
    unknown = [p for p in paths if p not in have]
    if unknown:
        return unknown
    proj["selected"] = [s["path"] for s in proj["sources"] if s["path"] in set(paths)]
    save(d, "project.json", proj)
    return []


def add_output(d, path, workflow=None):
    """Record a generated deliverable (translated code, tag table) so the workbench and export can show it."""
    proj = load(d, "project.json")
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(os.path.dirname(d)))
    entry = {"path": rel, "sha256": sha256(path), "workflow": workflow, "added": now()}
    proj["outputs"] = [o for o in proj.get("outputs", []) if o["path"] != rel] + [entry]
    save(d, "project.json", proj)


def remove_source(d, rel):
    proj = load(d, "project.json")
    before = len(proj["sources"])
    proj["sources"] = [s for s in proj["sources"] if s["path"] != rel]
    if "selected" in proj:
        proj["selected"] = [p for p in proj["selected"] if p != rel]
    save(d, "project.json", proj)
    return before != len(proj["sources"])


def write_mapping(d, rows):
    with open(_path(d, "mapping.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MAPPING_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def read_mapping(d):
    p = _path(d, "mapping.csv")
    if not os.path.exists(p):
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---- review queue ---------------------------------------------------------

def queue(d):
    return load(d, "review_queue.json", {"schema": "rungstone/review_queue@1", "items": []})


def queue_add(d, risk, location, issue, fix, workflow="translate", source=None):
    if risk not in RISKS:
        raise ValueError("risk must be one of %s" % (RISKS,))
    q = queue(d)
    n = 1 + max([int(i["id"][1:]) for i in q["items"] if i["id"][1:].isdigit()] or [0])
    item = {"id": "R%d" % n, "risk": risk, "workflow": workflow, "location": location, "issue": issue,
            "proposed_fix": fix, "status": "open", "created": now()}
    if source:
        item["source"] = source
    q["items"].append(item)
    save(d, "review_queue.json", q)
    log(d, "queue.add", "%s %s: %s" % (risk, location, issue), item["id"])
    return item


def queue_decide(d, ids, status, note=None, fix=None, by=None):
    q = queue(d)
    done = []
    for item in q["items"]:
        if item["id"] in ids:
            item["status"] = status
            item["decided_at"] = now()
            item["decided_by"] = by or "engineer"
            if note:
                item["resolution"] = note
            if fix:
                item["previous_fix"] = item.get("proposed_fix")
                item["proposed_fix"] = fix
            done.append(item["id"])
            log(d, "queue." + status, note or fix or "", item["id"], actor=by)
    save(d, "review_queue.json", q)
    missing = sorted(set(ids) - set(done))
    return done, missing


def open_items(d, risk=None):
    return [i for i in queue(d)["items"] if i["status"] == "open" and (risk is None or i["risk"] == risk)]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rungstone project state")
    ap.add_argument("--dir", help="project folder (default: search upward from cwd)")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("init")
    p.add_argument("--name")
    p.add_argument("--target", help="target platform, e.g. siemens-scl, rockwell-l5x, iec-st")
    p = sub.add_parser("source", help="record input files (path + sha256) in project.json")
    p.add_argument("files", nargs="+")
    p.add_argument("--format", help="format as reported by parse_project.py, e.g. l5x, ladder-text, siemens-scl")
    p = sub.add_parser("output", help="record generated files (translated code, tag tables) for the workbench")
    p.add_argument("files", nargs="+")
    p.add_argument("--workflow", default="translate")
    p = sub.add_parser("select", help="choose which recorded sources the workflows run on")
    p.add_argument("paths", nargs="*", help="source paths as listed in project.json")
    p.add_argument("--all", action="store_true")
    p.add_argument("--none", action="store_true")
    sub.add_parser("status")
    sub.add_parser("gate-export")
    p = sub.add_parser("log")
    p.add_argument("action")
    p.add_argument("detail", nargs="?", default="")
    q = sub.add_parser("queue")
    qs = q.add_subparsers(dest="qcmd")
    ql = qs.add_parser("list")
    ql.add_argument("--open", action="store_true")
    ql.add_argument("--risk", choices=RISKS)
    qa = qs.add_parser("add")
    qa.add_argument("--risk", choices=RISKS, required=True)
    qa.add_argument("--location", required=True)
    qa.add_argument("--issue", required=True)
    qa.add_argument("--fix", required=True)
    qa.add_argument("--workflow", default="translate")
    for name in ("approve", "reject"):
        x = qs.add_parser(name)
        x.add_argument("ids", nargs="+")
        x.add_argument("--note", required=(name == "reject"))
        x.add_argument("--by", help="name recorded as the decision maker (default: engineer)")
    qe = qs.add_parser("edit")
    qe.add_argument("id")
    qe.add_argument("--fix", required=True)
    qe.add_argument("--note")
    qe.add_argument("--by", help="name recorded as the decision maker (default: engineer)")
    args = ap.parse_args(argv)

    if args.cmd == "init":
        d = find_dir(args.dir, create=False) if args.dir is None else None
        d = d or find_dir(args.dir or os.getcwd(), create=True)
        proj = init(d, args.name)
        if args.target:
            proj["target"] = args.target
            save(d, "project.json", proj)
        print(json.dumps({"state_dir": d, "project": proj["name"], "target": proj.get("target")}))
        return 0
    d = find_dir(args.dir)
    if d is None:
        print(json.dumps({"error": "no .rungstone/ folder found; run state.py init"}))
        return 1
    if args.cmd == "status":
        q = queue(d)["items"]
        counts = dict(("%s_%s" % (r, s), sum(1 for i in q if i["risk"] == r and i["status"] == s))
                      for r in RISKS for s in STATUSES)
        print(json.dumps({"state_dir": d, "project": load(d, "project.json", {}).get("name"),
                          "target": load(d, "project.json", {}).get("target"),
                          "sources": [s["path"] for s in load(d, "project.json", {}).get("sources", [])],
                          "selected": selected_sources(load(d, "project.json", {})),
                          "mapping_rows": len(read_mapping(d)),
                          "tagmap_entries": len(load(d, "tagmap.json", {}).get("tags", {})),
                          "queue": counts, "export_blocked": bool(open_items(d, "high"))}, indent=2))
        return 0
    if args.cmd == "gate-export":
        hi = open_items(d, "high")
        if hi:
            print(json.dumps({"blocked": True, "reason": "%d high-risk review item(s) still open" % len(hi),
                              "open_high": [{"id": i["id"], "location": i["location"], "issue": i["issue"]} for i in hi]}))
            return 2
        print(json.dumps({"blocked": False, "open_medium_low": len(open_items(d))}))
        return 0
    if args.cmd == "log":
        log(d, args.action, args.detail)
        return 0
    if args.cmd == "output":
        missing = [f for f in args.files if not os.path.isfile(f)]
        if missing:
            print(json.dumps({"error": "not a file", "files": missing}))
            return 1
        for f in args.files:
            add_output(d, f, args.workflow)
            log(d, "output.added", "%s (%s)" % (f, args.workflow))
        print(json.dumps({"outputs": load(d, "project.json").get("outputs", [])}))
        return 0
    if args.cmd == "select":
        proj = load(d, "project.json")
        paths = [] if args.none else ([s["path"] for s in proj["sources"]] if args.all else args.paths)
        unknown = set_selected(d, paths)
        if unknown:
            print(json.dumps({"error": "not recorded sources; add them with state.py source", "paths": unknown}))
            return 1
        log(d, "source.selected", ", ".join(paths) or "(none)")
        print(json.dumps({"selected": selected_sources(load(d, "project.json"))}))
        return 0
    if args.cmd == "source":
        missing = [f for f in args.files if not os.path.isfile(f)]
        if missing:
            print(json.dumps({"error": "not a file", "files": missing}))
            return 1
        for f in args.files:
            add_source(d, f, args.format)
            log(d, "source.added", f)
        print(json.dumps({"sources": load(d, "project.json")["sources"]}))
        return 0
    if args.cmd == "queue":
        if args.qcmd == "list":
            items = [i for i in queue(d)["items"] if (not args.open or i["status"] == "open") and
                     (not args.risk or i["risk"] == args.risk)]
            print(json.dumps(items, indent=2, ensure_ascii=False))
        elif args.qcmd == "add":
            print(json.dumps(queue_add(d, args.risk, args.location, args.issue, args.fix, args.workflow)))
        elif args.qcmd in ("approve", "reject"):
            done, missing = queue_decide(d, set(args.ids), "approved" if args.qcmd == "approve" else "rejected", args.note,
                                         by=args.by)
            print(json.dumps({"updated": done, "not_found": missing}))
        elif args.qcmd == "edit":
            done, missing = queue_decide(d, {args.id}, "edited", args.note, args.fix, by=args.by)
            print(json.dumps({"updated": done, "not_found": missing}))
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
