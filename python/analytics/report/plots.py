"""Matplotlib figures, one per question the reader will actually ask.

Every function takes a ToolResult and returns the PNG's filename, or None when
there is nothing to draw. Returning None rather than drawing an empty axes is
deliberate: an empty chart in a report reads as "zero", and zero is not the same
claim as "the tool could not answer".
"""
import logging
from collections import defaultdict

import matplotlib

matplotlib.use("Agg")           # no display on a build machine
import matplotlib.pyplot as plt  # noqa: E402

log = logging.getLogger(__name__)

_FIGSIZE = (9, 4.5)
_DPI = 120


def _usable(result):
    return result.status == "ok" and result.values


def _save(fig, out_dir, name):
    fig.tight_layout()
    fig.savefig(str(out_dir) + "/" + name, dpi=_DPI)
    plt.close(fig)
    log.debug("wrote %s", name)
    return name


def success_rate_per_head(result, out_dir):
    """Bar chart: success rate per head, with the weakest head highlighted."""
    if not _usable(result) or not isinstance(result.values, list):
        return None
    rows = [r for r in result.values if r.get("success_rate") is not None]
    if not rows:
        return None
    heads = [r["head_id"] for r in rows]
    rates = [r["success_rate"] * 100 for r in rows]
    worst = min(range(len(rates)), key=lambda i: rates[i])
    colors = ["#c0392b" if i == worst else "#2c7fb8" for i in range(len(rates))]

    fig, ax = plt.subplots(figsize=_FIGSIZE)
    ax.bar([str(h) for h in heads], rates, color=colors)
    ax.set_xlabel("head")
    ax.set_ylabel("success rate (%)")
    ax.set_title("Success rate per capping head")
    # A machine at 99.99% needs a zoomed axis or every bar looks identical.
    low = min(rates)
    ax.set_ylim(max(0.0, low - (100 - low) * 0.5 - 0.01), 100.0)
    ax.grid(axis="y", alpha=0.3)
    return _save(fig, out_dir, "success_rate_per_head.png")


def capping_speed_over_time(result, out_dir):
    """Line chart: pieces/hour per bucket, with the mean over active buckets."""
    if not _usable(result) or not result.values.get("buckets"):
        return None
    buckets = result.values["buckets"]
    xs = [b["bucket_start"] for b in buckets]
    ys = [b["pieces_per_hour"] for b in buckets]

    fig, ax = plt.subplots(figsize=_FIGSIZE)
    ax.plot(xs, ys, marker="o", markersize=3, linewidth=1.2, color="#2c7fb8")
    ax.axhline(result.values["mean_pieces_per_hour"], color="#c0392b",
               linestyle="--", linewidth=1,
               label=f"mean over active buckets: "
                     f"{result.values['mean_pieces_per_hour']:.0f}/h")
    ax.set_xlabel("bucket start")
    ax.set_ylabel("pieces / hour")
    ax.set_title("Capping speed over time")
    ax.legend(loc="best", fontsize="small")
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    return _save(fig, out_dir, "capping_speed.png")


def torque_rolling_mean(result, out_dir):
    """One line per head: rolling mean torque, so a walking head is visible."""
    if not _usable(result) or not result.values.get("series"):
        return None
    by_head = defaultdict(list)
    for point in result.values["series"]:
        if point["rolling_mean"] is not None:
            by_head[point["head_id"]].append((point["bucket"], point["rolling_mean"]))
    if not by_head:
        return None

    drifting = {d["head_id"] for d in result.values.get("drift", []) if d["drifting"]}
    fig, ax = plt.subplots(figsize=_FIGSIZE)
    for head, points in sorted(by_head.items()):
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        if head in drifting:
            ax.plot(xs, ys, linewidth=2.0, color="#c0392b", label=f"head {head} (drifting)")
        else:
            ax.plot(xs, ys, linewidth=0.7, alpha=0.35, color="#7f8c8d")
    ax.set_xlabel("bucket")
    ax.set_ylabel("rolling mean torque (Nm)")
    ax.set_title("Per-head rolling mean torque")
    if drifting:
        ax.legend(loc="best", fontsize="small")
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    return _save(fig, out_dir, "torque_rolling_mean.png")


def drift_ranking(result, out_dir):
    """Heads ranked by |Mann-Kendall tau|, with the drift threshold marked."""
    if not _usable(result) or not result.values.get("drift"):
        return None
    from analytics.tools.trend import DRIFT_TAU

    drift = result.values["drift"]
    heads = [str(d["head_id"]) for d in drift]
    taus = [d["tau"] for d in drift]
    colors = ["#c0392b" if d["drifting"] else "#2c7fb8" for d in drift]

    fig, ax = plt.subplots(figsize=_FIGSIZE)
    ax.bar(heads, taus, color=colors)
    ax.axhline(DRIFT_TAU, color="#c0392b", linestyle="--", linewidth=1)
    ax.axhline(-DRIFT_TAU, color="#c0392b", linestyle="--", linewidth=1,
               label=f"drift threshold |tau| = {DRIFT_TAU}")
    ax.set_xlabel("head")
    ax.set_ylabel("Mann-Kendall tau")
    ax.set_title("Drift magnitude per head (rising = positive)")
    ax.legend(loc="best", fontsize="small")
    ax.grid(axis="y", alpha=0.3)
    return _save(fig, out_dir, "drift_ranking.png")


def anomalies_over_time(result, out_dir):
    """Scatter of every flagged closure, coloured by why it was flagged."""
    if not _usable(result):
        return None
    groups = [
        ("rejected closures", result.values.get("faults", []), "#c0392b"),
        ("outside torque band", result.values.get("threshold_hits", []), "#e67e22"),
        ("robust deviation", result.values.get("deviation_hits", []), "#8e44ad"),
    ]
    if not any(items for _, items, _ in groups):
        return None

    fig, ax = plt.subplots(figsize=_FIGSIZE)
    for label, items, color in groups:
        if not items:
            continue
        ax.scatter([i["ts"] for i in items], [i["app_torque"] for i in items],
                   s=14, alpha=0.7, color=color, label=f"{label} ({len(items)})")
    ax.set_xlabel("timestamp")
    ax.set_ylabel("closing torque (Nm)")
    ax.set_title("Flagged closures over time")
    ax.legend(loc="best", fontsize="small")
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    return _save(fig, out_dir, "anomalies_over_time.png")
