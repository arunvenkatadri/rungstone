"""UserPromptExpansion hook: block /rungstone:export while high-risk review items are open.

export.py enforces the same gate itself; this hook stops the command before
Claude starts working on it and tells the user which items are open.
"""
import json
import os
import sys


def find_state(start):
    cur = os.path.abspath(start)
    while True:
        cand = os.path.join(cur, ".rungstone", "review_queue.json")
        if os.path.isfile(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    name = (data.get("command_name") or "").split(":")[-1]
    if name != "export":
        return 0
    q = find_state(data.get("cwd") or os.getcwd())
    if not q:
        return 0  # no project state yet; export.py will explain
    try:
        with open(q, encoding="utf-8") as f:
            items = json.load(f).get("items", [])
    except (OSError, ValueError):
        return 0
    hi = [i for i in items if i.get("status") == "open" and i.get("risk") == "high"]
    if hi:
        lines = ", ".join("%s (%s)" % (i.get("id"), i.get("location", "")) for i in hi[:10])
        print(json.dumps({"decision": "block",
                          "reason": "Rungstone: export blocked - %d high-risk review item(s) still open: %s. "
                                    "Approve, edit or reject them first (e.g. 'approve R1', 'reject R2: reason')."
                                    % (len(hi), lines)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
