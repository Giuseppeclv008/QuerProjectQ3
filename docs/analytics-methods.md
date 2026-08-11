# Analytics methods

What each WP2 tool computes, exactly — the definition, the SQL shape, the
degenerate cases, and the assumptions it declares in its own provenance.

Every tool is a pure function `(Config, **kwargs) -> ToolResult`. None of them
raises: a gap in the data is a `status` on the result, not an exception. Nothing
below the CLI propagates an error, because a report generated unattended must
still land on disk saying what it could not answer.

All measured figures below come from `events_3mo.duckdb` — 55,132,433 rows,
machine `MCC`, 36 heads, 2026-02-01 → 2026-04-30.

---

## Status semantics — the slide-6 bitmask

The `status` column is **not an enumeration**. It is a bitmask: bit 0 is the
reject signal, and bits 1–6 are the conditions that caused it. The brief's slide
6 lists 14 rows, which is 7 conditions × {reject, no reject}.

| bit | value | condition |
|---|---|---|
| 0 | 1 | **reject signal** — the cap was rejected |
| 1 | 2 | No Load |
| 2 | 4 | No Closure |
| 3 | 8 | No InTorque |
| 4 | 16 | No CapTurns |
| 5 | 32 | Following Error |
| 6 | 64 | Bad Closure |

So `status = 65` is `64 + 1` — Bad Closure **with** the reject signal — and
`status = 9` is `8 + 1`, No InTorque with reject. A status is a rejection if and
only if it is **odd**:

```sql
CAST(status AS BIGINT) % 2 = 1
```

This is the single definition of failure across the toolkit
(`analytics/status.py:REJECT_SQL`) and in the C++ tier
(`CapEvent::is_reject`). It replaced an earlier `status == 65` rule, which
undercounted: on February alone, 748 closures carry the reject bit but only 732
have status 65 — the other 16 are status 9, No InTorque.

### Measured distribution, three months

| status | torque > 0 | count | share | decoded |
|---|---|---:|---:|---|
| 0 | yes | 31,655,161 | 57.4166% | clean capping operation |
| 2 | no | 23,447,151 | 42.5288% | No Load — the idle cycle |
| 0 | no | 16,552 | 0.0300% | clean, but no load applied |
| 2 | yes | 12,461 | 0.0226% | No Load **with** torque |
| 65 | yes | 1,071 | 0.0019% | Bad Closure + reject |
| 9 | yes | 24 | 0.0000% | No InTorque + reject |
| 4 | yes | 10 | 0.0000% | No Closure, not rejected |
| 4 | no | 2 | 0.0000% | No Closure, not rejected |
| 65 | no | 1 | 0.0000% | Bad Closure, no torque |

1,071 + 24 + 1 = **1,096 rejected closures** over three months, which is exactly
what `CAST(status AS BIGINT) % 2 <> 0` returns. The bitmask reading is confirmed by
the data, not merely consistent with it.

**No Load with torque (5,452 rows) is not a contradiction.** No Load means the
*first* torque threshold was not reached, so a sub-threshold torque reading is
expected. These closures are neither clean nor rejected: they carry no pass/fail
verdict, and the tools say so rather than silently assigning one.

---

## `overview` — scope and data quality

**Question:** what is actually in this period, and can I trust it?

Counts capping operations and no-load cycles, the head range, the time range,
torque readings outside the configured band, torque readings that are missing
entirely, and counter-reset markers.

```sql
SELECT COUNT(*) FILTER (WHERE app_torque > 0)                    AS capping_operations,
       COUNT(*) FILTER (WHERE status = 2 AND app_torque = 0)     AS no_load_cycles,
       COUNT(*) FILTER (WHERE app_torque > 0
                          AND (app_torque < ? OR app_torque > ?)) AS invalid_torque,
       ...
FROM cap_events WHERE <scope>
```

**Assumption declared:** a capping operation is a closure with torque > 0;
no-load cycles (status 2, torque 0) are excluded from success denominators.

---

## `success_rates` — the flagship KPI

**Question:** what fraction of capping operations succeeded, overall / per head /
per day?

```
success_rate = successful / (successful + failed)

successful := status = 0 AND app_torque > 0
failed     := reject bit set (status odd) AND app_torque > 0
```

**No-load cycles are in neither.** A head that only ever cycled with no load has
performed zero capping operations; reporting it at 0% would read as a
catastrophically failing head when nothing was ever capped. It is omitted.

**The denominator is not every capping operation.** A closure that is neither
`status = 0` nor a reject carries no pass/fail verdict — the 5,452 No-Load-with-
torque and 6 No-Closure rows above. They are excluded from the ratio, so
`successful + failed` can be less than the total. The report states the
difference explicitly; on February that is 5,580 closures out of 14,824,304, and
without saying so the printed counts visibly fail to add up.

**Degenerate case:** a group with no verdicts at all yields `success_rate =
None`, never `0.0` and never a `ZeroDivisionError`.

Measured, February: 14,817,976 successful, 748 rejected → **99.9950%**.

---

## `capping_speed` — throughput

**Question:** how many pieces per hour, and how does that move over time?

Groups closures with torque > 0 into hour or day buckets and divides by the
hours in the bucket that actually saw a closure.

**`mean_pieces_per_hour` is the mean over *active* buckets only.** `GROUP BY`
never emits a bucket with zero capping operations, so an idle hour or an idle
weekend does not drag the mean down. It is a typical *active-bucket* rate, not
total pieces over elapsed time — the returned assumptions say so.

The denominator is the hours that actually saw a closure, not the bucket's
calendar length. It used to be a flat 24 for day buckets, so the reported
"pieces/hour" was pieces-per-day over 24 — while `idle_periods` reported
thousands of idle head-hours for the same month. The two numbers described
different machines.

Measured, February: 27,984.7704 pieces/hour over 28 active day-buckets. AROL
define production speed as bottles closed per unit time
(`material/various.txt`), so `pieces_per_second` is reported alongside.

---

## `idle_periods` — downtime, separated from failure

**Question:** how much of the time was the machine not capping at all?

A gaps-and-islands query over per-head runs of no-load cycles. Consecutive
no-load rows for one head form an island; a run lasting at least
`idle_min_seconds` (default 300) is an idle period.

Ordering is tie-broken on `cap_seq` as well as `ts`. The tie-breaker is now
belt-and-braces rather than load-bearing: the window is `PARTITION BY head_id`,
and within one head a duplicate timestamp is unrepresentable — the store's
identity is `(machine_id, head_id, ts)`, and a head closes at most once per poll
because caps missed between polls arrive as one event with `delta > 1`. Measured
on the rebuilt store: **0 duplicate `(machine_id, head_id, ts)` across
55,132,433 rows**. An earlier version of this paragraph claimed the PLC "can
emit two rows with the same timestamp", which is true across heads (36 share
each poll) and false within one.

**Scope limit:** `cap_events` only holds rows where a counter advanced, so a
machine that is switched off produces no rows and no islands. This measures
no-load *cycling*, not downtime. AROL detect a stopped machine from the raw
pool — consecutive rows identical but for the timestamp.

**Assumption declared:** an idle period is a sustained run of no-load cycles
(status 2.0, torque 0).

---

## `anomalies` — two independent detectors

**Question:** which closures are out of the ordinary, and in what way?

Three counts, computed together:

- **Rejected closures** — the reject bit, as above.
- **Threshold hits** — torque outside the *configured* band `[torque_min,
  torque_max]`. This is an absolute, product-specific limit.
- **Deviation hits** — torque outside its own head's robust band, `median ± k ·
  MAD` (default `k = 3`).

**Why MAD and not σ.** Standard deviation is computed from the very points you
are trying to find. A handful of extreme outliers inflate σ enough that they fall
inside their own band and hide themselves. The median and the median absolute
deviation have a 50% breakdown point, so the band stays where the bulk of the
data is regardless of how extreme the outliers are.

The two detectors are independent on purpose: a head can drift entirely within
the configured band (deviation hits, no threshold hits), and a correctly centred
head can run a product whose band is wrong (threshold hits, no deviation hits).

Measured, February: 748 rejected, 130 outside the configured band, 1,612,634
beyond their head's robust band. The itemised lists are capped at
`max_anomaly_items` (default 5,000) per category; the reported counts stay
exact.

---

## `trend` — is anything walking?

**Question:** is a signal moving over time, and for which heads?

Builds a per-head daily series of the chosen signal (`torque` or
`success_rate`), then computes the **Mann-Kendall** tau over it.

Mann-Kendall is non-parametric: it counts concordant and discordant pairs, so it
assumes neither linearity nor Gaussian noise. It answers "is this monotonically
moving" rather than "what is the slope", which is the right question for a
maintenance trigger. A rolling mean (`window`, default 7 days) is returned
alongside for plotting.

**`|tau| ≥ 0.5` flags drift.** Measured over three months, no head exceeds the
threshold on either signal.

---

## `head_correlation` — which head is out of step?

**Question:** do the 36 heads move together?

Pearson correlation on the per-head bucketed mean-torque series, giving a 36×36
matrix, plus each head's mean correlation to its peers.

**A head with undefined correlation is omitted, not reported as zero.** A head
whose torque is constant over the period has zero variance, so its correlation is
undefined; reporting `0.0` would present it as the most anomalous head on the
machine when in truth the statistic does not apply. It appears as `None` in the
matrix and is left out of the ranking.

**The ranking is a ranking, not a diagnosis.** The tool returns every head sorted
by mean correlation ascending — it does not filter. The report therefore names an
"odd head out" only when the lowest head is actually distinguishable from the
closest-tracking one at the precision printed; otherwise it states that the heads
agree. On the real store all 36 heads correlate above 0.9999, and naming the
numerically-lowest one produced the self-defeating claim that *the odd head out
has a correlation of 1.000*.

---

## `torque_stats` — distribution per head

**Question:** how tight is each head, and about what centre?

Count, mean, median, standard deviation, min and max of applied torque per head
(or per day, or overall), optionally filtered to successful closures only.

Reported ordered by standard deviation descending, so the most variable head is
first. Measured over three months: head 9, σ = 0.0612 Nm about a median of
1.997 Nm.

---

## What every result carries

Every tool returns the same envelope, so the renderer never has to special-case
a failure:

| field | meaning |
|---|---|
| `tool` | which tool produced this |
| `status` | `ok` \| `insufficient_data` \| `error` |
| `values` | the numbers |
| `provenance.period` | the scope requested |
| `provenance.rows_scanned` | how much data stood behind it |
| `provenance.filters` | every predicate applied |
| `provenance.assumptions` | every judgement call made |
| `message` | why, when `status` is not `ok` |

`provenance` is what populates the report's *Confidence and limits* section. A
gap in the analysis is stated rather than omitted, and the row counts let a
reader tell "the rate is 100%" from "the rate is 100% of four closures".
