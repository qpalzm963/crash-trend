"""Release Regression Gate and Quality Alerts domain package (Issue #57)."""

from __future__ import annotations

from crash_trend.gate.artifact import (
    AlertHookPayload,
    AlertSeverity,
    GateStatus,
    PlatformGateResult,
    ReleaseGateArtifact,
    RuleEvaluationResult,
    RuleStatus,
    load_release_gate_artifact,
    save_release_gate_artifact,
    validate_release_gate_artifact,
)
from crash_trend.gate.evaluator import (
    evaluate_app_release_gate,
    evaluate_release,
)
from crash_trend.gate.policy import (
    GatePolicy,
    ThresholdRule,
    load_gate_policy,
    load_gate_policy_from_file,
)

__all__ = [
    "AlertHookPayload",
    "AlertSeverity",
    "GatePolicy",
    "GateStatus",
    "PlatformGateResult",
    "ReleaseGateArtifact",
    "RuleEvaluationResult",
    "RuleStatus",
    "ThresholdRule",
    "evaluate_app_release_gate",
    "evaluate_release",
    "load_gate_policy",
    "load_gate_policy_from_file",
    "load_release_gate_artifact",
    "save_release_gate_artifact",
    "validate_release_gate_artifact",
]
