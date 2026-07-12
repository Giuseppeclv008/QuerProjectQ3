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

    # Status semantics, measured at closure (spec §3.1).
    success_status: float = 0.0    # + torque > 0  -> a real cap
    no_load_status: float = 2.0    # + torque == 0 -> No-Load cycle
    fault_status: float = 65.0

    # Expected torque operating band (Nm). Measured range: 1.885 - 2.317.
    torque_min: float = 1.5
    torque_max: float = 2.5

    # Robust deviation band: median +/- mad_k * MAD.
    mad_k: float = 3.0

    # A head is idle after this many seconds of sustained No-Load.
    idle_min_seconds: int = 300


def load_config(path):
    """Load config from a JSON file. `path=None` yields the defaults."""
    if path is None:
        return Config()
    with open(path) as fh:
        raw = json.load(fh)

    known = {f.name for f in fields(Config)}
    for key in raw:
        if key not in known:
            raise ConfigError(f"unknown config key {key!r}; known keys: {sorted(known)}")

    cfg = Config(**raw)
    if cfg.torque_min >= cfg.torque_max:
        raise ConfigError(
            f"torque_min ({cfg.torque_min}) must be < torque_max ({cfg.torque_max}); "
            "this band would exclude all data"
        )
    if cfg.idle_min_seconds <= 0:
        raise ConfigError(f"idle_min_seconds must be > 0, got {cfg.idle_min_seconds}")
    return cfg
