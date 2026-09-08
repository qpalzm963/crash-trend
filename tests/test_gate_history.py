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
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crash_trend.alerts.dispatcher import evaluate_alert_decision
from crash_trend.alerts.policy import AlertPolicy
from crash_trend.alerts.state import AlertDeliveryStore
from crash_trend.dashboard.releases import get_releases_js
from crash_trend.gate.artifact import ReleaseGateArtifact
from crash_trend.gate.history import (
    GateSnapshot,
    ReleaseGateHistoryStore,
    classify_gate_transition,
    compute_evaluation_key,
    compute_policy_identity,
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

    def test_concurrent_recording_atomic_idempotency(self) -> None:
        """Verifies concurrent writes to same evaluation_key do not crash and result in exactly 1 record."""
        with tempfile.TemporaryDirectory() as tmpdir:
            file_db = Path(tmpdir) / "concurrent.sqlite3"
            shared_store = ReleaseGateHistoryStore(file_db)

            snap = GateSnapshot(
                app_id="race_app",
                platform="ios",
                version="3.0.0",
                previous_version="2.9.0",
                gate_status="fail",
                sample_sufficient=True,
                policy_version="1.0",
                policy_identity="hash123",
                comparison_window="30d",
                evaluated_at="2026-09-08T12:00:00Z",
                evaluation_key="",
                triggered_reasons=["crash_spike"],
                rule_results=[],
                normalized_metrics={},
                summary="Race test",
            )

            results: list[tuple[GateSnapshot, bool]] = []
            errors: list[Exception] = []

            def worker() -> None:
                try:
                    thread_store = ReleaseGateHistoryStore(file_db)
                    res = thread_store.record_snapshot(snap)
                    results.append(res)
                    thread_store.close()
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(errors), 0, f"Concurrent workers raised exceptions: {errors}")
            self.assertEqual(len(results), 10)

            # Exactly one worker reports is_new=True
            new_counts = sum(1 for _, is_new in results if is_new)
            self.assertEqual(new_counts, 1)

            # Database has exactly 1 row
            conn = shared_store._connect()
            cur = conn.execute("SELECT COUNT(*) as cnt FROM release_gate_snapshots WHERE app_id='race_app'")
            self.assertEqual(cur.fetchone()["cnt"], 1)
            shared_store.close()

    def test_policy_identity_affects_evaluation_key(self) -> None:
        """Verifies policy changes alter policy_identity and allow distinct evaluation_key."""
        pol1 = {"rules": [{"metric_name": "crash_rate_pct", "threshold": 20}]}
        pol2 = {"rules": [{"metric_name": "crash_rate_pct", "threshold": 50}]}

        id1 = compute_policy_identity(pol1, "1.0")
        id2 = compute_policy_identity(pol2, "1.0")
        self.assertNotEqual(id1, id2)

        k1 = compute_evaluation_key("demo", "android", "1.0.0", "2026-09-08T10:00:00Z", "1.0", id1)
        k2 = compute_evaluation_key("demo", "android", "1.0.0", "2026-09-08T10:00:00Z", "1.0", id2)
        self.assertNotEqual(k1, k2)

        snap1 = GateSnapshot(
            app_id="demo", platform="android", version="1.0.0", previous_version=None,
            gate_status="fail", sample_sufficient=True, policy_version="1.0", policy_identity=id1,
            comparison_window="30d", evaluated_at="2026-09-08T10:00:00Z", evaluation_key=k1,
            triggered_reasons=[], rule_results=[], normalized_metrics={}, summary="Fail under pol1",
        )
        snap2 = GateSnapshot(
            app_id="demo", platform="android", version="1.0.0", previous_version=None,
            gate_status="pass", sample_sufficient=True, policy_version="1.0", policy_identity=id2,
            comparison_window="30d", evaluated_at="2026-09-08T10:00:00Z", evaluation_key=k2,
            triggered_reasons=[], rule_results=[], normalized_metrics={}, summary="Pass under pol2",
        )

        _, is_new1 = self.store.record_snapshot(snap1)
        _, is_new2 = self.store.record_snapshot(snap2)
        self.assertTrue(is_new1)
        self.assertTrue(is_new2)

        history = self.store.get_release_gate_history("demo", "android", "1.0.0")
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].policy_identity, id1)
        self.assertEqual(history[1].policy_identity, id2)

    def test_prune_snapshots_preserves_latest_invariant(self) -> None:
        """Verifies older snapshots are pruned, but the latest evaluation snapshot is NEVER pruned."""
        app = "prune_app"
        pf = "ios"

        # Release 1.0.0 has 3 evaluations from 2020
        for day in ("01", "02", "03"):
            self.store.record_snapshot(GateSnapshot(
                app_id=app, platform=pf, version="1.0.0", previous_version=None,
                gate_status="fail" if day != "03" else "pass", sample_sufficient=True,
                policy_version="1.0", policy_identity="", comparison_window="30d",
                evaluated_at=f"2020-01-{day}T10:00:00Z", evaluation_key="",
                triggered_reasons=[], rule_results=[], normalized_metrics={}, summary=f"Day {day}",
            ))

        # Release 2.0.0 has 1 evaluation from 2020
        self.store.record_snapshot(GateSnapshot(
            app_id=app, platform=pf, version="2.0.0", previous_version="1.0.0",
            gate_status="warn", sample_sufficient=True,
            policy_version="1.0", policy_identity="", comparison_window="30d",
            evaluated_at="2020-01-01T10:00:00Z", evaluation_key="",
            triggered_reasons=[], rule_results=[], normalized_metrics={}, summary="Only eval",
        ))

        # Release 3.0.0 has 1 evaluation from 2026
        self.store.record_snapshot(GateSnapshot(
            app_id=app, platform=pf, version="3.0.0", previous_version="2.0.0",
            gate_status="pass", sample_sufficient=True,
            policy_version="1.0", policy_identity="", comparison_window="30d",
            evaluated_at="2026-09-08T10:00:00Z", evaluation_key="",
            triggered_reasons=[], rule_results=[], normalized_metrics={}, summary="Recent eval",
        ))

        # Prune older than 2025-01-01
        deleted = self.store.prune_snapshots(app, before="2025-01-01T00:00:00Z")
        self.assertEqual(deleted, 2)  # The first two evaluations of 1.0.0 (day 01 and 02)

        # Confirm 1.0.0 latest (day 03) is preserved
        h1 = self.store.get_release_gate_history(app, pf, "1.0.0")
        self.assertEqual(len(h1), 1)
        self.assertEqual(h1[0].evaluated_at, "2020-01-03T10:00:00Z")
        self.assertEqual(h1[0].gate_status, "pass")

        # Confirm 2.0.0 single snapshot is preserved despite being from 2020
        h2 = self.store.get_release_gate_history(app, pf, "2.0.0")
        self.assertEqual(len(h2), 1)
        self.assertEqual(h2[0].gate_status, "warn")

        # Confirm 3.0.0 is preserved
        h3 = self.store.get_release_gate_history(app, pf, "3.0.0")
        self.assertEqual(len(h3), 1)

    def test_prune_preserves_latest_even_when_inserted_out_of_order(self) -> None:
        """Verifies regression fix: out-of-order pipeline inserts preserve true authoritative latest evaluation.

        If a newer evaluation (10:05) is inserted first (smaller id), and a delayed older evaluation (10:00)
        is inserted later (larger id), pruning older records MUST preserve the true latest evaluation (10:05),
        NOT the row with MAX(id).
        """
        app = "out_of_order_app"
        pf = "android"
        ver = "4.0.0"

        # 1. Insert newer evaluation first (10:05 UTC) -> gets smaller id
        snap_newer = GateSnapshot(
            app_id=app,
            platform=pf,
            version=ver,
            previous_version=None,
            gate_status="pass",
            sample_sufficient=True,
            policy_version="1.0",
            policy_identity="",
            comparison_window="30d",
            evaluated_at="2020-01-01T10:05:00Z",
            evaluation_key="",
            triggered_reasons=[],
            rule_results=[],
            normalized_metrics={},
            summary="Newer evaluation (10:05)",
        )
        saved_newer, _ = self.store.record_snapshot(snap_newer)

        # 2. Insert delayed older evaluation later (10:00 UTC) -> gets larger id
        snap_older = GateSnapshot(
            app_id=app,
            platform=pf,
            version=ver,
            previous_version=None,
            gate_status="fail",
            sample_sufficient=True,
            policy_version="1.0",
            policy_identity="",
            comparison_window="30d",
            evaluated_at="2020-01-01T10:00:00Z",
            evaluation_key="",
            triggered_reasons=["crash_rate"],
            rule_results=[],
            normalized_metrics={},
            summary="Delayed older evaluation (10:00)",
        )
        saved_older, _ = self.store.record_snapshot(snap_older)

        # Verify that older evaluation has larger id than newer evaluation
        self.assertIsNotNone(saved_newer.id)
        self.assertIsNotNone(saved_older.id)
        self.assertGreater(saved_older.id, saved_newer.id)

        # Confirm authoritative latest before pruning is the newer one (10:05, pass)
        latest_before = self.store.get_latest_release_gate_state(app, pf, ver)
        self.assertIsNotNone(latest_before)
        self.assertEqual(latest_before.evaluated_at, "2020-01-01T10:05:00Z")
        self.assertEqual(latest_before.gate_status, "pass")

        # 3. Execute pruning before 2025-01-01
        deleted = self.store.prune_snapshots(app, before="2025-01-01T00:00:00Z")
        self.assertEqual(deleted, 1)

        # 4. Invariant check: The remaining record MUST be the authoritative latest (10:05, pass),
        # NOT the one with MAX(id) (10:00, fail)
        history = self.store.get_release_gate_history(app, pf, ver)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].id, saved_newer.id)
        self.assertEqual(history[0].evaluated_at, "2020-01-01T10:05:00Z")
        self.assertEqual(history[0].gate_status, "pass")

        latest_after = self.store.get_latest_release_gate_state(app, pf, ver)
        self.assertIsNotNone(latest_after)
        self.assertEqual(latest_after.evaluated_at, "2020-01-01T10:05:00Z")
        self.assertEqual(latest_after.gate_status, "pass")

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

    def test_recovery_fallback_when_history_store_is_empty_or_new(self) -> None:
        """Verifies regression fix: if history store is newly created/empty, fallback to delivery store sends recovery."""
        app = "chat_app"
        pf = "ios"
        ver = "2.1.0"

        # 1. Delivery store recorded a prior FAIL alert
        rec_id = self.delivery_store.record_attempt(
            app_id=app,
            platform=pf,
            version=ver,
            provider="google_chat",
            alert_fingerprint="fp-fail-210",
            gate_status="fail",
            attempted_at="2026-09-08T08:00:00Z",
            reasons=["crash_rate_pct"],
        )
        self.delivery_store.update_result(rec_id, status="sent", delivered_at="2026-09-08T08:00:01Z", attempt_count=1)

        # 2. History store is completely empty (no evaluations for 2.1.0)
        self.assertEqual(len(self.history_store.get_release_gate_history(app, pf, ver)), 0)

        # 3. New evaluation is PASS -> Must send recovery alert via fallback to delivery store
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
        self.assertIn("recovered to pass after previous fail alert", decision.reason.lower())

    def test_recovery_suppressed_when_history_confirms_prior_was_not_failure(self) -> None:
        """Verifies recovery is suppressed if Gate history authority confirms prior evaluation was already pass."""
        app = "chat_app"
        pf = "android"
        ver = "2.2.0"

        # Gate history confirms prior was PASS
        self.history_store.record_snapshot(GateSnapshot(
            app_id=app, platform=pf, version=ver, previous_version=None,
            gate_status="pass", sample_sufficient=True, policy_version="1.0",
            comparison_window="30d", evaluated_at="2026-09-08T08:00:00Z", evaluation_key="",
            triggered_reasons=[], rule_results=[], normalized_metrics={}, summary="All pass",
        ))

        # Current evaluation is still PASS
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

        self.assertEqual(decision.decision, "suppressed")


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
                    "policy_version": "1.0",
                    "policy_identity": "abcdef123456",
                    "comparison_window": "30d",
                },
            ],
        }

        errors: list[str] = []
        validate_release_catalog([catalog_item], errors)
        self.assertEqual(errors, [])

    def test_dashboard_js_contains_timeline_badges(self) -> None:
        """Verifies release dashboard JavaScript includes visual metadata badges in timeline."""
        js_code = get_releases_js()
        self.assertIn("視窗:", js_code)
        self.assertIn("政策: v", js_code)
        self.assertIn("comparison_window", js_code)
        self.assertIn("policy_identity", js_code)


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

    def test_cli_prune_older_than_days(self) -> None:
        """Verifies CLI --prune-older-than-days operates correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test_hist.sqlite3"
            store = ReleaseGateHistoryStore(db_path)
            store.record_snapshot(GateSnapshot(
                app_id="cli_app", platform="android", version="1.0.0", previous_version=None,
                gate_status="fail", sample_sufficient=True, policy_version="1.0",
                policy_identity="", comparison_window="30d", evaluated_at="2020-01-01T10:00:00Z",
                evaluation_key="", triggered_reasons=[], rule_results=[], normalized_metrics={}, summary="Old",
            ))
            store.record_snapshot(GateSnapshot(
                app_id="cli_app", platform="android", version="1.0.0", previous_version=None,
                gate_status="pass", sample_sufficient=True, policy_version="1.0",
                policy_identity="", comparison_window="30d", evaluated_at="2020-01-02T10:00:00Z",
                evaluation_key="", triggered_reasons=[], rule_results=[], normalized_metrics={}, summary="Latest",
            ))
            store.close()

            captured = io.StringIO()
            with patch("sys.stdout", captured):
                with patch("sys.argv", ["gate.history", "--app", "cli_app", "--prune-older-than-days", "30", "--db", str(db_path)]):
                    with self.assertRaises(SystemExit) as cm:
                        history_cli_main()
                    self.assertEqual(cm.exception.code, 0)

            out = captured.getvalue()
            self.assertIn("已清理 30 天以前的歷史評估快照", out)
            self.assertIn("共刪除 1 筆", out)


if __name__ == "__main__":
    unittest.main()
