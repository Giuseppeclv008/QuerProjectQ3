"""The brief's 43 example queries, asked of the real agent and checked against SQL.

The unit tests pin each tool and each sentence of the report. They cannot say
whether a model, handed a question, plans the right analyses and answers with the
right figures, and that is what went wrong in every early run: "no failed
closures were recorded" (there were 748), "the torque did not change in March"
(the median went 1.999 -> 1.748 -> 2.198 Nm), 130 closures "above 2.5 Nm" (3).
This file asks all 43 queries of slides 13-18 of the brief through `arol ask` and
checks the answers.

The expected figures are not written down. They are computed here, from the
store, with SQL that shares no code with `analytics`, and formatted the way the
report prints them. A change to a prompt, a tool or the model that breaks an
answer fails the query it breaks.

It needs a model, so it is opt-in:

    cd python
    AROL_LIVE_QUERIES=1 ../.venv/bin/python -m pytest -q tests/test_brief_queries_live.py

(about eight minutes on qwen3:14b; `-k q19` asks one query). It is skipped, with
the reason, when that variable is unset, when events_3mo.duckdb is absent, or when
Ollama does not hold the configured model. The helpers that read a report run in
every build.

Three queries are marked `xfail`: the answers they get are known to be weak
(see docs/validation-log.md). A strict failure there would be noise; an XPASS is
the signal that one has been fixed and the mark can go.
"""
import json
import math
import os
import re
import string
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pytest

from analytics import cli
from analytics.config import Config

ROOT = Path(__file__).resolve().parents[2]
STORE = Path(os.environ.get("AROL_LIVE_STORE", ROOT / "events_3mo.duckdb"))
FEB, MAR = "2026-02", "2026-03"

# Every figure an expectation may name. `_truth` must return exactly these.
TRUTH_KEYS = (
    "caps", "ok", "failed", "noverdict", "rate",
    "worst_head", "worst_rate", "worst_failed", "second_head", "second_failed",
    "head1_failed", "head2_failed", "head1_rate", "head2_rate",
    "head3_failed", "head4_failed",
    "t_mean", "t_min", "t_max", "t_sd", "f_mean", "f_sd",
    "band_out", "above", "failed_low",
    "sd_all_head", "sd_all", "sd_ok_head", "sd_ok",
    "weakest_day", "weakest_day_rate", "most_rejects_day", "most_rejects_n",
    "mar_weakest_day",
    "hour_lo_rate", "hour_lo_hour", "hour_hi_rate", "hour_hi_hour", "hour_p",
    "corr", "mar_min_mean",
    "ts_min", "ts_max", "oob_all", "rate_all",
    "all_worst_head", "all_worst_failed", "all_second_head", "all_second_failed",
)


@dataclass(frozen=True)
class Q:
    """One query and what its answer has to contain.

    Every string is a format string over TRUTH_KEYS and is matched without regard
    to case. `has`: all must be in the Answer block. `any_of`: at least one group
    must be there whole. `regex`: all must match the Answer block. `not_`: none
    may be there. `report`: all must be somewhere in the report. `tools`: the
    plan must hold all of them.
    """
    id: int
    question: str
    period: str | None
    tools: tuple = ()
    has: tuple = ()
    any_of: tuple = ()
    regex: tuple = ()
    not_: tuple = ()
    report: tuple = ()
    xfail: str = ""


QUERIES = [
    # Exploration
    Q(1, "How many capping operations were performed in the selected month?", FEB,
      has=("{caps}",)),
    Q(2, "How many closure events were performed by each head?", FEB,
      tools=("success_rates",), report=("### Success rate per head (table)",),
      not_=("do not specify",)),
    Q(3, "Show me the time range covered by the dataset.", None,
      has=("{ts_min}", "{ts_max}")),
    Q(4, "Are there any missing or invalid torque values?", None,
      has=("{oob_all}",)),
    # Quality
    Q(5, "What percentage of capping operations were successful?", FEB,
      has=("{rate}", "{ok}")),
    Q(6, "How many closures ended with a positive outcome?", FEB,
      has=("{ok}",), not_=("{caps} closures ended",)),
    Q(7, "How many failed capping operations were recorded?", FEB,
      has=("{failed}",), not_=("no failed capping operations were recorded",)),
    Q(8, "What is the success rate per capping head?", FEB,
      has=("{worst_rate}",), report=("### Success rate per head (table)",)),
    Q(9, "Which head shows the lowest success rate?", FEB,
      has=("{worst_head}", "{worst_rate}")),
    # Torque
    Q(10, "What is the average closing torque for successful capping operations?", FEB,
      has=("{t_mean}",), report=("{t_min}", "{t_max}", "{t_sd}")),
    Q(11, "Show the torque distribution for all successful closures.", FEB,
      any_of=(("{t_mean}",), ("torque_histogram",)), report=("torque_histogram.png",)),
    Q(12, "Are there torque values outside the expected operating range of 1.5 to 2.5 Nm?", FEB,
      has=("{band_out}",)),
    Q(13, "Compare the average torque of successful vs failed closures.", FEB,
      has=("{t_mean}", "{f_mean}")),
    Q(14, "Which head shows the highest torque variability?", FEB,
      any_of=(("{sd_all_head}", "{sd_all}"), ("{sd_ok_head}", "{sd_ok}"))),
    # Time and trend
    Q(15, "How did the capping success rate evolve over time?", FEB,
      has=("{weakest_day}", "{weakest_day_rate}")),
    Q(16, "Show a daily breakdown of successful vs failed closures.", FEB,
      report=("### Success rate per day (table)", "failed_closures_per_day.png")),
    Q(17, "Are there specific time intervals with abnormal failure rates?", FEB,
      any_of=(("{weakest_day}",), ("{hour_hi_hour}",))),
    Q(18, "Did the average torque change over the observed month?", MAR,
      tools=("compare_periods",), report=("{mar_min_mean}",),
      not_=("did not show a significant change", "no significant change")),
    Q(19, "Is there a correlation between time of day and failure probability?", FEB,
      tools=("failure_correlation",), has=("{hour_lo_rate}", "{hour_hi_rate}"),
      report=("chi-square p = {hour_p}",)),
    # Filtering
    Q(20, "Show only capping operations with a positive outcome.", FEB,
      tools=("closure_filter",), has=("{ok}",)),
    Q(21, "List all failed capping events with torque below 1.5 Nm.", FEB,
      tools=("closure_filter",), has=("{failed_low}",)),
    Q(22, "How many closures had torque above 2.5 Nm?", FEB,
      tools=("closure_filter",), regex=(r"\b{above}\b.{{0,40}}(?:closures|capping)",),
      not_=("{band_out} closures had torque above",)),
    Q(23, "Show all capping events for head 3 with failed outcome.", FEB,
      tools=("closure_filter",), regex=(r"\b{head3_failed}\b",)),
    Q(24, "Count successful closures after removing all duplicated entries.", FEB,
      has=("{ok}",)),
    # Diagnostic and comparative
    Q(25, "Which capping head behaves differently from the others?", FEB,
      any_of=(("no head stands out",), ("none of the heads",), ("no head behaves",)),
      not_=("head {sd_ok_head} behaves differently", "head {sd_all_head} behaves differently"),
      xfail="names head 9 although the finding beside it says no head stands out"),
    Q(26, "Is there a head with an unusual number of failed closures?", FEB,
      has=("{worst_head}", "{worst_failed}")),
    Q(27, "Compare performance between head 1 and head 2.", FEB,
      has=("{head1_rate}", "{head2_rate}")),
    Q(28, "Which head contributes most to overall failures?", FEB,
      regex=(r"(?s){worst_head}.*{worst_failed}.*{second_head}.*{second_failed}",)),
    Q(29, "Does higher torque correlate with higher success rate?", FEB,
      tools=("failure_correlation",), has=("{corr}", "negligible")),
    # Explanation
    Q(30, "Why is the overall success rate lower on certain days?", MAR,
      has=("{mar_weakest_day}",),
      any_of=(("cannot",), ("does not provide",), ("does not record",), ("not record",))),
    Q(31, "Explain why head 4 has more failed closures.", FEB,
      has=("cannot explain",), regex=(r"\b{head4_failed} rejected",)),
    Q(32, "Summarize the main issues observed in the capping process.", None,
      not_=("high reject rates", "widespread"),
      xfail="calls the rejects of one head at 0.013% 'high', and the March step 'widespread'"),
    Q(33, "Which signals should be monitored more closely?", None,
      has=("{all_worst_head}", "{all_second_head}", "{all_worst_failed}", "{all_second_failed}")),
    Q(34, "Generate a short report on capping quality for this dataset.", None,
      has=("{rate_all}",)),
    # Visualization
    Q(35, "Plot the closing torque over time for successful closures.", FEB,
      has=("torque_rolling_mean.png",)),
    Q(36, "Show a histogram of closing torque values.", FEB,
      tools=("torque_stats",), has=("torque_histogram.png",)),
    Q(37, "Create a chart showing success rate per head.", FEB,
      has=("success_rate_per_head.png",)),
    Q(38, "Visualize failed closures over time.", FEB,
      has=("failed_closures_per_day.png",)),
    Q(39, "Generate a dashboard summary of capping performance.", FEB,
      has=("{rate}", "{failed}")),
    # Meta
    Q(40, "What preprocessing steps were applied to the raw data?", None,
      tools=("methodology",), any_of=(("skipp",), ("refused",))),
    Q(41, "How were duplicated closures detected and removed?", None,
      tools=("methodology",), any_of=(("unique key",), ("once",))),
    Q(42, "Which assumptions were made during data cleaning?", None,
      tools=("methodology",), any_of=(("torque > 0",), ("plc reset",))),
    Q(43, "What features are used to classify a successful closure?", None,
      tools=("methodology",), has=("status is 0", "bit 0")),
]


# ---------------------------------------------------------------------------
# Reading a report. These run in every build.
# ---------------------------------------------------------------------------

def answer_block(report):
    """The model's answer: the *Answer* bullet and the bullets nested under it."""
    findings = report.split("## Findings", 1)[1].split("## Confidence and limits", 1)[0]
    lines, inside = [], False
    for line in findings.splitlines():
        if line.startswith("- **Answer.**"):
            inside = True
            lines.append(line)
        elif inside and line.startswith("  "):
            lines.append(line)
        elif inside and line.strip():
            break
    return "\n".join(lines)


def plan_tools(report):
    section = report.split("## Analyses executed", 1)[1].split("## Findings", 1)[0]
    return re.findall(r"^\d+\. `(\w+)\(", section, re.M)


def sources(report):
    m = re.search(r"narrative source: (\w+), plan source: (\w+)", report)
    return m.groups() if m else (None, None)


def _fmt(template, truth):
    return template.format(**truth)


def problems(q, truth, report):
    """What is wrong with this report as an answer to `q`; empty if nothing."""
    out = []
    narrative, plan = sources(report)
    if plan != "llm":
        out.append(f"the plan came from the {plan}, not from the model")
    if narrative != "llm":
        why = re.search(r"- \*\*Narration\.\*\* (.*)", report)
        out.append("the model's answer was rejected and the template stands"
                   + (f": {why.group(1)}" if why else ""))
    missing = [t for t in q.tools if t not in plan_tools(report)]
    if missing:
        out.append(f"the plan lacks {missing}: {plan_tools(report)}")
    answer = answer_block(report)
    low = answer.lower()
    if not answer:
        out.append("the report has no Answer bullet")
    for s in q.has:
        if _fmt(s, truth).lower() not in low:
            out.append(f"the answer lacks {_fmt(s, truth)!r}")
    if q.any_of and not any(all(_fmt(s, truth).lower() in low for s in group)
                            for group in q.any_of):
        out.append(f"the answer holds none of {[[_fmt(s, truth) for s in g] for g in q.any_of]}")
    for pattern in q.regex:
        if not re.search(_fmt(pattern, truth), answer, re.I):
            out.append(f"the answer does not match {_fmt(pattern, truth)!r}")
    for s in q.not_:
        if _fmt(s, truth).lower() in low:
            out.append(f"the answer says {_fmt(s, truth)!r}")
    for s in q.report:
        if _fmt(s, truth).lower() not in report.lower():
            out.append(f"the report lacks {_fmt(s, truth)!r}")
    return out


_DUMMY = {k: "0" for k in TRUTH_KEYS}


def _placeholders(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_the_queries_are_the_briefs_43_and_numbered_in_order():
    assert [q.id for q in QUERIES] == list(range(1, 44))
    assert len({q.question for q in QUERIES}) == 43


def test_every_figure_an_expectation_names_is_one_the_truth_provides():
    for q in QUERIES:
        for text in (*q.has, *q.not_, *q.report, *q.regex,
                     *(s for g in q.any_of for s in g)):
            assert _placeholders(text) <= set(TRUTH_KEYS), (q.id, text)
            _fmt(text, _DUMMY)                      # and it formats


def test_the_answer_is_the_bullet_and_what_is_nested_under_it():
    report = ("## Findings\n\n- **Answer.** First.\n  - Second.\n  - Third.\n"
              "- **Scope.** Not part of the answer.\n\n## Confidence and limits\n")
    assert answer_block(report) == "- **Answer.** First.\n  - Second.\n  - Third."


def test_a_report_without_an_answer_bullet_gives_an_empty_block():
    assert answer_block("## Findings\n\n- **Scope.** x\n\n## Confidence and limits\n") == ""


def test_the_plan_is_read_from_the_analyses_section():
    report = ("## Analyses executed\n\n1. `trend(by='day')` — why → **ok**\n"
              "2. `compare_periods(by='week')` — why → **ok**\n\n## Findings\n")
    assert plan_tools(report) == ["trend", "compare_periods"]


def _report(answer, plan="success_rates", narrative="llm", source="llm", extra=""):
    return (f"*Generated now — narrative source: {narrative}, plan source: {source}, model: m.*\n\n"
            f"## Analyses executed\n\n1. `{plan}(by='head')` — why → **ok**\n\n"
            f"## Findings\n\n- **Answer.** {answer}\n\n## Confidence and limits\n\n{extra}")


def test_a_right_answer_has_no_problem():
    q = Q(0, "q", None, tools=("success_rates",), has=("{ok}",), not_=("no failed",))
    assert problems(q, {"ok": "14,817,976"}, _report("14,817,976 closures succeeded.")) == []


def test_a_missing_figure_and_a_forbidden_phrase_are_both_reported():
    q = Q(0, "q", None, has=("{ok}",), not_=("no failed",))
    found = problems(q, {"ok": "14,817,976"}, _report("No failed closures, 14,824,304 in all."))
    assert any("lacks '14,817,976'" in p for p in found)
    assert any("says 'no failed'" in p for p in found)


def test_matching_ignores_case_and_any_of_needs_one_whole_group():
    q = Q(0, "q", None, any_of=(("alpha", "beta"), ("gamma",)))
    assert problems(q, {}, _report("GAMMA only.")) == []
    assert any("none of" in p for p in problems(q, {}, _report("alpha only.")))


def test_a_regex_expectation_is_matched_against_the_answer():
    q = Q(0, "q", None, regex=(r"\b{n}\b.{{0,20}}closures",))
    assert problems(q, {"n": "3"}, _report("3 closures were above.")) == []
    assert problems(q, {"n": "3"}, _report("130 closures were above."))


def test_a_template_fallback_and_a_router_plan_are_failures_with_the_reason():
    q = Q(0, "q", None)
    rejected = _report("x", narrative="template",
                       extra="- **Narration.** the answer states 99.98, which is not in the findings.\n")
    found = problems(q, {}, rejected)
    assert any("rejected" in p and "99.98" in p for p in found)
    assert any("router" in p for p in problems(q, {}, _report("x", source="router")))


def test_a_plan_that_lacks_a_required_tool_is_reported():
    q = Q(0, "q", None, tools=("compare_periods",))
    assert any("lacks ['compare_periods']" in p for p in problems(q, {}, _report("x")))


def test_a_report_must_name_a_figure_somewhere_if_asked():
    q = Q(0, "q", None, report=("torque_histogram.png",))
    assert problems(q, {}, _report("x"))
    assert problems(q, {}, _report("x", extra="![h](torque_histogram.png)\n")) == []


# ---------------------------------------------------------------------------
# The live run.
# ---------------------------------------------------------------------------

def _chi_square_p(chi2, df):
    """Wilson-Hilferty: independent of analytics.tools.factors, good to ~1e-3 here."""
    z = ((chi2 / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
    return 0.5 * math.erfc(z / math.sqrt(2))


def _truth(store):
    """The figures the answers must carry, from SQL that shares no code with analytics."""
    con = duckdb.connect(str(store), read_only=True)
    rej = "CAST(status AS BIGINT) % 2 <> 0"
    in_feb = "machine_id='MCC' AND ts >= '2026-02-01' AND ts < '2026-03-01'"
    in_mar = "machine_id='MCC' AND ts >= '2026-03-01' AND ts < '2026-04-01'"
    everything = "machine_id='MCC'"

    def one(sql):
        return con.execute(sql).fetchone()

    def n(x):
        return f"{int(x):,}"

    def pct(x):
        return f"{x * 100:.4f}%"

    caps, ok, failed = one(f"""SELECT count(*) FILTER (app_torque>0),
        count(*) FILTER (status=0 AND app_torque>0), count(*) FILTER ({rej} AND app_torque>0)
        FROM cap_events WHERE {in_feb}""")
    heads = {h: (o, f) for h, o, f in con.execute(f"""SELECT head_id,
        count(*) FILTER (status=0 AND app_torque>0), count(*) FILTER ({rej} AND app_torque>0)
        FROM cap_events WHERE {in_feb} GROUP BY 1""").fetchall()}
    rate_of = lambda h: heads[h][0] / (heads[h][0] + heads[h][1])
    worst = min(heads, key=lambda h: (rate_of(h), h))
    by_failed = sorted(heads, key=lambda h: (-heads[h][1], h))

    t_mean, t_min, t_max, t_sd = one(f"""SELECT avg(app_torque), min(app_torque), max(app_torque),
        stddev_samp(app_torque) FROM cap_events WHERE {in_feb} AND status=0 AND app_torque>0""")
    f_mean, f_sd = one(f"""SELECT avg(app_torque), stddev_samp(app_torque) FROM cap_events
        WHERE {in_feb} AND {rej} AND app_torque>0""")
    band_out = one(f"""SELECT count(*) FROM cap_events WHERE {in_feb} AND app_torque>0
        AND (app_torque<1.5 OR app_torque>2.5)""")[0]
    above = one(f"SELECT count(*) FROM cap_events WHERE {in_feb} AND app_torque>2.5")[0]
    failed_low = one(f"""SELECT count(*) FROM cap_events WHERE {in_feb} AND {rej}
        AND app_torque>0 AND app_torque<1.5""")[0]
    sd_all = one(f"""SELECT head_id, stddev_samp(app_torque) s FROM cap_events WHERE {in_feb}
        AND app_torque>0 GROUP BY 1 ORDER BY s DESC LIMIT 1""")
    sd_ok = one(f"""SELECT head_id, stddev_samp(app_torque) s FROM cap_events WHERE {in_feb}
        AND status=0 AND app_torque>0 GROUP BY 1 ORDER BY s DESC LIMIT 1""")

    def days(where):
        rows = con.execute(f"""SELECT cast(ts as date), count(*) FILTER (status=0 AND app_torque>0),
            count(*) FILTER ({rej} AND app_torque>0) FROM cap_events WHERE {where}
            GROUP BY 1 ORDER BY 1""").fetchall()
        return [(str(d), o, f) for d, o, f in rows if o + f]
    feb_days = days(in_feb)
    weakest = min(feb_days, key=lambda r: (r[1] / (r[1] + r[2]), r[0]))
    busiest = max(feb_days, key=lambda r: (r[2], -int(r[0][-2:])))
    mar_weakest = min(days(in_mar), key=lambda r: (r[1] / (r[1] + r[2]), r[0]))

    hours = con.execute(f"""SELECT hour(ts), count(*), count(*) FILTER ({rej})
        FROM cap_events WHERE {in_feb} AND app_torque>0 AND (status=0 OR {rej})
        GROUP BY 1 ORDER BY 1""").fetchall()
    total_n, total_k = sum(r[1] for r in hours), sum(r[2] for r in hours)
    p0 = total_k / total_n
    chi2 = sum((k - m * p0) ** 2 / (m * p0 * (1 - p0)) for _, m, k in hours)
    per_100k = {h: k / m * 1e5 for h, m, k in hours}
    lo_h, hi_h = min(per_100k, key=lambda h: (per_100k[h], h)), max(per_100k, key=lambda h: (per_100k[h], -h))
    corr = one(f"""SELECT corr(app_torque, CASE WHEN status=0 THEN 1.0 ELSE 0.0 END) FROM cap_events
        WHERE {in_feb} AND app_torque>0 AND (status=0 OR {rej})""")[0]
    mar_min_mean = one(f"""SELECT min(m) FROM (SELECT avg(app_torque) m FROM cap_events
        WHERE {in_mar} AND app_torque>0 GROUP BY date_trunc('week', ts))""")[0]

    ts_min, ts_max = one(f"SELECT min(ts), max(ts) FROM cap_events WHERE {everything}")
    oob_all = one(f"""SELECT count(*) FROM cap_events WHERE {everything} AND app_torque>0
        AND (app_torque<1.5 OR app_torque>2.5)""")[0]
    ok_all, failed_all = one(f"""SELECT count(*) FILTER (status=0 AND app_torque>0),
        count(*) FILTER ({rej} AND app_torque>0) FROM cap_events WHERE {everything}""")
    all_heads = con.execute(f"""SELECT head_id, count(*) FILTER ({rej} AND app_torque>0) f
        FROM cap_events WHERE {everything} GROUP BY 1 ORDER BY f DESC, head_id""").fetchall()
    con.close()

    return {
        "caps": n(caps), "ok": n(ok), "failed": n(failed), "noverdict": n(caps - ok - failed),
        "rate": pct(ok / (ok + failed)),
        "worst_head": str(worst), "worst_rate": pct(rate_of(worst)), "worst_failed": n(heads[worst][1]),
        "second_head": str(by_failed[1]), "second_failed": n(heads[by_failed[1]][1]),
        "head1_failed": n(heads[1][1]), "head2_failed": n(heads[2][1]),
        "head1_rate": pct(rate_of(1)), "head2_rate": pct(rate_of(2)),
        "head3_failed": n(heads[3][1]), "head4_failed": n(heads[4][1]),
        "t_mean": f"{t_mean:.4f}", "t_min": f"{t_min:.3f}", "t_max": f"{t_max:.3f}", "t_sd": f"{t_sd:.4f}",
        "f_mean": f"{f_mean:.4f}", "f_sd": f"{f_sd:.4f}",
        "band_out": n(band_out), "above": n(above), "failed_low": n(failed_low),
        "sd_all_head": str(sd_all[0]), "sd_all": f"{sd_all[1]:.4f}",
        "sd_ok_head": str(sd_ok[0]), "sd_ok": f"{sd_ok[1]:.4f}",
        "weakest_day": weakest[0], "weakest_day_rate": pct(weakest[1] / (weakest[1] + weakest[2])),
        "most_rejects_day": busiest[0], "most_rejects_n": n(busiest[2]),
        "mar_weakest_day": mar_weakest[0],
        "hour_lo_rate": f"{per_100k[lo_h]:.2f}", "hour_lo_hour": f"{lo_h:02d}:00",
        "hour_hi_rate": f"{per_100k[hi_h]:.2f}", "hour_hi_hour": f"{hi_h:02d}:00",
        "hour_p": f"{_chi_square_p(chi2, len(hours) - 1):.2f}",
        "corr": f"{corr:.3f}", "mar_min_mean": f"{mar_min_mean:.4f}",
        "ts_min": str(ts_min), "ts_max": str(ts_max), "oob_all": n(oob_all),
        "rate_all": pct(ok_all / (ok_all + failed_all)),
        "all_worst_head": str(all_heads[0][0]), "all_worst_failed": n(all_heads[0][1]),
        "all_second_head": str(all_heads[1][0]), "all_second_failed": n(all_heads[1][1]),
    }


@pytest.fixture(scope="module")
def live():
    """The truth, once, if the run can happen at all; otherwise the reason it cannot."""
    if os.environ.get("AROL_LIVE_QUERIES") != "1":
        pytest.skip("set AROL_LIVE_QUERIES=1 to ask the real agent the 43 queries "
                    "(a model on Ollama, about eight minutes)")
    if not STORE.exists():
        pytest.skip(f"{STORE} absent; build it with scripts/build_store.sh")
    cfg = Config(store_path=str(STORE))
    try:
        with urllib.request.urlopen(f"{cfg.ollama_host}/api/tags", timeout=5) as r:
            held = {m["name"] for m in json.load(r).get("models", [])}
    except Exception as exc:                          # noqa: BLE001
        pytest.skip(f"Ollama is not reachable at {cfg.ollama_host}: {exc}")
    if cfg.model not in held:
        pytest.skip(f"Ollama does not hold {cfg.model}; ollama pull {cfg.model}")
    truth = _truth(STORE)
    assert set(truth) == set(TRUTH_KEYS), set(truth) ^ set(TRUTH_KEYS)
    return truth


def _marks(q):
    return [pytest.mark.xfail(reason=q.xfail, strict=False)] if q.xfail else []


@pytest.mark.parametrize("q", [pytest.param(q, id=f"q{q.id:02d}", marks=_marks(q))
                               for q in QUERIES])
def test_the_agent_answers_the_brief_query(q, live, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"store_path": str(STORE), "machine_id": "MCC"}))
    argv = ["ask", q.question, "--config", str(config), "--out", str(tmp_path / "out")]
    if q.period:
        argv += ["--period", q.period]
    assert cli.main(argv) == 0
    (run,) = (tmp_path / "out" / "ask").iterdir()
    report = (run / "report.md").read_text(encoding="utf-8")
    found = problems(q, live, report)
    assert not found, (f"\n{q.question}\n" + "\n".join(f"  - {p}" for p in found)
                       + f"\n\nAnswer:\n{answer_block(report)}")
