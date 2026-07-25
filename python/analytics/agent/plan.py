"""What the agent decides, before anything is executed.

A Plan is data, not behaviour: it can come from the keyword router or from the
model, it can be printed, logged, diffed in a test, and replayed. That is what
makes `arol report kpi` reproducible and `arol ask` auditable.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlanStep:
    tool: str
    args: dict = field(default_factory=dict)
    rationale: str = ""


@dataclass(frozen=True)
class Plan:
    goal: str
    steps: list = field(default_factory=list)
    source: str = "router"   # "router" | "llm"
    note: str = ""           # why the router was used, if it was
