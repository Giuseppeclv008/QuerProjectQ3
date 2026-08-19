# Identify an unusual head

*Generated 2026-08-19T17:23:33Z — narrative source: template, plan source: llm, model: ollama:qwen2.5:7b.*

## Goal

Identify an unusual head

## Data used

- Store: `events_3mo.duckdb`, machine `MCC`
- Store fingerprint: 55,132,433 rows, 36 heads, 2026-01-31 16:00:06 → 2026-04-30 16:59:59
- Torque band: 1.5–2.5 Nm; robust band k = 3.0; idle threshold 300s
- Rows scanned across all steps: 31,668,727

## Analyses executed

1. `head_correlation(by='day')` — Compare heads to find outliers based on correlation. → **ok**

## Findings

- **Head agreement.** All 36 heads move together (mean correlation 0.9999-1.0000), so none is out of step in shape. This says nothing about level: Pearson is invariant to a per-head offset, and a head running steadily below the others scores the same.



## Confidence and limits

- **Narration.** the model's findings carried no bullet; it announced findings rather than stating them.
- `head_correlation`: 31,668,727 rows scanned; filters: heads=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36], by=day
- **Assumption.** 3 buckets is a floor, not power: treat correlations over few buckets as suggestive only.
- **Assumption.** Pearson correlation is invariant to a per-head affine shift, so this ranking cannot see a level offset: a head running consistently below the others while moving with them scores ~1 and is reported as tracking. Compare per-head median torque (torque_stats by head) to rule that out.
- **Assumption.** a head with no defined correlation to any peer (constant torque, zero variance, or fewer than 3 shared buckets) is omitted from outliers and shown as None in the matrix.
- **Assumption.** heads correlate on their bucketed mean torque series; the head with the lowest mean correlation to its peers is the one out of step *in shape*.

## Next checks

- Compare per-head median torque (torque_stats by head) — the correlation ranking cannot see a head that tracks the pack at a lower level.

## Tool-call trace

Every call this report is built from, in order. The full record is in
`trace.json` alongside this file.

| # | tool | arguments | status | rows scanned |
|---|---|---|---|---|
| 1 | `head_correlation` | `by='day'` | ok | 31,668,727 |
