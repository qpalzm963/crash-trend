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
from crash_trend.gate.metric_rules import (
    COMPARISON_METRIC_SPECS,
    ComparisonMetricSpec,
    MetricClassification,
    build_comparison_metric_evaluations,
    classification_implied_by_payload,
    classify_threshold_breach,
    evaluate_comparison_metric,
    format_threshold_display,
    spec_for_metric,
    threshold_rule_for,
)
from crash_trend.gate.policy import (
    GatePolicy,
    ThresholdRule,
    load_gate_policy,
    load_gate_policy_from_file,
)

__all__ = [
    "COMPARISON_METRIC_SPECS",
    "AlertHookPayload",
    "AlertSeverity",
    "ComparisonMetricSpec",
    "GatePolicy",
    "GateSnapshot",
    "GateStatus",
    "GateTransition",
    "MetricClassification",
    "PlatformGateResult",
    "ReleaseDecision",
    "ReleaseGateArtifact",
    "ReleaseGateHistoryStore",
    "ReleaseGateTrendItem",
    "RuleEvaluationResult",
    "RuleStatus",
    "ThresholdRule",
    "build_comparison_metric_evaluations",
    "classify_gate_transition",
    "classification_implied_by_payload",
    "classify_threshold_breach",
    "decision_from_gate_result",
    "derive_decision",
    "evaluate_app_release_gate",
    "evaluate_comparison_metric",
    "evaluate_release",
    "format_threshold_display",
    "get_gate_history_store",
    "load_gate_policy",
    "load_gate_policy_from_file",
    "load_release_gate_artifact",
    "save_release_gate_artifact",
    "spec_for_metric",
    "threshold_rule_for",
    "validate_release_gate_artifact",
]

