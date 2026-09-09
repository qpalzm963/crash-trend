"""Release Regression Gate and Quality Alerts domain package (Issue #57)."""

from __future__ import annotations

from crash_trend.gate.artifact import (
    AlertHookPayload,
    AlertSeverity,
    GateStatus,
    PlatformGateResult,
    ReleaseDecision,
    ReleaseGateArtifact,
    RuleEvaluationResult,
    RuleStatus,
    load_release_gate_artifact,
    save_release_gate_artifact,
    validate_release_gate_artifact,
)
from crash_trend.gate.decision import (
    decision_from_gate_result,
    derive_decision,
)
from crash_trend.gate.evaluator import (
    evaluate_app_release_gate,
    evaluate_release,
)
from crash_trend.gate.history import (
    GateSnapshot,
    GateTransition,
    ReleaseGateHistoryStore,
    ReleaseGateTrendItem,
    classify_gate_transition,
    get_gate_history_store,
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
    "GateSnapshot",
    "GateStatus",
    "GateTransition",
    "PlatformGateResult",
    "ReleaseDecision",
    "ReleaseGateArtifact",
    "ReleaseGateHistoryStore",
    "ReleaseGateTrendItem",
    "RuleEvaluationResult",
    "RuleStatus",
    "ThresholdRule",
    "classify_gate_transition",
    "decision_from_gate_result",
    "derive_decision",
    "evaluate_app_release_gate",
    "evaluate_release",
    "get_gate_history_store",
    "load_gate_policy",
    "load_gate_policy_from_file",
    "load_release_gate_artifact",
    "save_release_gate_artifact",
    "validate_release_gate_artifact",
]

