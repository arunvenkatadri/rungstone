"""
chart.py - trend chart of historian signals with event markers.

One panel per analog signal (up to --max-analog-panels, the rest share the
last panel) plus one stacked step panel for boolean/state signals.  Event
markers come from --mark "TIME=label" and/or an alarm/event log (--alarms).
Writes PNG with matplotlib when it is installed, otherwise (or with --svg) a
self-contained SVG drawn by this script.  Dark neutral theme.

Usage:
  python3 chart.py FILE --tags PT_101,PSL_101,P101_Run --around 2025-06-12T02:29:44 \
      --before 15m --after 3m --mark "2025-06-12T02:29:44=P-101 trip" --alarms alarms.csv \
      --out p101_trip.png [--title "..."] [--svg]
"""

import argparse
import json
import os
import sys
from xml.sax.saxutils import escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ops_csv as oc  # noqa: E402
import historian as hs  # noqa: E402

BG, PANEL, GRID, TEXT, MUTED, AMBER = "#15171c", "#1d2027", "#2b2f38", "#d7dae0", "#8b919c", "#ffb000"
PALETTE = ["#4cc9f0", "#f4a261", "#90be6d", "#e76f51", "#b8a1ff", "#f9c74f", "#43aa8b", "#f28482"]


def bucket(points, n):
    """Min/max bucketing that keeps spikes; points = [(t, v)]."""
    if len(points) <= 2 * n:
        return points
    t0, t1 = points[0][0], points[-1][0]
    w = (t1 - t0) / float(n) or 1.0
    out, cur, b = [], [], 0
    for t, v in points:
        nb = int((t - t0) / w)
        if nb != b and cur:
            lo = min(cur, key=lambda p: p[1])
            hi = max(cur, key=lambda p: p[1])
            out.extend(sorted({lo, hi}))
            cur = []
        b = nb
        cur.append((t, v))
    if cur:
        out.extend(sorted({min(cur, key=lambda p: p[1]), max(cur, key=lambda p: p[1])}))
    return out


def gather(a):
    tp = oc.TimeParser()
    start, end, center = hs.time_range(a, tp)
    tags = [t.strip() for t in a.tags.split(",")] if a.tags else None
    info, tp, series = hs.load_series(a.file, tags, start, end, tp)
    if not series:
        raise SystemExit("no data for those tags/time range")
    order = [t for t in (tags or sorted(series)) if t in series] or sorted(series)
    analog, discrete = [], []
    for name in order:
        s = series[name]
        kind = hs.series_kind(s)
        pts = [(t, v) for t, k, v in s if k in ("num", "bool")]
        if kind == "analog":
            analog.append((name, bucket(pts, 600)))
        elif kind in ("bool", "state") and pts:
            discrete.append((name, pts))
    marks = []
    for m in a.mark or []:
        ts, _, label = m.partition("=")
        marks.append((oc.parse_time_arg(ts, tp), label or ts))
    if a.alarms:
        for ev in oc.iter_events(a.alarms, start=start, end=end):
            if (ev.get("state") or "ACT").upper() in ("ACT", "ACTIVE", "EVT", "ALARM", "IN", ""):
                marks.append((ev["t"], (ev["message"] or ev["source"] or "")[:48]))
    merged = []
    for t, label in sorted(marks):
        if merged and abs(merged[-1][0] - t) < 0.001:
            merged[-1] = (merged[-1][0], merged[-1][1] + "; " + label)
        else:
            merged.append((t, label))
    marks = merged
    t0 = start if start is not None else min(p[0][0] for _, p in analog + discrete)
    t1 = end if end is not None else max(p[-1][0] for _, p in analog + discrete)
    return analog, discrete, sorted(marks), t0, t1, tp


def stagger(vis, t0, t1):
    """Vertical level per marker so badges that are close in time do not overlap."""
    lv, last = [], None
    for t, _ in vis:
        close = last is not None and (t - last[0]) < 0.025 * ((t1 - t0) or 1.0)
        lv.append((last[1] + 1) % 3 if close else 0)
        last = (t, lv[-1])
    return lv


def hhmmss(t):
    return oc.fmt_time(t)[11:19]


def render_mpl(a, analog, discrete, marks, t0, t1, tp):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import datetime as dt
    epoch = dt.datetime(1970, 1, 1)
    conv = lambda t: epoch + dt.timedelta(seconds=t)
    groups = analog[:a.max_analog_panels - 1] + ([("+".join(n for n, _ in analog[a.max_analog_panels - 1:]), None)]
                                                 if len(analog) >= a.max_analog_panels else [])
    if len(analog) >= a.max_analog_panels:
        rest = analog[a.max_analog_panels - 1:]
    panels = len(groups) + (1 if discrete else 0)
    heights = [2.2] * len(groups) + ([0.55 * max(len(discrete), 2) + 0.6] if discrete else [])
    n_marks = len([m for m in marks if t0 <= m[0] <= t1])
    fig, axes = plt.subplots(panels, 1, figsize=(11, sum(heights) + 1.0 + 0.16 * n_marks), sharex=True,
                             gridspec_kw={"height_ratios": heights}, facecolor=BG)
    if panels == 1:
        axes = [axes]
    ci = 0
    for i, (name, pts) in enumerate(groups):
        ax = axes[i]
        series = [(name, pts)] if pts is not None else rest
        for n, p in series:
            ax.plot([conv(t) for t, _ in p], [v for _, v in p], color=PALETTE[ci % len(PALETTE)], lw=1.3, label=n)
            ci += 1
        ax.set_ylabel(name if pts is not None else "value", color=TEXT, fontsize=9)
        ax.legend(loc="upper left", fontsize=8, facecolor=PANEL, edgecolor=GRID, labelcolor=TEXT)
    if discrete:
        ax = axes[-1]
        ticks = []
        for j, (n, p) in enumerate(discrete):
            off = j * 1.5
            xs, ys = [], []
            vals = sorted(set(v for _, v in p))
            scale = (lambda v: (vals.index(v) / float(max(len(vals) - 1, 1)))) if len(vals) > 2 else (lambda v: 1.0 if v else 0.0)
            for t, v in p:
                xs.append(conv(t))
                ys.append(off + scale(v))
            xs.append(conv(t1))
            ys.append(ys[-1])
            ax.step(xs, ys, where="post", color=PALETTE[(ci + j) % len(PALETTE)], lw=1.4)
            ticks.append((off + 0.5, n))
        ax.set_yticks([t for t, _ in ticks])
        ax.set_yticklabels([n for _, n in ticks], fontsize=8, color=TEXT)
        ax.set_ylim(-0.3, len(discrete) * 1.5)
    for ax in axes:
        ax.set_facecolor(PANEL)
        ax.grid(True, color=GRID, lw=0.6)
        ax.tick_params(colors=MUTED, labelsize=8)
        for sp in ax.spines.values():
            sp.set_color(GRID)
        for t, label in marks:
            ax.axvline(conv(t), color=AMBER, lw=1.0, ls="--", alpha=0.9)
    vis = [(t, l) for t, l in marks if t0 <= t <= t1]
    levels = stagger(vis, t0, t1)
    for i, (t, label) in enumerate(vis, 1):
        axes[0].annotate(str(i), xy=(conv(t), 1.0), xycoords=("data", "axes fraction"), xytext=(0, 3 + 13 * levels[i - 1]),
                         textcoords="offset points", ha="center", va="bottom", fontsize=8, color=BG,
                         bbox=dict(boxstyle="round,pad=0.2", fc=AMBER, ec=AMBER))
    if vis:
        legend = "\n".join("%d  %s  %s" % (i, hhmmss(t), l[:110]) for i, (t, l) in enumerate(vis, 1))
        fig.text(0.01, 0.005, legend, color=AMBER, fontsize=7.5, family="monospace", va="bottom")
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    axes[-1].set_xlim(conv(t0), conv(t1))
    axes[-1].set_xlabel("time (%s) - %s" % (oc.fmt_time(t0)[:10], oc.zone_note(tp)), color=MUTED, fontsize=8)
    fig.suptitle(a.title or "Trend", color=TEXT, fontsize=12, x=0.01, ha="left")
    fig.tight_layout(rect=(0, min(0.4, 0.16 * n_marks / (sum(heights) + 1.0 + 0.16 * n_marks) + 0.01), 1, 1))
    fig.savefig(a.out, dpi=130, facecolor=BG)
    return a.out


def render_svg(a, analog, discrete, marks, t0, t1, tp):
    W, L, R, PH, GAP, TOP = 1100, 150, 20, 170, 18, 80
    plot_w = W - L - R
    groups = [[x] for x in analog[:a.max_analog_panels - 1]]
    if len(analog) >= a.max_analog_panels:
        groups.append(analog[a.max_analog_panels - 1:])
    dh = (24 * max(len(discrete), 1) + 20) if discrete else 0
    vis = [(t, l) for t, l in marks if t0 <= t <= t1]
    H = TOP + len(groups) * (PH + GAP) + (dh + GAP if discrete else 0) + 46 + 15 * len(vis)
    X = lambda t: L + (t - t0) / ((t1 - t0) or 1.0) * plot_w
    o = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" font-family="ui-monospace,Menlo,Consolas,monospace" font-size="11">' % (W, H, W, H),
         '<rect width="100%%" height="100%%" fill="%s"/>' % BG,
         '<text x="12" y="24" fill="%s" font-size="15">%s</text>' % (TEXT, escape(a.title or "Trend")),
         '<text x="12" y="40" fill="%s" font-size="10">%s .. %s - %s</text>' % (MUTED, oc.fmt_time(t0), oc.fmt_time(t1), escape(oc.zone_note(tp)))]
    y = TOP
    ci = 0
    panel_boxes = []
    for g in groups:
        vals = [v for _, p in g for _, v in p]
        lo, hi = min(vals), max(vals)
        pad = (hi - lo) * 0.08 or 1.0
        lo, hi = lo - pad, hi + pad
        Y = lambda v, y=y, lo=lo, hi=hi: y + PH - (v - lo) / (hi - lo) * PH
        o.append('<rect x="%d" y="%d" width="%d" height="%d" fill="%s" stroke="%s"/>' % (L, y, plot_w, PH, PANEL, GRID))
        for k in range(5):
            v = lo + (hi - lo) * k / 4.0
            yy = Y(v)
            o.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s"/>' % (L, L + plot_w, yy, yy, GRID))
            o.append('<text x="%d" y="%.1f" fill="%s" text-anchor="end">%s</text>' % (L - 6, yy + 4, MUTED, "%.4g" % v))
        lx = L + 8
        for name, p in g:
            col = PALETTE[ci % len(PALETTE)]
            ci += 1
            pts = " ".join("%.1f,%.1f" % (X(t), Y(v)) for t, v in p)
            o.append('<polyline fill="none" stroke="%s" stroke-width="1.4" points="%s"/>' % (col, pts))
            o.append('<rect x="%d" y="%d" width="10" height="3" fill="%s"/><text x="%d" y="%d" fill="%s">%s</text>' % (
                lx, y + 10, col, lx + 14, y + 15, TEXT, escape(name)))
            lx += 24 + 7 * len(name)
        o.append('<text x="12" y="%d" fill="%s">%s</text>' % (y + PH / 2, TEXT, escape(g[0][0] if len(g) == 1 else "value")))
        panel_boxes.append((y, PH))
        y += PH + GAP
    if discrete:
        o.append('<rect x="%d" y="%d" width="%d" height="%d" fill="%s" stroke="%s"/>' % (L, y, plot_w, dh, PANEL, GRID))
        for j, (n, p) in enumerate(discrete):
            base = y + 14 + j * 24 + 14
            vals = sorted(set(v for _, v in p))
            sc = (lambda v: vals.index(v) / float(max(len(vals) - 1, 1))) if len(vals) > 2 else (lambda v: 1.0 if v else 0.0)
            d = []
            prev = None
            for t, v in p:
                yy = base - 12 * sc(v)
                if prev is not None:
                    d.append("L%.1f,%.1f" % (X(t), prev))
                d.append(("M" if prev is None else "L") + "%.1f,%.1f" % (X(t), yy))
                prev = yy
            d.append("L%.1f,%.1f" % (X(t1), prev))
            o.append('<path d="%s" fill="none" stroke="%s" stroke-width="1.5"/>' % (" ".join(d), PALETTE[(ci + j) % len(PALETTE)]))
            o.append('<text x="%d" y="%d" fill="%s" text-anchor="end">%s</text>' % (L - 6, base - 2, TEXT, escape(n)))
        panel_boxes.append((y, dh))
        y += dh + GAP
    for k in range(7):
        t = t0 + (t1 - t0) * k / 6.0
        o.append('<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="%s" stroke-dasharray="2,4"/>' % (X(t), X(t), TOP, y - GAP, GRID))
        o.append('<text x="%.1f" y="%d" fill="%s" text-anchor="middle">%s</text>' % (X(t), y + 4, MUTED, hhmmss(t)))
    levels = stagger(vis, t0, t1)
    for i, (t, label) in enumerate(vis, 1):
        dy = 15 * levels[i - 1]
        o.append('<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="%s" stroke-dasharray="5,3"/>' % (X(t), X(t), TOP, y - GAP, AMBER))
        o.append('<rect x="%.1f" y="%d" width="14" height="13" rx="3" fill="%s"/><text x="%.1f" y="%d" fill="%s" font-size="9" text-anchor="middle">%d</text>' % (
            X(t) - 7, TOP - 14 - dy, AMBER, X(t), TOP - 4 - dy, BG, i))
    o.append('<text x="%d" y="%d" fill="%s" font-size="10">time</text>' % (L + plot_w / 2, y + 22, MUTED))
    for i, (t, label) in enumerate(vis, 1):
        o.append('<text x="12" y="%d" fill="%s" font-size="10">%d  %s  %s</text>' % (y + 40 + 15 * (i - 1), AMBER, i, hhmmss(t), escape(label[:120])))
    o.append("</svg>")
    out = a.out if a.out.lower().endswith(".svg") else os.path.splitext(a.out)[0] + ".svg"
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(o))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file")
    ap.add_argument("--tags")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--around")
    ap.add_argument("--before")
    ap.add_argument("--after")
    ap.add_argument("--mark", action="append", help='"TIME=label" event marker (repeatable)')
    ap.add_argument("--alarms", help="alarm/event log CSV; active events become markers")
    ap.add_argument("--title")
    ap.add_argument("--out", required=True)
    ap.add_argument("--svg", action="store_true", help="force the built-in SVG renderer")
    ap.add_argument("--max-analog-panels", type=int, default=4)
    a = ap.parse_args(argv)
    analog, discrete, marks, t0, t1, tp = gather(a)
    renderer = "svg"
    path = None
    if not a.svg and not a.out.lower().endswith(".svg"):
        try:
            path = render_mpl(a, analog, discrete, marks, t0, t1, tp)
            renderer = "matplotlib"
        except ImportError:
            path = None
    if path is None:
        path = render_svg(a, analog, discrete, marks, t0, t1, tp)
    sys.stdout.write(json.dumps({"chart": path, "renderer": renderer, "analog": [n for n, _ in analog],
                                 "discrete": [n for n, _ in discrete], "markers": [{"n": i, "t": oc.fmt_time(t), "label": l} for i, (t, l) in enumerate([m for m in marks if t0 <= m[0] <= t1], 1)],
                                 "start": oc.fmt_time(t0), "end": oc.fmt_time(t1)}) + "\n")


if __name__ == "__main__":
    main()
