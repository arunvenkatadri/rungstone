#!/usr/bin/env python3
"""PreToolUse hook: block anything that would write to a network address or device.

Rungstone is read-only and offline. This denies shell commands and MCP tool
calls that talk to controllers, field devices or remote hosts in a writing
way (PLC/OPC/Modbus client libraries, remote shells, uploads), and running
customer-supplied scripts through Windows script hosts. Pure local file
work is untouched.

Scope: plugin hooks run in every session, so only the PLC/fieldbus-specific
patterns (client libraries, device write calls, plant MCP write tools) are
enforced everywhere. Generic patterns (ssh, curl POST, raw sockets, script
hosts) are enforced only inside an Rungstone project (a .rungstone/ folder at
or above the working directory) or when RUNGSTONE_GUARD=always is set.
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
    (r"\b(ssh|scp|sftp|telnet|ftp|tftp|rsh|rlogin|nc|ncat|netcat|socat)\b\s", "remote shell / raw socket tool"),
    (r"\bcurl\b[^|;&]*(-X\s*(POST|PUT|PATCH|DELETE)|--data|-d\s|--upload-file|-T\s|-F\s|--form)", "HTTP write (curl)"),
    (r"\bwget\b[^|;&]*--(post-data|post-file|method=(POST|PUT|PATCH|DELETE))", "HTTP write (wget)"),
    (r"Invoke-(WebRequest|RestMethod)[^|;&]*-Method\s+(Post|Put|Patch|Delete)", "HTTP write (PowerShell)"),
    (r"/dev/(tcp|udp)/", "raw socket redirection"),
    (r"\bsocket\.(socket|create_connection)\b", "raw socket in inline code"),
    (r"\b(cscript|wscript)(\.exe)?\b", "Windows script host (never run customer HMI/SCADA scripts)"),
    (r"\brsync\b[^|;&]*\s[\w.-]+@?[\w.-]+:", "remote copy (rsync)"),
]
MCP_WRITE = re.compile(r"(?i)^(write|set|put|download|force|program|upload|delete|start|stop|run|execute|toggle)")
MCP_PLC = re.compile(r"(?i)(plc|opc|modbus|s7|logix|siemens|rockwell|codesys|twincat|ads|historian|scada|ignition|wincc|"
                     r"factorytalk|kepware|profinet|ethernetip|cip)")


def in_project(cwd):
    if os.environ.get("RUNGSTONE_GUARD", "").lower() == "always":
        return True
    cur = os.path.abspath(cwd or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(cur, ".rungstone")):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


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
                return deny("%s detected in command. Rungstone never connects to or writes to controllers, devices "
                            "or remote hosts, and never runs customer scripts. Produce files for the engineer to load "
                            "instead. If this is genuinely local and harmless, the user can run it themselves." % why)
        return 0
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        server = parts[1] if len(parts) > 1 else ""
        action = parts[-1]
        if MCP_PLC.search(server + " " + action) and MCP_WRITE.search(action):
            return deny("MCP tool '%s' looks like a write/control action on a plant system. Rungstone is read-only; "
                        "only read/query tools are allowed." % tool)
    return 0


if __name__ == "__main__":
    sys.exit(main())
