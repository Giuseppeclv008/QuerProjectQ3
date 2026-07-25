"""Deterministic plans: the demo path, and the fallback when the model is not there.

The three `report` verbs are the brief's own examples (report anomalies, report
drift, report kpi) and they run these plans with no model in the loop. That is
what makes the demo byte-reproducible: same store, same period, same report.

`route()` exists for one reason -- when the model cannot be reached, `arol ask`
still has to answer with real numbers rather than an error page. It picks the
nearest canned plan and the report's limits section says the model was not used.
"""
from analytics.agent.plan import Plan, PlanStep

REPORT_TYPES = ("kpi", "drift", "anomalies")

_TITLES = {
    "kpi": "Capping KPI report",
    "drift": "Torque drift report",
    "anomalies": "Anomaly report",
}

# Keyword -> report type. Deliberately small and readable: this is a fallback,
# not a natural-language understanding layer.
_KEYWORDS = {
    "drift": ("drift", "drifting", "trend", "over time", "evolve", "evolved",
              "change over", "changed over", "moving average", "walking",
              "correlate", "correlation", "differently", "compare head"),
    "anomalies": ("anomaly", "anomalies", "anomalous", "abnormal", "outlier",
                  "outside", "threshold", "fault", "faults", "reject", "rejected",
                  "deviation", "unusual", "spike"),
    "kpi": ("kpi", "success", "successful", "rate", "how many", "percentage",
            "throughput", "speed", "pieces", "idle", "count", "overview",
            "summary", "performed"),
}


def _steps(report_type, period):
    if report_type == "kpi":
        return [
            PlanStep("overview", {"period": period},
                     "Establish the scope: counts, heads, time range, data-quality flags."),
            PlanStep("success_rates", {"period": period, "by": "overall"},
                     "The headline KPI the brief asks for first."),
            PlanStep("success_rates", {"period": period, "by": "head"},
                     "Per-head breakdown, to name the weakest head."),
            PlanStep("capping_speed", {"period": period, "bucket": "day"},
                     "Production rate in pieces/hour, day by day."),
            PlanStep("idle_periods", {"period": period},
                     "Sustained No-Load runs, to separate downtime from failure."),
        ]
    if report_type == "drift":
        return [
            PlanStep("trend", {"period": period, "signal": "torque",
                               "by": "day", "window": 7},
                     "Rolling mean/sigma of torque per head, with Mann-Kendall drift."),
            PlanStep("trend", {"period": period, "signal": "success_rate",
                               "by": "day", "window": 7},
                     "Whether quality moved with torque, or independently of it."),
            PlanStep("torque_stats", {"period": period, "outcome": "successful",
                                      "by": "head"},
                     "Variability ranking, to separate a drifting head from a noisy one."),
            PlanStep("head_correlation", {"period": period, "by": "day"},
                     "Which head is out of step with the rest of the machine."),
        ]
    if report_type == "anomalies":
        return [
            PlanStep("anomalies", {"period": period, "method": "both"},
                     "Threshold hits, robust per-head deviation, and rejected closures."),
            PlanStep("overview", {"period": period},
                     "Denominator for every count above, plus data-quality flags."),
            PlanStep("success_rates", {"period": period, "by": "day"},
                     "Daily rates, to place any abnormal interval in context."),
        ]
    raise ValueError(f"unknown report type {report_type!r}; known: {list(REPORT_TYPES)}")


def canned_plan(report_type, period):
    """The fixed plan behind one of the brief's three `report` verbs."""
    steps = _steps(report_type, period)
    scope = period or "the whole store"
    return Plan(
        goal=f"{_TITLES[report_type]} for {scope}.",
        steps=steps,
        source="router",
    )


def route(question, period):
    """Nearest canned plan for a free-text question, with no model in the loop."""
    lowered = question.lower()
    scores = {
        rtype: sum(1 for kw in words if kw in lowered)
        for rtype, words in _KEYWORDS.items()
    }
    best = max(REPORT_TYPES, key=lambda r: (scores[r], -REPORT_TYPES.index(r)))
    if scores[best] == 0:
        best, note = "kpi", ("no keyword matched the question, so the default KPI "
                             "plan was used")
    else:
        note = (f"the keyword router selected the {best} plan "
                f"({scores[best]} keyword match(es))")
    plan = canned_plan(best, period)
    return Plan(goal=f"Answer: {question}", steps=plan.steps,
                source="router", note=note)
