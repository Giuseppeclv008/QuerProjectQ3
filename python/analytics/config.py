"""Configuration. No path, band, or threshold is hard-coded anywhere else.

WP5 requires configuration-driven datasets. This is also where the status
semantics live (spec §3.2): they were inferred from the measured joint
(status, torque) distribution, so if AROL confirms a different encoding it is a
change here and nowhere else.
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

    # Robust deviation band: median +/- mad_k * MAD.
    mad_k: float = 3.0

    # A head is idle after this many seconds of sustained No-Load.
    idle_min_seconds: int = 300

    # WP3: the model that plans and narrates. It never computes a number.
    model: str = "claude-opus-5"
    effort: str = "high"            # low | medium | high | xhigh | max
    max_tokens: int = 16000
    api_timeout_s: float = 120.0

    def __post_init__(self):
        if self.torque_min >= self.torque_max:
            raise ConfigError(
                f"torque_min ({self.torque_min}) must be < torque_max ({self.torque_max}); "
                "this band would exclude all data"
            )
        if self.idle_min_seconds <= 0:
            raise ConfigError(f"idle_min_seconds must be > 0, got {self.idle_min_seconds}")
        if self.effort not in ("low", "medium", "high", "xhigh", "max"):
            raise ConfigError(
                f"effort must be one of low/medium/high/xhigh/max, got {self.effort!r}"
            )
        if self.max_tokens < 1024:
            raise ConfigError(f"max_tokens must be >= 1024, got {self.max_tokens}")


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

    known = {f.name for f in fields(Config)}
    for key in raw:
        if key not in known:
            raise ConfigError(f"unknown config key {key!r}; known keys: {sorted(known)}")

    return Config(**raw)
