"""
simulate.py - pure-Python scan-cycle simulator for Rungstone Verify.

Every scan: apply scenario inputs -> execute the program top to bottom ->
record observed outputs.  Fixed scan time (default 10 ms).  Results are
always labelled "simulated, not hardware-tested".

Programs are loaded with parse_project.parse_file (L5X, RSLogix 500 ASCII /
neutral text, SCL source, IEC ST, PLCopen ST, TwinCAT ST).  Safety logic that
parse_project excluded is never simulated; calling it makes the scenario
"not run".

Supported ladder (Logix neutral text and SLC ASCII):
  XIC XIO OTE OTL OTU ONS OSR OSF TON TOF RTO RES CTU CTD MOV MOVE CLR ADD SUB
  MUL DIV MOD NEG ABS CPT EQU NEQ GRT GEQ LES LEQ LIM SCP SCL(SLC) AND OR XOR
  NOT AFI NOP END TND JSR, series/parallel branches.
Supported ST/SCL: := assignment, IF/ELSIF/ELSE, CASE (values, lists, ranges),
  FOR, WHILE, REPEAT, RETURN/EXIT, expressions (OR XOR AND & NOT = <> < > <= >=
  + - * / MOD **), TRUE/FALSE, numbers, 16#.., T#.. (ms), #local, "global".m,
  IEC FBs TON TOF TP R_TRIG F_TRIG CTU CTD CTUD (inst(IN:=..) and
  "DB".TON(..) / #t.TON(..) forms), ABS MIN MAX LIMIT SEL NORM_X SCALE_X SQRT
  TRUNC ROUND and *_TO_* conversions.
Anything else on an executed path -> verdict "not run" with the reason.

Integer division (documented vendor behaviour):
  Logix DIV / ST:   INT/INT -> truncate; a REAL operand with integer dest -> round
  SLC 500 DIV:      result rounded (half away from zero)
  IEC / S7 SCL:     INT/INT -> truncate toward zero
REAL -> integer stores round to nearest, ties to even (assumption; see
approximations).

Usage:
  simulate.py run --program FILE [--entry NAME] --scenarios S.json [-o out.json]
  simulate.py compare --source FILE --target FILE --mapping mapping.csv \
      --scenarios S.json [--tolerance 1] [--source-entry N] [--target-entry N] \
      [-o verify_results.json] [--state-dir DIR]
"""

import argparse
import csv
import io
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_project as pp  # noqa: E402
import dependency_map as dm  # noqa: E402
from _shared import safe_xml  # noqa: E402

LABEL = "simulated, not hardware-tested"
APPROXIMATIONS = [
    "Fixed scan time; every task/program runs once per scan in listed order (periodic task rates and priorities not modelled).",
    "Asynchronous Logix I/O updates are not modelled: inputs change only between scans (image-table behaviour).",
    "Bits of words are separate signals (writing N7:0 does not change N7:0/3; Word.5 is independent of Word).",
    "REAL to integer stores round to nearest with ties to even; SLC DIV rounds half away from zero.",
    "Timers advance by elapsed simulated time since the instruction last executed; first true scan keeps ACC/ET at 0.",
    "Analog values are whatever the scenario writes; module filtering, update times and over/under-range flags are not modelled.",
    "Power cycle is instantaneous: Rockwell keeps tag values (OTE cleared, TON reset, ONS/OSR storage set, CTU/CTD .CU/.CD set by prescan); "
    "Siemens/IEC reset non-retain variables to their declared initial values. Scenario-driven inputs keep their values.",
    "Faults (division by zero, array bounds, watchdog) are not modelled; they stop the scenario as 'not run'.",
]

IEC_FBS = {"TON", "TOF", "TP", "R_TRIG", "F_TRIG", "CTU", "CTD", "CTUD", "TON_TIME", "TOF_TIME", "TP_TIME",
           "CTU_INT", "CTU_DINT", "CTD_INT", "CTD_DINT", "CTUD_INT", "CTUD_DINT"}
FB_POSITIONAL = {"TON": ["in", "pt"], "TOF": ["in", "pt"], "TP": ["in", "pt"], "R_TRIG": ["clk"], "F_TRIG": ["clk"],
                 "CTU": ["cu", "r", "pv"], "CTD": ["cd", "ld", "pv"], "CTUD": ["cu", "cd", "r", "ld", "pv"]}
INT_RANGES = {"SINT": (-128, 127), "INT": (-32768, 32767), "DINT": (-2 ** 31, 2 ** 31 - 1), "LINT": (-2 ** 63, 2 ** 63 - 1),
              "USINT": (0, 255), "BYTE": (0, 255), "UINT": (0, 65535), "WORD": (0, 65535), "UDINT": (0, 2 ** 32 - 1),
              "DWORD": (0, 2 ** 32 - 1), "ULINT": (0, 2 ** 64 - 1), "LWORD": (0, 2 ** 64 - 1),
              "TIME": (-2 ** 31, 2 ** 31 - 1)}
REAL_TYPES = {"REAL", "LREAL"}
TIMER_MEMBERS = {"pre": "DINT", "acc": "DINT", "en": "BOOL", "tt": "BOOL", "dn": "BOOL"}
COUNTER_MEMBERS = {"pre": "DINT", "acc": "DINT", "cu": "BOOL", "cd": "BOOL", "dn": "BOOL", "ov": "BOOL", "un": "BOOL"}
IEC_FB_MEMBERS = {"in": "BOOL", "q": "BOOL", "pt": "TIME", "et": "TIME", "clk": "BOOL", "cu": "BOOL", "cd": "BOOL",
                  "r": "BOOL", "ld": "BOOL", "qu": "BOOL", "qd": "BOOL", "pv": "INT", "cv": "INT"}


class NotRun(Exception):
    def __init__(self, reason, where=None):
        Exception.__init__(self, reason)
        self.reason = reason
        self.where = where


# --------------------------------------------------------------------------
# Names, types, values
# --------------------------------------------------------------------------

def norm(name):
    s = (name or "").strip().lstrip("\\")
    if dm.SLC_RE.fullmatch(s):
        return dm.norm_slc(s).lower()
    s = s.replace('"', "").replace("#", "")
    s = re.sub(r"\s*([.\[\],])\s*", r"\1", s)
    return s.lower()


SLC_KEY_RE = re.compile(r"^(st|mg|pd|[iosbtcrnflad])(\d*):(\d+)(?:\.(\d+))?(?:/(\d+))?(?:\.([a-z]+))?$")


def slc_type(key):
    m = SLC_KEY_RE.match(key)
    if not m:
        return None
    f, _, _, _, bit, member = m.groups()
    if member:
        return "INT" if member in ("acc", "pre", "len", "pos") else "BOOL"
    if bit is not None:
        return "BOOL"
    return {"n": "INT", "i": "INT", "o": "INT", "f": "REAL", "l": "DINT", "s": "INT", "b": "INT"}.get(f)


def is_slc(key):
    return bool(SLC_KEY_RE.match(key))


def round_even(x):
    return int(round(x))


def round_away(x):
    return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)


def wrap(v, t):
    lo, hi = INT_RANGES[t]
    span = hi - lo + 1
    return (v - lo) % span + lo


def coerce(v, t):
    if t is None:
        if isinstance(v, bool):
            return v
        return v
    t = t.upper()
    if t == "BOOL":
        return bool(v)
    if t in REAL_TYPES:
        return float(v)
    if t in INT_RANGES:
        if isinstance(v, float):
            if math.isnan(v) or math.isinf(v):
                raise NotRun("non-finite REAL stored into %s" % t)
            v = round_even(v)
        return wrap(int(v), t)
    return v


def parse_time_literal(s):
    body = s.split("#", 1)[1].replace("_", "").lower()
    neg = body.startswith("-")
    body = body.lstrip("+-")
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)(ms|us|ns|d|h|m|s)", body):
        n = float(num)
        total += n * {"d": 86400000, "h": 3600000, "m": 60000, "s": 1000, "ms": 1, "us": 0.001, "ns": 1e-6}[unit]
    v = int(round(total))
    return -v if neg else v


def parse_number(s):
    s = s.replace("_", "")
    m = re.match(r"^(?:([A-Za-z_]+)#)?(?:(16|8|2)#([0-9A-Fa-f]+)|([-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?))$", s)
    if not m:
        return None
    typ, base, digits, dec = m.groups()
    if base:
        v = int(digits, int(base))
    elif dec is not None:
        if typ and typ.upper() == "BOOL":
            return bool(int(float(dec)))
        v = float(dec) if ("." in dec or "e" in dec.lower()) else int(dec)
        if typ and typ.upper() in REAL_TYPES:
            v = float(v)
    else:
        return None
    return v


# --------------------------------------------------------------------------
# Tokenizer and parser for ST / SCL / CPT expressions
# --------------------------------------------------------------------------

KEYWORDS = {"IF", "THEN", "ELSIF", "ELSE", "END_IF", "CASE", "OF", "END_CASE", "FOR", "TO", "BY", "DO", "END_FOR",
            "WHILE", "END_WHILE", "REPEAT", "UNTIL", "END_REPEAT", "RETURN", "EXIT", "CONTINUE", "AND", "OR", "XOR",
            "NOT", "MOD", "TRUE", "FALSE", "AND_THEN", "OR_ELSE"}

_TOK_RE_ST = re.compile(r"""
 (?P<ws>\s+)
|(?P<comment>\(\*.*?\*\)|/\*.*?\*/|//[^\n]*)
|(?P<time>(?i:(?:LTIME|TIME|LT|T)\#[-+]?[0-9a-z_.]+))
|(?P<based>(?i:(?:[A-Z_]+\#)?(?:16\#[0-9A-F_]+|8\#[0-7_]+|2\#[01_]+)))
|(?P<typed>(?i:[A-Z_]+\#[-+]?[0-9][0-9._eE+-]*))
|(?P<num>\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][-+]?\d+)?)
|(?P<str>'(?:[^'$]|\$.)*')
|(?P<qsym>"[^"]+")
|(?P<ident>\#?[A-Za-z_]\w*)
|(?P<op>:=|=>|<>|<=|>=|\*\*|\.\.|[()\[\],;:.+\-*/=<>&])
""", re.X | re.S)

_TOK_RE_LOGIX = re.compile(r"""
 (?P<ws>\s+)
|(?P<comment>\(\*.*?\*\)|/\*.*?\*/|//[^\n]*)
|(?P<slc>(?:ST|MG|PD|[IOSBTCRNFLAD])\d{0,3}:\d+(?:\.\d+)?(?:/\d+)?(?:[./](?:ACC|PRE|DN|EN|TT|CU|CD|OV|UN|LEN|POS))?)
|(?P<based>(?:16\#[0-9A-Fa-f_]+|8\#[0-7_]+|2\#[01_]+))
|(?P<num>\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][-+]?\d+)?)
|(?P<str>'(?:[^'$]|\$.)*')
|(?P<ident>\\?[A-Za-z_]\w*(?::\d+)?(?::[IOCS])?)
|(?P<op>:=|<>|<=|>=|\*\*|[()\[\],;:.+\-*/=<>&])
""", re.X | re.S)


class Tok(object):
    __slots__ = ("kind", "val", "line")

    def __init__(self, kind, val, line):
        self.kind, self.val, self.line = kind, val, line

    def __repr__(self):
        return "%s:%r" % (self.kind, self.val)


def tokenize(text, logix=False):
    rx = _TOK_RE_LOGIX if logix else _TOK_RE_ST
    out = []
    pos, line = 0, 0
    n = len(text)
    while pos < n:
        m = rx.match(text, pos)
        if not m:
            raise NotRun("cannot tokenize near %r" % text[pos:pos + 20], line)
        kind = m.lastgroup
        val = m.group(0)
        if kind not in ("ws", "comment"):
            if kind == "ident" and val.upper() in KEYWORDS:
                kind = "kw"
                val = val.upper()
            out.append(Tok(kind, val, line))
        line += val.count("\n")
        pos = m.end()
    out.append(Tok("eof", None, line))
    return out


class Parser(object):
    def __init__(self, toks):
        self.t = toks
        self.i = 0

    def peek(self, k=0):
        return self.t[min(self.i + k, len(self.t) - 1)]

    def next(self):
        tk = self.t[self.i]
        self.i += 1
        return tk

    def at(self, kind, val=None, k=0):
        tk = self.peek(k)
        return tk.kind == kind and (val is None or tk.val == val)

    def accept(self, kind, val=None):
        if self.at(kind, val):
            return self.next()
        return None

    def expect(self, kind, val=None):
        tk = self.next()
        if tk.kind != kind or (val is not None and tk.val != val):
            raise NotRun("parse error: expected %s %s, found %r" % (kind, val or "", tk.val), tk.line)
        return tk

    # ---- statements ----
    def stmts(self, stop):
        out = []
        while True:
            tk = self.peek()
            if tk.kind == "eof" or (tk.kind == "kw" and tk.val in stop):
                return out
            if "CASELABEL" in stop and self.is_case_label():
                return out
            s = self.stmt()
            if s is not None:
                out.append(s)

    def is_case_label(self):
        k = 0
        seen = False
        while True:
            tk = self.peek(k)
            if tk.kind in ("num", "based", "typed", "ident", "qsym") or (tk.kind == "op" and tk.val in ("..", ",", "-", "+", ".")):
                seen = True
                k += 1
                continue
            return seen and tk.kind == "op" and tk.val == ":"

    def stmt(self):
        tk = self.peek()
        line = tk.line
        if tk.kind == "op" and tk.val == ";":
            self.next()
            return None
        if tk.kind == "kw":
            v = tk.val
            if v == "IF":
                self.next()
                arms = []
                cond = self.expr()
                self.expect("kw", "THEN")
                body = self.stmts({"ELSIF", "ELSE", "END_IF"})
                arms.append((cond, body))
                els = []
                while True:
                    if self.accept("kw", "ELSIF"):
                        c = self.expr()
                        self.expect("kw", "THEN")
                        arms.append((c, self.stmts({"ELSIF", "ELSE", "END_IF"})))
                    elif self.accept("kw", "ELSE"):
                        els = self.stmts({"END_IF"})
                    else:
                        break
                self.expect("kw", "END_IF")
                self.accept("op", ";")
                return ("if", line, arms, els)
            if v == "CASE":
                self.next()
                sel = self.expr()
                self.expect("kw", "OF")
                arms, els = [], []
                while True:
                    if self.accept("kw", "ELSE"):
                        els = self.stmts({"END_CASE"})
                        continue
                    if self.at("kw", "END_CASE"):
                        break
                    labels = []
                    while True:
                        lo = self.expr()
                        hi = None
                        if self.accept("op", ".."):
                            hi = self.expr()
                        labels.append((lo, hi))
                        if not self.accept("op", ","):
                            break
                    self.expect("op", ":")
                    arms.append((labels, self.stmts({"END_CASE", "ELSE", "CASELABEL"})))
                self.expect("kw", "END_CASE")
                self.accept("op", ";")
                return ("case", line, sel, arms, els)
            if v == "FOR":
                self.next()
                var = self.lvalue()
                self.expect("op", ":=")
                a = self.expr()
                self.expect("kw", "TO")
                b = self.expr()
                step = None
                if self.accept("kw", "BY"):
                    step = self.expr()
                self.expect("kw", "DO")
                body = self.stmts({"END_FOR"})
                self.expect("kw", "END_FOR")
                self.accept("op", ";")
                return ("for", line, var, a, b, step, body)
            if v == "WHILE":
                self.next()
                c = self.expr()
                self.expect("kw", "DO")
                body = self.stmts({"END_WHILE"})
                self.expect("kw", "END_WHILE")
                self.accept("op", ";")
                return ("while", line, c, body)
            if v == "REPEAT":
                self.next()
                body = self.stmts({"UNTIL"})
                self.expect("kw", "UNTIL")
                c = self.expr()
                self.expect("kw", "END_REPEAT")
                self.accept("op", ";")
                return ("repeat", line, body, c)
            if v in ("RETURN", "EXIT", "CONTINUE"):
                self.next()
                self.accept("op", ";")
                return (v.lower(), line)
            raise NotRun("unsupported statement %s" % v, line)
        lv = self.lvalue()
        if self.accept("op", ":="):
            e = self.expr()
            self.accept("op", ";")
            return ("assign", line, lv, e)
        if self.at("op", "("):
            args = self.args()
            self.accept("op", ";")
            return ("call", line, lv, args)
        raise NotRun("unsupported statement near %r" % self.peek().val, line)

    def lvalue(self):
        tk = self.next()
        if tk.kind not in ("ident", "qsym", "slc"):
            raise NotRun("parse error: expected a variable, found %r" % tk.val, tk.line)
        parts = [("n", tk.val)]
        while True:
            if self.at("op", ".") and self.peek(1).kind in ("ident", "qsym", "num", "kw"):
                self.next()
                parts.append(("n", self.next().val))
            elif self.at("op", "["):
                self.next()
                idx = [self.expr()]
                while self.accept("op", ","):
                    idx.append(self.expr())
                self.expect("op", "]")
                parts.append(("i", idx))
            else:
                return ("var", parts, tk.line)

    def args(self):
        self.expect("op", "(")
        out = []
        if self.accept("op", ")"):
            return out
        while True:
            if self.peek().kind == "ident" and self.peek(1).kind == "op" and self.peek(1).val in (":=", "=>"):
                name = self.next().val
                op = self.next().val
                if op == ":=":
                    out.append(("named", name.lstrip("#").lower(), self.expr()))
                else:
                    out.append(("out", name.lstrip("#").lower(), self.lvalue()))
            else:
                out.append(("pos", self.expr()))
            if self.accept("op", ")"):
                return out
            self.expect("op", ",")

    # ---- expressions ----
    def expr(self):
        e = self.xor_()
        while self.at("kw", "OR") or self.at("kw", "OR_ELSE"):
            self.next()
            e = ("bin", "OR", e, self.xor_())
        return e

    def xor_(self):
        e = self.and_()
        while self.accept("kw", "XOR"):
            e = ("bin", "XOR", e, self.and_())
        return e

    def and_(self):
        e = self.cmp_()
        while self.at("kw", "AND") or self.at("kw", "AND_THEN") or self.at("op", "&"):
            self.next()
            e = ("bin", "AND", e, self.cmp_())
        return e

    def cmp_(self):
        e = self.rel_()
        while self.at("op", "=") or self.at("op", "<>"):
            op = self.next().val
            e = ("bin", op, e, self.rel_())
        return e

    def rel_(self):
        e = self.add_()
        while self.peek().kind == "op" and self.peek().val in ("<", ">", "<=", ">="):
            op = self.next().val
            e = ("bin", op, e, self.add_())
        return e

    def add_(self):
        e = self.mul_()
        while self.peek().kind == "op" and self.peek().val in ("+", "-"):
            op = self.next().val
            e = ("bin", op, e, self.mul_())
        return e

    def mul_(self):
        e = self.pow_()
        while (self.peek().kind == "op" and self.peek().val in ("*", "/")) or self.at("kw", "MOD"):
            op = self.next().val
            e = ("bin", op, e, self.pow_())
        return e

    def pow_(self):
        e = self.unary()
        while self.accept("op", "**"):
            e = ("bin", "**", e, self.unary())
        return e

    def unary(self):
        if self.accept("kw", "NOT"):
            return ("not", self.unary())
        if self.accept("op", "-"):
            return ("neg", self.unary())
        if self.accept("op", "+"):
            return self.unary()
        return self.primary()

    def primary(self):
        tk = self.peek()
        if tk.kind == "op" and tk.val == "(":
            self.next()
            e = self.expr()
            self.expect("op", ")")
            return e
        if tk.kind == "kw" and tk.val in ("TRUE", "FALSE"):
            self.next()
            return ("lit", tk.val == "TRUE")
        if tk.kind == "time":
            self.next()
            return ("lit", parse_time_literal(tk.val))
        if tk.kind in ("num", "based", "typed"):
            self.next()
            v = parse_number(tk.val)
            if v is None:
                raise NotRun("unsupported literal %s" % tk.val, tk.line)
            return ("lit", v)
        if tk.kind == "str":
            self.next()
            return ("lit", tk.val[1:-1])
        if tk.kind in ("ident", "qsym", "slc"):
            if tk.kind == "ident" and self.peek(1).kind == "op" and self.peek(1).val == "(":
                self.next()
                return ("call", tk.val.lstrip("#").upper(), self.args(), tk.line)
            return self.lvalue()
        raise NotRun("parse error near %r" % tk.val, tk.line)


def parse_st(text, logix=False):
    # REGION / END_REGION carry free text; drop those lines
    text = re.sub(r"(?im)^[ \t]*(END_)?REGION\b[^\n]*$", "", text)
    p = Parser(tokenize(text, logix))
    body = p.stmts(set())
    if not p.at("eof"):
        raise NotRun("parse error near %r" % p.peek().val, p.peek().line)
    return body


def parse_expr(text, logix=True):
    p = Parser(tokenize(text, logix))
    e = p.expr()
    if not p.at("eof"):
        raise NotRun("cannot parse expression %r" % text)
    return e


# --------------------------------------------------------------------------
# Ladder compilation
# --------------------------------------------------------------------------

def compile_rung(text):
    toks = []
    if re.search(r"[A-Za-z_]\w*\(", text):
        i, n = 0, len(text)
        while i < n:
            c = text[i]
            if c.isspace() or c == ";":
                i += 1
            elif c in "[,]":
                toks.append(c)
                i += 1
            else:
                m = re.match(r"([A-Za-z_]\w*)\(", text[i:])
                if not m:
                    raise NotRun("cannot parse rung text near %r" % text[i:i + 20])
                j = i + m.end()
                depth = 1
                while j < n and depth:
                    if text[j] == "(":
                        depth += 1
                    elif text[j] == ")":
                        depth -= 1
                    j += 1
                toks.append(("I", m.group(1).upper(), dm.split_args(text[i + m.end():j - 1])))
                i = j
    else:
        words = text.split()
        k = 0
        while k < len(words):
            w = words[k].upper()
            k += 1
            if w in ("SOR", "EOR"):
                continue
            if w == "BST":
                toks.append("[")
                continue
            if w == "NXB":
                toks.append(",")
                continue
            if w == "BND":
                toks.append("]")
                continue
            n_ops = dm.SLC_ARITY.get(w)
            if n_ops is None:
                ops = []
                while k < len(words) and not re.match(r"^[A-Z]{2,5}$", words[k]):
                    ops.append(words[k])
                    k += 1
            else:
                ops = words[k:k + n_ops]
                k += n_ops
            toks.append(("I", w, ops))

    def seq(i):
        out = []
        while i < len(toks):
            t = toks[i]
            if t in (",", "]"):
                return out, i
            if t == "[":
                legs = []
                i += 1
                while True:
                    leg, i = seq(i)
                    legs.append(leg)
                    if i >= len(toks):
                        raise NotRun("unbalanced branch")
                    if toks[i] == ",":
                        i += 1
                        continue
                    i += 1  # ']'
                    break
                out.append(("B", legs))
                continue
            out.append(t)
            i += 1
        return out, i

    tree, i = seq(0)
    if i != len(toks):
        raise NotRun("unbalanced branch")
    return tree


def walk_instr(tree):
    for el in tree:
        if el[0] == "B":
            for leg in el[1]:
                for x in walk_instr(leg):
                    yield x
        else:
            yield el


# --------------------------------------------------------------------------
# Program model
# --------------------------------------------------------------------------

class Program(object):
    def __init__(self, path, entry=None):
        self.path = path
        pr = pp.parse_file(path, pp.Ctx())
        self.pr = pr
        fmt = pr["format"]
        self.fmt = fmt
        if fmt in ("rockwell-l5x", "ladder-text"):
            self.platform = "rockwell"
        elif fmt.startswith("siemens"):
            self.platform = "siemens"
        else:
            self.platform = "iec"
        self.div_mode = "slc" if fmt == "ladder-text" else ("logix" if fmt == "rockwell-l5x" else "iec")
        self.types, self.init, self.retain, self.fb_kind, self.decl_inputs = {}, {}, set(), {}, set()
        self.excluded = set()
        for e in pr.get("excluded_safety", []):
            self.excluded.add(e["name"].split("/")[-1].split(" ")[0].lower())
            self.excluded.add(e["name"].lower())
        self.load_error = None
        self.routines = dict((r["id"].lower(), r) for r in pr.get("routines", []))
        self.compiled = {}
        self.ote, self.timers, self.ons, self.counters = set(), {}, {}, {}
        for t in pr.get("tags", []):
            self.declare(t.get("name"), t.get("data_type"), t.get("retain"))
            if t.get("usage") == "Input":
                self.decl_inputs.add(norm(t["name"]))
        for r in pr.get("routines", []):
            for sec in r.get("interface", []):
                s = sec["section"].upper()
                for m in sec["members"]:
                    self.declare(m["name"], m.get("data_type"), m.get("retain") or "RETAIN" in s)
                    if "INPUT" in s or (m.get("address") or "").upper().startswith("%I"):
                        self.decl_inputs.add(norm(m["name"]))
        try:
            self.entries = self.pick_entries(entry)
            self.startup = self.pick_startup()
        except NotRun as e:
            self.entries, self.startup = [], []
            self.load_error = e
        self.read_initial_values()
        if self.platform == "rockwell" and self.load_error is None:
            for rid in list(self.routines):
                r = self.routines[rid]
                if r.get("rungs"):
                    try:
                        self.compile(rid)
                    except NotRun:
                        pass

    def declare(self, name, dtype, retain=None):
        if not name:
            return
        k = norm(name)
        dt = (dtype or "").upper().strip('"')
        if dt in ("TIMER", "FBD_TIMER"):
            for m, t in TIMER_MEMBERS.items():
                self.types["%s.%s" % (k, m)] = t
            self.types[k] = "TIMER"
        elif dt in ("COUNTER", "FBD_COUNTER"):
            for m, t in COUNTER_MEMBERS.items():
                self.types["%s.%s" % (k, m)] = t
            self.types[k] = "COUNTER"
        elif dt in IEC_FBS or dt in ("IEC_TIMER", "IEC_COUNTER", "IEC_LTIMER"):
            base = dt.split("_")[0] if dt not in ("R_TRIG", "F_TRIG") else dt
            self.fb_kind[k] = base if base in FB_POSITIONAL else None
            for m, t in IEC_FB_MEMBERS.items():
                self.types["%s.%s" % (k, m)] = ("DINT" if dt.endswith("DINT") and m in ("pv", "cv") else t)
            self.types[k] = dt
        elif dt:
            base = re.sub(r"\[.*$", "", dt).strip()
            self.types[k] = base
        if retain:
            self.retain.add(k)

    # ---- entry selection ----
    def pick_entries(self, entry):
        rs = self.pr.get("routines", [])
        if entry:
            for r in rs:
                if r["id"].lower() == entry.lower() or r["name"].lower() == entry.lower():
                    return [r["id"].lower()]
            raise NotRun("entry %s not found" % entry)
        if self.fmt == "rockwell-l5x":
            progs = []
            for t in self.pr.get("tasks", []):
                for p in t.get("programs", []):
                    if p not in progs:
                        progs.append(p)
            if not progs:
                progs = sorted(set(r["program"] for r in rs if r.get("main")))
            out = []
            for p in progs:
                main = [r for r in rs if r.get("program") == p and r.get("main")]
                if main:
                    out.append(main[0]["id"].lower())
                elif p.lower() in self.excluded:
                    continue
            if not out:
                raise NotRun("no main routine found to execute")
            return out
        if self.fmt == "ladder-text":
            return [rs[0]["id"].lower()] if rs else []
        cands = [r for r in rs if (r.get("lines") or r.get("networks")) and not self.is_startup(r)]
        for t in self.pr.get("tasks", []):
            names = [p.lower() for p in t.get("programs", [])]
            hit = [r["id"].lower() for r in cands if r["name"].lower() in names]
            if hit:
                return hit
        ob = [r for r in cands if r.get("kind", "").lower() in ("organization_block", "ob")
              and re.search(r"(?i)main|ob1$|program.?cycle", r["name"])]
        if ob:
            return [ob[0]["id"].lower()]
        progs = [r for r in cands if r.get("kind", "").lower() == "program"]
        if progs:
            return [r["id"].lower() for r in progs]
        if len(cands) == 1:
            return [cands[0]["id"].lower()]
        raise NotRun("ambiguous entry point (%s); choose one with --entry" % ", ".join(r["name"] for r in cands))

    @staticmethod
    def is_startup(r):
        return bool(re.search(r"(?i)startup|^ob_?100$|cold.?start", r.get("name", "")))

    def pick_startup(self):
        return [r["id"].lower() for r in self.pr.get("routines", []) if self.is_startup(r) and r.get("lines")]

    # ---- initial values (read for simulation only; never placed in context) ----
    def read_initial_values(self):
        try:
            if self.fmt == "rockwell-l5x":
                root = safe_xml.fromstring(pp.read_text(self.path))
                self._l5x_values(root)
            elif self.fmt == "plcopen-xml":
                root = safe_xml.fromstring(pp.read_text(self.path))
                for v in root.iter():
                    if pp.lname(v) != "variable" or not v.attrib.get("name"):
                        continue
                    for sv in v.iter():
                        if pp.lname(sv) == "simpleValue" and sv.attrib.get("value") is not None:
                            self.set_init(v.attrib["name"], sv.attrib["value"])
            elif self.fmt in ("siemens-scl", "iec-st", "beckhoff-twincat"):
                text = pp.read_text(self.path)
                for m in re.finditer(r'(?im)^\s*("[^"]+"|[A-Za-z_]\w*)\s*(?:\{[^}]*\})?\s*(?:AT\s+%\S+\s*)?:\s*[^;:=]+?:=\s*([^;]+);', text):
                    self.set_init(m.group(1), m.group(2).strip())
        except (NotRun, safe_xml.ParseError, safe_xml.UnsafeXMLError, ValueError):
            pass

    def set_init(self, name, raw):
        raw = raw.strip()
        try:
            v = eval_const(raw)
        except NotRun:
            return
        k = norm(name)
        self.init[k] = coerce(v, self.types.get(k))

    def _l5x_values(self, root):
        for tags in root.iter():
            if pp.lname(tags) != "Tag" or tags.attrib.get("TagType") == "Alias":
                continue
            name = tags.attrib.get("Name")
            for data in tags:
                if pp.lname(data) != "Data" or data.attrib.get("Format") != "Decorated":
                    continue
                for el in data.iter():
                    ln = pp.lname(el)
                    if ln == "DataValue" and el.attrib.get("Value") is not None:
                        self.set_raw(norm(name), el.attrib["Value"])
                    elif ln == "DataValueMember" and el.attrib.get("Value") is not None:
                        self.set_raw("%s.%s" % (norm(name), el.attrib["Name"].lower()), el.attrib["Value"])

    def set_raw(self, k, val):
        v = parse_number(val.replace("_", "")) if not val.startswith("2#") else int(val[2:].replace("_", ""), 2)
        if v is None:
            return
        self.init[k] = coerce(v, self.types.get(k))

    # ---- compilation ----
    def compile(self, rid):
        if rid in self.compiled:
            c = self.compiled[rid]
            if isinstance(c, NotRun):
                raise c
            return c
        r = self.routines.get(rid)
        try:
            if r is None:
                raise NotRun("routine %s not found" % rid)
            if r.get("protected"):
                raise NotRun("routine %s is source-protected" % r["id"])
            if r.get("rungs"):
                rungs = []
                for x in r["rungs"]:
                    where = "%s rung %s" % (r["id"], x.get("number"))
                    if x.get("withheld"):
                        rungs.append((where, NotRun("rung withheld as safety logic", where)))
                        continue
                    try:
                        tree = compile_rung(x.get("text") or "")
                    except NotRun as e:
                        rungs.append((where, NotRun(e.reason, where)))
                        continue
                    rungs.append((where, tree))
                    self.note_instructions(tree)
                c = ("ladder", rungs)
            elif r.get("lines") or (r.get("networks") and all(n.get("text") is not None and not n.get("summarized") for n in r["networks"])):
                if r.get("lines"):
                    lines = r["lines"]
                else:
                    lines = []
                    for n in r["networks"]:
                        for i, l in enumerate((n.get("text") or "").splitlines()):
                            lines.append({"number": "%s.%d" % (n["number"], i + 1), "text": l})
                text = "\n".join(l["text"] for l in lines)
                try:
                    body = parse_st(text, logix=(self.fmt == "rockwell-l5x"))
                except NotRun as e:
                    ln = lines[e.where]["number"] if isinstance(e.where, int) and e.where < len(lines) else "?"
                    raise NotRun("%s (%s line %s)" % (e.reason, r["id"], ln), "%s line %s" % (r["id"], ln))
                c = ("st", body, [l["number"] for l in lines], r["id"])
            elif r.get("networks"):
                raise NotRun("%s: graphical (LAD/FBD) network summaries are not executable; provide SCL/ST source or ladder text instead" % r["id"])
            else:
                c = ("ladder", [])
        except NotRun as e:
            self.compiled[rid] = e
            raise
        self.compiled[rid] = c
        return c

    def note_instructions(self, tree):
        for _, mn, ops in walk_instr(tree):
            if not ops:
                continue
            k = norm(ops[0]) if ops else None
            if mn == "OTE":
                self.ote.add(k)
            elif mn in ("TON", "TOF", "RTO"):
                self.timers[k] = mn
                if len(ops) == 4:  # SLC ASCII: timer, time base, preset, accum
                    try:
                        base = float(ops[1])
                    except ValueError:
                        base = 1.0
                    self.timers[k + "#base"] = base
                    pre = parse_number(ops[2])
                    if pre is not None:
                        self.init.setdefault(k + ".pre", pre)
                elif len(ops) >= 2:
                    pre = parse_number(ops[1])
                    if pre is not None:
                        self.init.setdefault(k + ".pre", pre)
            elif mn in ("CTU", "CTD"):
                self.counters[k] = mn
                if len(ops) >= 2:
                    pre = parse_number(ops[1])
                    if pre is not None:
                        self.init.setdefault(k + ".pre", pre)
            elif mn in ("ONS", "OSR", "OSF"):
                self.ons[k] = mn
                if mn in ("OSR", "OSF") and len(ops) > 1:
                    self.ote.add(norm(ops[1]))

    def type_of(self, k):
        t = self.types.get(k)
        if t:
            return t
        if is_slc(k):
            t = slc_type(k)
            if t and self.fmt == "ladder-text" and t == "INT":
                return t
            return t
        m = re.match(r"^(.*)\.(pre|acc|en|tt|dn|cu|cd|ov|un|q|et|pt|cv|pv|in|clk)$", k)
        if m and self.types.get(m.group(1)) in ("TIMER", "COUNTER"):
            return (TIMER_MEMBERS if self.types[m.group(1)] == "TIMER" else COUNTER_MEMBERS).get(m.group(2))
        return None


def eval_const(raw):
    e = parse_expr(raw, logix=False)
    return Sim.const(e)


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------

class TND(Exception):
    pass


class Return(Exception):
    pass


class Exit(Exception):
    pass


class Sim(object):
    def __init__(self, prog, scan_ms=10):
        self.p = prog
        self.scan_ms = scan_ms
        self.now = 0
        self.store = dict(prog.init)
        self.external = set()
        self.first_scan = True
        self.where = None
        self.call_depth = 0

    # ---- store ----
    def get(self, k):
        if k in ("s:fs", "s:1/15"):
            return self.first_scan
        if k in self.store:
            return self.store[k]
        t = self.p.type_of(k)
        if t == "BOOL":
            return False
        if t in REAL_TYPES:
            return 0.0
        return 0

    def set(self, k, v):
        t = self.p.type_of(k)
        if isinstance(v, bool) and t is None:
            self.store[k] = v
            return
        self.store[k] = coerce(v, t)

    def set_input(self, k, v):
        self.external.add(k)
        self.set(k, v)

    # ---- scan ----
    def scan(self):
        try:
            if self.first_scan:
                for rid in self.p.startup:
                    self.run_routine(rid)
            for rid in self.p.entries:
                self.run_routine(rid)
        except TND:
            pass
        self.first_scan = False

    def run_routine(self, rid):
        if rid.split("/")[-1] in self.p.excluded or rid in self.p.excluded:
            raise NotRun("calls %s, which was excluded as safety logic and is never simulated" % rid, self.where)
        c = self.p.compile(rid)
        self.call_depth += 1
        if self.call_depth > 32:
            raise NotRun("JSR nesting deeper than 32 (recursion?)", self.where)
        try:
            if c[0] == "ladder":
                for where, tree in c[1]:
                    self.where = where
                    if isinstance(tree, NotRun):
                        raise tree
                    self.seq(tree, True)
            else:
                _, body, linenos, rid_disp = c
                self.st_linenos, self.st_rid = linenos, rid_disp
                try:
                    self.exec_stmts(body)
                except Return:
                    pass
        finally:
            self.call_depth -= 1

    # ---- power cycle ----
    def power_cycle(self):
        p = self.p
        if p.platform == "rockwell":
            for k in p.ote:
                self.store[k] = False
            for k, kind in p.timers.items():
                if "#" in k:
                    continue
                self.store.pop(k + ".__frac", None)
                if kind == "TON":
                    for m in ("acc",):
                        self.store[k + "." + m] = 0
                    for m in ("en", "tt", "dn"):
                        self.store[k + "." + m] = False
                elif kind == "TOF":
                    self.store[k + ".acc"] = self.get(k + ".pre")
                    for m in ("en", "tt", "dn"):
                        self.store[k + "." + m] = False
                else:
                    self.store[k + ".en"] = False
                    self.store[k + ".tt"] = False
            for k, kind in p.ons.items():
                self.store[k] = kind != "OSF"
            for k, kind in p.counters.items():
                self.store[k + (".cu" if kind == "CTU" else ".cd")] = True
        else:
            keep = {}
            for k, v in self.store.items():
                base = re.split(r"[.\[]", k, 1)[0]
                if (k in self.external or base in self.external or k in p.retain or base in p.retain
                        or k in p.decl_inputs or base in p.decl_inputs):
                    keep[k] = v
            self.store = dict(p.init)
            self.store.update(keep)
        self.first_scan = True

    # ---- operand helpers (ladder) ----
    def key(self, op):
        op = op.strip()

        def idx(m):
            inner = m.group(1)
            vals = [str(int(self.val(x))) for x in inner.split(",")]
            return "[" + ",".join(vals) + "]"
        if "[" in op:
            op = re.sub(r"\[([^\]]+)\]", idx, op)
        return norm(op)

    def val(self, op):
        op = (op or "").strip()
        if op in ("", "?"):
            return None
        v = parse_number(op)
        if v is not None:
            return v
        if re.fullmatch(r"\\?[A-Za-z_][\w:.\[\]/ ,]*", op) or dm.SLC_RE.fullmatch(op.lstrip("#")):
            if not re.search(r"\s(AND|OR|XOR|MOD|NOT)\s", op.upper()) and not re.search(r"[+\-*/()]", op):
                return self.get(self.key(op.lstrip("#")))
        return self.eval(parse_expr(op, logix=True))

    def num(self, op, what):
        v = self.val(op)
        if v is None:
            raise NotRun("%s operand is unset ('?'); give its value in the scenario 'initial'" % what, self.where)
        return v

    # ---- ladder execution ----
    def seq(self, tree, cond):
        for el in tree:
            if el[0] == "B":
                outs = [self.seq(leg, cond) for leg in el[1]]
                cond = any(outs)
            else:
                cond = self.instr(el[1], el[2], cond)
        return cond

    def instr(self, mn, ops, c):
        f = getattr(self, "i_" + mn, None)
        if f is None:
            raise NotRun("unsupported instruction %s" % mn, self.where)
        return f(ops, c)

    def i_XIC(self, o, c):
        return c and bool(self.get(self.key(o[0])))

    def i_XIO(self, o, c):
        return c and not bool(self.get(self.key(o[0])))

    def i_OTE(self, o, c):
        self.set(self.key(o[0]), bool(c))
        return c

    def i_OTL(self, o, c):
        if c:
            self.set(self.key(o[0]), True)
        return c

    def i_OTU(self, o, c):
        if c:
            self.set(self.key(o[0]), False)
        return c

    def i_ONS(self, o, c):
        k = self.key(o[0])
        out = c and not bool(self.get(k))
        self.set(k, bool(c))
        return out

    def i_OSR(self, o, c):
        s, q = self.key(o[0]), self.key(o[1])
        self.set(q, bool(c and not self.get(s)))
        self.set(s, bool(c))
        return c

    def i_OSF(self, o, c):
        s, q = self.key(o[0]), self.key(o[1])
        self.set(q, bool((not c) and self.get(s)))
        self.set(s, bool(c))
        return c

    def _base_ms(self, k):
        return self.p.timers.get(k + "#base", 0.001) * 1000.0

    def _advance(self, k, base_ms, pre):
        last = self.store.get(k + ".__last", self.now)
        delta = self.now - last + self.store.get(k + ".__frac", 0.0)
        acc = int(self.get(k + ".acc"))
        units = int(delta // base_ms)
        self.store[k + ".__frac"] = delta - units * base_ms
        acc = min(acc + units, pre) if acc < pre else acc
        self.set(k + ".acc", acc)
        return acc

    def _timer(self, o, c, kind):
        k = self.key(o[0])
        base_ms = self._base_ms(k)
        if (k + ".pre") not in self.store:
            raise NotRun("preset of %s is unknown (tag data not available); give %s.PRE in the scenario 'initial'" % (o[0], o[0]),
                         self.where)
        pre = int(self.get(k + ".pre"))
        en_prev = bool(self.get(k + ".en"))
        if kind == "TON":
            if c:
                if en_prev:
                    acc = self._advance(k, base_ms, pre)
                else:
                    self.store[k + ".__frac"] = 0.0
                    acc = int(self.get(k + ".acc"))
                self.set(k + ".en", True)
                done = acc >= pre
                if done and acc > pre:
                    self.set(k + ".acc", pre)
                self.set(k + ".dn", done)
                self.set(k + ".tt", not done)
            else:
                for m in ("en", "tt", "dn"):
                    self.set(k + "." + m, False)
                self.set(k + ".acc", 0)
                self.store[k + ".__frac"] = 0.0
        elif kind == "TOF":
            if c:
                self.set(k + ".en", True)
                self.set(k + ".dn", True)
                self.set(k + ".tt", False)
                self.set(k + ".acc", 0)
                self.store[k + ".__frac"] = 0.0
            else:
                if self.get(k + ".dn"):
                    if en_prev:
                        acc = int(self.get(k + ".acc"))
                        self.store[k + ".__frac"] = 0.0
                    else:
                        acc = self._advance(k, base_ms, pre)
                    if acc >= pre:
                        self.set(k + ".dn", False)
                        self.set(k + ".tt", False)
                    else:
                        self.set(k + ".tt", True)
                self.set(k + ".en", False)
        else:  # RTO
            if c:
                if en_prev:
                    acc = self._advance(k, base_ms, pre)
                else:
                    acc = int(self.get(k + ".acc"))
                self.set(k + ".en", True)
                done = acc >= pre
                self.set(k + ".dn", done)
                self.set(k + ".tt", not done)
            else:
                self.set(k + ".en", False)
                self.set(k + ".tt", False)
        self.store[k + ".__last"] = self.now
        return c

    def i_TON(self, o, c):
        return self._timer(o, c, "TON")

    def i_TOF(self, o, c):
        return self._timer(o, c, "TOF")

    def i_RTO(self, o, c):
        return self._timer(o, c, "RTO")

    def _count(self, o, c, up):
        k = self.key(o[0])
        if (k + ".pre") not in self.store:
            raise NotRun("preset of %s is unknown; give %s.PRE in the scenario 'initial'" % (o[0], o[0]), self.where)
        edge = ".cu" if up else ".cd"
        prev = bool(self.get(k + edge))
        if c and not prev:
            t = self.p.type_of(k + ".acc") or "DINT"
            lo, hi = INT_RANGES.get(t, INT_RANGES["DINT"])
            acc = int(self.get(k + ".acc")) + (1 if up else -1)
            if acc > hi:
                acc = lo
                self.set(k + ".ov", True)
            elif acc < lo:
                acc = hi
                self.set(k + ".un", True)
            self.set(k + ".acc", acc)
        self.set(k + edge, bool(c))
        self.set(k + ".dn", int(self.get(k + ".acc")) >= int(self.get(k + ".pre")))
        return c

    def i_CTU(self, o, c):
        return self._count(o, c, True)

    def i_CTD(self, o, c):
        return self._count(o, c, False)

    def i_RES(self, o, c):
        if c:
            k = self.key(o[0])
            self.set(k + ".acc", 0)
            for m in ("en", "tt", "dn", "cu", "cd", "ov", "un"):
                if (k + "." + m) in self.store or m in ("dn",):
                    self.set(k + "." + m, False)
            self.store[k + ".__frac"] = 0.0
        return c

    def _store(self, dest, v):
        k = self.key(dest)
        self.set(k, v)

    def i_MOV(self, o, c):
        if c:
            self._store(o[1], self.num(o[0], "MOV source"))
        return c

    i_MOVE = i_MOV

    def i_CLR(self, o, c):
        if c:
            self._store(o[0], 0)
        return c

    def _math(self, o, c, fn):
        if c:
            a, b = self.num(o[0], "source A"), self.num(o[1], "source B")
            self._store(o[2], fn(a, b, self.p.type_of(self.key(o[2]))))
        return c

    def i_ADD(self, o, c):
        return self._math(o, c, lambda a, b, t: a + b)

    def i_SUB(self, o, c):
        return self._math(o, c, lambda a, b, t: a - b)

    def i_MUL(self, o, c):
        return self._math(o, c, lambda a, b, t: a * b)

    def i_DIV(self, o, c):
        return self._math(o, c, lambda a, b, t: self.divide(a, b, t))

    def i_MOD(self, o, c):
        def f(a, b, t):
            if b == 0:
                raise NotRun("MOD by zero: platform fault behaviour is not modelled", self.where)
            return a - b * int(a / b) if isinstance(a, int) and isinstance(b, int) else math.fmod(a, b)
        return self._math(o, c, f)

    def i_AND(self, o, c):
        return self._math(o, c, lambda a, b, t: int(a) & int(b))

    def i_OR(self, o, c):
        return self._math(o, c, lambda a, b, t: int(a) | int(b))

    def i_XOR(self, o, c):
        return self._math(o, c, lambda a, b, t: int(a) ^ int(b))

    def i_NOT(self, o, c):
        if c:
            self._store(o[1], ~int(self.num(o[0], "source")))
        return c

    def i_NEG(self, o, c):
        if c:
            self._store(o[1], -self.num(o[0], "source"))
        return c

    def i_ABS(self, o, c):
        if c:
            self._store(o[1], abs(self.num(o[0], "source")))
        return c

    def i_CPT(self, o, c):
        if c:
            dest = o[0]
            expr = ",".join(o[1:])
            v = self.eval(parse_expr(expr, logix=True))
            self._store(dest, v)
        return c

    def _cmp(self, o, c, fn):
        return c and fn(self.num(o[0], "source A"), self.num(o[1], "source B"))

    def i_EQU(self, o, c):
        return self._cmp(o, c, lambda a, b: a == b)

    def i_NEQ(self, o, c):
        return self._cmp(o, c, lambda a, b: a != b)

    def i_GRT(self, o, c):
        return self._cmp(o, c, lambda a, b: a > b)

    def i_GEQ(self, o, c):
        return self._cmp(o, c, lambda a, b: a >= b)

    def i_LES(self, o, c):
        return self._cmp(o, c, lambda a, b: a < b)

    def i_LEQ(self, o, c):
        return self._cmp(o, c, lambda a, b: a <= b)

    def i_LIM(self, o, c):
        lo, t, hi = self.num(o[0], "low"), self.num(o[1], "test"), self.num(o[2], "high")
        return c and ((lo <= t <= hi) if lo <= hi else (t >= lo or t <= hi))

    def i_SCP(self, o, c):
        if c:
            x, a, b, sa, sb = [self.num(o[i], "SCP operand %d" % i) for i in range(5)]
            if b == a:
                raise NotRun("SCP with input min = input max", self.where)
            v = (float(x) - a) * (sb - sa) / float(b - a) + sa
            k = self.key(o[5])
            if (self.p.type_of(k) or "INT") not in REAL_TYPES:
                v = round_away(v) if self.p.div_mode == "slc" else round_even(v)
            self.set(k, v)
        return c

    def i_SCL(self, o, c):  # SLC SCL source rate offset dest; dest = src*rate/10000 + offset
        if c:
            src, rate, off = self.num(o[0], "source"), self.num(o[1], "rate"), self.num(o[2], "offset")
            self._store(o[3], round_away(src * rate / 10000.0 + off))
        return c

    def i_AFI(self, o, c):
        return False

    def i_NOP(self, o, c):
        return c

    def i_END(self, o, c):
        return c

    def i_TND(self, o, c):
        if c:
            raise TND()
        return c

    def i_JSR(self, o, c):
        if c:
            target = o[0].strip() if o else ""
            prog = "/".join(self.where.split(" rung")[0].split("/")[:-1]) if self.where else ""
            rid = ("%s/%s" % (prog, target)).lower() if prog else target.lower()
            if target.lower() in self.p.excluded or rid in self.p.excluded:
                raise NotRun("JSR to %s, which was excluded as safety logic and is never simulated" % target, self.where)
            if rid not in self.p.routines:
                raise NotRun("JSR target %s not found in the parsed project" % target, self.where)
            if len(o) > 2:
                raise NotRun("JSR with parameters is not simulated", self.where)
            saved = self.where
            self.run_routine(rid)
            self.where = saved
        return c

    def divide(self, a, b, dest_type):
        if b == 0:
            raise NotRun("division by zero: platform fault behaviour is not modelled", self.where)
        mode = self.p.div_mode
        ints = isinstance(a, int) and isinstance(b, int)
        dest_int = ints if dest_type is None else (dest_type.upper() not in REAL_TYPES)
        if mode == "slc":
            q = float(a) / b
            return round_away(q) if dest_int else q
        if ints:
            q = abs(a) // abs(b)
            return q if (a >= 0) == (b >= 0) else -q
        q = float(a) / b
        return round_even(q) if (mode == "logix" and dest_int) else q

    # ---- expressions ----
    @staticmethod
    def const(e):
        if e[0] == "lit":
            return e[1]
        if e[0] == "neg":
            return -Sim.const(e[1])
        if e[0] == "not":
            return not Sim.const(e[1])
        raise NotRun("not a constant")

    def lv_key(self, lv):
        parts = []
        for kind, v in lv[1]:
            if kind == "n":
                parts.append("." + norm(v) if parts else norm(v))
            else:
                parts.append("[" + ",".join(str(int(self.eval(x))) for x in v) + "]")
        return "".join(parts)

    def eval(self, e):
        t = e[0]
        if t == "lit":
            return e[1]
        if t == "var":
            return self.get(self.lv_key(e))
        if t == "not":
            v = self.eval(e[1])
            if isinstance(v, bool) or v in (0, 1) and not isinstance(v, float):
                return not v
            return ~int(v)
        if t == "neg":
            return -self.eval(e[1])
        if t == "bin":
            op = e[1]
            a = self.eval(e[2])
            if op == "AND" and isinstance(a, bool) and not a:
                return False
            if op == "OR" and isinstance(a, bool) and a:
                return True
            b = self.eval(e[3])
            if op in ("AND", "OR", "XOR"):
                if isinstance(a, bool) or isinstance(b, bool):
                    a, b = bool(a), bool(b)
                    return {"AND": a and b, "OR": a or b, "XOR": a != b}[op]
                return {"AND": int(a) & int(b), "OR": int(a) | int(b), "XOR": int(a) ^ int(b)}[op]
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if op == "/":
                return self.divide(a, b, None if isinstance(a, int) and isinstance(b, int) else "REAL")
            if op == "MOD":
                if b == 0:
                    raise NotRun("MOD by zero", self.where)
                return a - b * int(float(a) / b) if isinstance(a, int) and isinstance(b, int) else math.fmod(a, b)
            if op == "**":
                return float(a) ** b
            if op == "=":
                return a == b
            if op == "<>":
                return a != b
            if op == "<":
                return a < b
            if op == ">":
                return a > b
            if op == "<=":
                return a <= b
            if op == ">=":
                return a >= b
        if t == "call":
            return self.func(e[1], e[2], e[3])
        raise NotRun("unsupported expression %r" % (t,), self.where)

    def func(self, name, args, line):
        pos = [self.eval(a[1]) for a in args if a[0] == "pos"]
        named = dict((a[1], self.eval(a[2])) for a in args if a[0] == "named")

        def arg(i, *names):
            for n in names:
                if n in named:
                    return named[n]
            if i < len(pos):
                return pos[i]
            raise NotRun("%s: missing argument %s" % (name, names[0] if names else i), self.st_where(line))

        if name == "ABS":
            return abs(arg(0, "in"))
        if name in ("MIN", "MAX"):
            vals = pos + [v for k, v in sorted(named.items())]
            return min(vals) if name == "MIN" else max(vals)
        if name == "LIMIT":
            mn, x, mx = arg(0, "mn", "min"), arg(1, "in"), arg(2, "mx", "max")
            return max(mn, min(x, mx))
        if name == "SEL":
            return arg(2, "in1") if arg(0, "g") else arg(1, "in0")
        if name == "NORM_X":
            mn, x, mx = arg(0, "min"), arg(1, "value"), arg(2, "max")
            if mx == mn:
                raise NotRun("NORM_X with MIN = MAX", self.st_where(line))
            return (float(x) - mn) / float(mx - mn)
        if name == "SCALE_X":
            mn, x, mx = arg(0, "min"), arg(1, "value"), arg(2, "max")
            return float(x) * (mx - mn) + mn
        if name == "SQRT":
            return math.sqrt(arg(0, "in"))
        if name == "TRUNC":
            return int(arg(0, "in"))
        if name == "ROUND":
            return round_even(arg(0, "in"))
        m = re.match(r"^(?:([A-Z]+)_)?TO_([A-Z]+)$", name)
        if m:
            src, dst = m.group(1), m.group(2)
            v = arg(0, "in")
            if dst == "TIME" or src == "TIME":
                return int(v)
            if dst in REAL_TYPES:
                return float(v)
            if dst == "BOOL":
                return bool(v)
            if dst in INT_RANGES:
                return coerce(v, dst)
        raise NotRun("function %s is not simulated" % name, self.st_where(line))

    # ---- ST statements ----
    def st_where(self, line):
        try:
            return "%s line %s" % (self.st_rid, self.st_linenos[line])
        except (AttributeError, IndexError, TypeError):
            return self.where

    def assign(self, lv, v):
        self.set(self.lv_key(lv), v)

    def exec_stmts(self, body):
        for s in body:
            self.where = self.st_where(s[1])
            k = s[0]
            if k == "assign":
                self.assign(s[2], self.eval(s[3]))
            elif k == "if":
                for cond, blk in s[2]:
                    if self.eval(cond):
                        self.exec_stmts(blk)
                        break
                else:
                    self.exec_stmts(s[3])
            elif k == "case":
                v = self.eval(s[2])
                for labels, blk in s[3]:
                    hit = False
                    for lo, hi in labels:
                        a = self.eval(lo)
                        if (hi is None and v == a) or (hi is not None and a <= v <= self.eval(hi)):
                            hit = True
                            break
                    if hit:
                        self.exec_stmts(blk)
                        break
                else:
                    self.exec_stmts(s[4])
            elif k == "for":
                _, _, var, a, b, step, blk = s
                i, end = self.eval(a), self.eval(b)
                st = self.eval(step) if step is not None else 1
                n = 0
                self.assign(var, i)
                while (i <= end) if st > 0 else (i >= end):
                    try:
                        self.exec_stmts(blk)
                    except Exit:
                        break
                    i = self.get(self.lv_key(var)) + st
                    self.assign(var, i)
                    n += 1
                    if n > 100000:
                        raise NotRun("FOR loop exceeded 100000 iterations", self.where)
            elif k in ("while", "repeat"):
                n = 0
                while True:
                    if k == "while" and not self.eval(s[2]):
                        break
                    try:
                        self.exec_stmts(s[3] if k == "while" else s[2])
                    except Exit:
                        break
                    if k == "repeat" and self.eval(s[3]):
                        break
                    n += 1
                    if n > 100000:
                        raise NotRun("loop exceeded 100000 iterations (would trip the watchdog)", self.where)
            elif k == "call":
                self.call_fb(s[2], s[3], s[1])
            elif k == "return":
                raise Return()
            elif k == "exit":
                raise Exit()
            elif k == "continue":
                raise NotRun("CONTINUE is not simulated", self.where)

    def call_fb(self, lv, args, line):
        parts = lv[1]
        last = parts[-1][1].lstrip("#").upper() if parts[-1][0] == "n" else None
        if last in IEC_FBS and len(parts) > 1:
            inst = self.lv_key(("var", parts[:-1], lv[2]))
            kind = last
        else:
            inst = self.lv_key(lv)
            kind = self.p.fb_kind.get(inst)
            dt = self.p.types.get(inst, "")
            if kind is None and dt.upper() in IEC_FBS:
                kind = dt.upper()
        if kind is None:
            raise NotRun("call to %s: user function blocks/functions are not simulated (only IEC standard FBs)"
                         % self.lv_key(lv), self.where)
        kind = kind.split("_")[0] if kind not in ("R_TRIG", "F_TRIG") else kind
        if kind in ("CTU", "CTD", "CTUD") and (self.p.types.get(inst, "").upper().endswith("DINT")):
            ctype = "DINT"
        else:
            ctype = "INT"
        names = FB_POSITIONAL.get(kind)
        if names is None:
            raise NotRun("FB type %s is not simulated" % kind, self.where)
        pi = 0
        for a in args:
            if a[0] == "named":
                self.set(inst + "." + a[1], self.eval(a[2]))
            elif a[0] == "pos":
                if pi >= len(names):
                    raise NotRun("too many arguments to %s" % kind, self.where)
                self.set(inst + "." + names[pi], self.eval(a[1]))
                pi += 1
        g = lambda m: self.get(inst + "." + m)
        st = self.store
        now = self.now
        if kind == "TON":
            if g("in"):
                if not st.get(inst + ".__run"):
                    st[inst + ".__run"] = True
                    st[inst + ".__start"] = now
                et = min(now - st[inst + ".__start"], int(g("pt")))
                self.set(inst + ".et", et)
                self.set(inst + ".q", et >= int(g("pt")))
            else:
                st[inst + ".__run"] = False
                self.set(inst + ".et", 0)
                self.set(inst + ".q", False)
        elif kind == "TOF":
            if g("in"):
                st[inst + ".__run"] = False
                self.set(inst + ".q", True)
                self.set(inst + ".et", 0)
            elif g("q"):
                if not st.get(inst + ".__run"):
                    st[inst + ".__run"] = True
                    st[inst + ".__start"] = now
                et = min(now - st[inst + ".__start"], int(g("pt")))
                self.set(inst + ".et", et)
                if et >= int(g("pt")):
                    self.set(inst + ".q", False)
        elif kind == "TP":
            inn = bool(g("in"))
            if not st.get(inst + ".__act") and inn and not st.get(inst + ".__prev"):
                st[inst + ".__act"] = True
                st[inst + ".__start"] = now
            if st.get(inst + ".__act"):
                et = min(now - st[inst + ".__start"], int(g("pt")))
                self.set(inst + ".et", et)
                self.set(inst + ".q", et < int(g("pt")))
                if et >= int(g("pt")) and not inn:
                    st[inst + ".__act"] = False
                    self.set(inst + ".et", 0)
            st[inst + ".__prev"] = inn
        elif kind in ("R_TRIG", "F_TRIG"):
            clk = bool(g("clk"))
            m = bool(st.get(inst + ".__m", False))
            self.set(inst + ".q", (clk and not m) if kind == "R_TRIG" else (m and not clk))
            st[inst + ".__m"] = clk
        else:
            lo, hi = INT_RANGES[ctype]
            cv = int(g("cv"))
            pv = int(g("pv"))
            if kind in ("CTU", "CTUD"):
                cu = bool(g("cu"))
                if g("r"):
                    cv = 0
                elif cu and not st.get(inst + ".__cu") and cv < hi:
                    cv += 1
                st[inst + ".__cu"] = cu
            if kind in ("CTD", "CTUD"):
                cd = bool(g("cd"))
                if g("ld"):
                    cv = pv
                elif cd and not st.get(inst + ".__cd") and cv > lo:
                    if not (kind == "CTUD" and g("r")):
                        cv -= 1
                st[inst + ".__cd"] = cd
            self.store[inst + ".cv"] = cv
            if kind == "CTU":
                self.set(inst + ".q", cv >= pv)
            elif kind == "CTD":
                self.set(inst + ".q", cv <= 0)
            else:
                self.set(inst + ".qu", cv >= pv)
                self.set(inst + ".qd", cv <= 0)
        for a in args:
            if a[0] == "out":
                self.assign(a[2], self.get(inst + "." + a[1]))


# --------------------------------------------------------------------------
# Scenarios, mapping, comparison
# --------------------------------------------------------------------------

def load_scenarios(path):
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    sc = d["scenarios"] if isinstance(d, dict) else d
    for s in sc:
        s.setdefault("scan_ms", 10)
        s.setdefault("events", [])
        s.setdefault("initial", {})
        if "duration_ms" not in s:
            s["duration_ms"] = max([e["at_ms"] for e in s["events"]] + [0]) + 1000
    return sc


def load_mapping(path):
    """old (normalised) -> (new normalised, conversion or None)."""
    out = {}
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        old = r.get("old_address") or r.get("old_symbol") or r.get("old")
        new = r.get("new_symbol") or r.get("new_address") or r.get("new")
        if not old or not new:
            continue
        conv = None
        m = re.search(r"raw:\s*(-?[\d.]+)\.\.(-?[\d.]+)\s*->\s*(-?[\d.]+)\.\.(-?[\d.]+)", r.get("notes") or "")
        if m:
            conv = tuple(float(x) for x in m.groups())
        out[norm(old)] = (norm(new), conv)
        if r.get("old_symbol") and r.get("old_address"):
            out.setdefault(norm(r["old_symbol"]), (norm(new), conv))
    return out


def map_name(name, mapping):
    k = norm(name)
    if k in mapping:
        return mapping[k]
    m = re.match(r"^(.*)\.([a-z0-9_]+)$", k)
    if m and m.group(1) in mapping and mapping[m.group(1)][1] is None:
        return (mapping[m.group(1)][0] + "." + m.group(2), None)
    return None


def to_target(v, conv):
    if conv is None:
        return v
    a, b, c, d = conv
    return (float(v) - a) * (d - c) / (b - a) + c


def to_source_scale(v, conv):
    if conv is None:
        return v
    a, b, c, d = conv
    return (float(v) - c) * (b - a) / (d - c) + a


def scalar(v):
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return v
    return v


def run_scenario(prog, sc, rename=None):
    """Return (trace dict name->list of values per scan, None) or (None, NotRun)."""
    if prog.load_error:
        return None, prog.load_error
    rename = rename or {}
    sim = Sim(prog, sc.get("scan_ms", 10))
    observe = sc.get("observe", [])
    keys = []
    for name in observe:
        r = rename.get(name)
        keys.append((name, r[0] if r else norm(name), r[1] if r else None))
    trace = dict((name, []) for name in observe)
    try:
        for name, v in sc.get("initial", {}).items():
            r = rename.get(name)
            k = r[0] if r else norm(name)
            if name.lower().endswith(".pre") or name.lower().endswith(".pt"):
                sim.set(k, to_target(v, r[1] if r else None))
            else:
                sim.set_input(k, to_target(v, r[1] if r else None))
        events = sorted(sc.get("events", []), key=lambda e: e["at_ms"])
        ei = 0
        steps = int(sc["duration_ms"] // sc["scan_ms"]) + 1
        for n in range(steps):
            sim.now = n * sc["scan_ms"]
            while ei < len(events) and events[ei]["at_ms"] <= sim.now:
                ev = events[ei]
                if ev.get("power_cycle"):
                    sim.power_cycle()
                for name, v in (ev.get("set") or {}).items():
                    r = rename.get(name)
                    sim.set_input(r[0] if r else norm(name), to_target(v, r[1] if r else None))
                ei += 1
            sim.scan()
            for name, k, conv in keys:
                trace[name].append(scalar(to_source_scale(sim.get(k), conv)) if conv else scalar(sim.get(k)))
        return trace, None
    except NotRun as e:
        if e.where is None:
            e.where = sim.where
        return None, e


def compress(trace, scan_ms):
    out = {}
    for name, vals in trace.items():
        ch = []
        prev = object()
        for i, v in enumerate(vals):
            if v != prev:
                ch.append([i * scan_ms, v])
                prev = v
        if len(ch) > 60:  # ramps: keep the output compact
            ch = ch[:25] + [["...", "%d changes omitted" % (len(ch) - 50)]] + ch[-25:]
        out[name] = ch
    return out


def cmd_run(args):
    prog = Program(args.program, args.entry)
    res = {"tool": "rungstone/simulate", "label": LABEL, "program": os.path.basename(args.program),
           "format": prog.fmt, "approximations": APPROXIMATIONS, "scenarios": []}
    for sc in load_scenarios(args.scenarios):
        trace, err = run_scenario(prog, sc)
        r = {"id": sc.get("id"), "name": sc.get("name"), "category": sc.get("category"), "label": LABEL}
        if err:
            r.update(verdict="not run", reason=err.reason, where=err.where)
        else:
            r.update(verdict="ran", changes=compress(trace, sc["scan_ms"]),
                     final=dict((k, v[-1] if v else None) for k, v in trace.items()))
        res["scenarios"].append(r)
    return res


def compare_traces(sc, ts, tt, tol):
    first = None
    diffs = {}
    n = min(len(v) for v in ts.values()) if ts else 0
    for i in range(n):
        for name in sc.get("observe", []):
            a, b = ts[name][i], tt[name][i]
            if isinstance(a, bool) or isinstance(b, bool):
                same = int(a) == int(b)  # tolerance never applies to discrete signals
            elif isinstance(a, (int, float)) and isinstance(b, (int, float)):
                same = abs(a - b) <= tol
            else:
                same = a == b
            if not same:
                diffs[name] = diffs.get(name, 0) + 1
                if first is None:
                    first = {"t_ms": i * sc["scan_ms"], "signal": name, "source": a, "target": b}
    return first, diffs


def cmd_compare(args):
    src = Program(args.source, args.source_entry)
    tgt = Program(args.target, args.target_entry)
    mapping = load_mapping(args.mapping)
    res = {"tool": "rungstone/simulate", "schema": "rungstone/verify_results@1", "label": LABEL,
           "source": os.path.basename(args.source), "target": os.path.basename(args.target),
           "mapping": os.path.basename(args.mapping), "tolerance": args.tolerance,
           "approximations": APPROXIMATIONS, "runs": []}
    for sc in load_scenarios(args.scenarios):
        r = {"id": sc.get("id"), "name": sc.get("name"), "category": sc.get("category"), "label": LABEL}
        names = set(sc.get("observe", [])).union(sc.get("initial", {})).union(
            n for e in sc.get("events", []) for n in (e.get("set") or {}))
        rename = {}
        unmapped = []
        for n in sorted(names):
            m = map_name(n, mapping)
            if m is None:
                unmapped.append(n)
            else:
                rename[n] = m
        if unmapped:
            r.update(verdict="not run", reason="no mapping for: %s" % ", ".join(unmapped))
            res["runs"].append(r)
            continue
        ts, es = run_scenario(src, sc)
        tt, et = run_scenario(tgt, sc, rename)
        if es or et:
            reasons = []
            if es:
                reasons.append("source: %s%s" % (es.reason, " at %s" % es.where if es.where else ""))
            if et:
                reasons.append("target: %s%s" % (et.reason, " at %s" % et.where if et.where else ""))
            r.update(verdict="not run", reason="; ".join(reasons))
        else:
            first, diffs = compare_traces(sc, ts, tt, args.tolerance)
            r["scans"] = len(next(iter(ts.values()))) if ts else 0
            if first is None:
                r["verdict"] = "match"
            else:
                r.update(verdict="differs", first_divergence=first, differing_scans=diffs)
            r["final"] = dict((n, {"source": ts[n][-1], "target": tt[n][-1]}) for n in sc.get("observe", []))
        res["runs"].append(r)
    res["summary"] = dict((v, sum(1 for r in res["runs"] if r["verdict"] == v)) for v in ("match", "differs", "not run"))
    if args.state_dir:
        from _shared import state
        d = state.find_dir(args.state_dir, create=True)
        if state.load(d, "project.json") is None:
            state.init(d)
        state.save(d, "verify_results.json", res)
        state.log(d, "verify", "%s vs %s: %s" % (res["source"], res["target"], json.dumps(res["summary"])))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="Rungstone scan-cycle simulator (results are simulated, not hardware-tested)")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--program", required=True)
    r.add_argument("--entry")
    r.add_argument("--scenarios", required=True)
    r.add_argument("-o", "--output")
    c = sub.add_parser("compare")
    c.add_argument("--source", required=True)
    c.add_argument("--target", required=True)
    c.add_argument("--mapping", required=True)
    c.add_argument("--scenarios", required=True)
    c.add_argument("--tolerance", type=float, default=0)
    c.add_argument("--source-entry")
    c.add_argument("--target-entry")
    c.add_argument("--state-dir")
    c.add_argument("-o", "--output")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        res = cmd_run(args)
    elif args.cmd == "compare":
        res = cmd_compare(args)
    else:
        ap.print_help()
        return 1
    s = json.dumps(res, indent=1, ensure_ascii=False)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(s + "\n")
        summ = res.get("summary") or dict((v, sum(1 for x in res["scenarios"] if x["verdict"] == v)) for v in ("ran", "not run"))
        sys.stderr.write("%s: %s (%s)\n" % (args.output, json.dumps(summ), LABEL))
    else:
        sys.stdout.write(s + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
