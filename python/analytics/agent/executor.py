"""Run a plan. Return values, never raise.

Two things happen here that happen nowhere else:

1. Every exception a tool can raise becomes a ToolResult.error. Plan 6 left one
   real gap -- store.period_clause raises ValueError on an unparseable period --
   and deferred it because a human typed the period. A language model types it
   now, so the gap is closed here, once, for all eight tools rather than eight
   times inside them.

2. Nulls are stripped from the arguments. The plan schema is a closed object that
   requires every key, so the model emits `"by": null` for arguments it does not
   care about; passing those through would shadow each tool's own default.
"""
import logging
from dataclasses import dataclass, field, replace

from analytics.agent.registry import TOOLS, validate_step
from analytics.result import ToolResult

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Execution:
    plan: object
    results: list = field(default_factory=list)
    trace: list = field(default_factory=list)


def execute(cfg, plan):
    """Run every step. A failing step is recorded and the plan continues."""
    results, trace = [], []
    for index, step in enumerate(plan.steps, start=1):
        args = {k: v for k, v in step.args.items() if v is not None}
        reason = validate_step(replace(step, args=args))
        if reason is not None:
            log.warning("step %d rejected: %s", index, reason)
            result = ToolResult.error(step.tool, reason, period=args.get("period"), filters=[f"args={args}"])
        else:
            log.info("step %d: %s(%s)", index, step.tool,
                     ", ".join(f"{k}={v!r}" for k, v in sorted(args.items())))
            try:
                result = TOOLS[step.tool].fn(cfg, **args)
                if not isinstance(result, ToolResult):
                    raise TypeError(
                        f"{step.tool} returned {type(result).__name__}, not ToolResult"
                    )
            except Exception as exc:                      # noqa: BLE001 -- deliberate
                # A tool that raises is a tool that cannot report its own gap. The
                # agent must still be able to read the failure and route around it.
                log.warning("step %d raised %s: %s", index, type(exc).__name__, exc)
                result = ToolResult.error(
                    step.tool, f"{type(exc).__name__}: {exc}",
                    period=args.get("period"), filters=[f"args={args}"],
                )
        results.append(result)
        trace.append({
            "step": index,
            "tool": step.tool,
            "args": args,
            "rationale": step.rationale,
            "status": result.status,
            "rows_scanned": result.provenance.rows_scanned,
            "message": result.message,
        })
    return Execution(plan=plan, results=results, trace=trace)
