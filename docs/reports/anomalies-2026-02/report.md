# Anomaly report for 2026-02.

*Generated 2026-07-26T09:52:36Z — narrative source: template, plan source: router.*

## Goal

Anomaly report for 2026-02.

## Data used

- Store: `events_3mo.duckdb`, machine `MCC`
- Torque band: 1.5–2.5 Nm; robust band k = 3.0; idle threshold 300s
- Rows scanned across all steps: 27,573,751

## Analyses executed

1. `anomalies(method='both', period='2026-02')` — Threshold hits, robust per-head deviation, and rejected closures. → **ok**
2. `overview(period='2026-02')` — Denominator for every count above, plus data-quality flags. → **ok**
3. `success_rates(by='day', period='2026-02')` — Daily rates, to place any abnormal interval in context. → **ok**

## Findings

- **Anomalies.** 383 rejected closures, 68 outside the torque band, 678,325 beyond their head's robust band.
- **Scope.** 6,672,649 capping operations across heads 1-36, from 2026-02-01 08:43:30 to 2026-02-28 15:59:59. 3,774,599 no-load cycles are excluded from every rate below.
- **Data quality.** 68 closures carry torque outside the configured band; 0 carry no torque reading at all.
- **Counter resets.** 36 reset markers in scope.
- **Weakest day.** 2026-02-05 at 99.9851% over 94,248 capping operations.

### Anomalies Over Time

![anomalies over time](anomalies_over_time.png)

## Confidence and limits

- **No model was used to plan this report.** The tool calls below are a fixed plan; the numbers would be identical either way.
- `anomalies`: 10,450,551 rows scanned; filters: method=both, band=[1.5, 2.5], mad_k=3.0
- `overview`: 10,450,551 rows scanned; filters: period=2026-02
- `success_rates`: 6,672,649 rows scanned; filters: app_torque > 0 (capping operations only)
- **Assumption.** a capping operation is a closure with torque > 0; no-load cycles (status 2, torque 0) are excluded from success denominators.
- **Assumption.** deviation uses median +/- k*MAD (robust); mean/sigma would let extreme outliers inflate the band and hide themselves.

## Next checks

- Correlate the rejected closures against the cap supplier lot running at those timestamps.
- Confirm the configured torque band matches the product currently running on the line.
- Inspect day 2026-02-05 mechanically before the next changeover.

## Tool-call trace

Every call this report is built from, in order. The full record is in
`trace.json` alongside this file.

| # | tool | arguments | status | rows scanned |
|---|---|---|---|---|
| 1 | `anomalies` | `method='both', period='2026-02'` | ok | 10,450,551 |
| 2 | `overview` | `period='2026-02'` | ok | 10,450,551 |
| 3 | `success_rates` | `by='day', period='2026-02'` | ok | 6,672,649 |
