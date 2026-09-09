"""Tests for the canonical Release Decision contract (Issue #72).

Covers:
1. derive_decision() for all five gate states (pass / warn / fail /
   insufficient_data / baseline), including the guarantee that
   insufficient_data and baseline are never presented as PASS.
2. reasons[] sourced verbatim from the deterministic rule_results evidence.
3. Determinism: identical input always yields an identical contract.
4. Contract propagation into the Release Gate artifact and the Dashboard JSON
   release_gate summary.
5. Google Chat alert path consuming the same contract instead of re-deriving it.
6. Negative / structural drift test: no consumer maintains an independent
   gate_status -> wording/action mapping.
7. Backward compatibility: payloads without `decision` still validate.
"""

from __future__ import annotations

import inspect
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crash_trend.alerts.dispatcher import build_alert_message
from crash_trend.build_dashboard import assemble_bundle_from_apps
from crash_trend.gate import GatePolicy, evaluate_release, validate_release_gate_artifact
from crash_trend.gate.decision import (
    _DECISION_TABLE,
    decision_from_gate_result,
    derive_decision,
)
from crash_trend.schema_v2 import (
    VALID_DECISION_ACTIONS,
    VALID_GATE_STATUSES,
    validate_dashboard_v2,
    validate_release_catalog,
    validate_release_decision,
)

FIXTURES = ROOT / "tests" / "fixtures"

# Canonical gate status literals that consumers must not branch on to produce
# recommendation / action wording.
GATE_STATUS_LITERALS = ("pass", "warn", "fail", "insufficient_data", "baseline")


def _rule(rule_name: str, status: str, reason: str) -> dict[str, Any]:
    """Builds a rule_results entry shaped like crash_trend.gate.evaluator output."""
    return {
        "rule_name": rule_name,
        "metric_name": rule_name,
        "current_value": 0.3,
        "previous_value": 0.0,
        "warn_threshold": 0.1,
        "fail_threshold": 0.25,
        "status": status,
        "reason": reason,
    }


class TestDeriveDecisionFiveStates(unittest.TestCase):
    """Each of the five gate states maps to exactly one action / recommendation."""

    def test_pass_recommends_proceeding(self) -> None:
        dec = derive_decision("pass", [_rule("crash_rate_regression", "pass", "崩潰率變動 -1.00% 於正常範圍")])
        self.assertEqual(dec["status"], "pass")
        self.assertEqual(dec["action"], "proceed")
        self.assertTrue(dec["recommendation"])
        # PASS carries no regression evidence.
        self.assertEqual(dec["reasons"], [])

    def test_warn_recommends_investigating(self) -> None:
        dec = derive_decision("warn", [_rule("anr_rate_regression", "warn", "ANR 率上升 +34.00%，達到警告門檻 (+20.0%)")])
        self.assertEqual(dec["status"], "warn")
        self.assertEqual(dec["action"], "investigate")

    def test_fail_recommends_holding(self) -> None:
        dec = derive_decision("fail", [_rule("crash_rate_regression", "fail", "崩潰率上升 +30.00%，達到失敗門檻 (+25.0%)")])
        self.assertEqual(dec["status"], "fail")
        self.assertEqual(dec["action"], "hold")

    def test_insufficient_data_is_never_presented_as_pass(self) -> None:
        dec = derive_decision(
            "insufficient_data",
            [_rule("sample_sufficiency", "insufficient_data", "樣本數不足：數據未達充足門檻，未達最小門檻 (1000 sessions)")],
            sample_sufficient=False,
        )
        self.assertEqual(dec["status"], "insufficient_data")
        self.assertEqual(dec["action"], "await_data")
        self.assertNotEqual(dec["action"], "proceed")

    def test_baseline_is_never_presented_as_pass(self) -> None:
        dec = derive_decision("baseline", [_rule("baseline_version", "pass", "無前版基準資料，作為初始基準版本")])
        self.assertEqual(dec["status"], "baseline")
        self.assertEqual(dec["action"], "establish_baseline")
        self.assertNotEqual(dec["action"], "proceed")

    def test_every_state_has_a_distinct_action(self) -> None:
        actions = {s: derive_decision(s)["action"] for s in GATE_STATUS_LITERALS}
        self.assertEqual(len(set(actions.values())), len(GATE_STATUS_LITERALS), actions)
        # Only `pass` may authorize continuing the rollout.
        self.assertEqual([s for s, a in actions.items() if a == "proceed"], ["pass"])

    def test_unknown_status_degrades_to_await_data_not_pass(self) -> None:
        """An unrecognized gate state must never read as a green light."""
        dec = derive_decision("totally_unknown")
        self.assertEqual(dec["status"], "insufficient_data")
        self.assertEqual(dec["action"], "await_data")

    def test_pass_with_insufficient_sample_degrades_to_await_data(self) -> None:
        """Sample state is a first-class input: PASS on a thin sample is not PASS."""
        dec = derive_decision("pass", [], sample_sufficient=False)
        self.assertEqual(dec["status"], "insufficient_data")
        self.assertEqual(dec["action"], "await_data")

    def test_regression_status_is_not_softened_by_sample_state(self) -> None:
        """WARN/FAIL evidence already exists and must not be downgraded."""
        for status, action in (("warn", "investigate"), ("fail", "hold")):
            dec = derive_decision(status, [], sample_sufficient=False)
            self.assertEqual(dec["status"], status)
            self.assertEqual(dec["action"], action)


class TestDecisionReasonsSourcing(unittest.TestCase):
    """reasons[] must reuse existing rule_results evidence, never new prose."""

    def test_fail_reasons_are_verbatim_fail_rule_reasons(self) -> None:
        fail_reason = "崩潰率上升 +30.00%，達到失敗門檻 (+25.0%)"
        warn_reason = "ANR 率上升 +15.00%，達到警告門檻 (+10.0%)"
        dec = derive_decision(
            "fail",
            [
                _rule("crash_rate_regression", "fail", fail_reason),
                _rule("anr_rate_regression", "warn", warn_reason),
                _rule("introduced_issues_count", "pass", "新引入問題數 1 個於正常範圍"),
                _rule("fatal_rate_regression", "skip", "未取得 Fatal 崩潰率數據"),
            ],
        )
        self.assertEqual(dec["reasons"], [fail_reason])

    def test_warn_reasons_only_include_warn_evidence(self) -> None:
        warn_reason = "復發問題數 2 個，達到警告門檻 (1 個)"
        dec = derive_decision(
            "warn",
            [
                _rule("regressed_issues_count", "warn", warn_reason),
                _rule("crash_rate_regression", "pass", "崩潰率變動 +1.00% 於正常範圍"),
            ],
        )
        self.assertEqual(dec["reasons"], [warn_reason])

    def test_fail_without_fail_rules_falls_back_to_warn_evidence(self) -> None:
        warn_reason = "無崩潰用戶率下降 0.80%，達到警告門檻 (-0.50%)"
        dec = derive_decision("fail", [_rule("crash_free_users_drop", "warn", warn_reason)])
        self.assertEqual(dec["reasons"], [warn_reason])

    def test_zero_baseline_regression_wording_is_preserved(self) -> None:
        """The evaluator's zero-baseline wording must survive into the contract."""
        item: dict[str, Any] = {
            "version": "2.1.0",
            "platform": "android",
            "status": "latest",
            "recent_health": {"30d": {"sessions_total": 20000, "sample_sufficient": True}},
            "vs_previous": {
                "previous_version": "2.0.0",
                "anr_rate_change_pct": 0.01,
                "zero_baseline_anr": True,
            },
        }
        res = evaluate_release(item, GatePolicy(min_sessions=1000))
        self.assertEqual(res["gate_status"], "fail")
        zero_baseline_reasons = [r for r in res["decision"]["reasons"] if "零基準退化" in r]
        self.assertTrue(zero_baseline_reasons, res["decision"]["reasons"])
        # Verbatim reuse: the reason string is exactly a rule_results reason.
        rule_reasons = {r["reason"] for r in res["rule_results"]}
        for reason in res["decision"]["reasons"]:
            self.assertIn(reason, rule_reasons)

    def test_insufficient_data_reasons_come_from_sample_rule(self) -> None:
        sample_reason = "樣本數不足：數據未達充足門檻，未達最小門檻 (1000 sessions)"
        dec = derive_decision(
            "insufficient_data",
            [_rule("sample_sufficiency", "insufficient_data", sample_reason)],
            sample_sufficient=False,
        )
        self.assertEqual(dec["reasons"], [sample_reason])

    def test_baseline_reasons_come_from_baseline_rule(self) -> None:
        baseline_reason = "無前版基準資料，作為初始基準版本"
        dec = derive_decision(
            "baseline",
            [
                _rule("baseline_version", "pass", baseline_reason),
                _rule("crash_rate_regression", "pass", "崩潰率變動 0.00% 於正常範圍"),
            ],
        )
        self.assertEqual(dec["reasons"], [baseline_reason])

    def test_missing_rule_results_yield_empty_reasons_not_invented_prose(self) -> None:
        for status in GATE_STATUS_LITERALS:
            dec = derive_decision(status, [])
            self.assertEqual(dec["reasons"], [], status)


class TestDecisionDeterminism(unittest.TestCase):
    """Identical inputs must always produce an identical contract."""

    def test_repeated_derivation_is_identical(self) -> None:
        rules = [
            _rule("crash_rate_regression", "fail", "崩潰率上升 +30.00%，達到失敗門檻 (+25.0%)"),
            _rule("anr_rate_regression", "warn", "ANR 率上升 +12.00%，達到警告門檻 (+10.0%)"),
        ]
        first = derive_decision("fail", rules)
        for _ in range(5):
            self.assertEqual(derive_decision("fail", rules), first)

    def test_derivation_does_not_mutate_inputs(self) -> None:
        rules = [_rule("crash_rate_regression", "fail", "崩潰率上升 +30.00%")]
        snapshot = json.dumps(rules, ensure_ascii=False, sort_keys=True)
        derive_decision("fail", rules)
        self.assertEqual(json.dumps(rules, ensure_ascii=False, sort_keys=True), snapshot)

    def test_status_normalization_is_case_and_space_insensitive(self) -> None:
        self.assertEqual(derive_decision("  FAIL "), derive_decision("fail"))

    def test_decision_from_gate_result_accepts_both_key_conventions(self) -> None:
        rules = [_rule("crash_rate_regression", "warn", "崩潰率上升 +15.00%，達到警告門檻 (+10.0%)")]
        artifact_shape = {"gate_status": "warn", "sample_sufficient": True, "rule_results": rules}
        bundle_shape = {"status": "warn", "sample_sufficient": True, "rule_results": rules}
        self.assertEqual(
            decision_from_gate_result(artifact_shape),
            decision_from_gate_result(bundle_shape),
        )
        self.assertEqual(decision_from_gate_result(artifact_shape), derive_decision("warn", rules))


class TestGateArtifactCarriesDecision(unittest.TestCase):
    """The gate artifact is the shared contract carrier for every consumer."""

    def setUp(self) -> None:
        self.policy = GatePolicy(min_sessions=1000)

    def _evaluate(self, item: dict[str, Any]) -> dict[str, Any]:
        return dict(evaluate_release(item, self.policy))

    def test_all_five_states_attach_a_matching_decision(self) -> None:
        cases: dict[str, dict[str, Any]] = {
            "baseline": {
                "version": "1.0.0",
                "platform": "android",
                "status": "latest",
                "recent_health": {"30d": {"sessions_total": 15000, "sample_sufficient": True}},
                "vs_previous": None,
            },
            "insufficient_data": {
                "version": "2.1.0",
                "platform": "android",
                "status": "latest",
                "recent_health": {"30d": {"sessions_total": 500, "crash_events": 2}},
                "vs_previous": {"previous_version": "2.0.0", "crash_rate_change_pct": 0.5},
            },
            "pass": {
                "version": "2.1.0",
                "platform": "android",
                "status": "latest",
                "recent_health": {"30d": {"sessions_total": 20000, "sample_sufficient": True}},
                "vs_previous": {"previous_version": "2.0.0", "crash_rate_change_pct": -0.05},
            },
            "warn": {
                "version": "2.1.0",
                "platform": "android",
                "status": "latest",
                "recent_health": {"30d": {"sessions_total": 20000, "sample_sufficient": True}},
                "vs_previous": {"previous_version": "2.0.0", "crash_rate_change_pct": 0.15},
            },
            "fail": {
                "version": "2.1.0",
                "platform": "android",
                "status": "latest",
                "recent_health": {"30d": {"sessions_total": 20000, "sample_sufficient": True}},
                "vs_previous": {"previous_version": "2.0.0", "crash_rate_change_pct": 0.30},
            },
        }
        for expected_status, item in cases.items():
            res = self._evaluate(item)
            self.assertEqual(res["gate_status"], expected_status, item)
            decision = res["decision"]
            self.assertEqual(validate_release_decision(decision), [])
            self.assertEqual(decision["status"], expected_status)
            self.assertEqual(
                decision,
                decision_from_gate_result(res),
                "artifact decision must equal the canonical derivation",
            )

    def test_artifact_validation_accepts_and_checks_decision(self) -> None:
        item: dict[str, Any] = {
            "version": "2.1.0",
            "platform": "android",
            "status": "latest",
            "recent_health": {"30d": {"sessions_total": 20000, "sample_sufficient": True}},
            "vs_previous": {"previous_version": "2.0.0", "crash_rate_change_pct": 0.30},
        }
        res = self._evaluate(item)
        artifact: dict[str, Any] = {
            "schema_version": "1.0",
            "app_id": "shop_app",
            "generated_at": "2026-09-09T10:00:00Z",
            "overall_status": "fail",
            "should_alert": True,
            "alert_severity": "critical",
            "alert_summary": "summary",
            "platforms": {"android": res},
            "policy_version": "1.0",
            "policy": {},
        }
        self.assertEqual(validate_release_gate_artifact(artifact), [])

        # An invalid action must be rejected rather than silently accepted.
        artifact["platforms"]["android"]["decision"]["action"] = "ship_it"
        errors = validate_release_gate_artifact(artifact)
        self.assertTrue(any("decision.action" in e for e in errors), errors)


class TestGoogleChatConsumesContract(unittest.TestCase):
    """The Google Chat path renders the contract; it does not re-derive it."""

    def test_alert_message_renders_contract_fields(self) -> None:
        rules = [_rule("crash_rate_regression", "fail", "崩潰率上升 +30.00%，達到失敗門檻 (+25.0%)")]
        decision = derive_decision("fail", rules)
        msg = build_alert_message(
            app_id="shop_app",
            platform="android",
            target_version="3.2.0",
            previous_version="3.1.0",
            gate_status="fail",
            is_recovery=False,
            rule_results=rules,
            policy_version="1.0",
            evaluated_at="2026-09-09T10:00:00Z",
            decision=decision,
            alert_severity="critical",
        )
        self.assertEqual(msg.decision_action, decision["action"])
        self.assertEqual(msg.recommendation, decision["recommendation"])
        self.assertEqual(msg.regression_reasons, decision["reasons"])
        self.assertIn(decision["recommendation"], msg.summary)
        self.assertIn(decision["recommendation"], msg.text)
        self.assertIn(decision["action"], msg.text)

    def test_alert_message_reads_injected_contract_verbatim(self) -> None:
        """Sentinel wording proves the message reads the field, not the status."""
        sentinel = {
            "status": "warn",
            "action": "investigate",
            "recommendation": "SENTINEL_RECOMMENDATION",
            "reasons": ["SENTINEL_REASON"],
        }
        msg = build_alert_message(
            app_id="shop_app",
            platform="ios",
            target_version="3.2.0",
            previous_version="3.1.0",
            gate_status="warn",
            is_recovery=False,
            # Deliberately contradicts the sentinel: it must be ignored.
            rule_results=[_rule("anr_rate_regression", "warn", "IGNORED_RULE_REASON")],
            policy_version="1.0",
            evaluated_at="2026-09-09T10:00:00Z",
            decision=sentinel,
        )
        self.assertIn("SENTINEL_RECOMMENDATION", msg.summary)
        self.assertIn("SENTINEL_RECOMMENDATION", msg.text)
        self.assertEqual(msg.regression_reasons, ["SENTINEL_REASON"])
        self.assertNotIn("IGNORED_RULE_REASON", msg.text)

    def test_alert_message_without_contract_derives_via_domain_function(self) -> None:
        """Pre-V3.2 artifacts fall back to the same domain function."""
        rules = [_rule("crash_rate_regression", "warn", "崩潰率上升 +15.00%，達到警告門檻 (+10.0%)")]
        msg = build_alert_message(
            app_id="shop_app",
            platform="android",
            target_version="3.2.0",
            previous_version="3.1.0",
            gate_status="warn",
            is_recovery=False,
            rule_results=rules,
            policy_version="1.0",
            evaluated_at="2026-09-09T10:00:00Z",
        )
        expected = derive_decision("warn", rules)
        self.assertEqual(msg.decision_action, expected["action"])
        self.assertEqual(msg.recommendation, expected["recommendation"])

    def test_all_five_states_produce_contract_aligned_messages(self) -> None:
        for status in GATE_STATUS_LITERALS:
            expected = derive_decision(status)
            msg = build_alert_message(
                app_id="shop_app",
                platform="android",
                target_version="3.2.0",
                previous_version="3.1.0",
                gate_status=status,
                is_recovery=False,
                rule_results=[],
                policy_version="1.0",
                evaluated_at="2026-09-09T10:00:00Z",
            )
            self.assertEqual(msg.decision_action, expected["action"], status)
            self.assertEqual(msg.recommendation, expected["recommendation"], status)
            self.assertIn(expected["status"].upper(), msg.title, status)


class TestNoConsumerRederivesDecision(unittest.TestCase):
    """Negative / structural drift test (Issue #72 drift test decision).

    #72 deliberately does not ship the V3 decision-first rendering (that is #73),
    so there is no second live consumer to compare wording against yet. Instead
    these tests assert the structural invariant that makes such drift
    impossible: the canonical wording exists in exactly one module, and the
    alert renderer contains no gate_status branching at all.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.decision_module = ROOT / "crash_trend" / "gate" / "decision.py"
        cls.package_sources = sorted(
            p for p in (ROOT / "crash_trend").rglob("*.py") if p != cls.decision_module
        )
        cls.canonical_wording = sorted({entry[1] for entry in _DECISION_TABLE.values()})

    def test_canonical_wording_exists_in_exactly_one_module(self) -> None:
        """No consumer may hold its own copy of the recommendation wording."""
        self.assertTrue(self.canonical_wording)
        for path in self.package_sources:
            text = path.read_text(encoding="utf-8")
            for wording in self.canonical_wording:
                self.assertNotIn(
                    wording,
                    text,
                    f"{path.relative_to(ROOT)} duplicates canonical decision wording "
                    f"'{wording}'; read release_gate.decision instead",
                )

    def test_alert_renderer_has_no_gate_status_branching(self) -> None:
        """build_alert_message must not map gate_status to wording or actions."""
        src = inspect.getsource(build_alert_message)
        for literal in GATE_STATUS_LITERALS:
            self.assertNotIn(
                f'"{literal}"',
                src,
                f"build_alert_message branches on gate status '{literal}'; "
                "recommendation/action must come from the decision contract",
            )
            self.assertNotIn(f"'{literal}'", src)

    def test_alert_package_declares_no_status_to_action_mapping(self) -> None:
        """No module under crash_trend/alerts may name a decision action."""
        for path in sorted((ROOT / "crash_trend" / "alerts").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for action in sorted(VALID_DECISION_ACTIONS):
                self.assertNotIn(
                    f'"{action}"',
                    text,
                    f"{path.relative_to(ROOT)} hardcodes decision action '{action}'",
                )

    def test_dashboard_js_has_no_independent_decision_wording(self) -> None:
        """Dashboard JS string literals must not restate recommendations.

        #73 renders the decision; this guard keeps that rendering bound to the
        contract field instead of a second status -> wording table in JS.
        """
        for path in sorted((ROOT / "crash_trend" / "dashboard").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for wording in self.canonical_wording:
                self.assertNotIn(wording, text, f"{path.relative_to(ROOT)} duplicates decision wording")
            for action in sorted(VALID_DECISION_ACTIONS):
                self.assertNotIn(f'"{action}"', text, f"{path.relative_to(ROOT)} hardcodes decision action")

    def test_decision_table_and_schema_enums_stay_in_sync(self) -> None:
        """Schema validation enums must not drift from the derivation table."""
        self.assertEqual(set(_DECISION_TABLE), VALID_GATE_STATUSES)
        self.assertEqual({entry[0] for entry in _DECISION_TABLE.values()}, VALID_DECISION_ACTIONS)


class TestDecisionBackwardCompatibility(unittest.TestCase):
    """Payloads produced before V3.2 must still validate and render."""

    def test_release_gate_summary_without_decision_still_validates(self) -> None:
        catalog: list[dict[str, Any]] = [
            {
                "version": "3.2.0",
                "platform": "android",
                "first_seen": "2026-08-01T00:00:00Z",
                "last_seen": "2026-09-01T00:00:00Z",
                "release_date": "2026-08-01",
                "status": "latest",
                "lifetime_crashes": 10,
                "lifetime_issues": 2,
                "lifetime_affected_users": 5,
                "lifetime_fatal": 3,
                "lifetime_anr": 1,
                "recent_health": {},
                "release_gate": {
                    "status": "warn",
                    "should_alert": True,
                    "alert_severity": "warning",
                    "alert_summary": "legacy summary",
                    "rules_triggered": ["crash_rate_regression"],
                },
            }
        ]
        errors: list[str] = []
        validate_release_catalog(catalog, errors)
        self.assertEqual(errors, [])

    def test_release_gate_summary_with_decision_validates(self) -> None:
        catalog: list[dict[str, Any]] = [
            {
                "version": "3.2.0",
                "platform": "android",
                "first_seen": "2026-08-01T00:00:00Z",
                "last_seen": "2026-09-01T00:00:00Z",
                "release_date": "2026-08-01",
                "status": "latest",
                "lifetime_crashes": 10,
                "lifetime_issues": 2,
                "lifetime_affected_users": 5,
                "lifetime_fatal": 3,
                "lifetime_anr": 1,
                "recent_health": {},
                "release_gate": {
                    "status": "warn",
                    "should_alert": True,
                    "alert_severity": "warning",
                    "alert_summary": "summary",
                    "rules_triggered": ["anr_rate_regression"],
                    "decision": derive_decision(
                        "warn",
                        [_rule("anr_rate_regression", "warn", "ANR 率上升 +34.00%，達到警告門檻 (+20.0%)")],
                    ),
                },
            }
        ]
        errors: list[str] = []
        validate_release_catalog(catalog, errors)
        self.assertEqual(errors, [])

    def test_malformed_decision_is_reported(self) -> None:
        errors: list[str] = []
        validate_release_catalog(
            [
                {
                    "version": "3.2.0",
                    "platform": "android",
                    "first_seen": None,
                    "last_seen": None,
                    "release_date": None,
                    "status": "latest",
                    "lifetime_crashes": 0,
                    "lifetime_issues": 0,
                    "lifetime_affected_users": 0,
                    "lifetime_fatal": 0,
                    "lifetime_anr": 0,
                    "recent_health": {},
                    "release_gate": {
                        "status": "warn",
                        "should_alert": True,
                        "alert_severity": "warning",
                        "alert_summary": "summary",
                        "rules_triggered": [],
                        "decision": {"status": "warn", "action": "ship_it", "reasons": "nope"},
                    },
                }
            ],
            errors,
        )
        self.assertTrue(any("decision.action" in e for e in errors), errors)
        self.assertTrue(any("decision.reasons" in e for e in errors), errors)
        self.assertTrue(any("decision.recommendation is required" in e for e in errors), errors)

    def test_gate_artifact_without_decision_still_validates(self) -> None:
        artifact: dict[str, Any] = {
            "schema_version": "1.0",
            "app_id": "shop_app",
            "generated_at": "2026-09-09T10:00:00Z",
            "overall_status": "warn",
            "should_alert": True,
            "alert_severity": "warning",
            "alert_summary": "legacy artifact",
            "platforms": {
                "android": {
                    "platform": "android",
                    "target_version": "3.2.0",
                    "previous_version": "3.1.0",
                    "gate_status": "warn",
                    "sample_sufficient": True,
                    "rule_results": [],
                    "alert": {
                        "should_alert": True,
                        "alert_severity": "warning",
                        "alert_summary": "legacy",
                        "trigger_rules": [],
                    },
                    "evaluated_at": "2026-09-09T10:00:00Z",
                }
            },
            "policy_version": "1.0",
            "policy": {},
        }
        self.assertEqual(validate_release_gate_artifact(artifact), [])

    def test_bundle_adapter_backfills_decision_for_legacy_app_data(self) -> None:
        """A pre-V3.2 per-app bundle gains the contract via the gate domain function."""
        fixture = json.loads((FIXTURES / "dashboard_v2.json").read_text(encoding="utf-8"))
        app_v2 = fixture["apps"]["shop_app"]
        # Simulate an already-enriched pre-V3.2 bundle: lifecycle is present, so
        # the catalog is not rebuilt and only the decision backfill can supply
        # the contract.
        for issue in app_v2["top_issues"]:
            issue.setdefault(
                "lifecycle",
                {
                    "status": "persistent",
                    "latest_version": "3.2.0",
                    "first_seen_version": "3.1.0",
                    "last_seen_version": "3.2.0",
                    "versions_seen": 2,
                    "confidence": "high",
                    "previously_absent_since": None,
                    "reappeared_version": None,
                    "reason": None,
                },
            )
        legacy_gate = {
            "status": "warn",
            "should_alert": True,
            "alert_severity": "warning",
            "alert_summary": "legacy summary",
            "rules_triggered": ["anr_rate_regression"],
            "sample_sufficient": True,
            "rule_results": [_rule("anr_rate_regression", "warn", "ANR 率上升 +34.00%，達到警告門檻 (+20.0%)")],
        }
        app_v2["release_catalog"] = [
            {
                "version": "3.2.0",
                "platform": "android",
                "first_seen": "2026-08-01T00:00:00Z",
                "last_seen": "2026-09-01T00:00:00Z",
                "release_date": "2026-08-01",
                "status": "latest",
                "lifetime_crashes": 10,
                "lifetime_issues": 2,
                "lifetime_affected_users": 5,
                "lifetime_fatal": 3,
                "lifetime_anr": 1,
                "recent_health": {},
                "release_gate": legacy_gate,
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmproot = Path(tmpdir)
            out_app = tmproot / "out" / "shop_app"
            out_app.mkdir(parents=True)
            (out_app / "dashboard_v2.json").write_text(json.dumps(app_v2, ensure_ascii=False), encoding="utf-8")

            fake_cfg = {
                "apps": {
                    "shop_app": {
                        "app_id": "shop_app",
                        "display_name": "Shop App",
                        "platforms": ["android"],
                    }
                }
            }
            with patch("crash_trend.build_dashboard.ROOT", tmproot):
                bundle = assemble_bundle_from_apps(fake_cfg)

        self.assertIsNotNone(bundle)
        assert bundle is not None
        self.assertEqual(validate_dashboard_v2(bundle), [])
        rebuilt_gate = bundle["apps"]["shop_app"]["release_catalog"][0]["release_gate"]
        self.assertEqual(rebuilt_gate["decision"], decision_from_gate_result(legacy_gate))
        self.assertEqual(rebuilt_gate["decision"]["action"], "investigate")

    def test_pre_v3_2_dashboard_bundles_still_validate(self) -> None:
        """Existing fixtures carry no decision field and must keep validating."""
        for name in ("dashboard_v2.json", "dashboard_v2_no_sessions.json"):
            data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
            self.assertEqual(validate_dashboard_v2(data), [], name)
            self.assertNotIn("decision", json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
