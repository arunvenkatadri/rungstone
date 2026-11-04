"""
openplc_check.py - check a generated .st file against what OpenPLC Runtime / Beremiz (MatIEC) accept.

Run it on every OpenPLC translation before showing it to the engineer. It reads the file as text and
never runs it. Errors are things the OpenPLC upload would reject or silently get wrong; warnings
are behaviour differences the engineer has to decide on.

Checks:
  structure    PROGRAM/FUNCTION_BLOCK/FUNCTION/TYPE blocks balanced; one CONFIGURATION Config0 with
               RESOURCE Res0 (the OpenPLC compile script expects exactly these names); every
               program instance names a declared PROGRAM and task
  syntax       only (* *) comments; no {...} pragmas (MatIEC passes them through as C code); no
               CODESYS/TwinCAT extensions (OOP, POINTER TO/ADR, __NEW, __TRY, S=/R=, bit access x.3,
               __XWORD, LTIME...). REF_TO / REF() are accepted: OpenPLC compiles with references on.
  names        no POU, type or variable named like a standard function/block (TON, MID, INT_TO_REAL...)
               or an IEC keyword (STEP, ACTION, FROM, ON, TASK, RETAIN...); no POU or type named
               like the standard blocks' parameters (IN, P, L, N, K, Q...)
  leftovers    Siemens SCL syntax: BEGIN, REGION/END_REGION, #local
  located vars located (AT %...) and plain variables in separate VAR blocks;
               %IX/%QX a.b (b 0-7), %IB/%IW/%ID/%IL, %QB/%QW/%QD/%QL, %MW/%MD/%ML, index 0-1023; no
               %MX/%MB; type width matches the size letter; no duplicates; only in PROGRAM or
               CONFIGURATION (not in FUNCTION_BLOCK/FUNCTION)
  behaviour    VAR RETAIN is not kept by OpenPLC (only %MW/%MD/%ML with Persistent Storage on);
               more than one task; missing DRAFT header

Optional: --iec2c PATH --iec-lib DIR also compiles the file with MatIEC's iec2c (as OpenPLC does)
into a temporary folder and reports its errors. Nothing is uploaded or run.

Usage:
  python3 openplc_check.py FILE.st [--iec2c PATH --iec-lib DIR] [-o out.json]
Exit code 0 when there are no errors, 1 otherwise.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile


SIZE_TYPES = {
    "X": {"BOOL"},
    "B": {"BYTE", "SINT", "USINT"},
    "W": {"INT", "UINT", "WORD"},
    "D": {"DINT", "UDINT", "DWORD", "REAL"},
    "L": {"LINT", "ULINT", "LWORD", "LREAL"},
}
ADDR_RE = re.compile(r"%([A-Z]+)([\d.*]*)", re.I)
LOCATED_RE = re.compile(r"^\s*(\w+)\s+AT\s+(%\S+?)\s*:\s*([\w\[\]. ]+?)\s*(?::=[^;]*)?;", re.I)
BLOCK_RE = re.compile(r"^\s*(PROGRAM|FUNCTION_BLOCK|FUNCTION|TYPE|CONFIGURATION|RESOURCE)\b\s*(\w+)?", re.I)
END_RE = re.compile(r"^\s*END_(PROGRAM|FUNCTION_BLOCK|FUNCTION|TYPE|CONFIGURATION|RESOURCE)\b", re.I)
VAR_RE = re.compile(r"^\s*(VAR\w*)\b([^;]*)$", re.I)

# Names in MatIEC's standard library (OpenPLC's lib/*.txt). A POU, type or variable with one of these
# names fails to compile (TON as a program name crashes the compiler). Type-suffixed and _TO_
# conversion variants (ADD_INT, INT_TO_REAL...) are matched by STD_VARIANT_RE.
STD_NAMES = set("""ABS ACOS ADD AND ASIN ATAN CONCAT COS CTD CTU CTUD DELETE DIV DIVTIME EQ EXP EXPT FIND F_TRIG GE
GT HYSTERESIS INSERT LE LEFT LEN LIMIT LN LOG LT MAX MID MIN MOD MOVE MUL MULTIME MUX NE NOT OR REPLACE RIGHT ROL
ROR RS RTC R_TRIG SEL SEMA SHL SHR SIN SQRT SR SUB TAN TOF TON TP TRUNC XOR DERIVATIVE INTEGRAL PID RAMP""".split())
ELEMENTARY_RE = r"(BOOL|SINT|INT|DINT|LINT|USINT|UINT|UDINT|ULINT|REAL|LREAL|TIME|DATE|TOD|DT|BYTE|WORD|DWORD|LWORD|STRING)"
STD_VARIANT_RE = re.compile(r"^(%s_TO_%s|(BYTE|WORD|DWORD|LWORD)_BCD_TO_\w+|\w+_TO_BCD_\w+|(%s)_%s|(%s)_(DATE_TOD|DT_TIME|TOD_TIME)|"
                            r"DATE_AND_TIME_TO_\w+)$" % (ELEMENTARY_RE, ELEMENTARY_RE, "|".join(sorted(STD_NAMES)), ELEMENTARY_RE,
                                                        "|".join(sorted(STD_NAMES))), re.I)
# Parameter names of the standard functions: a POU or type with one of these names breaks the library.
STD_PARAMS = set("CLK G IN IN0 IN1 IN2 K L MN MX N P Q".split())
# IEC keywords MatIEC won't take as a name, checked against OpenPLC's MatIEC build. STEP is the common
# one: SCL sequences often have a variable called Step. END_ACTION and END_TRANSITION hang iec2c.
RESERVED = set("""ACTION AND ARRAY AT BY CASE CONFIGURATION CONSTANT DO ELSE ELSIF EN END_ACTION END_CASE END_FOR
END_IF END_REPEAT END_STEP END_TRANSITION END_WHILE ENO EXIT FALSE FOR FROM FUNCTION FUNCTION_BLOCK F_EDGE IF
INITIAL_STEP MOD NON_RETAIN NOT NULL OF ON OR PROGRAM READ_ONLY READ_WRITE REF REF_TO REPEAT RESOURCE RETAIN RETURN
R_EDGE STEP STRUCT TASK THEN TO TRANSITION TRUE TYPE UNTIL VAR VAR_ACCESS VAR_CONFIG VAR_EXTERNAL VAR_GLOBAL
VAR_INPUT VAR_IN_OUT VAR_OUTPUT VAR_TEMP WHILE WITH XOR""".split())
RESERVED_RE = re.compile(r"^%s$" % ELEMENTARY_RE, re.I)


def std_clash(name):
    u = name.upper()
    return u in STD_NAMES or bool(STD_VARIANT_RE.match(u))


def reserved(name):
    u = name.upper()
    return u in RESERVED or bool(RESERVED_RE.match(u))


# Syntax MatIEC doesn't take, with the reason. Comments and strings are blanked before matching.
FORBIDDEN = [
    (re.compile(r"\{"), "pragma", "{...} pragmas: MatIEC passes them through as C code. Remove them."),
    (re.compile(r"\bEXTENDS\b|\bIMPLEMENTS\b", re.I), "oop", "EXTENDS/IMPLEMENTS: no inheritance or interfaces; flatten."),
    (re.compile(r"^\s*(METHOD|PROPERTY|INTERFACE)\b", re.I | re.M), "oop", "METHOD/PROPERTY/INTERFACE: not supported; use FB inputs/outputs."),
    (re.compile(r"\b(THIS|SUPER)\s*\^", re.I), "oop", "THIS^/SUPER^: not supported."),
    (re.compile(r"\bPOINTER\s+TO\b|\bADR\s*\(", re.I), "pointer", "POINTER TO / ADR(): use REF_TO / REF() or array indexing."),
    (re.compile(r"\bREFERENCE\s+TO\b|__ISVALIDREF", re.I), "pointer", "REFERENCE TO: use VAR_IN_OUT or REF_TO."),
    (re.compile(r"__(NEW|DELETE)\s*\(", re.I), "memory", "__NEW/__DELETE: no dynamic memory; use fixed arrays."),
    (re.compile(r"__(TRY|CATCH|FINALLY|ENDTRY)\b", re.I), "exception", "__TRY/__CATCH: not supported; check before the operation."),
    (re.compile(r"\b(MEMCPY|MEMSET|MEMCMP|SIZEOF)\s*\(", re.I), "memory", "Raw memory functions: not available; copy element by element."),
    (re.compile(r"[\w\]\)]\s+[SR]=(?!=)"), "operator", "S= / R= assignments: write IF ... THEN x := TRUE; END_IF."),
    (re.compile(r"(?<![\w.#%])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\.\d+\b(?!\.\d)"), "bit_access",
     "Bit access x.3: use AND with a mask and SHR."),
    (re.compile(r"\b__(XWORD|UXINT|XINT)\b", re.I), "type", "__XWORD/__UXINT/__XINT: use a fixed-width type."),
    (re.compile(r"\b(LTIME|LDATE|LTOD|LDT)\b|\bLTIME#", re.I), "type", "LTIME/LDATE/LTOD/LDT: use TIME/DATE/TOD/DT."),
    (re.compile(r"\bUNION\b", re.I), "type", "UNION: not supported."),
    (re.compile(r"\bCONTINUE\b", re.I), "statement", "CONTINUE: not in MatIEC; restructure the loop."),
    (re.compile(r"^\s*BEGIN\s*$", re.I), "scl", "BEGIN: Siemens SCL; IEC ST has no BEGIN between the declarations and the code. Remove it."),
    (re.compile(r"^\s*(END_)?REGION\b(?!\s*[:.\[(,])", re.I), "scl", "REGION/END_REGION: Siemens SCL; write the region name as a (* comment *)."),
    (re.compile(r"(?<![\w#])#[A-Za-z_]"), "scl", "#name: Siemens SCL local-variable prefix; drop the #."),
    (re.compile(r"\bEND_(IF|CASE|FOR|WHILE|REPEAT)\b(?!\s*;)", re.I), "semicolon",
     "END_IF/END_CASE/END_FOR/END_WHILE/END_REPEAT needs a ';' after it in MatIEC (CODESYS lets you leave it out)."),
]


def modbus_of(addr):
    """OpenPLC located address -> Modbus object it is served as (zero-based), or None if not reachable.

    From OpenPLC's Modbus server: %QX coils, %IX discrete inputs, %IW input registers, %QW holding
    registers 0-1023, %MW 1024+n, %MD 2048+2n (two registers, high word first), %ML 4096+4n.
    """
    if not addr:
        return None
    m = re.fullmatch(r"%([IQM])([XWDLB])(\d+)(?:\.(\d))?", addr.strip(), re.I)
    if not m:
        return None
    area, size, idx = m.group(1).upper(), m.group(2).upper(), int(m.group(3))
    bit = int(m.group(4)) if m.group(4) is not None else None
    if size == "X" and bit is not None:
        if area == "Q":
            return "coil %d" % (idx * 8 + bit)
        if area == "I":
            return "discrete input %d" % (idx * 8 + bit)
        return None
    if area == "I" and size == "W":
        return "input register %d" % idx
    if area == "Q" and size == "W":
        return "holding register %d" % idx
    if area == "M" and size == "W":
        return "holding register %d" % (1024 + idx)
    if area == "M" and size == "D":
        return "holding registers %d-%d" % (2048 + 2 * idx, 2049 + 2 * idx)
    if area == "M" and size == "L":
        return "holding registers %d-%d" % (4096 + 4 * idx, 4099 + 4 * idx)
    return None


def blank_comments(text, errors):
    """Return text with (* *) comments and strings blanked (newlines kept). '//' comments are errors."""
    out = []
    i, n, line = 0, len(text), 1
    depth = 0
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            out.append(c)
            i += 1
            continue
        if depth:
            if text.startswith("*)", i):
                depth -= 1
                out.append("  ")
                i += 2
            else:
                out.append(" ")
                i += 1
            continue
        if text.startswith("(*", i):
            depth = 1
            out.append("  ")
            i += 2
            continue
        if text.startswith("//", i):
            errors.append({"line": line, "rule": "comment",
                           "message": "'//' comment: MatIEC takes only (* *) comments."})
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
            continue
        if c in "'\"":
            j = text.find(c, i + 1)
            j = n if j < 0 else j + 1
            out.append(c + " " * max(0, j - i - 2) + (c if j - i >= 2 else ""))
            i = j
            continue
        out.append(c)
        i += 1
    if depth:
        errors.append({"line": line, "rule": "comment", "message": "Unclosed (* comment."})
    return "".join(out)


def check_address(addr, dtype, errors, line, name):
    m = ADDR_RE.fullmatch(addr)
    if not m:
        errors.append({"line": line, "rule": "address", "message": "%s AT %s: not an address OpenPLC understands." % (name, addr)})
        return None
    kind, pos = m.group(1).upper(), m.group(2)
    if "*" in pos:
        errors.append({"line": line, "rule": "address",
                       "message": "%s AT %s: TwinCAT-style unresolved address; give a real %%IX/%%QX/... address." % (name, addr)})
        return None
    if len(kind) != 2 or kind[0] not in "IQM" or kind[1] not in SIZE_TYPES:
        errors.append({"line": line, "rule": "address", "message": "%s AT %s: unknown area/size '%s'." % (name, addr, kind)})
        return None
    area, size = kind[0], kind[1]
    if area == "M" and size in "XB":
        errors.append({"line": line, "rule": "address",
                       "message": "%s AT %s: OpenPLC has no %%M%s memory. Use %%MW/%%MD/%%ML, or a plain variable "
                                  "(an unlocated BOOL), or %%QX if an HMI must see it." % (name, addr, size)})
        return None
    parts = pos.split(".")
    try:
        idx = int(parts[0])
        bit = int(parts[1]) if len(parts) > 1 and parts[1] != "" else None
    except ValueError:
        errors.append({"line": line, "rule": "address", "message": "%s AT %s: bad index." % (name, addr)})
        return None
    if size == "X":
        if bit is None or len(parts) != 2:
            errors.append({"line": line, "rule": "address", "message": "%s AT %s: bit addresses need byte.bit, e.g. %%IX0.3." % (name, addr)})
            return None
        if bit > 7:
            errors.append({"line": line, "rule": "address", "message": "%s AT %s: bit must be 0-7." % (name, addr)})
    elif len(parts) != 1:
        errors.append({"line": line, "rule": "address", "message": "%s AT %s: word/byte addresses have no bit part." % (name, addr)})
    if idx > 1023:
        errors.append({"line": line, "rule": "address", "message": "%s AT %s: index must be 0-1023." % (name, addr)})
    base = dtype.upper().split("[")[0].strip()
    if base not in SIZE_TYPES[size]:
        errors.append({"line": line, "rule": "address_type",
                       "message": "%s AT %s : %s: a %%%s%s location holds %s." % (
                           name, addr, dtype, area, size, "/".join(sorted(SIZE_TYPES[size])))})
    return "%%%s%s" % (kind, pos)


def check(text):
    errors, warnings = [], []
    code = blank_comments(text, errors)
    lines = code.split("\n")

    # structure
    stack, blocks, located, seen_addr = [], [], [], {}
    var_section = None
    for no, ln in enumerate(lines, 1):
        bm = BLOCK_RE.match(ln)
        if bm and not (bm.group(1).upper() == "PROGRAM" and stack and stack[-1][0] == "RESOURCE"):
            kind = bm.group(1).upper()
            stack.append((kind, bm.group(2), no))
            blocks.append({"kind": kind, "name": bm.group(2), "line": no})
            continue
        em = END_RE.match(ln)
        if em:
            kind = em.group(1).upper()
            if not stack or stack[-1][0] != kind:
                errors.append({"line": no, "rule": "structure", "message": "END_%s without a matching %s." % (kind, kind)})
            else:
                stack.pop()
            continue
        vm = VAR_RE.match(ln)
        if vm and ln.strip().upper().startswith("VAR"):
            var_section = (vm.group(1).upper() + " " + vm.group(2).upper()).strip()
            section_kinds = {"located": None, "plain": None}
            if "RETAIN" in var_section and "NON_RETAIN" not in var_section:
                warnings.append({"line": no, "rule": "retain",
                                 "message": "VAR RETAIN: OpenPLC does not keep these through a restart. Values that must "
                                            "survive go on %MW/%MD/%ML with Persistent Storage enabled (Settings)."})
            continue
        if re.match(r"^\s*END_VAR\b", ln, re.I):
            var_section = None
            continue
        if var_section:
            dm = re.match(r"^\s*(\w+(?:\s*,\s*\w+)*)\s*(AT\s+\S+\s*)?:", ln)
            if dm:
                kind = "located" if dm.group(2) else "plain"
                section_kinds[kind] = section_kinds[kind] or (dm.group(1), no)
                if section_kinds["located"] and section_kinds["plain"] and kind == ("plain" if section_kinds["plain"][1] == no else "located"):
                    other = section_kinds["located" if kind == "plain" else "plain"]
                    errors.append({"line": no, "rule": "mixed_var_block",
                                   "message": "%s and %s (line %d) are in the same VAR block. MatIEC needs located "
                                              "(AT %%...) and plain variables in separate VAR ... END_VAR blocks."
                                              % (dm.group(1), other[0], other[1])})
                for n in re.split(r"\s*,\s*", dm.group(1)):
                    if std_clash(n):
                        errors.append({"line": no, "rule": "name_clash",
                                       "message": "Variable %s has the name of a standard function or block; MatIEC "
                                                  "rejects it. Rename it." % n})
                    elif reserved(n):
                        errors.append({"line": no, "rule": "reserved_name",
                                       "message": "Variable %s is an IEC keyword; MatIEC rejects it. Rename it (e.g. "
                                                  "%sNo) everywhere it is used." % (n, n)})
        lm = LOCATED_RE.match(ln)
        if lm and var_section:
            owner = stack[-1][0] if stack else None
            if owner in ("FUNCTION_BLOCK", "FUNCTION"):
                errors.append({"line": no, "rule": "located_in_fb",
                               "message": "%s AT %s: located variables are only allowed in a PROGRAM or the "
                                          "CONFIGURATION, not in a %s." % (lm.group(1), lm.group(2), owner)})
            a = check_address(lm.group(2), lm.group(3), errors, no, lm.group(1))
            if a:
                if a.upper() in seen_addr:
                    errors.append({"line": no, "rule": "duplicate_address",
                                   "message": "%s AT %s: same address as %s (line %d)." % (
                                       lm.group(1), a, seen_addr[a.upper()][0], seen_addr[a.upper()][1])})
                seen_addr[a.upper()] = (lm.group(1), no)
                located.append({"name": lm.group(1), "address": a, "type": lm.group(3).strip(), "line": no,
                                "modbus": modbus_of(a)})
    for kind, name, no in stack:
        errors.append({"line": no, "rule": "structure", "message": "%s %s is never closed (END_%s)." % (kind, name or "", kind)})

    for b in blocks:
        if b["kind"] in ("PROGRAM", "FUNCTION_BLOCK", "FUNCTION") and b["name"] and \
                (std_clash(b["name"]) or reserved(b["name"]) or b["name"].upper() in STD_PARAMS):
            errors.append({"line": b["line"], "rule": "name_clash",
                           "message": "%s %s clashes with an IEC keyword or a name in the standard library (a function, "
                                      "block or one of their parameters: %s). Rename it, e.g. prog_%s / FB_%s."
                                      % (b["kind"], b["name"], " ".join(sorted(STD_PARAMS)), b["name"], b["name"])})
    for no, ln in enumerate(lines, 1):
        tm = re.match(r"^\s*(\w+)\s*:\s*(STRUCT\b|\()", ln)
        if tm and (std_clash(tm.group(1)) or reserved(tm.group(1)) or tm.group(1).upper() in STD_PARAMS):
            errors.append({"line": no, "rule": "name_clash",
                           "message": "Type %s clashes with an IEC keyword or a standard library name." % tm.group(1)})
    programs = {b["name"].lower() for b in blocks if b["kind"] == "PROGRAM" and b["name"]}
    configs = [b for b in blocks if b["kind"] == "CONFIGURATION"]
    resources = [b for b in blocks if b["kind"] == "RESOURCE"]
    if not programs:
        errors.append({"line": 1, "rule": "structure", "message": "No PROGRAM found."})
    if len(configs) != 1 or (configs and configs[0]["name"] != "Config0"):
        errors.append({"line": configs[0]["line"] if configs else 1, "rule": "configuration",
                       "message": "Needs exactly one 'CONFIGURATION Config0' (OpenPLC's compile script expects that name)."})
    if len(resources) != 1 or (resources and not re.search(r"\bRESOURCE\s+Res0\s+ON\s+PLC\b", lines[resources[0]["line"] - 1], re.I)):
        errors.append({"line": resources[0]["line"] if resources else 1, "rule": "configuration",
                       "message": "Needs exactly one 'RESOURCE Res0 ON PLC' inside Config0."})
    tasks = {}
    for no, ln in enumerate(lines, 1):
        tm = re.match(r"^\s*TASK\s+(\w+)\s*\(([^)]*)\)", ln, re.I)
        if tm:
            tasks[tm.group(1).lower()] = {"name": tm.group(1), "args": tm.group(2).strip(), "line": no}
    instances = []
    for no, ln in enumerate(lines, 1):
        pm = re.match(r"^\s*PROGRAM\s+(\w+)\s+WITH\s+(\w+)\s*:\s*(\w+)\s*;", ln, re.I)
        if pm:
            instances.append({"instance": pm.group(1), "task": pm.group(2), "program": pm.group(3), "line": no})
            if pm.group(3).lower() not in programs:
                errors.append({"line": no, "rule": "configuration", "message": "Instance %s runs PROGRAM %s, which isn't declared." % (pm.group(1), pm.group(3))})
            if pm.group(2).lower() not in tasks:
                errors.append({"line": no, "rule": "configuration", "message": "Instance %s uses task %s, which isn't declared." % (pm.group(1), pm.group(2))})
    if configs and not instances:
        errors.append({"line": configs[0]["line"], "rule": "configuration", "message": "No 'PROGRAM inst WITH task : prog;' in the resource."})
    unused = sorted(p for p in programs if p not in {i["program"].lower() for i in instances})
    for p in unused:
        warnings.append({"line": 1, "rule": "unused_program", "message": "PROGRAM %s has no instance in Config0, so it never runs." % p})
    if len(tasks) > 1:
        warnings.append({"line": min(t["line"] for t in tasks.values()), "rule": "tasks",
                         "message": "More than one task. Test the timing on the target runtime before relying on "
                                    "task priorities; keeping one task is safer."})

    # forbidden syntax
    for no, ln in enumerate(lines, 1):
        for rx, rule, msg in FORBIDDEN:
            if rx.search(ln):
                errors.append({"line": no, "rule": rule, "message": msg})

    if "DRAFT" not in text[:600].upper():
        warnings.append({"line": 1, "rule": "header", "message": "No DRAFT header comment at the top of the file."})
    errors.sort(key=lambda e: e["line"])
    return {"ok": not errors, "errors": errors, "warnings": warnings, "located": located,
            "tasks": list(tasks.values()), "instances": instances}


def compile_iec2c(path, iec2c, lib):
    """Compile with MatIEC iec2c the way OpenPLC does, into a temp folder. Returns (ok, output)."""
    tmp = tempfile.mkdtemp(prefix="rungstone-iec2c-")
    try:
        cmd = [iec2c, "-f", "-l", "-p", "-r", "-R", "-a", "-I", lib, "-T", tmp, os.path.abspath(path)]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return p.returncode == 0, (p.stdout + p.stderr).strip()[-4000:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file")
    ap.add_argument("--iec2c", help="path to MatIEC iec2c (optional compile check)")
    ap.add_argument("--iec-lib", help="MatIEC lib folder (with ieclib.txt), needed with --iec2c")
    ap.add_argument("-o", "--out")
    a = ap.parse_args(argv)
    with open(a.file, encoding="utf-8", errors="replace") as f:
        text = f.read()
    res = check(text)
    res["file"] = a.file
    if a.iec2c:
        if not a.iec_lib:
            ap.error("--iec2c needs --iec-lib")
        ok, out = compile_iec2c(a.file, a.iec2c, a.iec_lib)
        res["iec2c"] = {"ok": ok, "output": out}
        if not ok:
            res["ok"] = False
    else:
        res["iec2c"] = None
    text_out = json.dumps(res, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text_out)
    print(text_out)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
