"""Canonical Release Decision contract derivation (Issue #72).

Single Source of Truth for release recommendation / action / reasons.

Every consumer (Dashboard bundle, Google Chat alerts, future CLI or GitHub Check)
MUST read the derived contract instead of maintaining its own
`gate_status -> wording/action` mapping. The canonical wording literals live in
this module only; a structural contract test asserts they appear nowhere else.

Derivation is a deterministic pure function of:

    release_gate.status + rule_results[] + sample state

`reasons` strictly reuses the deterministic `reason` strings already produced by
`crash_trend.gate.evaluator`; this module never invents new regression prose.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from crash_trend.schema_v2 import (
    CANONICAL_DECISION_ACTIONS,
    DecisionAction,
    ReleaseDecision,
    canonical_decision_status,
)

# Canonical status -> recommendation wording. This table is the ONLY place the
# recommendation wording may exist across the codebase. The paired `action`
# comes from `CANONICAL_DECISION_ACTIONS` in `crash_trend.schema_v2`, so schema
# validation and this derivation can never disagree about the canonical pair.
_RECOMMENDATION_BY_STATUS: dict[str, str] = {
    "pass": "指標均在安全閾值內，可以繼續發布",
    "warn": "建議先觀察並調查退化指標，暫緩擴大發布",
    "fail": "建議停止擴大發布，優先處理退化問題",
    "insufficient_data": "樣本不足尚無法判定品質，請等待資料累積後再決定是否擴大發布",
    "baseline": "無前版可比較，本版作為基準；請持續觀察後再決定是否擴大發布",
}

# Canonical status -> (action, recommendation) derivation table.
_DECISION_TABLE: dict[str, tuple[DecisionAction, str]] = {
    status: (CANONICAL_DECISION_ACTIONS[status], recommendation)
    for status, recommendation in _RECOMMENDATION_BY_STATUS.items()
}


def _reasons_by_rule_status(
    rule_results: Sequence[Mapping[str, Any]],
    rule_status: str,
) -> list[str]:
    """Collects deterministic `reason` strings from rules with the given rule status."""
    return [
        str(r["reason"])
        for r in rule_results
        if isinstance(r, Mapping) and r.get("status") == rule_status and r.get("reason")
    ]


def _reasons_by_rule_name(
    rule_results: Sequence[Mapping[str, Any]],
    rule_name: str,
) -> list[str]:
    """Collects deterministic `reason` strings from rules with the given rule name."""
    return [
        str(r["reason"])
        for r in rule_results
        if isinstance(r, Mapping) and r.get("rule_name") == rule_name and r.get("reason")
    ]


def derive_decision(
    status: str,
    rule_results: Sequence[Mapping[str, Any]] | None = None,
    sample_sufficient: bool = True,
) -> ReleaseDecision:
    """Derives the canonical Release Decision from gate evaluation output.

    Deterministic and side-effect free: identical inputs always yield an
    identical contract.

    `insufficient_data` and `baseline` are first-class states and are never
    presented as PASS. An unrecognized status is treated as `insufficient_data`
    so an unknown gate state can never be read as a green light. A `pass` status
    reported with an insufficient sample is likewise downgraded to
    `insufficient_data`; `warn` / `fail` keep their status because regression
    evidence already exists and must not be softened.
    """
    rules = list(rule_results or [])
    norm = canonical_decision_status(status, sample_sufficient)
    action, recommendation = _DECISION_TABLE[norm]

    if norm == "fail":
        # Prefer the breaching rules; fall back to warn evidence when a fail
        # status was produced without any fail-level rule.
        reasons = _reasons_by_rule_status(rules, "fail") or _reasons_by_rule_status(rules, "warn")
    elif norm == "warn":
        reasons = _reasons_by_rule_status(rules, "warn")
    elif norm == "insufficient_data":
        reasons = _reasons_by_rule_status(rules, "insufficient_data")
    elif norm == "baseline":
        # The evaluator's baseline rule carries rule status `pass`, so evidence
        # is selected by rule name instead of rule status.
        reasons = _reasons_by_rule_name(rules, "baseline_version")
    else:
        reasons = []

    return {
        "status": cast(Literal["pass", "warn", "fail", "insufficient_data", "baseline"], norm),
        "action": action,
        "recommendation": recommendation,
        "reasons": reasons,
    }


def gate_evaluated_quality(result: Mapping[str, Any]) -> bool:
    """判斷一份 gate 結果／summary 是否真的做過品質評估。

    啟用中的 gate 必定至少留下一筆 `rule_results`：樣本不足規則、基準版規則，
    或指標規則本身（即使指標缺漏也會留下 `skip` 佔位）。反之，完全沒有 rule 證據
    的結果代表 gate 根本沒有評估品質（例如 policy `enabled: false`），此時不得由它
    推導 Release Decision —— 否則 `pass -> proceed`「可以繼續發布」會把「gate 未啟用」
    偽裝成「已驗證安全」（Issue #72 review）。
    """
    rules = result.get("rule_results")
    return isinstance(rules, list) and len(rules) > 0


def decision_from_gate_result(result: Mapping[str, Any]) -> ReleaseDecision:
    """Derives the decision from a `PlatformGateResult` or `ReleaseGateSummary` mapping.

    Accepts either key convention (`gate_status` in the gate artifact,
    `status` in the dashboard `release_gate` summary) so consumers never have to
    normalize the shape themselves.
    """
    status = result.get("gate_status")
    if status is None:
        status = result.get("status", "")
    sample_sufficient = result.get("sample_sufficient")
    return derive_decision(
        str(status),
        result.get("rule_results") or [],
        sample_sufficient if isinstance(sample_sufficient, bool) else True,
    )
