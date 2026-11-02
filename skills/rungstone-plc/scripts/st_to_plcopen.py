"""
st_to_plcopen.py - wrap a generated IEC 61131-3 .st file as PLCopen XML (TC6 v2.01).

For OpenPLC Editor / Beremiz, which keep projects as PLCopen XML (plc.xml), and for any other tool
that imports PLCopenXML. Reads Rungstone's own generated ST: TYPE blocks (struct, enum, alias),
PROGRAM / FUNCTION_BLOCK / FUNCTION with their VAR sections, and the CONFIGURATION (tasks, program
instances, globals). Bodies stay ST. Comments after declarations become documentation.

Scalar initial values are kept; array/struct initialisers are not (listed under "warnings").

Usage:
  python3 st_to_plcopen.py FILE.st -o plc.xml [--project-name NAME]
"""

import argparse
import datetime
import json
import re
import sys
from xml.sax.saxutils import quoteattr

ELEMENTARY = {"BOOL", "SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT", "REAL", "LREAL", "TIME",
              "DATE", "TOD", "DT", "TIME_OF_DAY", "DATE_AND_TIME", "BYTE", "WORD", "DWORD", "LWORD"}
SECTION_XML = {"VAR_INPUT": "inputVars", "VAR_OUTPUT": "outputVars", "VAR_IN_OUT": "inOutVars", "VAR": "localVars",
               "VAR_TEMP": "tempVars", "VAR_EXTERNAL": "externalVars", "VAR_GLOBAL": "globalVars"}
POU_XML = {"PROGRAM": "program", "FUNCTION_BLOCK": "functionBlock", "FUNCTION": "function"}
NS = "http://www.plcopen.org/xml/tc6_0201"
XHTML = "http://www.w3.org/1999/xhtml"


def split_comment(line):
    m = re.search(r"\(\*(.*?)\*\)\s*$", line)
    if m:
        return line[:m.start()].rstrip(), m.group(1).strip()
    return line.rstrip(), None


def type_xml(t):
    t = t.strip()
    u = t.upper()
    m = re.match(r"ARRAY\s*\[(.+?)\]\s*OF\s+(.+)$", t, re.I)
    if m:
        dims = "".join('<dimension lower="%s" upper="%s"/>' % tuple(x.strip() for x in d.split(".."))
                       for d in m.group(1).split(","))
        return "<array>%s<baseType>%s</baseType></array>" % (dims, type_xml(m.group(2)))
    m = re.match(r"(W?STRING)\s*[\[(]\s*(\d+)\s*[\])]$", t, re.I)
    if m:
        return '<%s length="%s"/>' % (m.group(1).lower(), m.group(2))
    if u in ("STRING", "WSTRING"):
        return "<%s/>" % u.lower()
    if u in ELEMENTARY:
        return "<%s/>" % {"TIME_OF_DAY": "TOD", "DATE_AND_TIME": "DT"}.get(u, u)
    return "<derived name=%s/>" % quoteattr(t)


def parse_decl(line, warnings, where):
    """'a, b AT %IX0.0 : BOOL := TRUE; (* c *)' -> list of dicts."""
    body, comment = split_comment(line)
    m = re.match(r"^\s*([\w\s,]+?)\s*(?:AT\s+(%\S+))?\s*:\s*(.+?)\s*(?::=\s*(.+?))?\s*;\s*$", body, re.I)
    if not m:
        return []
    init = m.group(4)
    if init and (init.strip().startswith(("[", "(")) or "," in init):
        warnings.append("%s: initial value '%s' not exported (array/struct initialiser); set it in the editor." % (where, init))
        init = None
    return [{"name": n.strip(), "address": m.group(2), "type": m.group(3).strip(), "init": init, "doc": comment}
            for n in m.group(1).split(",") if n.strip()]


def var_xml(v, indent):
    attrs = ' name=%s' % quoteattr(v["name"])
    if v.get("address"):
        attrs += " address=%s" % quoteattr(v["address"])
    out = '%s<variable%s><type>%s</type>' % (indent, attrs, type_xml(v["type"]))
    if v.get("init"):
        out += '<initialValue><simpleValue value=%s/></initialValue>' % quoteattr(v["init"].strip())
    if v.get("doc"):
        out += '<documentation><xhtml:p><![CDATA[%s]]></xhtml:p></documentation>' % v["doc"].replace("]]>", "]] >")
    return out + "</variable>"


def sections_xml(sections, indent):
    out = []
    for sec in sections:
        tag = SECTION_XML.get(sec["kind"])
        if not tag or not sec["vars"]:
            continue
        q = "".join(' %s="true"' % k for k in ("retain", "constant") if sec.get(k))
        out.append("%s<%s%s>" % (indent, tag, q))
        out += [var_xml(v, indent + "  ") for v in sec["vars"]]
        out.append("%s</%s>" % (indent, tag))
    return out


def read_sections(lines, warnings, where):
    sections, cur = [], None
    for ln in lines:
        s = ln.strip()
        m = re.match(r"^(VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT|VAR_TEMP|VAR_EXTERNAL|VAR_GLOBAL|VAR)\b(.*)$", s, re.I)
        if m and cur is None:
            q = m.group(2).upper()
            cur = {"kind": m.group(1).upper(), "vars": [], "retain": "RETAIN" in q and "NON_RETAIN" not in q,
                   "constant": "CONSTANT" in q}
            sections.append(cur)
            continue
        if re.match(r"^END_VAR\b", s, re.I):
            cur = None
            continue
        if cur is not None and s and not s.startswith("(*"):
            cur["vars"] += parse_decl(s, warnings, where)
    return sections


def convert(text, project_name="Rungstone"):
    warnings = []
    pous, types, tasks, globals_ = [], [], [], []
    header = re.match(r"\s*\(\*(.*?)\*\)", text, re.S)
    header = header.group(1).strip() if header else ""

    for m in re.finditer(r"^\s*TYPE\b(.*?)^\s*END_TYPE\b", text, re.S | re.M | re.I):
        body = m.group(1)
        for sm in re.finditer(r"(\w+)\s*:\s*STRUCT\b(.*?)END_STRUCT\s*;", body, re.S | re.I):
            members = []
            for ln in sm.group(2).splitlines():
                members += parse_decl(ln, warnings, "TYPE " + sm.group(1))
            types.append({"name": sm.group(1), "kind": "struct", "members": members})
        rest = re.sub(r"(\w+)\s*:\s*STRUCT\b.*?END_STRUCT\s*;", "", body, flags=re.S | re.I)
        for em in re.finditer(r"(\w+)\s*:\s*\(([^)]*)\)\s*(?::=\s*(\w+))?\s*;", rest):
            types.append({"name": em.group(1), "kind": "enum", "values": [v.strip() for v in em.group(2).split(",") if v.strip()]})
        rest = re.sub(r"(\w+)\s*:\s*\([^)]*\)[^;]*;", "", rest)
        for am in re.finditer(r"(\w+)\s*:\s*([^;:=]+?)\s*(?::=[^;]*)?;", rest):
            types.append({"name": am.group(1), "kind": "alias", "base": am.group(2).strip()})

    cfg = re.search(r"^\s*CONFIGURATION\s+(\w+)(.*?)^\s*END_CONFIGURATION\b", text, re.S | re.M | re.I)
    cfg_name, res_name = "Config0", "Res0"
    if cfg:
        cfg_name = cfg.group(1)
        rm = re.search(r"RESOURCE\s+(\w+)\s+ON\s+\w+", cfg.group(2), re.I)
        res_name = rm.group(1) if rm else res_name
        for tm in re.finditer(r"TASK\s+(\w+)\s*\(([^)]*)\)", cfg.group(2), re.I):
            args = dict((k.strip().upper(), v.strip()) for k, v in (a.split(":=", 1) for a in tm.group(2).split(",") if ":=" in a))
            tasks.append({"name": tm.group(1), "interval": args.get("INTERVAL"), "priority": args.get("PRIORITY", "0"), "instances": []})
        for pm in re.finditer(r"PROGRAM\s+(\w+)\s+WITH\s+(\w+)\s*:\s*(\w+)\s*;", cfg.group(2), re.I):
            for t in tasks:
                if t["name"].lower() == pm.group(2).lower():
                    t["instances"].append((pm.group(1), pm.group(3)))
        for sec in read_sections(cfg.group(2).splitlines(), warnings, "CONFIGURATION"):
            if sec["kind"] == "VAR_GLOBAL":
                globals_.append(sec)
    text_wo_cfg = text[:cfg.start()] + text[cfg.end():] if cfg else text

    for m in re.finditer(r"^\s*(PROGRAM|FUNCTION_BLOCK|FUNCTION)\s+(\w+)\s*(?::\s*(\w+))?(.*?)^\s*END_\1\b",
                         text_wo_cfg, re.S | re.M | re.I):
        kind, name, ret, inner = m.group(1).upper(), m.group(2), m.group(3), m.group(4)
        lines = inner.splitlines()
        last_end = max([i for i, l in enumerate(lines) if re.match(r"^\s*END_VAR\b", l, re.I)], default=-1)
        sections = read_sections(lines[:last_end + 1], warnings, "%s %s" % (kind, name))
        body = "\n".join(lines[last_end + 1:]).strip("\n")
        body = "\n".join(l[2:] if l.startswith("  ") else l for l in body.splitlines())
        pous.append({"kind": kind, "name": name, "return": ret, "sections": sections, "body": body})

    now = datetime.datetime.now().replace(microsecond=0).isoformat()
    x = ['<?xml version="1.0" encoding="utf-8"?>',
         '<!-- GENERATED BY Rungstone - DRAFT FOR ENGINEER REVIEW - NOT APPROVED. Not for use on a production system '
         'without review and testing by a qualified engineer. -->',
         '<project xmlns="%s" xmlns:xhtml="%s" xmlns:xsd="http://www.w3.org/2001/XMLSchema">' % (NS, XHTML),
         '  <fileHeader companyName="Rungstone" productName="Rungstone" productVersion="1" creationDateTime="%s"/>' % now,
         '  <contentHeader name=%s modificationDateTime="%s">' % (quoteattr(project_name), now),
         '    <coordinateInfo><fbd><scaling x="10" y="10"/></fbd><ld><scaling x="10" y="10"/></ld>'
         '<sfc><scaling x="10" y="10"/></sfc></coordinateInfo>',
         '  </contentHeader>', '  <types>', '    <dataTypes>']
    for t in types:
        x.append('      <dataType name=%s><baseType>' % quoteattr(t["name"]))
        if t["kind"] == "struct":
            x.append("        <struct>")
            x += [var_xml(v, "          ") for v in t["members"]]
            x.append("        </struct>")
        elif t["kind"] == "enum":
            x.append("        <enum><values>%s</values></enum>" % "".join(
                "<value name=%s/>" % quoteattr(v.split(":=")[0].strip()) for v in t["values"]))
        else:
            x.append("        %s" % type_xml(t["base"]))
        x.append("      </baseType></dataType>")
    x += ["    </dataTypes>", "    <pous>"]
    for p in pous:
        x.append('      <pou name=%s pouType="%s">' % (quoteattr(p["name"]), POU_XML[p["kind"]]))
        x.append("        <interface>")
        if p["return"]:
            x.append("          <returnType>%s</returnType>" % type_xml(p["return"]))
        x += sections_xml(p["sections"], "          ")
        x.append("        </interface>")
        x.append("        <body><ST><xhtml:p><![CDATA[%s]]></xhtml:p></ST></body>" % p["body"].replace("]]>", "]] >"))
        if p is pous[0] and header:
            x.append("        <documentation><xhtml:p><![CDATA[%s]]></xhtml:p></documentation>" % header.replace("]]>", "]] >"))
        x.append("      </pou>")
    x += ["    </pous>", "  </types>", "  <instances>", "    <configurations>",
          '      <configuration name=%s>' % quoteattr(cfg_name), '        <resource name=%s>' % quoteattr(res_name)]
    for t in tasks:
        attrs = ' name=%s priority=%s' % (quoteattr(t["name"]), quoteattr(t["priority"]))
        if t["interval"]:
            attrs += " interval=%s" % quoteattr(t["interval"])
        x.append("          <task%s>" % attrs)
        x += ['            <pouInstance name=%s typeName=%s/>' % (quoteattr(i), quoteattr(ty)) for i, ty in t["instances"]]
        x.append("          </task>")
    x += sections_xml(globals_, "          ")
    x += ["        </resource>", "      </configuration>", "    </configurations>", "  </instances>", "</project>"]
    if not pous:
        warnings.append("No PROGRAM / FUNCTION_BLOCK / FUNCTION found.")
    return "\n".join(x) + "\n", {"pous": [p["name"] for p in pous], "types": [t["name"] for t in types],
                                 "tasks": [t["name"] for t in tasks], "warnings": warnings}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--project-name", default="Rungstone translation")
    a = ap.parse_args(argv)
    with open(a.file, encoding="utf-8") as f:
        xml, info = convert(f.read(), a.project_name)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(xml)
    info["written"] = a.out
    print(json.dumps(info, indent=1))


if __name__ == "__main__":
    main()
