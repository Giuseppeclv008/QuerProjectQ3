# Capping KPI report for 2026-02.

*Generated 2026-07-24T12:00:00Z — narrative source: template, plan source: router.*

## Goal

Capping KPI report for 2026-02.

## Data used

- Store: `tiny.duckdb`, machine `MCC`
- Torque band: 1.5–2.5 Nm; robust band k = 3.0; idle threshold 300s
- Rows scanned across all steps: 26

## Analyses executed

1. `overview(period='2026-02')` — Establish the scope: counts, heads, time range, data-quality flags. → **ok**
2. `success_rates(by='overall', period='2026-02')` — The headline KPI the brief asks for first. → **ok**
3. `success_rates(by='head', period='2026-02')` — Per-head breakdown, to name the weakest head. → **ok**
4. `capping_speed(bucket='day', period='2026-02')` — Production rate in pieces/hour, day by day. → **ok**
5. `idle_periods(period='2026-02')` — Sustained No-Load runs, to separate downtime from failure. → **insufficient_data**

## Findings

- **Scope.** 6 capping operations across heads 1-3, from 2026-02-01 00:00:00 to 2026-02-01 00:00:40. 2 no-load cycles are excluded from every rate below.
- **Success rate.** 66.6667% (4 successful, 2 rejected). Lowest head: 2.
- **Weakest head.** 2 at 33.3333% over 3 capping operations.
- **Throughput.** 0 pieces/hour, averaged over 1 active buckets.

### Success Rate Per Head

![success rate per head](success_rate_per_head.png)

### Capping Speed

![capping speed](capping_speed.png)

## Confidence and limits

- **No model was used to plan this report.** The tool calls below are a fixed plan; the numbers would be identical either way.
- `overview`: 8 rows scanned; filters: period=2026-02
- `success_rates`: 6 rows scanned; filters: app_torque > 0 (capping operations only)
- `success_rates`: 6 rows scanned; filters: app_torque > 0 (capping operations only)
- `capping_speed`: 6 rows scanned; filters: bucket=day, app_torque > 0 (only real caps produce pieces)
- `idle_periods`: **insufficient_data** — no idle periods of >= 300s in period '2026-02'
- **Assumption.** a capping operation is a closure with torque > 0; no-load cycles (status 2, torque 0) are excluded from success denominators.
- **Assumption.** mean_pieces_per_hour is the mean over active buckets only; buckets with zero capping operations are not emitted, so idle hours/days do not lower it.

## Next checks

- Inspect head 2 mechanically before the next changeover.

## Tool-call trace

Every call this report is built from, in order. The full record is in
`trace.json` alongside this file.

| # | tool | arguments | status | rows scanned |
|---|---|---|---|---|
| 1 | `overview` | `period='2026-02'` | ok | 8 |
| 2 | `success_rates` | `by='overall', period='2026-02'` | ok | 6 |
| 3 | `success_rates` | `by='head', period='2026-02'` | ok | 6 |
| 4 | `capping_speed` | `bucket='day', period='2026-02'` | ok | 6 |
| 5 | `idle_periods` | `period='2026-02'` | insufficient_data | 0 |
