"""
static.py - one self-contained workbench HTML page for hosts without a local browser server.

  python3 static.py --state-dir .rungstone -o workbench.html

The current .rungstone/ state is embedded as JSON.  The page is read-only:
Approve / Edit / Reject build an instruction line ("approve R1, R3; reject
R2: reason") that the user pastes into chat; the assistant then applies it with
state.py and regenerates this page.  No network access, no external assets.
"""

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import server  # noqa: E402  (shares build_state / render_app; never starts a server here)


def build(state_dir, app=None):
    st = server.build_state(os.path.abspath(state_dir))
    st["state_dir"] = os.path.basename(st["state_dir"]) or ".rungstone"
    return server.render_app({"mode": "static", "state": st, "app": app if app in server.APPS else server.DEFAULT_APP})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--state-dir", help=".rungstone folder (default: search upward from cwd)")
    ap.add_argument("-o", "--output", default="workbench.html")
    ap.add_argument("--app", choices=server.APPS)
    args = ap.parse_args(argv)
    d = args.state_dir or server.state.find_dir()
    if d and os.path.isdir(os.path.join(d, server.state.STATE_DIR)):
        d = os.path.join(d, server.state.STATE_DIR)
    if not d or not os.path.isdir(d):
        print(json.dumps({"error": "no .rungstone/ folder found; run state.py init first"}))
        return 1
    html = build(d, args.app)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)
    print(json.dumps({"written": os.path.abspath(args.output), "bytes": len(html.encode("utf-8")),
                      "mode": "static (approvals happen in chat)"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
