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


def effective_args(step):
    """The args that will actually reach the tool.

    Structured outputs require every property of the flat arg schema, so a model
    plan spells out every argument -- including the ones its tool does not take --
    and sets the unused ones to null. A null means "use the tool's default", which
    in Python means not passing the argument at all. Dropping the nulls here is
    what makes a model plan and a router plan indistinguishable downstream, so
    validation, execution and figure selection must all read a step through this
    function or they will disagree about what the step says.
    """
    return {k: v for k, v in step.args.items() if v is not None}
