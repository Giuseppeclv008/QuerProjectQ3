#!/usr/bin/env bash
# Build the cleaned event store from the raw month zips.
# Usage: scripts/build_store.sh <out.duckdb> [month.zip ...]
set -euo pipefail

OUT="${1:?usage: build_store.sh <out.duckdb> [month.zip ...]}"
shift
ZIPS=("$@")
[ ${#ZIPS[@]} -eq 0 ] && ZIPS=(telemetry_*.zip)

# The month zips are packaged inconsistently: some contain a top-level
# telemetry_.../ folder, others drop the day-files flat into the cwd. Extract a
# month only when its day-files are not already present (either way), then collect
# them whether they landed flat (base-YYYY-MM-DD.csv) or inside a base/ subdir.
for z in "${ZIPS[@]}"; do
    base="$(basename "${z%.zip}")"
    if ! compgen -G "${base}-*.csv" >/dev/null 2>&1 && [ ! -d "${z%.zip}" ]; then
        unzip -q "$z"
    fi
done

CSVS=()
for z in "${ZIPS[@]}"; do
    base="$(basename "${z%.zip}")"
    while IFS= read -r f; do CSVS+=("$f"); done < <(
        find . -maxdepth 2 \( -name "${base}-*.csv" -o -path "*/${base}/*.csv" \) | sort -u
    )
done

echo "cleaning ${#CSVS[@]} day-files into $OUT"
rm -f "$OUT"
./build/mas_monolith "$OUT" MCC 4 "${CSVS[@]}"
