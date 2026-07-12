"""The tool contract. Plan 7's agent consumes exactly this and nothing else.

Two rules make hallucinated statistics structurally impossible downstream:
  1. Every number a tool returns is accompanied by the provenance that justifies
     it -- the period, the rows scanned, the filters applied.
  2. Degenerate data is a *value*, not an exception: a tool returns status
     "insufficient_data" and the report names the gap in confidence/limits,
     rather than the pipeline dying or silently emitting a fabricated 0%.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Provenance:
    period: str | None = None
    rows_scanned: int = 0
    filters: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)


@dataclass(frozen=True)
class ToolResult:
    tool: str
    status: str                 # "ok" | "insufficient_data" | "error"
    values: object              # dict, or list of dicts
    provenance: Provenance
    message: str = ""

    @staticmethod
    def ok(tool, values, *, period=None, rows_scanned=0, filters=None, assumptions=None):
        return ToolResult(
            tool=tool, status="ok", values=values,
            provenance=Provenance(period, rows_scanned, filters or [], assumptions or []),
        )

    @staticmethod
    def insufficient(tool, message, *, period=None, rows_scanned=0):
        return ToolResult(
            tool=tool, status="insufficient_data", values={},
            provenance=Provenance(period, rows_scanned), message=message,
        )

    @staticmethod
    def error(tool, message, *, period=None):
        return ToolResult(
            tool=tool, status="error", values={},
            provenance=Provenance(period), message=message,
        )
