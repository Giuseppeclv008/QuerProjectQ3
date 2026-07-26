# Capping KPI report for 2026-02.

*Generated 2026-07-26T09:52:33Z — narrative source: template, plan source: router.*

## Goal

Capping KPI report for 2026-02.

## Data used

- Store: `events_3mo.duckdb`, machine `MCC`
- Torque band: 1.5–2.5 Nm; robust band k = 3.0; idle threshold 300s
- Rows scanned across all steps: 40,919,049

## Analyses executed

1. `overview(period='2026-02')` — Establish the scope: counts, heads, time range, data-quality flags. → **ok**
2. `success_rates(by='overall', period='2026-02')` — The headline KPI the brief asks for first. → **ok**
3. `success_rates(by='head', period='2026-02')` — Per-head breakdown, to name the weakest head. → **ok**
4. `capping_speed(bucket='day', period='2026-02')` — Production rate in pieces/hour, day by day. → **ok**
5. `idle_periods(period='2026-02')` — Sustained No-Load runs, to separate downtime from failure. → **ok**

## Findings

- **Scope.** 6,672,649 capping operations across heads 1-36, from 2026-02-01 08:43:30 to 2026-02-28 15:59:59. 3,774,599 no-load cycles are excluded from every rate below.
- **Data quality.** 68 closures carry torque outside the configured band; 0 carry no torque reading at all.
- **Counter resets.** 36 reset markers in scope.
- **Success rate.** 99.9943% (6,669,339 successful, 383 rejected). Lowest head: 29. A further 2,927 closures carry no pass/fail verdict and are outside the rate.
- **Weakest head.** 29 at 99.9660% over 185,349 capping operations.
- **Throughput.** 11,121.0817 pieces/hour, averaged over 25 active buckets.
- **Idle time.** 12276 sustained no-load periods, 7,486.0 head-hours in total.

### Success Rate Per Head

![success rate per head](success_rate_per_head.png)

### Capping Speed

![capping speed](capping_speed.png)

## Confidence and limits

- **No model was used to plan this report.** The tool calls below are a fixed plan; the numbers would be identical either way.
- `overview`: 10,450,551 rows scanned; filters: period=2026-02
- `success_rates`: 6,672,649 rows scanned; filters: app_torque > 0 (capping operations only)
- `success_rates`: 6,672,649 rows scanned; filters: app_torque > 0 (capping operations only)
- `capping_speed`: 6,672,649 rows scanned; filters: bucket=day, app_torque > 0 (only real caps produce pieces)
- `idle_periods`: 10,450,551 rows scanned; filters: min_seconds=300
- **Assumption.** a capping operation is a closure with torque > 0; no-load cycles (status 2, torque 0) are excluded from success denominators.
- **Assumption.** an idle period is a sustained run of no-load cycles (status 2.0, torque 0).
- **Assumption.** mean_pieces_per_hour is the mean over active buckets only; buckets with zero capping operations are not emitted, so idle hours/days do not lower it.

## Next checks

- Confirm the configured torque band matches the product currently running on the line.
- Inspect head 29 mechanically before the next changeover.

## Tool-call trace

Every call this report is built from, in order. The full record is in
`trace.json` alongside this file.

| # | tool | arguments | status | rows scanned |
|---|---|---|---|---|
| 1 | `overview` | `period='2026-02'` | ok | 10,450,551 |
| 2 | `success_rates` | `by='overall', period='2026-02'` | ok | 6,672,649 |
| 3 | `success_rates` | `by='head', period='2026-02'` | ok | 6,672,649 |
| 4 | `capping_speed` | `bucket='day', period='2026-02'` | ok | 6,672,649 |
| 5 | `idle_periods` | `period='2026-02'` | ok | 10,450,551 |
