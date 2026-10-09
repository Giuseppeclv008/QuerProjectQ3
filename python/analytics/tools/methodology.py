"""How the data was prepared and how a closure is judged: the brief's meta queries.

"What preprocessing was applied?", "how were duplicated closures detected?",
"which assumptions were made in cleaning?", "what classifies a successful
closure?" are questions about the pipeline, and the pipeline is documented, but
not in anything a result carries. This tool returns that documentation as a
result, so a report can answer from it and say where it came from.

The texts restate README.txt (section 6, *Data formats*) and the rules the other
tools apply (`status.py`, the assumptions every result carries). They are the
documentation's words, not the store's: nothing here is measured, and a test
pins each claim against the code or the document it restates.
"""
from analytics.result import ToolResult
from analytics.status import CONDITIONS
from analytics.tools.overview import ASSUMPTION, DELTA_ASSUMPTION

TOPICS = ("preprocessing", "duplicates", "assumptions", "classification")

_PREPROCESSING = (
    "The raw telemetry is one CSV per day, one row per second, with a counter, "
    "an applied torque and a status for each head. A row with a field that is "
    "not a number or is out of range is skipped and counted, a file with a "
    "different header is refused, and a timestamp that goes backwards is kept "
    "and counted. The first row of a file only sets the counters. From there a "
    "head has closed when its counter changes between two consecutive rows, "
    "and one event is written for the change, carrying the torque and status "
    "of that row, the number of caps since the previous row (delta), and flags "
    "for several caps in one row and for a counter that went down (a PLC "
    "reset). The events are stored in the cap_events table."
)

_DUPLICATES = (
    "The raw rows are samples, not events: the same closure repeats on every "
    "row until its head's counter advances. A closure is therefore written once, "
    "when the counter changes between two consecutive rows, and a repeated "
    "poll produces no event. The store also keeps (machine_id, head_id, "
    "timestamp) as a unique key, so cleaning the same file twice adds no rows, "
    "and a Parquet store is read keeping one row per key."
)


def _assumptions():
    return [
        ASSUMPTION,
        DELTA_ASSUMPTION,
        "a counter that goes down is a PLC reset, flagged and counted as zero caps",
        "a stopped machine writes no rows, so a stop shows only as a gap between "
        "events and cannot be told from data that did not arrive",
        "the torque and status of an event are those of the row where the "
        "counter change was seen",
    ]


def _classification(cfg):
    bits = ", ".join(f"{name} ({bit})" for bit, name in CONDITIONS.items())
    return (
        f"A closure is a capping operation when its torque is above zero; a row "
        f"with torque 0 and status 2 is a no-load cycle and is not one. A capping "
        f"operation is successful when its status is {cfg.success_status:g}, and "
        f"rejected when bit 0 of its status is set (an odd number, for example "
        f"65). Any other status with torque carries no pass/fail verdict and "
        f"stays outside the success rate. The rate is successful / (successful "
        f"+ rejected). The status is a bitmask: bit 0 is the reject signal, and "
        f"the conditions are {bits}. The classification uses the status and "
        f"whether the torque is above zero; the torque band is a separate flag "
        f"and does not decide success."
    )


def methodology(cfg, topic=None):
    if topic is not None and topic not in TOPICS:
        return ToolResult.error(
            "methodology", f"topic must be one of {list(TOPICS)}, got {topic!r}")
    texts = {
        "preprocessing": _PREPROCESSING,
        "duplicates": _DUPLICATES,
        "assumptions": " ".join(f"({i}) {a}." for i, a in enumerate(_assumptions(), 1)),
        "classification": _classification(cfg),
    }
    wanted = TOPICS if topic is None else (topic,)
    return ToolResult.ok(
        "methodology",
        {"topics": [{"topic": t, "text": texts[t]} for t in wanted]},
        filters=[f"topic={topic}"] if topic else [],
        assumptions=["these are the pipeline's documented rules, restated; "
                     "nothing here is measured from the store"],
    )
