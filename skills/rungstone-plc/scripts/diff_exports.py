"""
diff_exports.py - what changed between two exports of the same PLC project, and what it affects.

Compares two versions (L5X, SimaticML, SCL, PLCopen XML, TwinCAT, ST, ladder text: anything
parse_project.py reads) and reports, per routine/POU, the rungs or lines that were added, removed or
changed, with the tags each change reads and writes, plus changes to tags, data types, interfaces,
tasks and library code. "Affects" lists where else the written tags are read, so the engineer can see
what a change reaches beyond the lines that moved. For CODESYS and TwinCAT, whose project files are
binary or noisy in version control, this gives a readable review of a change.

Safety logic is excluded from both versions and never compared: if anything safety-related was
added, removed or may have changed, say so and hand it to the safety team.

Not compared: tag values (timer presets in tag data, setpoints, initial values), HMI files, device and
I/O configuration.

Usage:
  python3 diff_exports.py OLD NEW [--md report.md] [-o out.json] [--context 1]
"""

import argparse
import difflib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dependency_map as dm  # noqa: E402


def norm(text):
    return re.sub(r"\s+", " ", (text or "").strip())


def items_of(r):
    if r.get("rungs"):
        return "rung", [(x.get("number"), x.get("text") or "", x.get("comment")) for x in r["rungs"]]
    if r.get("lines"):
        return "line", [(x.get("number"), x.get("text") or "", None) for x in r["lines"]]
    if r.get("networks"):
        return "network", [(x.get("number"), x.get("text") or "", x.get("comment") or x.get("title")) for x in r["networks"]]
    return "item", []


def rid(r):
    return "%s/%s" % (r["program"], r["name"]) if r.get("program") and r.get("program") != r.get("name") else r.get("name")


def refs(text, kind, language, known_types):
    if not text.strip():
        return set(), set()
    if kind == "rung" and language not in ("ST", "SCL"):
        rs = dm.refs_from_ladder(text, {})
    else:
        rs = dm.refs_from_st(text, language == "SCL", known_types)
    reads = {t.lstrip("#").split("[")[0] for t, a, _ in rs if a == "read"}
    writes = {t.lstrip("#").split("[")[0] for t, a, _ in rs if a in ("write", "unknown")}
    return reads, writes


def index(projects):
    routines, tags, types, tasks, excluded, third = {}, {}, {}, {}, {}, {}
    for pr in projects:
        for r in pr.get("routines", []):
            routines[rid(r)] = r
        for t in pr.get("tags", []):
            tags[(t.get("scope") or "") + "::" + (t.get("name") or "")] = t
        for d in pr.get("data_types", []):
            types[d.get("name")] = d
        for t in pr.get("tasks", []):
            tasks[t.get("name")] = t
        for e in pr.get("excluded_safety", []):
            excluded[e.get("name")] = e
        for e in pr.get("third_party", []):
            third[e.get("name")] = e
    return routines, tags, types, tasks, excluded, third


def iface(r):
    out = {}
    for sec in r.get("interface") or []:
        for m in sec.get("members") or []:
            out[m.get("name")] = "%s %s" % (sec.get("section"), m.get("data_type"))
    return out


def diff_routine(old, new, context, known_types):
    kind, a = items_of(old)
    _, b = items_of(new)
    lang = new.get("language") or old.get("language")
    sm = difflib.SequenceMatcher(a=[norm(x[1]) for x in a], b=[norm(x[1]) for x in b], autojunk=False)
    hunks = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            # comment-only changes on otherwise equal items
            for k in range(i2 - i1):
                ca, cb = a[i1 + k][2], b[j1 + k][2]
                if norm(ca) != norm(cb):
                    hunks.append({"op": "comment", "kind": kind, "old": [{"number": a[i1 + k][0], "comment": ca}],
                                  "new": [{"number": b[j1 + k][0], "comment": cb}], "reads": [], "writes": []})
            continue
        old_items = [{"number": x[0], "text": x[1]} for x in a[i1:i2]]
        new_items = [{"number": x[0], "text": x[1]} for x in b[j1:j2]]
        ro, wo = refs("\n".join(x[1] for x in a[i1:i2]), kind, lang, known_types)
        rn, wn = refs("\n".join(x[1] for x in b[j1:j2]), kind, lang, known_types)
        before = [{"number": x[0], "text": x[1]} for x in a[max(0, i1 - context):i1]] if context else []
        hunks.append({"op": {"replace": "changed", "delete": "removed", "insert": "added"}[op], "kind": kind,
                      "old": old_items, "new": new_items, "context_before": before,
                      "reads": sorted(ro | rn), "writes": sorted(wo | wn),
                      "writes_removed": sorted(wo - wn), "writes_added": sorted(wn - wo)})
    io, inew = iface(old), iface(new)
    iface_changes = []
    for n in sorted(set(io) | set(inew), key=str):
        if io.get(n) != inew.get(n):
            iface_changes.append({"name": n, "old": io.get(n), "new": inew.get(n)})
    meta = {}
    for k in ("language", "extends", "implements", "kind"):
        if old.get(k) != new.get(k):
            meta[k] = {"old": old.get(k), "new": new.get(k)}
    return hunks, iface_changes, meta


def compare(old_projects, new_projects, context=1):
    ro, to, dto, tko, eo, tpo = index(old_projects)
    rn, tn, dtn, tkn, en, tpn = index(new_projects)
    known_types = {n.upper() for n in list(dto) + list(dtn) if n}
    res = {"tool": "rungstone/diff", "routines": {"added": [], "removed": [], "changed": []},
           "tags": {"added": [], "removed": [], "changed": []}, "data_types": {"added": [], "removed": [], "changed": []},
           "tasks": [], "safety": [], "third_party": [], "affects": []}
    for k in sorted(set(rn) - set(ro), key=str):
        r = rn[k]
        kind, items = items_of(r)
        rd, wr = refs("\n".join(x[1] for x in items), kind, r.get("language"), known_types)
        res["routines"]["added"].append({"routine": k, "kind": r.get("kind"), "items": len(items), "writes": sorted(wr)})
    for k in sorted(set(ro) - set(rn), key=str):
        r = ro[k]
        kind, items = items_of(r)
        rd, wr = refs("\n".join(x[1] for x in items), kind, r.get("language"), known_types)
        res["routines"]["removed"].append({"routine": k, "kind": r.get("kind"), "items": len(items), "writes": sorted(wr)})
    for k in sorted(set(ro) & set(rn), key=str):
        hunks, ifc, meta = diff_routine(ro[k], rn[k], context, known_types)
        if hunks or ifc or meta:
            res["routines"]["changed"].append({"routine": k, "hunks": hunks, "interface": ifc, "meta": meta})

    def tag_view(t):
        return {k: t.get(k) for k in ("data_type", "alias_for", "address", "description", "retain", "persistent", "constant")
                if t.get(k) is not None}
    for k in sorted(set(tn) - set(to)):
        res["tags"]["added"].append(dict(name=k.split("::", 1)[1], scope=k.split("::", 1)[0], **tag_view(tn[k])))
    for k in sorted(set(to) - set(tn)):
        res["tags"]["removed"].append(dict(name=k.split("::", 1)[1], scope=k.split("::", 1)[0], **tag_view(to[k])))
    for k in sorted(set(to) & set(tn)):
        a, b = tag_view(to[k]), tag_view(tn[k])
        if a != b:
            res["tags"]["changed"].append({"name": k.split("::", 1)[1], "scope": k.split("::", 1)[0],
                                           "changes": {f: {"old": a.get(f), "new": b.get(f)} for f in set(a) | set(b)
                                                       if a.get(f) != b.get(f)}})
    for k in sorted(set(dtn) - set(dto), key=str):
        res["data_types"]["added"].append(k)
    for k in sorted(set(dto) - set(dtn), key=str):
        res["data_types"]["removed"].append(k)
    for k in sorted(set(dto) & set(dtn), key=str):
        ma = {m.get("name"): m.get("data_type") for m in dto[k].get("members") or []}
        mb = {m.get("name"): m.get("data_type") for m in dtn[k].get("members") or []}
        if ma != mb or dto[k].get("kind") != dtn[k].get("kind"):
            res["data_types"]["changed"].append({"name": k, "members": [
                {"name": n, "old": ma.get(n), "new": mb.get(n)} for n in sorted(set(ma) | set(mb), key=str) if ma.get(n) != mb.get(n)]})
    for k in sorted(set(tko) | set(tkn), key=str):
        a, b = tko.get(k), tkn.get(k)
        if a != b:
            res["tasks"].append({"name": k, "old": a, "new": b})
    for k in sorted(set(eo) | set(en), key=str):
        if k not in eo:
            res["safety"].append({"name": k, "change": "added", "reason": en[k].get("reason")})
        elif k not in en:
            res["safety"].append({"name": k, "change": "removed", "reason": eo[k].get("reason")})
        else:
            res["safety"].append({"name": k, "change": "present in both (not compared)", "reason": en[k].get("reason")})
    for k in sorted(set(tpo) | set(tpn), key=str):
        if (k in tpo) != (k in tpn):
            res["third_party"].append({"name": k, "change": "added" if k in tpn else "removed"})

    # what the changed writes reach: every other reader of those tags in the new version
    written, shown, owners = {}, {}, {}
    for c in res["routines"]["changed"]:
        for h in c["hunks"]:
            for t in h.get("writes", []):
                shown.setdefault(t.lower(), t)
                owners.setdefault(t.lower(), set()).update(c["routine"].lower().split("/"))
                written.setdefault(t.lower(), set()).add("%s %s %s" % (c["routine"], h["kind"],
                                                                       ",".join(str(x["number"]) for x in (h["new"] or h["old"]))))
    for c in res["routines"]["added"] + res["routines"]["removed"]:
        for t in c["writes"]:
            shown.setdefault(t.lower(), t)
            written.setdefault(t.lower(), set()).add(c["routine"])
    if written:
        dep = dm.analyse(new_projects, [])
        readers = {t["tag"].lower(): t.get("readers") or [] for t in dep["tags"]}
        readers.update({t.get("key", "").lower(): t.get("readers") or [] for t in dep["tags"]})
        for tag, srcs in sorted(written.items()):
            cands = [tag, tag.split(".")[0]] + ["%s.%s" % (o, tag) for o in sorted(owners.get(tag, ()))]
            rd = next((readers[c] for c in cands if readers.get(c)), [])
            res["affects"].append({"tag": shown[tag], "changed_in": sorted(srcs), "read_by": rd[:12]})
    n = res["routines"]
    res["summary"] = {"routines_added": len(n["added"]), "routines_removed": len(n["removed"]),
                      "routines_changed": len(n["changed"]),
                      "items_changed": sum(len(c["hunks"]) for c in n["changed"]),
                      "tags_added": len(res["tags"]["added"]), "tags_removed": len(res["tags"]["removed"]),
                      "tags_changed": len(res["tags"]["changed"]), "tasks_changed": len(res["tasks"]),
                      "safety_items": len(res["safety"])}
    res["not_compared"] = ["Safety logic (excluded from both versions).",
                           "Tag values: timer presets held in tag data, setpoints, initial values.",
                           "HMI/SCADA files, device, I/O and network configuration."]
    return res


def code(lines, sign):
    return "\n".join("%s %s%s" % (sign, "%s: " % x["number"] if x.get("number") is not None else "", x["text"]) for x in lines)


def to_markdown(res, old_name, new_name):
    s = res["summary"]
    out = ["# Changes: %s -> %s" % (old_name, new_name), "",
           "%d routine(s) changed (%d rung/line changes), %d added, %d removed; %d tag(s) added, %d removed, %d changed; "
           "%d task change(s)." % (s["routines_changed"], s["items_changed"], s["routines_added"], s["routines_removed"],
                                   s["tags_added"], s["tags_removed"], s["tags_changed"], s["tasks_changed"]), ""]
    if res["safety"]:
        out += ["**Safety logic is not compared.** " + "; ".join("%s: %s" % (x["name"], x["change"]) for x in res["safety"]) +
                ". If it may have changed, the safety team reviews it.", ""]
    if res["affects"]:
        out += ["## What the changes reach", "", "| Tag written by changed logic | Changed in | Also read by |", "|---|---|---|"]
        for a in res["affects"]:
            out.append("| %s | %s | %s |" % (a["tag"], "; ".join(a["changed_in"]), "; ".join(a["read_by"]) or "nothing else"))
        out.append("")
    if res["routines"]["changed"]:
        out += ["## Logic changes", ""]
        for c in res["routines"]["changed"]:
            out.append("### %s" % c["routine"])
            for k, v in c["meta"].items():
                out.append("- %s: %s -> %s" % (k, v["old"], v["new"]))
            for i in c["interface"]:
                out.append("- Declaration `%s`: %s -> %s" % (i["name"], i["old"] or "(none)", i["new"] or "(removed)"))
            for h in c["hunks"]:
                nums = [x["number"] for x in (h["new"] or h["old"])]
                where = "%s %s" % (h["kind"], nums[0] if len(nums) == 1 else "%s-%s" % (nums[0], nums[-1])) if nums else h["kind"]
                if h["op"] == "comment":
                    out.append("- %s comment: \"%s\" -> \"%s\"" % (where, h["old"][0]["comment"] or "", h["new"][0]["comment"] or ""))
                    continue
                out.append("")
                out.append("**%s %s**%s" % (h["op"].capitalize(), where,
                                           " (writes: %s)" % ", ".join(h["writes"]) if h["writes"] else ""))
                out.append("```diff")
                if h["old"]:
                    out.append(code(h["old"], "-"))
                if h["new"]:
                    out.append(code(h["new"], "+"))
                out.append("```")
            out.append("")
    for label, key in (("Added", "added"), ("Removed", "removed")):
        if res["routines"][key]:
            out += ["## %s routines / POUs" % label, ""]
            out += ["- %s (%s, %d items)%s" % (r["routine"], r.get("kind") or "", r["items"],
                                               ": writes " + ", ".join(r["writes"]) if r["writes"] else "") for r in res["routines"][key]]
            out.append("")
    t = res["tags"]
    if t["added"] or t["removed"] or t["changed"]:
        out += ["## Tags", "", "| Change | Tag | Detail |", "|---|---|---|"]
        for x in t["added"]:
            out.append("| added | %s | %s |" % (x["name"], x.get("data_type") or x.get("alias_for") or ""))
        for x in t["removed"]:
            out.append("| removed | %s | %s |" % (x["name"], x.get("data_type") or x.get("alias_for") or ""))
        for x in t["changed"]:
            out.append("| changed | %s | %s |" % (x["name"], "; ".join("%s: %s -> %s" % (k, v["old"], v["new"])
                                                                       for k, v in sorted(x["changes"].items()))))
        out.append("")
    d = res["data_types"]
    if d["added"] or d["removed"] or d["changed"]:
        out += ["## Data types", ""]
        out += ["- added: %s" % x for x in d["added"]] + ["- removed: %s" % x for x in d["removed"]]
        for x in d["changed"]:
            out.append("- %s: %s" % (x["name"], "; ".join("%s %s -> %s" % (m["name"], m["old"], m["new"]) for m in x["members"])))
        out.append("")
    if res["tasks"]:
        out += ["## Tasks", ""]
        for x in res["tasks"]:
            if not x["old"] or not x["new"]:
                out.append("- %s: %s" % (x["name"], "added" if x["new"] else "removed"))
                continue
            out.append("- %s: %s" % (x["name"], "; ".join("%s %s -> %s" % (k, x["old"].get(k), x["new"].get(k))
                                                          for k in sorted(set(x["old"]) | set(x["new"]))
                                                          if x["old"].get(k) != x["new"].get(k))))
        out.append("")
    if res["third_party"]:
        out += ["## Library / OEM code", ""] + ["- %s %s" % (x["name"], x["change"]) for x in res["third_party"]] + [""]
    out += ["## Not compared", ""] + ["- " + x for x in res["not_compared"]]
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--md")
    ap.add_argument("-o", "--out")
    ap.add_argument("--context", type=int, default=1)
    ap.add_argument("--allow-suspected", action="append", default=[])
    a = ap.parse_args(argv)
    res = compare(dm.load_projects([a.old], a.allow_suspected), dm.load_projects([a.new], a.allow_suspected), a.context)
    res["old"], res["new"] = a.old, a.new
    if a.md:
        os.makedirs(os.path.dirname(os.path.abspath(a.md)), exist_ok=True)
        with open(a.md, "w", encoding="utf-8") as f:
            f.write(to_markdown(res, os.path.basename(a.old), os.path.basename(a.new)))
    text = json.dumps(res, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(json.dumps({"written": a.out, "md": a.md, "summary": res["summary"]}))
    else:
        print(text)


if __name__ == "__main__":
    main()
