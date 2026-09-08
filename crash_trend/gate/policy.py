"""Release Regression Gate threshold policy definitions and loader (Issue #57).

Defines:
- ThresholdRule: Pair of warn and fail thresholds for a metric.
- GatePolicy: Complete configuration for release regression gating.
- load_gate_policy: Resolves policy from app configuration or returns defaults.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ThresholdRule:
    """Thresholds for warn and fail triggers.

    warn: Value at or exceeding this threshold triggers WARN status.
    fail: Value at or exceeding this threshold triggers FAIL status.
    """

    warn: float | int
    fail: float | int

    def __post_init__(self) -> None:
        if self.warn > self.fail:
            # Enforce warn <= fail for sensible escalation
            object.__setattr__(self, "warn", self.fail)


@dataclass(frozen=True)
class GatePolicy:
    """Configurable threshold policy for Release Regression Gate."""

    policy_version: str = "1.0"
    enabled: bool = True
    min_sessions: int = 1000
    min_adoption_rate: float = 0.05
    min_version_events: int = 20

    # Normalized metric threshold rules
    crash_rate_change_pct: ThresholdRule = ThresholdRule(warn=0.10, fail=0.25)
    crash_free_users_drop: ThresholdRule = ThresholdRule(warn=0.005, fail=0.015)
    fatal_rate_change_pct: ThresholdRule = ThresholdRule(warn=0.10, fail=0.25)
    anr_rate_change_pct: ThresholdRule = ThresholdRule(warn=0.10, fail=0.25)
    regressed_issues_count: ThresholdRule = ThresholdRule(warn=1, fail=3)
    introduced_issues_count: ThresholdRule = ThresholdRule(warn=5, fail=10)

    def to_dict(self) -> dict[str, Any]:
        """Serializes policy to a clean dictionary for JSON artifacts."""
        return asdict(self)


def _parse_threshold_rule(
    raw: Any,
    default: ThresholdRule,
    is_int: bool = False,
) -> ThresholdRule:
    """Parses a threshold rule from dict or tuple/list safely."""
    if not isinstance(raw, dict):
        return default

    warn_raw = raw.get("warn", raw.get("warning"))
    fail_raw = raw.get("fail", raw.get("critical", raw.get("error")))

    w_val = default.warn
    if warn_raw is not None:
        try:
            w_val = int(warn_raw) if is_int else float(warn_raw)
        except (TypeError, ValueError):
            w_val = default.warn

    f_val = default.fail
    if fail_raw is not None:
        try:
            f_val = int(fail_raw) if is_int else float(fail_raw)
        except (TypeError, ValueError):
            f_val = default.fail

    if w_val < 0:
        w_val = default.warn
    if f_val < 0:
        f_val = default.fail

    return ThresholdRule(warn=w_val, fail=f_val)


def load_gate_policy(app_cfg: dict[str, Any] | None = None) -> GatePolicy:
    """Loads GatePolicy from app configuration (e.g. from apps.yaml).

    If no policy is specified in app_cfg, returns standard default policy.
    """
    if not isinstance(app_cfg, dict):
        return GatePolicy()

    gate_cfg = app_cfg.get("release_gate")
    if not isinstance(gate_cfg, dict):
        return GatePolicy()

    enabled = bool(gate_cfg.get("enabled", True))
    policy_version = str(gate_cfg.get("policy_version", "1.0"))

    min_sess_raw = gate_cfg.get("min_sessions", 1000)
    try:
        min_sessions = max(0, int(min_sess_raw))
    except (TypeError, ValueError):
        min_sessions = 1000

    min_adopt_raw = gate_cfg.get("min_adoption_rate", 0.05)
    try:
        min_adoption_rate = max(0.0, float(min_adopt_raw))
    except (TypeError, ValueError):
        min_adoption_rate = 0.05

    min_ev_raw = gate_cfg.get("min_version_events", 20)
    try:
        min_version_events = max(0, int(min_ev_raw))
    except (TypeError, ValueError):
        min_version_events = 20

    raw_thresh = gate_cfg.get("thresholds")
    thresholds: dict[str, Any] = raw_thresh if isinstance(raw_thresh, dict) else gate_cfg

    default_policy = GatePolicy()

    cr_rule = _parse_threshold_rule(
        thresholds.get("crash_rate_change_pct"),
        default_policy.crash_rate_change_pct,
    )
    cfu_rule = _parse_threshold_rule(
        thresholds.get("crash_free_users_drop"),
        default_policy.crash_free_users_drop,
    )
    fatal_rule = _parse_threshold_rule(
        thresholds.get("fatal_rate_change_pct"),
        default_policy.fatal_rate_change_pct,
    )
    anr_rule = _parse_threshold_rule(
        thresholds.get("anr_rate_change_pct"),
        default_policy.anr_rate_change_pct,
    )
    regressed_rule = _parse_threshold_rule(
        thresholds.get("regressed_issues_count"),
        default_policy.regressed_issues_count,
        is_int=True,
    )
    introduced_rule = _parse_threshold_rule(
        thresholds.get("introduced_issues_count"),
        default_policy.introduced_issues_count,
        is_int=True,
    )

    return GatePolicy(
        policy_version=policy_version,
        enabled=enabled,
        min_sessions=min_sessions,
        min_adoption_rate=min_adoption_rate,
        min_version_events=min_version_events,
        crash_rate_change_pct=cr_rule,
        crash_free_users_drop=cfu_rule,
        fatal_rate_change_pct=fatal_rule,
        anr_rate_change_pct=anr_rule,
        regressed_issues_count=regressed_rule,
        introduced_issues_count=introduced_rule,
    )


def load_gate_policy_from_file(path: Path | str) -> GatePolicy:
    """Loads GatePolicy from a JSON or YAML file."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Policy file not found: {path}")

    content = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        raw = yaml.safe_load(content) or {}
    else:
        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            raw = yaml.safe_load(content) or {}

    if not isinstance(raw, dict):
        raise ValueError(f"Policy file must contain a dictionary/mapping: {path}")

    # Support raw being the whole app config or release_gate sub-dict directly
    cfg = raw if "release_gate" in raw else {"release_gate": raw}
    return load_gate_policy(cfg)
