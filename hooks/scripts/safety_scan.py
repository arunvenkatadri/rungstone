#!/usr/bin/env python3
"""PostToolUse hook: warn when a file read or written looks like it contains safety logic.

Safety-rated logic must go through parse_project.py, which excludes it. If
Claude reads a raw export containing safety content, or writes a file that
contains safety instructions, Claude gets an additionalContext warning (the
action is not undone; the warning makes Claude correct course).
"""
import json
import os
import re
import sys

PATTERNS = [
    (r'Class="Safety"', "GuardLogix safety class"),
    (r"<ProgrammingLanguage>\s*F_(LAD|FBD|STL|SCL|DB)", "Siemens F-block"),
    (r"\b(ESTOP|DCS|DCST|DCSTL|DCSTM|DCSRT|DCM|DCAF|THRSE|TSAM|TSSM|SMAT|CROUT|FPMS|FSBM)\s*\(", "Rockwell safety instruction"),
    (r'"?\b(ESTOP1|TWO_H_EN|MUTING|EV1oo2DI|FDBACK|SFDOOR|ACK_GL)\b"?\s*\(', "Siemens safety instruction"),
    (r"\bSF_(EmergencyStop|ESPE|GuardMonitoring|GuardLocking|TwoHandControl\w*|SafetyRequest|MutingSeq|MutingPar\w*|"
     r"SafeStop\d|EDM|EnableSwitch|ModeSelector|Equivalent|Antivalent|TestableSafetySensor|OutControl)\b", "PLCopen safety FB"),
    (r"\bSAFE(BOOL|INT|DINT|WORD|TIME)\b", "IEC safety data type"),
]
LIMIT = 4 * 1024 * 1024


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    ti = data.get("tool_input") or {}
    tool = data.get("tool_name") or ""
    path = ti.get("file_path") or ""
    if "/.rungstone/" in path.replace("\\", "/") or "/rungstone-plc/references/" in path:
        return 0
    text = ""
    if tool == "Write":
        text = ti.get("content") or ""
    elif tool in ("Edit", "MultiEdit"):
        text = ti.get("new_string") or json.dumps(ti.get("edits") or [])
    elif tool == "Read" and path and os.path.isfile(path):
        try:
            with open(path, "rb") as f:
                text = f.read(LIMIT).decode("utf-8", "replace")
        except OSError:
            return 0
    hits = []
    for pat, why in PATTERNS:
        m = re.search(pat, text)
        if m:
            hits.append("%s (%s)" % (why, m.group(0)[:40]))
    if not hits:
        return 0
    if tool == "Read":
        msg = ("Rungstone safety notice: %s appears to contain safety-rated logic: %s. Do not analyse, summarise, translate "
               "or test that logic. Parse the file with parse_project.py (it excludes safety content) and show the "
               "SAFETY LOGIC EXCLUDED notice to the user." % (os.path.basename(path), "; ".join(hits)))
    else:
        msg = ("Rungstone safety notice: the file just written (%s) contains what looks like safety logic: %s. Rungstone "
               "never generates or exports safety logic. Remove it (safety signals may only appear as inputs read from "
               "the target safety program) and tell the user." % (os.path.basename(path) or "?", "; ".join(hits)))
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
