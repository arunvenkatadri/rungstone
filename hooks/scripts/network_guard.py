"""PreToolUse hook: block anything that would write to a network address or device.

Rungstone is read-only and offline. This denies shell commands and MCP tool
calls that talk to controllers, field devices or remote hosts in a writing
way (PLC/OPC/Modbus client libraries, remote shells, uploads), and running
customer-supplied scripts through Windows script hosts. Pure local file
work is untouched.

Scope: plugin hooks run in every session, so only the PLC/fieldbus-specific
patterns (client libraries, device write calls, plant MCP write tools) are
enforced everywhere. Generic patterns (ssh, curl POST, raw sockets, script
hosts) are enforced only inside an Rungstone project (a .rungstone/ folder at or above the working directory).

Files: Write/Edit/MultiEdit content is checked too, so the rule is not side-stepped
by writing a controller-writing script for the user to run. Everywhere, a file
that uses a PLC/fieldbus client library together with a write call is denied;
inside a Rungstone project, any file using such a library is denied.
"""
import json
import os
import re
import sys

ALWAYS = 2  # BASH_PATTERNS[0..ALWAYS] (PLC-specific) apply in every session
BASH_PATTERNS = [
    (r"\b(pycomm3|pylogix|snap7|python-snap7|cpppo|libplctag|plctag|pymodbus|minimalmodbus|umodbus|asyncua|opcua|"
     r"freeopcua|node-opcua|s7comm|ab_plc|pyads|pycomm)\b", "PLC/fieldbus client library"),
    (r"\b(mbpoll|modpoll|modbus-cli|modbus_write|opcua-client|uawrite|ua-write|ads-write)\b", "fieldbus write tool"),
    (r"\.(write_tag|write_register|write_registers|write_coil|write_coils|db_write|write_area|write_by_name|"
     r"set_value|write_value|write_attribute)\s*\(", "device write call"),
    # command position only, lower case: "SCP" and "NC" are PLC terms that appear in notes and file text
    (r"(?-i:(?:^|[;&|(`]|\$\()\s*(?:sudo\s+)?(ssh|scp|sftp|telnet|ftp|tftp|rsh|rlogin|nc|ncat|netcat|socat)\s)",
     "remote shell / raw socket tool"),
    (r"\bcurl\b[^|;&]*(-X\s*(POST|PUT|PATCH|DELETE)|--data|-d\s|--upload-file|-T\s|-F\s|--form)", "HTTP write (curl)"),
    (r"\bwget\b[^|;&]*--(post-data|post-file|method=(POST|PUT|PATCH|DELETE))", "HTTP write (wget)"),
    (r"Invoke-(WebRequest|RestMethod)[^|;&]*-Method\s+(Post|Put|Patch|Delete)", "HTTP write (PowerShell)"),
    (r"/dev/(tcp|udp)/", "raw socket redirection"),
    (r"\bsocket\.(socket|create_connection)\b", "raw socket in inline code"),
    (r"\b(cscript|wscript)(\.exe)?\b", "Windows script host (never run customer HMI/SCADA scripts)"),
    (r"\brsync\b[^|;&]*\s[\w.-]+@?[\w.-]+:", "remote copy (rsync)"),
]
WRITE_VERBS = ("write", "put", "download", "force", "program", "upload", "delete", "start", "stop", "run", "execute",
               "toggle", "set")


class _McpWrite(object):
    @staticmethod
    def search(action):
        return action.lower().startswith(WRITE_VERBS)


MCP_WRITE = _McpWrite()
MCP_PLC = re.compile(r"(?i)(plc|opc|modbus|s7|logix|siemens|rockwell|codesys|twincat|ads|historian|scada|ignition|wincc|"
                     r"factorytalk|kepware|profinet|ethernetip|cip)")


def in_project(cwd):
    cur = os.path.abspath(cwd or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(cur, ".rungstone")):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


FILE_WRITE_CALL = re.compile(r"\.(write|write_tag|write_register|write_registers|write_coil|write_coils|db_write|"
                             r"write_area|write_by_name|set_value|write_value|write_attribute|download)\s*\(", re.I)
REFUSAL = ("Rungstone never connects to or writes to controllers, devices or remote hosts, and that includes "
           "writing scripts or commands for the user to run against live equipment. Do not offer a workaround. "
           "Tell the user that changes to a running controller are made by the engineer in the vendor IDE "
           "(Studio 5000, TIA Portal, CODESYS, TwinCAT) under the site's change procedure, and offer offline help "
           "instead: explain the logic, prepare an importable file, or draft the change for review.")


def file_text(tool, ti):
    if tool == "Write":
        return ti.get("content") or ""
    if tool == "Edit":
        new = ti.get("new_string") or ""
    elif tool == "MultiEdit":
        new = "\n".join((e or {}).get("new_string") or "" for e in ti.get("edits") or [])
    else:
        return ""
    try:
        with open(ti.get("file_path") or "", encoding="utf-8", errors="replace") as fh:
            return fh.read(2000000) + "\n" + new
    except (OSError, ValueError):
        return new


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": "Rungstone read-only guard: " + reason}}))
    return 0


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    tool = data.get("tool_name") or ""
    ti = data.get("tool_input") or {}
    if tool == "Bash":
        cmd = ti.get("command") or ""
        scoped = in_project(data.get("cwd"))
        for n, (pat, why) in enumerate(BASH_PATTERNS):
            if n > ALWAYS and not scoped:
                break
            if re.search(pat, cmd, re.I):
                return deny("%s detected in command. %s" % (why, REFUSAL))
        return 0
    if tool in ("Write", "Edit", "MultiEdit"):
        text = file_text(tool, ti)
        if re.search(BASH_PATTERNS[0][0], text, re.I):
            if FILE_WRITE_CALL.search(text) or in_project(data.get("cwd")):
                return deny("file uses a PLC/fieldbus client library to talk to a device. " + REFUSAL)
        return 0
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        server = parts[1] if len(parts) > 1 else ""
        action = parts[-1]
        if MCP_PLC.search(server + " " + action) and MCP_WRITE.search(action):
            return deny("MCP tool '%s' looks like a write/control action on a plant system. Rungstone is read-only; "
                        "only read/query tools are allowed. %s" % (tool, REFUSAL))
    return 0


if __name__ == "__main__":
    sys.exit(main())
