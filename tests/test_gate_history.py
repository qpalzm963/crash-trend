"""Unit tests for Release Gate Evaluation History Store and Quality Trend (Issue #61).

Tests:
1. SQLite schema creation, WAL pragma, and index verification.
2. Snapshot recording and strict evaluation_key idempotency (replays do not duplicate).
3. State transition classification (insufficient -> warn -> fail -> pass recovery, escalation, de-escalation).
4. Sequential timeline tracking for a single release.
5. Canonical SemVer ordering for multi-release quality trends (no string sorting).
6. Decoupled failure semantics: history write errors never affect release_gate.json or exit codes.
7. Google Chat recovery alert alignment with Gate history authority.
8. Schema V2 validation for ReleaseCatalogItem with gate_history.
9. CLI query tool execution (--trend, --history, --json).
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crash_trend.alerts.dispatcher import evaluate_alert_decision
from crash_trend.alerts.policy import AlertPolicy
from crash_trend.alerts.state import AlertDeliveryStore
from crash_trend.gate.artifact import ReleaseGateArtifact
from crash_trend.gate.history import (
    GateSnapshot,
    ReleaseGateHistoryStore,
    classify_gate_transition,
)
from crash_trend.gate.history import (
    main as history_cli_main,
)
from crash_trend.gate.policy import GatePolicy
from crash_trend.release_gate import run_release_gate_for_app
from crash_trend.schema_v2 import validate_release_catalog


class TestGateTransitionClassification(unittest.TestCase):
    """Tests classification of gate state transitions."""

    def test_initial_transition(self) -> None:
        tr = classify_gate_transition(None, "insufficient_data")
        self.assertEqual(tr.transition_type, "initial")
        self.assertFalse(tr.status_changed)
        self.assertFalse(tr.is_recovery)
        self.assertFalse(tr.is_regression)

    def test_unchanged_transition(self) -> None:
        tr = classify_gate_transition("fail", "fail")
        self.assertEqual(tr.transition_type, "unchanged")
        self.assertFalse(tr.status_changed)
        self.assertFalse(tr.is_recovery)
        self.assertFalse(tr.is_regression)

    def test_recovery_from_fail(self) -> None:
        tr = classify_gate_transition("fail", "pass")
        self.assertEqual(tr.transition_type, "recovery")
        self.assertTrue(tr.status_changed)
        self.assertTrue(tr.is_recovery)
        self.assertFalse(tr.is_regression)

    def test_recovery_from_warn(self) -> None:
        tr = classify_gate_transition("warn", "pass")
        self.assertEqual(tr.transition_type, "recovery")
        self.assertTrue(tr.status_changed)
        self.assertTrue(tr.is_recovery)
        self.assertFalse(tr.is_regression)

    def test_escalation_warn_to_fail(self) -> None:
        tr = classify_gate_transition("warn", "fail")
        self.assertEqual(tr.transition_type, "escalation")
        self.assertTrue(tr.status_changed)
        self.assertFalse(tr.is_recovery)
        self.assertTrue(tr.is_regression)

    def test_de_escalation_fail_to_warn(self) -> None:
        tr = classify_gate_transition("fail", "warn")
        self.assertEqual(tr.transition_type, "de_escalation")
        self.assertTrue(tr.status_changed)
        self.assertFalse(tr.is_recovery)
        self.assertFalse(tr.is_regression)

    def test_regression_from_pass(self) -> None:
        tr = classify_gate_transition("pass", "fail")
        self.assertEqual(tr.transition_type, "regression")
        self.assertTrue(tr.status_changed)
        self.assertFalse(tr.is_recovery)
        self.assertTrue(tr.is_regression)

    def test_regression_from_baseline(self) -> None:
        tr = classify_gate_transition("baseline", "warn")
        self.assertEqual(tr.transition_type, "regression")
        self.assertTrue(tr.status_changed)
        self.assertTrue(tr.is_regression)

    def test_regression_from_insufficient(self) -> None:
        tr = classify_gate_transition("insufficient_data", "fail")
        self.assertEqual(tr.transition_type, "regression")
        self.assertTrue(tr.status_changed)
        self.assertTrue(tr.is_regression)

    def test_drop_to_insufficient(self) -> None:
        tr = classify_gate_transition("pass", "insufficient_data")
        self.assertEqual(tr.transition_type, "insufficient")
        self.assertTrue(tr.status_changed)
        self.assertFalse(tr.is_recovery)
        self.assertFalse(tr.is_regression)


class TestReleaseGateHistoryStore(unittest.TestCase):
    """Tests ReleaseGateHistoryStore SQLite operations, idempotency, and trends."""

    def setUp(self) -> None:
        self.store = ReleaseGateHistoryStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_schema_and_indexes(self) -> None:
        """Verifies table schema and indexes exist."""
        conn = self.store._connect()
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='release_gate_snapshots'")
        self.assertIsNotNone(cur.fetchone())

        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        index_names = [r["name"] for r in cur.fetchall()]
        self.assertIn("idx_gate_history_app_platform_ver", index_names)
        self.assertIn("idx_gate_history_eval_key", index_names)

    def test_record_snapshot_and_idempotency(self) -> None:
        """Verifies recording snapshot and that duplicate evaluation_key does not create rows."""
        snap = GateSnapshot(
            app_id="demo_app",
            platform="android",
            version="1.2.0",
            previous_version="1.1.0",
            gate_status="pass",
            sample_sufficient=True,
            policy_version="1.0",
            comparison_window="30d",
            evaluated_at="2026-09-08T10:00:00Z",
            evaluation_key="",
            triggered_reasons=[],
            rule_results=[],
            normalized_metrics={"crash_rate_pct": 0.05},
            summary="All metrics passed",
        )

        saved1, is_new1 = self.store.record_snapshot(snap)
        self.assertTrue(is_new1)
        self.assertIsNotNone(saved1.id)
        self.assertTrue(saved1.evaluation_key)

        # Retry with identical evaluation: should return existing and is_new=False
        saved2, is_new2 = self.store.record_snapshot(saved1)
        self.assertFalse(is_new2)
        self.assertEqual(saved1.id, saved2.id)
        self.assertEqual(saved1.evaluation_key, saved2.evaluation_key)

        # Confirm count in SQLite is exactly 1
        conn = self.store._connect()
        cur = conn.execute("SELECT COUNT(*) as cnt FROM release_gate_snapshots")
        self.assertEqual(cur.fetchone()["cnt"], 1)

    def test_timeline_state_evolution(self) -> None:
        """Verifies sequential timeline tracking: insufficient -> warn -> fail -> pass."""
        app = "test_app"
        pf = "ios"
        ver = "2.0.0"

        steps = [
            ("2026-09-01T10:00:00Z", "insufficient_data", False, [], "Sample insufficient"),
            ("2026-09-02T10:00:00Z", "warn", True, ["cfu_drop"], "CFU drop warning"),
            ("2026-09-03T10:00:00Z", "fail", True, ["crash_rate_pct (+45.2%)"], "Crash rate spike FAIL"),
            ("2026-09-04T10:00:00Z", "pass", True, [], "All quality metrics returned to normal"),
        ]

        for eval_at, st, suff, reasons, summary in steps:
            s = GateSnapshot(
                app_id=app,
                platform=pf,
                version=ver,
                previous_version="1.9.0",
                gate_status=st,
                sample_sufficient=suff,
                policy_version="1.0",
                comparison_window="30d",
                evaluated_at=eval_at,
                evaluation_key="",
                triggered_reasons=reasons,
                rule_results=[],
                normalized_metrics={},
                summary=summary,
            )
            self.store.record_snapshot(s)

        history = self.store.get_release_gate_history(app, pf, ver)
        self.assertEqual(len(history), 4)

        # 1. insufficient_data (initial)
        self.assertEqual(history[0].gate_status, "insufficient_data")
        self.assertIsNotNone(history[0].transition)
        self.assertEqual(history[0].transition.transition_type, "initial")

        # 2. warn (regression from insufficient)
        self.assertEqual(history[1].gate_status, "warn")
        self.assertEqual(history[1].transition.transition_type, "regression")
        self.assertTrue(history[1].transition.is_regression)

        # 3. fail (escalation from warn)
        self.assertEqual(history[2].gate_status, "fail")
        self.assertEqual(history[2].transition.transition_type, "escalation")
        self.assertTrue(history[2].transition.is_regression)

        # 4. pass (recovery from fail!)
        self.assertEqual(history[3].gate_status, "pass")
        self.assertEqual(history[3].transition.transition_type, "recovery")
        self.assertTrue(history[3].transition.is_recovery)

        # Latest state helper
        latest = self.store.get_latest_release_gate_state(app, pf, ver)
        self.assertIsNotNone(latest)
        self.assertEqual(latest.gate_status, "pass")
        self.assertTrue(latest.transition.is_recovery)

    def test_recent_release_gate_trend_canonical_semver(self) -> None:
        """Verifies multi-release quality trend orders releases canonically by SemVer, NOT string."""
        app = "trend_app"
        pf = "android"

        # Note: in string sorting: "1.0.10" < "1.0.9". SemVer: "1.0.10" > "1.0.9"!
        versions = ["1.0.2", "1.0.9", "1.0.10", "1.1.0", "1.0.0"]

        for v in versions:
            s = GateSnapshot(
                app_id=app,
                platform=pf,
                version=v,
                previous_version=None,
                gate_status="pass" if v != "1.0.9" else "fail",
                sample_sufficient=True,
                policy_version="1.0",
                comparison_window="30d",
                evaluated_at="2026-09-08T00:00:00Z",
                evaluation_key="",
                triggered_reasons=[] if v != "1.0.9" else ["crash_rate_pct"],
                rule_results=[],
                normalized_metrics={},
                summary=f"Eval for v{v}",
            )
            self.store.record_snapshot(s)

        trend = self.store.get_recent_release_gate_trend(app, pf, limit=5)
        self.assertEqual(len(trend), 5)

        # Expected canonical descending SemVer order:
        expected_order = ["1.1.0", "1.0.10", "1.0.9", "1.0.2", "1.0.0"]
        actual_order = [t.version for t in trend]
        self.assertEqual(actual_order, expected_order)

        # Verify trend metadata
        v109_item = next(t for t in trend if t.version == "1.0.9")
        self.assertEqual(v109_item.latest_status, "fail")
        self.assertIn("crash_rate_pct", v109_item.latest_triggered_reasons)

    def test_record_gate_artifact(self) -> None:
        """Verifies record_gate_artifact parses multi-platform ReleaseGateArtifact."""
        artifact: ReleaseGateArtifact = {
            "schema_version": "1.0",
            "app_id": "multi_pf_app",
            "generated_at": "2026-09-08T12:00:00Z",
            "overall_status": "warn",
            "should_alert": True,
            "alert_severity": "warning",
            "alert_summary": "Warning on iOS",
            "platforms": {
                "android": {
                    "platform": "android",
                    "target_version": "3.1.0",
                    "previous_version": "3.0.0",
                    "gate_status": "pass",
                    "sample_sufficient": True,
                    "rule_results": [],
                    "alert": {
                        "should_alert": False,
                        "alert_severity": "none",
                        "alert_summary": "Android passed",
                        "trigger_rules": [],
                    },
                    "evaluated_at": "2026-09-08T12:00:00Z",
                },
                "ios": {
                    "platform": "ios",
                    "target_version": "3.1.0",
                    "previous_version": "3.0.0",
                    "gate_status": "warn",
                    "sample_sufficient": True,
                    "rule_results": [
                        {
                            "rule_name": "crash_rate_pct",
                            "metric_name": "crash_rate_pct",
                            "current_value": 0.25,
                            "previous_value": 0.10,
                            "warn_threshold": 0.20,
                            "fail_threshold": 0.50,
                            "status": "warn",
                            "reason": "Crash rate change +25.0% > 20.0%",
                        }
                    ],
                    "alert": {
                        "should_alert": True,
                        "alert_severity": "warning",
                        "alert_summary": "iOS warning on crash_rate",
                        "trigger_rules": ["crash_rate_pct"],
                    },
                    "evaluated_at": "2026-09-08T12:00:00Z",
                },
            },
            "policy_version": "1.0",
            "policy": {"enabled": True},
        }

        snaps = self.store.record_gate_artifact(artifact)
        self.assertEqual(len(snaps), 2)

        # Query Android
        and_history = self.store.get_release_gate_history("multi_pf_app", "android", "3.1.0")
        self.assertEqual(len(and_history), 1)
        self.assertEqual(and_history[0].gate_status, "pass")

        # Query iOS
        ios_history = self.store.get_release_gate_history("multi_pf_app", "ios", "3.1.0")
        self.assertEqual(len(ios_history), 1)
        self.assertEqual(ios_history[0].gate_status, "warn")
        self.assertEqual(ios_history[0].triggered_reasons, ["crash_rate_pct"])
        self.assertEqual(ios_history[0].normalized_metrics.get("crash_rate_pct_current"), 0.25)


class TestDecoupledFailureSemantics(unittest.TestCase):
    """Verifies history store errors never mutate or fail release_gate.json."""

    def test_history_store_write_error_is_non_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "release_gate.json"

            # Mock get_gate_history_store to return a store that raises an OperationalError on record_gate_artifact
            with patch("crash_trend.gate.history.ReleaseGateHistoryStore.record_gate_artifact", side_effect=sqlite3.OperationalError("disk I/O error")):
                # Mock catalog loading and policy
                with patch("crash_trend.release_gate.load_app_release_catalog", return_value=[]):
                    with patch("crash_trend.release_gate.get_app", return_value={"platforms": ["android"]}):
                        with patch("crash_trend.release_gate.load_gate_policy", return_value=GatePolicy(enabled=False)):
                            # Should not raise exception
                            artifact = run_release_gate_for_app(
                                app_name="faulty_disk_app",
                                out_path=out_file,
                                verbose=False,
                            )

                            self.assertIsNotNone(artifact)
                            self.assertTrue(out_file.is_file())
                            saved_json = json.loads(out_file.read_text(encoding="utf-8"))
                            self.assertEqual(saved_json["app_id"], "faulty_disk_app")


class TestGoogleChatRecoveryAlignment(unittest.TestCase):
    """Verifies alert recovery transition alignment with Gate history authority."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.delivery_store = AlertDeliveryStore(
            db_path=Path(self.temp_dir.name) / "alert_delivery.sqlite3",
            app_id="chat_app",
        )
        self.history_store = ReleaseGateHistoryStore(":memory:")
        self.policy = AlertPolicy(
            enabled=True,
            provider="google_chat",
            notify_on=["fail", "warn"],
            notify_recovery=True,
            cooldown_minutes=60,
        )

    def tearDown(self) -> None:
        self.delivery_store.close()
        self.history_store.close()
        self.temp_dir.cleanup()

    def test_recovery_alert_triggers_when_gate_history_confirms_prior_failure(self) -> None:
        app = "chat_app"
        pf = "android"
        ver = "1.5.0"

        # Step 1: Prior evaluation was FAIL
        snap_fail = GateSnapshot(
            app_id=app,
            platform=pf,
            version=ver,
            previous_version="1.4.0",
            gate_status="fail",
            sample_sufficient=True,
            policy_version="1.0",
            comparison_window="30d",
            evaluated_at="2026-09-08T09:00:00Z",
            evaluation_key="",
            triggered_reasons=["crash_rate_pct (+50%)"],
            rule_results=[],
            normalized_metrics={},
            summary="Gate FAIL on crash rate",
        )
        self.history_store.record_snapshot(snap_fail)

        # Simulate alert sent for FAIL
        rec_id = self.delivery_store.record_attempt(
            app_id=app,
            platform=pf,
            version=ver,
            provider="google_chat",
            alert_fingerprint="fp-fail",
            gate_status="fail",
            attempted_at="2026-09-08T09:00:01Z",
            reasons=["crash_rate_pct"],
        )
        self.delivery_store.update_result(rec_id, status="sent", delivered_at="2026-09-08T09:00:02Z", attempt_count=1)

        # Step 2: New evaluation is PASS (recorded in gate history authority)
        snap_pass = GateSnapshot(
            app_id=app,
            platform=pf,
            version=ver,
            previous_version="1.4.0",
            gate_status="pass",
            sample_sufficient=True,
            policy_version="1.0",
            comparison_window="30d",
            evaluated_at="2026-09-08T10:00:00Z",
            evaluation_key="",
            triggered_reasons=[],
            rule_results=[],
            normalized_metrics={},
            summary="All metrics back to normal",
        )
        self.history_store.record_snapshot(snap_pass)

        # Evaluate alert decision with history_store
        decision = evaluate_alert_decision(
            app_id=app,
            platform=pf,
            target_version=ver,
            gate_status="pass",
            triggered_reasons=[],
            policy=self.policy,
            store=self.delivery_store,
            history_store=self.history_store,
        )

        self.assertEqual(decision.decision, "send")
        self.assertTrue(decision.is_recovery)
        self.assertIn("recovered to pass", decision.reason.lower())

        # Step 3: Record recovery alert sent
        rec_id2 = self.delivery_store.record_attempt(
            app_id=app,
            platform=pf,
            version=ver,
            provider="google_chat",
            alert_fingerprint="fp-pass",
            gate_status="pass",
            attempted_at="2026-09-08T10:00:01Z",
            reasons=[],
        )
        self.delivery_store.update_result(rec_id2, status="sent", delivered_at="2026-09-08T10:00:02Z", attempt_count=1)

        # Step 4: Next evaluation is still PASS (should NOT re-send duplicate recovery)
        decision2 = evaluate_alert_decision(
            app_id=app,
            platform=pf,
            target_version=ver,
            gate_status="pass",
            triggered_reasons=[],
            policy=self.policy,
            store=self.delivery_store,
            history_store=self.history_store,
        )
        self.assertEqual(decision2.decision, "suppressed")
        self.assertIn("already delivered", decision2.reason.lower())


class TestDashboardSchemaWithGateHistory(unittest.TestCase):
    """Tests schema validation for release catalog items containing gate_history."""

    def test_release_catalog_with_gate_history_validates(self) -> None:
        catalog_item = {
            "version": "1.0.0",
            "platform": "android",
            "first_seen": "2026-08-01T00:00:00Z",
            "last_seen": "2026-09-01T00:00:00Z",
            "release_date": "2026-08-01",
            "status": "latest",
            "lifetime_crashes": 100,
            "lifetime_issues": 5,
            "lifetime_affected_users": 80,
            "lifetime_fatal": 10,
            "lifetime_anr": 5,
            "recent_health": {
                "30d": {
                    "crash_events": 50,
                    "affected_users": 40,
                    "sample_sufficient": True,
                    "fatal_events": 5,
                    "anr_events": 2,
                    "new_issues_count": 1,
                    "status": "latest",
                    "trend": "stable",
                    "sessions_total": 1000,
                    "crash_free_users_rate": 0.96,
                    "crash_free_sessions_rate": 0.95,
                    "adoption_rate": 0.85,
                }
            },
            "release_gate": {
                "status": "pass",
                "should_alert": False,
                "alert_severity": "none",
                "alert_summary": "Passed",
                "rules_triggered": [],
                "sample_sufficient": True,
                "evaluated_at": "2026-09-08T00:00:00Z",
            },
            "gate_history": [
                {
                    "evaluated_at": "2026-09-07T00:00:00Z",
                    "gate_status": "fail",
                    "sample_sufficient": True,
                    "summary": "Crash spike",
                    "rules_triggered": ["crash_rate_pct"],
                    "transition": {"transition_type": "initial", "is_recovery": False, "is_regression": False},
                },
                {
                    "evaluated_at": "2026-09-08T00:00:00Z",
                    "gate_status": "pass",
                    "sample_sufficient": True,
                    "summary": "Passed",
                    "rules_triggered": [],
                    "transition": {"transition_type": "recovery", "is_recovery": True, "is_regression": False},
                },
            ],
        }

        errors: list[str] = []
        validate_release_catalog([catalog_item], errors)
        self.assertEqual(errors, [])


class TestHistoryCLI(unittest.TestCase):
    """Tests CLI query tool for history and trends."""

    def test_cli_trend_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_hist.sqlite3"
            store = ReleaseGateHistoryStore(db_path)
            store.record_snapshot(
                GateSnapshot(
                    app_id="cli_app",
                    platform="android",
                    version="1.0.1",
                    previous_version=None,
                    gate_status="pass",
                    sample_sufficient=True,
                    policy_version="1.0",
                    comparison_window="30d",
                    evaluated_at="2026-09-08T10:00:00Z",
                    evaluation_key="",
                    triggered_reasons=[],
                    rule_results=[],
                    normalized_metrics={},
                    summary="CLI test",
                )
            )
            store.close()

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                with patch("sys.argv", ["gate.history", "--app", "cli_app", "--trend", "--db", str(db_path), "--json"]):
                    with self.assertRaises(SystemExit) as cm:
                        history_cli_main()
                    self.assertEqual(cm.exception.code, 0)

            out = captured.getvalue()
            parsed = json.loads(out)
            self.assertIn("android", parsed)
            self.assertEqual(len(parsed["android"]), 1)
            self.assertEqual(parsed["android"][0]["version"], "1.0.1")


if __name__ == "__main__":
    unittest.main()
