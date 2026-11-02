"""
portability.py - lock-in report: what ties a PLC project to its platform, and what moving costs.

Reads the same inputs as parse_project.py (or its parsed.json) and checks every POU / routine
against a catalogue of platform-specific features: object orientation (EXTENDS, interfaces,
methods, properties), pointers and references, dynamic memory, runtime and vendor libraries
(file, sockets, clock, motion, Util), exceptions, CODESYS operators and pragmas, data types
that only some platforms have, task setup, direct I/O addresses and CFC bodies.

For each target platform every finding is rated:
  portable       moves as-is (or with a mechanical rename)
  rework         an equivalent exists, but the code has to be rewritten around it
  no_equivalent  the target has nothing comparable; the function moves elsewhere (HMI, historian,
                 separate controller) or the design changes

and carries a rough effort range in engineer-hours. The totals are a planning range from static
analysis of the files supplied, not a quote: they exclude testing, commissioning, I/O checkout and
any safety work (safety logic is excluded and must be redone by the safety team on the target).

Targets: openplc (OpenPLC Runtime / Beremiz, MatIEC compiler), iec-st (plain IEC 61131-3 ST without
vendor extensions), siemens-scl (S7-1500), rockwell (Logix 5000). The source's own platform is left out.

Usage:
  python3 portability.py FILES|parsed.json [--targets openplc,iec-st,siemens-scl,rockwell]
                         [--md report.md] [-o out.json]
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import parse_project as pp  # noqa: E402

TARGETS = ("openplc", "iec-st", "siemens-scl", "rockwell", "iec61499")
TARGET_NAMES = {"openplc": "OpenPLC / Beremiz", "iec-st": "Plain IEC 61131-3 ST",
                "siemens-scl": "Siemens S7-1500 SCL", "rockwell": "Rockwell Logix 5000",
                "iec61499": "IEC 61499 (4diac)"}
PLATFORM_NAMES = {"codesys": "CODESYS", "twincat": "Beckhoff TwinCAT", "rockwell": "Rockwell Logix",
                  "siemens": "Siemens TIA Portal", "iec": "IEC 61131-3 ST"}
# A target is the source's own platform when ...
SAME_PLATFORM = {"rockwell": "rockwell", "siemens": "siemens-scl", "iec": "iec-st"}

P, R, N = "portable", "rework", "no_equivalent"


def rate(openplc, iec, siemens, rockwell, iec61499=None):
    """iec61499 defaults to the plain IEC rating: 4diac ST is IEC 61131-3 ST without vendor extensions."""
    return {"openplc": openplc, "iec-st": iec, "siemens-scl": siemens, "rockwell": rockwell,
            "iec61499": iec61499 or iec}


# --------------------------------------------------------------------------
# Rule catalogue
#   where: code (regex on comment/string-free code), type (declared data types), kind (POU kind or
#          header facts), pragma, lang (body language), address, task, persistent, datatype
#   per:   unit (effort counted once per POU that has it), occurrence, or project
#   hours: (low, high) engineer-hours per unit/occurrence/project
# --------------------------------------------------------------------------

def rx(p, flags=re.I):
    return re.compile(p, flags)


RULES = [
    # --- object orientation -------------------------------------------------------------
    dict(id="oop_extends", title="Inheritance (EXTENDS)", category="object orientation", where="kind",
         test=lambda r: bool(r.get("extends")), per="unit", hours=(2, 6),
         ratings=rate(R, R, R, R),
         advice="Flatten: copy the base block's variables and logic into each derived block, or call a base "
                "instance from the derived one. None of the targets has function-block inheritance."),
    dict(id="oop_interface", title="Interfaces (INTERFACE / IMPLEMENTS / interface-typed variables)",
         category="object orientation", where="kind", per="unit", hours=(3, 8),
         test=lambda r: bool(r.get("implements")) or r.get("kind") == "interface",
         ratings=rate(R, R, R, R),
         advice="Replace calls through an interface with a CASE on a station/type number, or call each "
                "instance directly. Arrays of interface references become one array per concrete type."),
    dict(id="oop_method", title="Methods", category="object orientation", where="kind", per="occurrence",
         hours=(0.5, 2), test=lambda r: r.get("kind") == "method",
         ratings=rate(R, R, R, R),
         advice="Turn each method into a command input of the block (Reset := TRUE; call; Reset := FALSE), "
                "or into a separate FUNCTION that takes the instance data as IN_OUT. Logix: AOI with a command input."),
    dict(id="oop_property", title="Properties (Get/Set)", category="object orientation", where="kind",
         per="occurrence", hours=(0.25, 1), test=lambda r: r.get("kind") == "property",
         ratings=rate(R, R, R, R),
         advice="Replace with a plain output (Get) or input (Set) of the block."),
    dict(id="oop_this_super", title="THIS^ / SUPER^", category="object orientation", where="code",
         per="unit", hours=(0.25, 1), pattern=rx(r"\b(THIS|SUPER)\s*\^"),
         ratings=rate(R, R, R, R),
         advice="Disappears when inheritance and methods are flattened; check each use."),
    dict(id="interface_vars", title="Variables of interface type", category="object orientation", where="type",
         per="unit", hours=(1, 3), pattern=None,  # filled in at run time from the interface names found
         ratings=rate(R, R, R, R),
         advice="See interfaces: replace with direct instance calls or a CASE dispatch."),

    # --- pointers and memory -----------------------------------------------------------
    dict(id="pointers", title="Pointers (POINTER TO, ADR, ^)", category="pointers and memory", where="code+type",
         per="unit", hours=(2, 6), pattern=rx(r"\bPOINTER\s+TO\b|\bADR\s*\(|[\w\]\)](?<!THIS)(?<!SUPER)\^"),
         ratings=rate(R, R, R, N, R),
         advice={"iec61499": "Replace with array indexing or REF_TO.",
                 "openplc": "Replace with array indexing, or REF_TO/REF() if your MatIEC build supports them; test.",
                 "iec-st": "Replace with array indexing (REF_TO exists only in edition 3 runtimes).",
                 "siemens-scl": "Replace with array indexing, VARIANT or REF_TO (S7-1500, optimized access).",
                 "rockwell": "No pointers in Logix: use array indexing or indirect addressing with a DINT index."}),
    dict(id="references", title="References (REFERENCE TO, __ISVALIDREF, REF=)", category="pointers and memory",
         where="code+type", per="unit", hours=(1, 3),
         pattern=rx(r"\bREFERENCE\s+TO\b|__ISVALIDREF\s*\(|\bREF\s*="),
         ratings=rate(R, R, R, R),
         advice="Pass the data as VAR_IN_OUT instead (Logix: InOut parameter of an AOI)."),
    dict(id="dynamic_memory", title="Dynamic memory (__NEW / __DELETE)", category="pointers and memory",
         where="code", per="unit", hours=(4, 12), pattern=rx(r"__(NEW|DELETE)\s*\("),
         ratings=rate(N, N, N, N),
         advice="Redesign with fixed-size arrays allocated at compile time. No target allocates memory at run time."),
    dict(id="memory_functions", title="Raw memory functions (MEMCPY, MEMSET, SysMem*, SIZEOF)",
         category="pointers and memory", where="code", per="unit", hours=(1, 3),
         pattern=rx(r"\b(MEMCPY|MEMSET|MEMCMP|MEMMOVE|SysMem\w+|MemUtils\.\w+|SIZEOF)\s*\("),
         ratings=rate(R, R, R, R),
         advice={"openplc": "Replace with element-by-element loops or typed assignments.",
                 "iec-st": "Replace with element-by-element loops or typed assignments.",
                 "siemens-scl": "MOVE_BLK / MOVE_BLK_VARIANT, Serialize/Deserialize.",
                 "rockwell": "COP / CPS, SIZE."}),
    dict(id="union", title="UNION types", category="pointers and memory", where="datatype", per="occurrence",
         hours=(1, 3), test=lambda d: d.get("kind") == "union",
         ratings=rate(R, R, R, R),
         advice={"openplc": "Not supported: keep one representation and convert explicitly (bit shifts, masks).",
                 "iec-st": "Not in the standard: keep one representation and convert explicitly.",
                 "siemens-scl": "AT overlay (standard-access blocks only) or explicit conversion.",
                 "rockwell": "Keep one representation; COP between types where a byte copy is really meant."}),

    # --- runtime and vendor libraries ---------------------------------------------------
    dict(id="file_io", title="File access (CAA File, SysFile, FB_File*)", category="runtime libraries",
         where="code+type", per="unit", hours=(4, 16),
         pattern=rx(r"\bFILE\.\w+|\bCAA_?File\w*|\bSysFile\w*|\bFB_File\w+|\bFB_CsvMem\w*"),
         ratings=rate(N, N, R, N),
         advice={"iec61499": "Move logging to the HMI/SCADA or a historian, fed through a communication FB.",
                 "openplc": "No file access from the PLC program: log through the HMI/SCADA or a historian.",
                 "iec-st": "Outside the standard: move logging to the HMI/SCADA or a historian.",
                 "siemens-scl": "DataLogCreate / DataLogWrite to the memory card, or log in the HMI/historian.",
                 "rockwell": "Controllers have no file system for this: log in FactoryTalk/HMI or a historian."}),
    dict(id="comms", title="Sockets, serial and ADS (SysSock*, NBS, SysCom, ADS*)", category="runtime libraries",
         where="code+type", per="unit", hours=(4, 16),
         pattern=rx(r"\bSysSock\w*|\bNBS\.\w+|\bSysCom\w*|\bCOM\.\w+|\bADS(READ|WRITE|RDSTATE|WRTCTL|LOG\w*)\b|\bFB_Socket\w*"),
         ratings=rate(N, N, R, R, R),
         advice={"iec61499": "Use 4diac communication FBs (CLIENT/SERVER, PUBLISH/SUBSCRIBE incl. MQTT, OPC UA).",
                 "openplc": "No socket/serial blocks; use OpenPLC's Modbus server or an external gateway.",
                 "iec-st": "Outside the standard: needs the target runtime's own communication library.",
                 "siemens-scl": "TCON / TSEND / TRCV (Open User Communication) or PtP blocks.",
                 "rockwell": "MSG instruction (socket object or CIP), or a gateway module."}),
    dict(id="clock", title="Real-time clock (SysTimeRtc*, SysTime*, NT_GetTime, GETSYSTEMTIME)",
         category="runtime libraries", where="code+type", per="unit", hours=(0.5, 2),
         pattern=rx(r"\bSysTime\w*\s*\(|\bSysTimeRtc\w*|\bNT_GetTime\b|\bGETSYSTEMTIME\b|\bF_GetSystemTime\b|\bFB_LocalSystemTime\b"),
         ratings=rate(R, N, R, R, R),
         advice={"iec61499": "Use the runtime's time function or FB from the 4diac library.",
                 "openplc": "Use the runtime's RTC block if your build provides one; otherwise take time from the HMI.",
                 "iec-st": "No clock function in the standard: needs the runtime's own block.",
                 "siemens-scl": "RD_SYS_T / RD_LOC_T.",
                 "rockwell": "GSV WallClockTime (LocalDateTime)."}),
    dict(id="motion", title="Motion control (MC_*, AXIS_REF, SoftMotion, Tc2_MC2)", category="runtime libraries",
         where="code+type", per="unit", hours=(8, 40),
         pattern=rx(r"\bMC_\w+|\bAXIS_REF\w*|\bSMC_\w+|\bSM3_\w+|\bTc2_MC2\b"),
         ratings=rate(N, N, R, R),
         advice={"iec61499": "No motion library in the standard: the axes go to a drive or motion controller.",
                 "openplc": "No motion control: move the axes to a drive or motion controller with its own program.",
                 "iec-st": "Motion is not in IEC 61131-3: needs the target's motion library.",
                 "siemens-scl": "S7-1500 technology objects with the PLCopen-style MC_ blocks; axes are re-configured "
                                "and re-commissioned.",
                 "rockwell": "Logix motion instructions (MSO, MAH, MAM, MAJ...); axes are re-configured in Studio 5000 "
                             "and re-commissioned."}),
    dict(id="pid", title="PID control (Util PID / PID_FIXCYCLE, Tc2_ControllerToolbox)", category="runtime libraries",
         where="code+type", per="unit", hours=(4, 12),
         pattern=rx(r"\b(Util\.)?(PID|PID_FIXCYCLE|PD|PI)\b(?=\s*[;(]|\s*$)|\bFB_CTRL_PID\w*|\bFB_BasicPID\b"),
         ratings=rate(R, R, R, R),
         advice={"openplc": "Use OpenPLC's PID block; re-tune, since the algorithm and units differ.",
                 "iec-st": "Port or write a PID block; re-tune.",
                 "siemens-scl": "PID_Compact; re-tune.",
                 "rockwell": "PIDE (or PID); re-tune."}),
    dict(id="util_lib", title="CODESYS Util / Tc2_Utilities blocks (BLINK, HYSTERESIS, RAMP, LIMITALARM...)",
         category="runtime libraries", where="code+type", per="unit", hours=(0.5, 2),
         pattern=rx(r"\bUtil\.\w+|\b(BLINK|HYSTERESIS|LIMITALARM|RAMP_INT|RAMP_REAL|CHARCURVE|DERIVATIVE|INTEGRAL|"
                    r"LIN_TRAFO|STATISTICS_INT|STATISTICS_REAL|VARIANCE|GEN)\b"),
         ratings=rate(R, R, R, R),
         advice="Small blocks: rewrite in plain ST (a few lines each) or use the target's equivalent."),
    dict(id="system_lib", title="Other runtime/system libraries (Cmp*, SysProcess, IoDrv*, Tc2_System, Tc3_*)",
         category="runtime libraries", where="code+type", per="unit", hours=(1, 4),
         pattern=rx(r"\bCmp[A-Z]\w+|\bSys(Process|Shm|Except|Target|Event|Task)\w*|\bIoDrv\w+|\bETC_\w+|"
                    r"\bTc2_(System|Utilities|Standard|EtherCAT|ModbusSrv)\b|\bTc3_\w+|\bFB_Ec\w+|\bGETCURTASKINDEX\b"),
         ratings=rate(N, N, R, R),
         advice="Find the target's equivalent for each call, or move the function out of the PLC program."),

    # --- language extensions ------------------------------------------------------------
    dict(id="try_catch", title="Exception handling (__TRY / __CATCH)", category="language extensions",
         where="code", per="unit", hours=(1, 3), pattern=rx(r"__(TRY|CATCH|FINALLY|ENDTRY)\b"),
         ratings=rate(R, R, R, R),
         advice={"openplc": "Check the conditions before the operation instead (division by zero, array bounds).",
                 "iec-st": "Check the conditions before the operation instead.",
                 "siemens-scl": "Check before the operation, or GET_ERR_ID / GET_ERROR for local error handling.",
                 "rockwell": "Check before the operation; controller faults go to the fault routine."}),
    dict(id="set_reset_ops", title="CODESYS S= / R= assignments", category="language extensions", where="code",
         per="occurrence", hours=(0.1, 0.25), pattern=rx(r"[\w\]\)]\s+[SR]=(?!=)"),
         ratings=rate(R, R, R, R),
         advice="Rewrite as IF cond THEN x := TRUE; END_IF (or FALSE)."),
    dict(id="bit_access", title="Bit access on integers (x.3)", category="language extensions", where="code",
         per="occurrence", hours=(0.1, 0.25), pattern=rx(r"(?<![\w.#])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\.\d+\b(?!\.\d)"),
         ratings=rate(R, R, R, P, P),
         advice={"openplc": "Use masks and shifts, or .%X3 if your MatIEC build accepts it.",
                 "iec-st": "Use masks and shifts (edition 3 has .%X3).",
                 "siemens-scl": "Same idea, different syntax: x.%X3.",
                 "rockwell": "Same syntax: x.3."}),
    dict(id="platform_int", title="Platform-width types (__XWORD, __UXINT, __XINT)", category="data types",
         where="code+type", per="unit", hours=(0.25, 1), pattern=rx(r"\b__(XWORD|UXINT|XINT)\b"),
         ratings=rate(R, R, R, R),
         advice="Pick a fixed width (DWORD/UDINT or LWORD/ULINT) for the target."),
    dict(id="ltime", title="LTIME / LDATE / LTOD / LDT", category="data types", where="code+type", per="unit",
         hours=(0.25, 1), pattern=rx(r"\b(LTIME|LDATE|LTOD|LDT|LDATE_AND_TIME|LTIME_OF_DAY)\b|\bLTIME#"),
         ratings=rate(R, R, P, R),
         advice={"openplc": "Use TIME (32-bit ms) and check ranges.",
                 "iec-st": "Use TIME; LTIME is edition 3 only.",
                 "siemens-scl": "S7-1500 has LTIME.",
                 "rockwell": "No LTIME: use LINT microseconds or a TIMER/DINT ms value."}),
    dict(id="wide_types", title="64-bit types (LINT, LREAL, LWORD, ULINT)", category="data types",
         where="code+type", per="unit", hours=(0.1, 0.5), pattern=rx(r"\b(LINT|ULINT|LREAL|LWORD)\b"),
         ratings=rate(P, P, P, R),
         advice={"rockwell": "LINT and LREAL exist on 5x80 controllers but many instructions don't take them; "
                             "check each use or use DINT/REAL."}),
    dict(id="wstring", title="WSTRING", category="data types", where="code+type", per="unit", hours=(0.25, 1),
         pattern=rx(r"\bWSTRING\b"), ratings=rate(R, P, P, R),
         advice={"openplc": "Check WSTRING support in your build, or use STRING.",
                 "rockwell": "No WSTRING: STRING (ASCII, 82 characters by default) or a custom string type."}),
    dict(id="enum", title="Enumerations", category="data types", where="datatype", per="occurrence",
         hours=(0.5, 1.5), test=lambda d: d.get("kind") == "enum",
         ratings=rate(P, P, R, R),
         advice={"siemens-scl": "Replace with INT constants (check your TIA Portal version for enum support).",
                 "rockwell": "No enumerations: DINT constants or a UDT of constants."}),

    # --- pragmas ---------------------------------------------------------------------------
    dict(id="attributes", title="Attribute pragmas ({attribute '...'})", category="pragmas", where="pragma",
         per="unit", hours=(0.1, 0.5), pattern=rx(r"\{\s*attribute\b"), blocks_as_is=False,
         ratings=rate(R, R, R, R),
         advice="Most are compiler hints and drop away. Check these ones: qualified_only (names change), "
                "call_after_init / init_on_onlchange (initialisation), pack_mode (memory layout), "
                "symbol (what the HMI/OPC UA sees), enable_dynamic_creation (__NEW)."),
    dict(id="cond_compile", title="Conditional compilation ({IF defined...})", category="pragmas",
         where="code+pragma", per="unit", hours=(0.5, 2), pattern=rx(r"\{\s*(IF|ELSIF|ELSE|END_IF|define|undefine|error|warning|info)\b"),
         ratings=rate(R, R, R, R),
         advice="Decide which branch applies to the target and keep only that code."),

    # --- languages ------------------------------------------------------------------------
    dict(id="cfc", title="CFC bodies (CODESYS Continuous Function Chart)", category="languages", where="lang",
         per="unit", hours=(2, 8), test=lambda r: (r.get("language") or "").upper() == "CFC",
         ratings=rate(R, R, R, R),
         advice="CFC is not an IEC language: redraw as FBD or rewrite as ST. Execution order in CFC is explicit "
                "(numbered); keep it."),
    dict(id="sfc", title="SFC bodies", category="languages", where="lang", per="unit", hours=(2, 8),
         test=lambda r: (r.get("language") or "").upper() == "SFC",
         ratings=rate(P, R, R, R, R),
         advice={"openplc": "OpenPLC has SFC; check action qualifiers and timing.",
                 "iec-st": "Rewrite as a CASE state machine for ST-only runtimes.",
                 "siemens-scl": "S7-GRAPH, or a CASE state machine in SCL.",
                 "rockwell": "Logix SFC (semantics differ: check step timing and action qualifiers), or a state machine."}),
    dict(id="il", title="IL bodies (Instruction List)", category="languages", where="lang", per="unit",
         hours=(1, 4), test=lambda r: (r.get("language") or "").upper() == "IL",
         ratings=rate(P, R, R, R, R),
         advice="IL is deprecated in IEC 61131-3 edition 3: rewrite as ST."),

    # --- runtime behaviour and hardware -------------------------------------------------
    dict(id="persistent", title="PERSISTENT variables", category="runtime behaviour", where="persistent",
         per="project", hours=(1, 3), ratings=rate(R, R, R, R),
         advice={"iec61499": "No RETAIN/PERSISTENT in IEC 61499: store the values explicitly.",
                 "openplc": "Map to OpenPLC persistent storage and test what survives a restart and a download.",
                 "iec-st": "PERSISTENT is not portable: decide RETAIN vs. recipe storage on the target.",
                 "siemens-scl": "Retentive DB variables; a download can reset them unless snapshots are used.",
                 "rockwell": "All tags keep their values through power loss, but a download overwrites them."}),
    dict(id="tasks", title="Multiple tasks (intervals, priorities)", category="runtime behaviour", where="task",
         per="occurrence", hours=(0.5, 2), ratings=rate(R, P, R, R, R),
         advice={"iec61499": "Tasks become event sources: one E_CYCLE per rate driving the FBs that ran in that task.",
                 "openplc": "OpenPLC runs one scan cycle: check whether the fast tasks' timing still holds.",
                 "iec-st": "TASK / PROGRAM WITH is standard; the target runtime decides how priorities work.",
                 "siemens-scl": "Cyclic interrupt OBs (OB30-38) with matching intervals; data shared between "
                                "OBs needs consistency handling.",
                 "rockwell": "Periodic tasks; data shared between tasks needs buffering (UID/UIE or copies)."}),
    dict(id="io_addresses", title="Direct I/O addresses (%IX, %QX, AT %I*)", category="hardware", where="address",
         per="occurrence", hours=(0.05, 0.2), blocks_as_is=False, ratings=rate(R, R, R, R),
         advice={"iec61499": "No addresses: wire the FB's data inputs/outputs to the device's I/O FBs.",
                 "openplc": "Re-map to OpenPLC's %IX0.0 / %QX0.0 layout for the new hardware.",
                 "iec-st": "Re-map to the target hardware's addresses.",
                 "siemens-scl": "Re-map to the new %I / %Q byte.bit addresses in the tag table.",
                 "rockwell": "No direct addresses: alias tags to the module tags (Local:1:I.Data.0)."}),
]

RULE_BY_ID = {r["id"]: r for r in RULES}
# Platforms whose features the catalogue covers. Rockwell and Siemens sources need their own rules
# (GSV/SSV, MSG, AOIs, system blocks...); until then the report says so instead of scoring them.
COVERED_PLATFORMS = ("codesys", "twincat", "iec")

LIB_NAMESPACE_RE = re.compile(
    r"\b(CAA\w*|FILE|SysFile|SysMem|SysTime\w*|SysSock\w*|SysCom|Util|MemUtils|StringUtils|NBS|COM|SM3_\w+|SMC\w*|"
    r"ModbusFB|IoDrv\w+|ETC\w*|Cmp\w+|Tc2_\w+|Tc3_\w+|OSCAT\w*|Standard64)\.\w+")

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def strip_code(lines):
    """Blank out comments and string literals, keep line count (so line numbers stay right)."""
    out = []
    in_block = 0
    for ln in lines:
        s = ln
        res = []
        i = 0
        while i < len(s):
            if in_block:
                if s.startswith("*)", i):
                    in_block -= 1
                    i += 2
                elif s.startswith("(*", i):
                    in_block += 1
                    i += 2
                else:
                    i += 1
                res.append(" ")
                continue
            if s.startswith("(*", i):
                in_block += 1
                i += 2
                res.append("  ")
                continue
            if s.startswith("//", i):
                break
            if s[i] in "'\"":
                q = s[i]
                j = s.find(q, i + 1)
                j = len(s) if j < 0 else j + 1
                res.append(" " * (j - i))
                i = j
                continue
            res.append(s[i])
            i += 1
        out.append("".join(res))
    return out


def source_platform(projects):
    for pr in projects:
        fmt = pr.get("format", "")
        prod = ((pr.get("target") or {}).get("product") or "").lower() if isinstance(pr.get("target"), dict) else ""
        if fmt == "beckhoff-twincat" or "twincat" in prod:
            return "twincat"
        if fmt == "plcopen-xml":
            return "codesys"
        if fmt in ("rockwell-l5x", "l5x", "ladder-text"):
            return "rockwell"
        if fmt in ("simaticml", "siemens-scl", "siemens-simaticml"):
            return "siemens"
    fmts = {pr.get("format") for pr in projects}
    if any(f and "l5x" in f for f in fmts):
        return "rockwell"
    if any(f and ("simatic" in f or "siemens" in f) for f in fmts):
        return "siemens"
    return "iec"


def unit_of(r):
    return r.get("program") or r.get("name") or r.get("id")


def routine_code(r):
    items = r.get("lines") or r.get("rungs") or r.get("networks") or []
    raw = [it.get("text") or "" for it in items]
    nums = [it.get("number") for it in items]
    if r.get("lines"):
        return strip_code(raw), nums
    return raw, nums


def routine_types(r):
    out = []
    for sec in r.get("interface") or []:
        for m in sec.get("members") or []:
            if m.get("data_type"):
                out.append((m.get("name"), m["data_type"]))
    return out


def code_size(r):
    return len(r.get("lines") or []) + len(r.get("rungs") or []) + len(r.get("networks") or [])


# --------------------------------------------------------------------------
# Analysis
# --------------------------------------------------------------------------


def analyse(projects, targets=None):
    platform = source_platform(projects)
    if platform not in COVERED_PLATFORMS:
        return {"tool": "rungstone/portability", "source_platform": platform,
                "source_platform_name": PLATFORM_NAMES.get(platform, platform), "supported": False,
                "message": "The lock-in report covers CODESYS-based, Beckhoff TwinCAT and IEC 61131-3 ST sources. "
                           "%s sources are not scored: use Translate, whose review queue lists the "
                           "platform-specific items one by one." % PLATFORM_NAMES.get(platform, platform)}
    if targets is None:
        targets = [t for t in TARGETS if SAME_PLATFORM.get(platform) != t]
    routines, data_types, tags, tasks, excluded, warnings = [], [], [], [], [], []
    for pr in projects:
        routines += pr.get("routines", [])
        data_types += pr.get("data_types", [])
        tags += pr.get("tags", [])
        tasks += pr.get("tasks", [])
        excluded += pr.get("excluded_safety", [])
        warnings += pr.get("warnings", [])

    interface_names = sorted({r["name"] for r in routines if r.get("kind") == "interface"}
                             | {i for r in routines for i in (r.get("implements") or [])})
    itf_rx = re.compile(r"(?<![\w.])(%s)(?![\w])" % "|".join(map(re.escape, interface_names))) if interface_names else None

    units = {}        # unit -> {"lines": n, "kinds": set(), "routines": [...]}
    for r in routines:
        u = unit_of(r)
        d = units.setdefault(u, {"lines": 0, "routines": [], "kind": None})
        d["lines"] += code_size(r)
        d["routines"].append(r.get("id") or r.get("name"))
        if not r.get("program") or r.get("program") == u and r.get("name") == u:
            d["kind"] = d["kind"] or r.get("kind")

    hits = {}   # rule id -> {"count": n, "units": {unit: {"count": n, "where": [...]}}}

    def hit(rule_id, unit, where, n=1):
        h = hits.setdefault(rule_id, {"count": 0, "units": {}})
        h["count"] += n
        u = h["units"].setdefault(unit, {"count": 0, "where": []})
        u["count"] += n
        if where and len(u["where"]) < 6 and where not in u["where"]:
            u["where"].append(where)

    for r in routines:
        u = unit_of(r)
        rid = r.get("id") or r.get("name")
        code, nums = routine_code(r)
        types = routine_types(r)
        for rule in RULES:
            w = rule["where"]
            if w == "kind" and rule.get("test") and rule["test"](r):
                hit(rule["id"], u, rid)
            elif w == "lang" and rule["test"](r):
                hit(rule["id"], u, rid)
            if "code" in w and rule.get("pattern") is not None:
                for line, num in zip(code, nums):
                    found = rule["pattern"].findall(line)
                    if found:
                        hit(rule["id"], u, "%s line %s" % (rid, num) if num is not None else rid, len(found))
            if "type" in w:
                pat = rule.get("pattern")
                if rule["id"] == "interface_vars":
                    pat = itf_rx
                if pat is not None:
                    for name, t in types:
                        if pat.search(t):
                            hit(rule["id"], u, "%s: %s : %s" % (rid, name, t))
            if "pragma" in w and rule.get("pattern") is not None:
                for p in r.get("pragmas") or []:
                    if rule["pattern"].search(p):
                        hit(rule["id"], u, "%s %s" % (rid, p))

    for d in data_types:
        for rule in RULES:
            if rule["where"] == "datatype" and rule["test"](d):
                hit(rule["id"], "types", d.get("name"))
        for m in d.get("members") or []:
            for rule in RULES:
                if "type" in rule["where"] and rule.get("pattern") is not None and m.get("data_type") \
                        and rule["pattern"].search(m["data_type"]):
                    hit(rule["id"], "types", "%s.%s : %s" % (d.get("name"), m.get("name"), m["data_type"]))
        for p in d.get("pragmas") or []:
            if RULE_BY_ID["attributes"]["pattern"].search(p):
                hit("attributes", "types", "%s %s" % (d.get("name"), p))

    gvl_pragmas = {}
    for t in tags:
        scope = (t.get("scope") or "").split(":", 1)[-1] or "globals"
        for rule in RULES:
            if "type" in rule["where"] and rule.get("pattern") is not None and t.get("data_type") \
                    and rule["pattern"].search(t["data_type"]):
                hit(rule["id"], scope, "%s : %s" % (t.get("name"), t["data_type"]))
        if t.get("address"):
            hit("io_addresses", scope, "%s AT %s" % (t.get("name"), t["address"]))
        if t.get("persistent"):
            hit("persistent", scope, t.get("name"))
        for p in t.get("pragmas") or []:
            gvl_pragmas.setdefault(scope, set()).add(p)
    for scope, ps in gvl_pragmas.items():
        for p in sorted(ps):
            hit("attributes", scope, "%s %s" % (scope, p))
    for r in routines:
        for sec in r.get("interface") or []:
            for m in sec.get("members") or []:
                if m.get("address"):
                    hit("io_addresses", unit_of(r), "%s AT %s" % (m.get("name"), m["address"]))
                if m.get("persistent"):
                    hit("persistent", unit_of(r), m.get("name"))
    if len(tasks) > 1:
        for t in tasks[1:]:
            hit("tasks", "tasks", "%s (%s, priority %s)" % (t.get("name"), t.get("interval") or "event/freewheeling",
                                                           t.get("priority")))

    # libraries referenced by namespace
    libs = {}
    for r in routines:
        code, _ = routine_code(r)
        blob = "\n".join(code) + "\n" + "\n".join(t for _, t in routine_types(r))
        for m in LIB_NAMESPACE_RE.finditer(blob):
            libs.setdefault(m.group(1), set()).add(unit_of(r))

    # --- per target totals --------------------------------------------------------------
    findings = []
    for rid, h in hits.items():
        rule = RULE_BY_ID[rid]
        lo, hi = rule["hours"]
        if rule["per"] == "unit":
            mult = len(h["units"])
        elif rule["per"] == "occurrence":
            mult = h["count"]
        else:
            mult = 1
        adv = rule["advice"]
        f = {"rule": rid, "title": rule["title"], "category": rule["category"], "count": h["count"],
             "blocks_as_is": rule.get("blocks_as_is", True),
             "units": [{"name": k, "count": v["count"], "where": v["where"]} for k, v in sorted(h["units"].items())],
             "ratings": {}, "advice": {}, "hours": {}}
        for t in targets:
            rt = rule["ratings"][t]
            f["ratings"][t] = rt
            f["advice"][t] = adv.get(t, "") if isinstance(adv, dict) else adv
            f["hours"][t] = [0, 0] if rt == P else [round(lo * mult, 1), round(hi * mult, 1)]
        findings.append(f)
    order = {N: 0, R: 1, P: 2}
    findings.sort(key=lambda f: (min(order[f["ratings"][t]] for t in targets) if targets else 0,
                                 -max((f["hours"][t][1] for t in targets), default=0)))

    excluded_names = {e.get("name") for e in excluded}
    assessed_units = {u: d for u, d in units.items() if u not in excluded_names}
    total_lines = sum(d["lines"] for d in assessed_units.values())
    per_unit = {}
    for u in assessed_units:
        per_unit[u] = {}
        for t in targets:
            worst = P
            for f in findings:
                if RULE_BY_ID[f["rule"]].get("blocks_as_is", True) and any(x["name"] == u for x in f["units"]):
                    rt = f["ratings"][t]
                    if order[rt] < order[worst]:
                        worst = rt
            per_unit[u][t] = worst
    summary = {}
    for t in targets:
        portable_units = [u for u in assessed_units if per_unit[u][t] == P]
        plines = sum(assessed_units[u]["lines"] for u in portable_units)
        lo = sum(f["hours"][t][0] for f in findings)
        hi = sum(f["hours"][t][1] for f in findings)
        summary[t] = {
            "name": TARGET_NAMES[t],
            "portable_line_pct": round(100.0 * plines / total_lines) if total_lines else None,
            "portable_units": len(portable_units),
            "rework_units": sum(1 for u in assessed_units if per_unit[u][t] == R),
            "blocked_units": sum(1 for u in assessed_units if per_unit[u][t] == N),
            "blockers": [f["title"] for f in findings if f["ratings"][t] == N],
            "hours": [round(lo, 1), round(hi, 1)],
        }

    limits = [
        "Static analysis of the files supplied only. Libraries are seen where the code calls them; the "
        "library code itself is not in the export.",
        "Visualisations (CODESYS Visu/WebVisu, TwinCAT HMI), device/fieldbus configuration and I/O mapping "
        "outside the PLC code are not in a PLCopen export and are not assessed.",
        "Hours are a rough planning range for code changes only: no testing, commissioning, I/O checkout, "
        "documentation or safety work. Not a quote.",
    ]
    if excluded:
        limits.append("Safety logic was excluded and not assessed: the safety team re-implements it on a "
                      "certified safety platform for the target.")
    return {
        "tool": "rungstone/portability",
        "supported": True,
        "source_platform": platform,
        "source_platform_name": PLATFORM_NAMES.get(platform, platform),
        "targets": targets,
        "summary": summary,
        "findings": findings,
        "units": [{"name": u, "kind": d["kind"], "lines": d["lines"], "ratings": per_unit[u]}
                  for u, d in sorted(assessed_units.items())],
        "libraries": [{"namespace": k, "used_in": sorted(v)} for k, v in sorted(libs.items())],
        "tasks": tasks,
        "not_assessed": [{"name": e.get("name"), "reason": e.get("reason")} for e in excluded],
        "warnings": warnings,
        "limits": limits,
        "basis": "Rough planning estimate from static analysis; ratings are rules of thumb an engineer confirms.",
    }


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------

WORD = {P: "as-is", R: "rework", N: "no equivalent"}


def cell(s):
    return str(s).replace("|", "\\|").replace("\n", " ")


def pous(n):
    return "%d POU%s" % (n, "" if n == 1 else "s")


def hours(h):
    return "0 h" if h == [0, 0] else "%g-%g h" % (h[0], h[1])


def to_markdown(res, title=None):
    if not res.get("supported", True):
        return "# Lock-in report: not available\n\n%s\n" % res["message"]
    t = res["targets"]
    out = ["# Lock-in report: %s" % (title or res["source_platform_name"] + " project"), "",
           "> Rough planning estimate from static analysis of the files supplied. Ratings are rules of thumb "
           "for an engineer to confirm; hours cover code changes only (no testing, commissioning or safety work). "
           "Not a quote.", ""]
    if res["not_assessed"]:
        out += ["**Not assessed (safety logic, excluded):** " +
                ", ".join("%s (%s)" % (x["name"], x["reason"]) for x in res["not_assessed"]), ""]
    out += ["## Summary", "", "Source platform: **%s**. \"Moves as-is\" means no code changes; I/O re-mapping and "
            "compiler-hint pragmas still count toward the hours." % res["source_platform_name"], "",
            "| Target | Moves as-is | Needs rework | No equivalent | Rough effort |", "|---|---|---|---|---|"]
    for k in t:
        s = res["summary"][k]
        pct = "%s%% of code lines" % s["portable_line_pct"] if s["portable_line_pct"] is not None else "-"
        out.append("| %s | %s (%s) | %s | %s | %s |" % (
            s["name"], pct, pous(s["portable_units"]), pous(s["rework_units"]),
            cell(", ".join(s["blockers"])) if s["blockers"] else "none", hours(s["hours"])))
    out += ["", "## What ties it to %s" % res["source_platform_name"], "",
            "| # | Finding | Found in | " + " | ".join(TARGET_NAMES[k] for k in t) + " |",
            "|---|---|---|" + "---|" * len(t)]
    for i, f in enumerate(res["findings"], 1):
        where = ", ".join("%s (%d)" % (u["name"], u["count"]) for u in f["units"][:6])
        if len(f["units"]) > 6:
            where += ", +%d more" % (len(f["units"]) - 6)
        out.append("| %d | %s | %s | %s |" % (i, cell(f["title"]), cell(where), " | ".join(
            "%s, %s" % (WORD[f["ratings"][k]], hours(f["hours"][k])) for k in t)))
    out += ["", "### How to move each one", ""]
    for i, f in enumerate(res["findings"], 1):
        advs = {}
        for k in t:
            if f["ratings"][k] != P and f["advice"][k]:
                advs.setdefault(f["advice"][k], []).append(TARGET_NAMES[k])
        if not advs:
            continue
        out.append("%d. **%s**" % (i, f["title"]))
        for a, names in advs.items():
            out.append("   - %s: %s" % ("All targets" if len(names) == len(t) else ", ".join(names), a))
        ex = [w for u in f["units"] for w in u["where"]][:4]
        if ex:
            out.append("   - Examples: %s" % "; ".join("`%s`" % e for e in ex))
    out += ["", "## By POU", "", "| POU | Kind | Lines | " + " | ".join(TARGET_NAMES[k] for k in t) + " |",
            "|---|---|---|" + "---|" * len(t)]
    for u in res["units"]:
        out.append("| %s | %s | %d | %s |" % (cell(u["name"]), u["kind"] or "", u["lines"],
                                              " | ".join(WORD[u["ratings"][k]] for k in t)))
    if res["libraries"]:
        out += ["", "## Libraries referenced", "",
                "The library code is not in the export. Each one needs an equivalent on the target, or a license "
                "that allows using it there.", "", "| Library / namespace | Used in |", "|---|---|"]
        for l in res["libraries"]:
            out.append("| %s | %s |" % (l["namespace"], ", ".join(l["used_in"])))
    if res["tasks"]:
        out += ["", "## Tasks", "", "| Task | Interval | Priority | Runs |", "|---|---|---|---|"]
        for tk in res["tasks"]:
            out.append("| %s | %s | %s | %s |" % (tk.get("name"), tk.get("interval") or "event/freewheeling",
                                                   tk.get("priority") or "", ", ".join(tk.get("programs") or [])))
    out += ["", "## What this report does not see", ""] + ["- " + l for l in res["limits"]]
    if res["warnings"]:
        out += ["", "Parser warnings:", ""] + ["- " + w for w in res["warnings"]]
    return "\n".join(out) + "\n"


def load_projects(files, allow):
    ctx = pp.Ctx(allow)
    projects = []
    for f in files:
        if f.lower().endswith(".json"):
            try:
                d = json.loads(pp.read_text(f))
                if d.get("tool") == "rungstone/parse_project":
                    projects.extend(d["projects"])
                    continue
            except ValueError:
                pass
        projects.append(pp.parse_file(f, ctx))
    return projects


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--targets", help="comma list from: %s (default: all except the source platform)" % ",".join(TARGETS))
    ap.add_argument("--md", help="write the Markdown report here")
    ap.add_argument("--title")
    ap.add_argument("-o", "--out")
    ap.add_argument("--allow-suspected", action="append", default=[])
    a = ap.parse_args(argv)
    targets = None
    if a.targets:
        targets = [t.strip() for t in a.targets.split(",") if t.strip()]
        bad = [t for t in targets if t not in TARGETS]
        if bad:
            ap.error("unknown target(s): %s" % ", ".join(bad))
    res = analyse(load_projects(a.files, a.allow_suspected), targets)
    if a.md:
        os.makedirs(os.path.dirname(os.path.abspath(a.md)), exist_ok=True)
        with open(a.md, "w", encoding="utf-8") as f:
            f.write(to_markdown(res, a.title))
    text = json.dumps(res, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        s = res.get("summary", {})
        print(json.dumps({"written": a.out, "md": a.md, "source_platform": res["source_platform"],
                          "supported": res["supported"], "message": res.get("message"),
                          "summary": {k: {"portable_line_pct": v["portable_line_pct"], "hours": v["hours"],
                                          "blockers": v["blockers"]} for k, v in s.items()}}, indent=1))
    else:
        print(text)


if __name__ == "__main__":
    main()
