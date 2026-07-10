#!/usr/bin/env python3
"""Render spec-§9 benchmark plots + median table from bench/results.csv."""
import pathlib
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

GROUP = ["arch", "n_workers", "threads", "files"]


def load(csv_path) -> pd.DataFrame:
    return pd.read_csv(csv_path)


def medians(df: pd.DataFrame) -> pd.DataFrame:
    return (df.groupby(GROUP, as_index=False)
              .median(numeric_only=True)
              .drop(columns=["repeat"]))


def speedup(med: pd.DataFrame, files: int) -> pd.DataFrame:
    base = med[(med.arch == "mono-1T") & (med.files == files)]
    if base.empty:
        return pd.DataFrame(columns=list(med.columns) + ["speedup", "efficiency"])
    t1 = float(base.iloc[0].total_s)
    out = med[med.files == files].copy()
    out["speedup"] = t1 / out.total_s
    out["parallelism"] = out.apply(
        lambda r: r.n_workers if r.arch == "mas" else r.threads, axis=1)
    out["efficiency"] = out.speedup / out.parallelism.clip(lower=1)
    return out


def _dataframe_to_markdown(df: pd.DataFrame) -> str:
    """Convert DataFrame to markdown pipe table (no tabulate dependency)."""
    lines = []
    # Header row
    header = "| " + " | ".join(str(col) for col in df.columns) + " |"
    lines.append(header)
    # Separator row
    sep = "| " + " | ".join("---" for _ in df.columns) + " |"
    lines.append(sep)
    # Data rows
    for _, row in df.iterrows():
        values = []
        for val in row:
            if isinstance(val, float):
                values.append(f"{val:.6g}")
            else:
                values.append(str(val))
        data_row = "| " + " | ".join(values) + " |"
        lines.append(data_row)
    return "\n".join(lines)


def render(csv_path, out_dir) -> None:
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    med = medians(load(csv_path))
    vol_max = int(med.files.max())

    # 1) throughput vs N (largest volume present)
    fig, ax = plt.subplots()
    mas = med[(med.arch == "mas") & (med.files == vol_max)].sort_values("n_workers")
    if not mas.empty:
        ax.plot(mas.n_workers, mas.events_per_s, marker="o", label="MAS(N)")
    m1 = med[(med.arch == "mono-1T") & (med.files == vol_max)]
    if not m1.empty:
        ax.axhline(float(m1.iloc[0].events_per_s), ls="--", color="gray",
                   label="mono-1T")
    mt = med[(med.arch == "mono-MT") & (med.files == vol_max)].sort_values("threads")
    if not mt.empty:
        ax.plot(mt.threads, mt.events_per_s, marker="s", label="mono-MT(T)")
    ax.set_xlabel("N workers / T threads"); ax.set_ylabel("events/s")
    ax.set_title(f"Throughput ({vol_max} day-files, median of 3)"); ax.legend()
    fig.savefig(out / "throughput_vs_n.png", dpi=150, bbox_inches="tight")

    # 2) speedup + efficiency vs parallelism, ideal-linear reference
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10, 4))
    s = speedup(med, files=vol_max)
    for arch, marker in [("mas", "o"), ("mono-MT", "s")]:
        sub = s[s.arch == arch].sort_values("parallelism")
        if not sub.empty:
            a1.plot(sub.parallelism, sub.speedup, marker=marker, label=arch)
            a2.plot(sub.parallelism, sub.efficiency, marker=marker, label=arch)
    if not s.empty:
        px = sorted(s.parallelism.unique())
        a1.plot(px, px, ls=":", color="gray", label="ideal")
        a2.axhline(1.0, ls=":", color="gray")
    a1.set_xlabel("parallelism"); a1.set_ylabel("speedup S=T1/TN"); a1.legend()
    a2.set_xlabel("parallelism"); a2.set_ylabel("efficiency E=S/N"); a2.legend()
    fig.savefig(out / "speedup_efficiency.png", dpi=150, bbox_inches="tight")

    # 3) wall-clock vs volume per arch/parallelism
    fig, ax = plt.subplots()
    for (arch, n, t), sub in med.groupby(["arch", "n_workers", "threads"]):
        label = {"mas": f"MAS N={n}", "mono-MT": f"mono T={t}",
                 "mono-1T": "mono-1T"}[arch]
        sub = sub.sort_values("files")
        ax.plot(sub.files, sub.total_s, marker="o", label=label)
    ax.set_xlabel("day-files"); ax.set_ylabel("wall s (median)")
    ax.set_title("Wall-clock vs volume"); ax.legend(fontsize=7)
    fig.savefig(out / "wall_vs_volume.png", dpi=150, bbox_inches="tight")

    # 4) monolith threading speedup
    fig, ax = plt.subplots()
    mono = med[med.arch.isin(["mono-1T", "mono-MT"]) & (med.files == vol_max)]
    mono = mono.sort_values("threads")
    if not mono.empty:
        t1 = float(mono[mono.threads == 1].iloc[0].total_s)
        ax.plot(mono.threads, t1 / mono.total_s, marker="s", label="mono")
        ax.plot(mono.threads, mono.threads, ls=":", color="gray", label="ideal")
    ax.set_xlabel("T threads"); ax.set_ylabel("speedup")
    ax.set_title(f"Monolith threading ({vol_max} day-files)"); ax.legend()
    fig.savefig(out / "mono_threads_speedup.png", dpi=150, bbox_inches="tight")

    cols = GROUP + ["clean_s", "merge_s", "total_s", "events_per_s",
                    "peak_rss_mb", "cpu_pct"]
    md = ["# Benchmark results (median of 3 repeats)", "",
          _dataframe_to_markdown(med[cols]), "",
          "Caveats: laptop thermals (no fan control), median-of-3, "
          "N=16 on 8 cores is a deliberate oversubscription point, "
          "merge phase reported separately; mono-MT uses a std::thread "
          "atomic-counter pool (dynamic load balancing, slightly fairer "
          "than PUSH/PULL round-robin)."]
    (out / "results.md").write_text("\n".join(md) + "\n")


def main() -> int:
    csv = sys.argv[1] if len(sys.argv) > 1 else "bench/results.csv"
    out = sys.argv[2] if len(sys.argv) > 2 else "docs/bench"
    render(csv, out)
    print(f"wrote plots + results.md to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
