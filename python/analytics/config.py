"""Configuration. No path, band, or threshold is hard-coded anywhere else.

WP5 requires configuration-driven datasets. This is also where the status
semantics live (spec §3.2): the status bitmask is confirmed by the brief's own
slide-6 table and by the measured (status, torque) distribution, so if AROL
confirms a different encoding it is a change here and nowhere else.
"""
import json
from dataclasses import dataclass, fields


class ConfigError(Exception):
    """Bad config. Raised at startup, never mid-analysis."""


@dataclass(frozen=True)
class Config:
    store_path: str = "events.duckdb"
    machine_id: str = "MCC"

    # Status semantics. A clean closure is status 0; a No-Load cycle is status 2
    # with zero torque. A *failure* is not a single code -- it is any status whose
    # reject bit is set (analytics.status.REJECT_SQL), per the brief's slide-6
    # bitmask. `fault_status` was removed in Plan 7: `status == 65` named only one
    # of the six documented reject codes.
    success_status: float = 0.0    # + torque > 0  -> a real cap
    no_load_status: float = 2.0    # + torque == 0 -> No-Load cycle

    # Expected torque operating band (Nm). Measured range: 1.885 - 2.317.
    torque_min: float = 1.5
    torque_max: float = 2.5

    # Robust deviation band: median +/- mad_k sigma-equivalents, where the
    # scale is 1.4826*MAD (the consistency constant: MAD ~ 0.6745*sigma under
    # normality, so raw MAD read as "3*MAD = 3 sigma" was really ~2.02 sigma
    # -- a band ~1 sigma tighter than every reader assumed). mad_floor is the
    # minimum scale in Nm: a quantised torque sensor can produce a tiny but
    # nonzero MAD that collapses the band to sensor noise and flags a double-
    # digit percentage of production.
    mad_k: float = 3.0
    mad_floor: float = 0.01

    # A head is idle after this many seconds of sustained No-Load.
    idle_min_seconds: int = 300
    # An island of no-load cycles is broken when consecutive rows are further
    # apart than this. cap_events only holds rows where a counter advanced, so a
    # switched-off machine emits nothing -- and without a bound its whole
    # off-window was absorbed into the surrounding run and reported as idling.
    # 600 s is far above the ~1 Hz cycling cadence and far below any real stop.
    idle_max_gap_seconds: int = 600

    # WP3: the model that plans and narrates. It never computes a number.
    #
    # `provider` is the only field that has to change to move between a hosted
    # model and one running on the machine. Everything downstream -- the tools,
    # the executor, the renderer -- is untouched by the choice, because the model
    # only ever decides which analyses to run and how to word them.
    provider: str = "anthropic"     # anthropic | ollama
    model: str = "claude-opus-5"
    max_tokens: int = 16000
    api_timeout_s: float = 120.0

    # anthropic only. Rejected by Ollama, so it is not sent there.
    effort: str = "high"            # low | medium | high | xhigh | max

    # ollama only.
    ollama_host: str = "http://localhost:11434"
    num_ctx: int = 8192             # Ollama defaults to 2048; the planner needs ~2.6k

    # How much the model is asked to do. A smaller local model can route
    # reliably long before it can compose a whole plan, so the hard part is
    # optional. Every tier produces the same Plan type and the same numbers.
    #   plan     -- compose the full sequence of tool calls, with arguments
    #   select   -- choose which tools run; arguments come from their defaults
    #   classify -- choose one of the three report types; use its canned plan
    planning: str = "plan"

    # Ceiling on how many items of a list-valued result reach the narrator. Sized
    # for a large context; drop it to ~20 for a local model.
    narrator_max_items: int = 120

    # Most individual hits any one anomaly category will itemise. The tool used
    # to build a Python dict per hit with no bound -- 678,325 deviation hits on
    # February, 1,734,460 across the three months -- and hand all of them to
    # ax.scatter, which costs hundreds of MB to draw a solid block. The reported
    # counts stay exact; only the itemised sample is capped.
    max_anomaly_items: int = 5000

    PROVIDERS = ("anthropic", "ollama")
    PLANNING = ("plan", "select", "classify")

    def __post_init__(self):
        if self.torque_min >= self.torque_max:
            raise ConfigError(
                f"torque_min ({self.torque_min}) must be < torque_max ({self.torque_max}); "
                "this band would exclude all data"
            )
        if self.idle_min_seconds <= 0:
            raise ConfigError(f"idle_min_seconds must be > 0, got {self.idle_min_seconds}")
        if self.idle_max_gap_seconds <= 0:
            raise ConfigError(
                f"idle_max_gap_seconds must be > 0, got {self.idle_max_gap_seconds}")
        # No relation is required between idle_max_gap_seconds and
        # idle_min_seconds, and an earlier version of this file wrongly demanded
        # max_gap >= min_seconds. They measure different things: max_gap is a
        # continuity bound on the *data* (holes longer than it end a run), while
        # min_seconds is a duration floor on the *result*. Every combination is
        # coherent, and the rejected direction was the conservative one --
        # max_gap = 60 with min_seconds = 300 breaks a run at any hole longer than 60 s
        # and is strictly harder to inflate than the defaults, yet it raised.
        #
        # The direction that does warrant care is the one the defaults sit on:
        # with max_gap (600) above min_seconds (300), a data hole between the
        # two is absorbed into its surrounding run and can be reported as idle
        # head-time on its own. That is a disclosed modelling choice, not a
        # config error -- 600 s is far above the ~1 Hz cycling cadence, the
        # tool declares the bound in its assumptions, and docs/analytics-methods
        # states the trade-off -- so it is documented rather than rejected.
        if self.mad_k <= 0:
            raise ConfigError(
                f"mad_k must be > 0, got {self.mad_k}; at k <= 0 the deviation band "
                "collapses and every reading (k < 0) or every non-median reading "
                "(k = 0) is flagged"
            )
        if self.effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ConfigError(
                f"effort must be one of low/medium/high/xhigh/max, got {self.effort!r}"
            )
        if self.max_tokens < 1024:
            raise ConfigError(f"max_tokens must be >= 1024, got {self.max_tokens}")
        if self.provider not in self.PROVIDERS:
            raise ConfigError(
                f"provider must be one of {list(self.PROVIDERS)}, got {self.provider!r}"
            )
        if self.planning not in self.PLANNING:
            raise ConfigError(
                f"planning must be one of {list(self.PLANNING)}, got {self.planning!r}"
            )
        if self.narrator_max_items < 1:
            raise ConfigError(
                f"narrator_max_items must be >= 1, got {self.narrator_max_items}"
            )
        if self.max_anomaly_items < 1:
            raise ConfigError(
                f"max_anomaly_items must be >= 1, got {self.max_anomaly_items}"
            )
        # The planner's prompt is ~2,600 tokens of tool schema before the question
        # is added, and Ollama silently truncates rather than erroring.
        if self.provider == "ollama" and self.num_ctx < 4096:
            raise ConfigError(
                f"num_ctx must be >= 4096 for ollama (the planner prompt alone is "
                f"~2,600 tokens and is truncated silently), got {self.num_ctx}"
            )


def load_config(path):
    """Load config from a JSON file. `path=None` yields the defaults."""
    if path is None:
        return Config()
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file is not valid JSON: {path}") from exc

    known = {f.name: f.type for f in fields(Config)}
    for key in raw:
        if key not in known:
            raise ConfigError(f"unknown config key {key!r}; known keys: {sorted(known)}")

    # Types checked here, not discovered mid-analysis: {"torque_min": [1, 2]}
    # used to escape as a raw TypeError and {"mad_k": "three"} died inside
    # DuckDB three tools deep -- both defeating the documented "exit 2 before
    # any work starts" contract this function exists to give.
    coercible = {float: (int, float), int: (int,), str: (str,), bool: (bool,)}
    for key, value in raw.items():
        expected = coercible.get(known[key])
        if expected is None:
            continue
        if (not isinstance(value, expected)
                or (isinstance(value, bool) and known[key] is not bool)):
            raise ConfigError(
                f"{key} must be {known[key].__name__}, got "
                f"{type(value).__name__} ({value!r})"
            )

    return Config(**raw)
