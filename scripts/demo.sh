#!/usr/bin/env bash
# One end-to-end run: load the cleaned store, generate three report types.
#
# Usage: scripts/demo.sh [store.duckdb] [out_dir]
# Defaults to the three-month store built by scripts/build_store.sh.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
store="${1:-$here/events_3mo.duckdb}"
out="${2:-$here/docs/reports}"

if [[ ! -f "$store" ]]; then
  echo "error: store not found at $store" >&2
  echo "build it first: scripts/build_store.sh $store telemetry_*.zip" >&2
  exit 1
fi

cfg="$(mktemp -t arol-demo-XXXXXX.json)"
trap 'rm -f "$cfg"' EXIT
cat > "$cfg" <<JSON
{
  "store_path": "$store",
  "machine_id": "MCC",
  "torque_min": 1.5,
  "torque_max": 2.5,
  "mad_k": 3.0,
  "idle_min_seconds": 300,
  "idle_max_gap_seconds": 600
}
JSON

run() {  # run <slug> <args...>
  local slug="$1"; shift
  echo "=== $slug ==="
  "$here/scripts/arol" "$@" --config "$cfg" --out "$out/.staging"
}

run kpi        report kpi       --period 2026-02
run drift      report drift     --period 2026-02..2026-04
run anomalies  report anomalies --period 2026-02

mkdir -p "$out"
rm -rf "$out/kpi-2026-02" "$out/drift-2026-02_2026-04" "$out/anomalies-2026-02"
mv "$out/.staging/kpi"       "$out/kpi-2026-02"
mv "$out/.staging/drift"     "$out/drift-2026-02_2026-04"
mv "$out/.staging/anomalies" "$out/anomalies-2026-02"
rmdir "$out/.staging"

echo
echo "Reports written under $out:"
ls -1 "$out"
