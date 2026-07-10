import pathlib
import subprocess
import sys

import pandas as pd

import bench_plots


def synth_csv(tmp_path: pathlib.Path) -> pathlib.Path:
    rows = ["arch,n_workers,threads,files,repeat,clean_s,merge_s,total_s,"
            "events,rows_per_s,events_per_s,peak_rss_mb,cpu_pct"]
    # mono-1T medians: total 10.0 (of 9,10,11); mas N=2 median 6.0
    for rep, t in [(1, 9.0), (2, 10.0), (3, 11.0)]:
        rows.append(f"mono-1T,0,1,1,{rep},{t},0.0,{t},765711,1.0,1.0,100.0,99.0")
    for rep, t in [(1, 5.0), (2, 6.0), (3, 7.0)]:
        rows.append(f"mas,2,1,1,{rep},{t},1.0,{t},765711,1.0,1.0,200.0,150.0")
    p = tmp_path / "results.csv"
    p.write_text("\n".join(rows) + "\n")
    return p


def test_medians_and_speedup(tmp_path):
    df = bench_plots.load(synth_csv(tmp_path))
    med = bench_plots.medians(df)
    mono = med[(med.arch == "mono-1T") & (med.files == 1)].iloc[0]
    mas2 = med[(med.arch == "mas") & (med.n_workers == 2) & (med.files == 1)].iloc[0]
    assert mono.total_s == 10.0
    assert mas2.total_s == 6.0
    s = bench_plots.speedup(med, files=1)
    row = s[(s.arch == "mas") & (s.n_workers == 2)].iloc[0]
    assert abs(row.speedup - 10.0 / 6.0) < 1e-9
    assert abs(row.efficiency - (10.0 / 6.0) / 2) < 1e-9


def test_render_writes_all_outputs(tmp_path):
    out = tmp_path / "out"
    bench_plots.render(synth_csv(tmp_path), out)
    for name in ["throughput_vs_n.png", "speedup_efficiency.png",
                 "wall_vs_volume.png", "mono_threads_speedup.png",
                 "results.md"]:
        assert (out / name).exists(), name
    md = (out / "results.md").read_text()
    assert "median" in md.lower()


def test_cli(tmp_path):
    csv = synth_csv(tmp_path)
    out = tmp_path / "cli_out"
    r = subprocess.run([sys.executable,
                        str(pathlib.Path(__file__).parent / "bench_plots.py"),
                        str(csv), str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (out / "results.md").exists()
