"""Unit tests for Alert Delivery Observability, Health Metrics, and Dashboard Audit (Issue #63).

Tests:
1. Data projection: zero secrets, URL/token scrubbing, suppression reason mapping, recovery detection.
2. Query APIs: ordering, app/platform/version isolation, limit handling.
3. Deterministic health semantics: no_data, healthy, degraded, dry-run exclusion, suppression resilience, 24h counters.
4. Store resilience: missing database file, corrupted SQLite file.
5. CLI audit queries: human-readable table, JSON output, platform/version filtering.
6. Schema V2 integration and validation: AlertDeliveryAppData, ReleaseCatalogItem.alert_deliveries.
7. Dashboard UI integration: #view-notifications HTML/JS, Release Detail modal Alert Delivery Timeline.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crash_trend.alerts.__main__ import main as alerts_cli_main
from crash_trend.alerts.models import DeliveryRecord
from crash_trend.alerts.observability import (
    get_alert_observability_bundle,
    get_latest_release_delivery,
    get_recent_alert_deliveries,
    get_release_alert_history,
    record_to_delivery_item,
    summarize_alert_delivery_health,
)
from crash_trend.alerts.state import AlertDeliveryStore
from crash_trend.dashboard.releases import get_releases_js
from crash_trend.dashboard.sources import get_sources_html, get_sources_js
from crash_trend.schema_v2 import (
    validate_alert_delivery,
    validate_app_dashboard_v2,
    validate_release_catalog,
)


def _insert_record(
    store: AlertDeliveryStore,
    app_id: str,
    platform: str,
    version: str,
    status: str,
    gate_status: str,
    attempted_at: str,
    delivered_at: str | None = None,
    attempt_count: int = 1,
    http_status: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    dry_run: bool = False,
    reasons: list[str] | None = None,
) -> int:
    if status == "suppressed":
        return store.record_suppressed(
            app_id=app_id,
            platform=platform,
            version=version,
            provider="google_chat",
            alert_fingerprint=f"fp_{platform}_{version}",
            gate_status=gate_status,
            reason_text=error_message or "in cooldown",
            dry_run=dry_run,
            attempted_at=attempted_at,
            reasons=reasons,
        )
    rid = store.record_attempt(
        app_id=app_id,
        platform=platform,
        version=version,
        provider="google_chat",
        alert_fingerprint=f"fp_{platform}_{version}",
        gate_status=gate_status,
        attempted_at=attempted_at,
        dry_run=dry_run,
        reasons=reasons,
    )
    store.update_result(
        record_id=rid,
        status=status,
        delivered_at=delivered_at,
        attempt_count=attempt_count,
        http_status=http_status,
        error_code=error_code,
        error_message=error_message,
    )
    return rid


class TestAlertDeliveryProjection(unittest.TestCase):
    """Tests projection of raw DeliveryRecord into safe AlertDeliveryItem."""

    def test_record_to_delivery_item_sanitization(self) -> None:
        rec = DeliveryRecord(
            id=1,
            app_id="app1",
            platform="android",
            version="1.0.0",
            provider="google_chat",
            alert_fingerprint="fp1",
            status="failed",
            attempt_count=2,
            attempted_at="2026-09-08T10:00:00Z",
            delivered_at=None,
            http_status=500,
            error_code="HTTP_500",
            error_message="POST to https://chat.googleapis.com/v1/spaces/AAA/messages?key=AIzaSySecretToken&token=SecretBearer failed",
            thread_key="spaces/AAA/threads/BBB?key=AIzaSySecretToken",
            message_name="spaces/AAA/messages/CCC",
            gate_status="fail",
            reasons=["crash_free_users_drop"],
            dry_run=False,
        )
        item = record_to_delivery_item(rec)
        self.assertEqual(item.id, 1)
        self.assertEqual(item.status, "failed")
        self.assertEqual(item.gate_status, "fail")
        self.assertFalse(item.is_recovery)
        self.assertFalse(item.dry_run)
        # Secret tokens scrubbed
        self.assertNotIn("AIzaSySecretToken", str(item.error_message))
        self.assertNotIn("SecretBearer", str(item.error_message))
        self.assertNotIn("AIzaSySecretToken", str(item.thread_key))
        self.assertIn("<redacted>", str(item.error_message))
        self.assertIn("key=<redacted>", str(item.thread_key))

    def test_record_to_delivery_item_suppression(self) -> None:
        rec = DeliveryRecord(
            id=2,
            app_id="app1",
            platform="ios",
            version="2.0.0",
            provider="google_chat",
            alert_fingerprint="fp2",
            status="suppressed",
            attempt_count=0,
            attempted_at="2026-09-08T11:00:00Z",
            delivered_at=None,
            http_status=None,
            error_code="SUPPRESSED",
            error_message="Suppressed: in cooldown (32m remaining)",
            thread_key=None,
            message_name=None,
            gate_status="warn",
            reasons=["sample_insufficient"],
            dry_run=False,
        )
        item = record_to_delivery_item(rec)
        self.assertEqual(item.status, "suppressed")
        self.assertIsNone(item.error_message)
        self.assertEqual(item.suppression_reason, "Suppressed: in cooldown (32m remaining)")
        self.assertFalse(item.is_recovery)

    def test_record_to_delivery_item_recovery_detection(self) -> None:
        # Gate status pass -> inherently recovery
        rec_pass = DeliveryRecord(
            id=3,
            app_id="app1",
            platform="ios",
            version="2.0.0",
            provider="google_chat",
            alert_fingerprint="fp3",
            status="sent",
            attempt_count=1,
            attempted_at="2026-09-08T12:00:00Z",
            delivered_at="2026-09-08T12:00:01Z",
            http_status=200,
            error_code=None,
            error_message=None,
            thread_key=None,
            message_name=None,
            gate_status="pass",
            reasons=[],
            dry_run=False,
        )
        item_pass = record_to_delivery_item(rec_pass)
        self.assertTrue(item_pass.is_recovery)

        # Gate status fail with recover in text
        rec_rec = DeliveryRecord(
            id=4,
            app_id="app1",
            platform="android",
            version="3.0.0",
            provider="google_chat",
            alert_fingerprint="fp4",
            status="sent",
            attempt_count=1,
            attempted_at="2026-09-08T13:00:00Z",
            delivered_at="2026-09-08T13:00:01Z",
            http_status=200,
            error_code=None,
            error_message="Quality recovered to acceptable limits",
            thread_key=None,
            message_name=None,
            gate_status="warn",
            reasons=[],
            dry_run=False,
        )
        item_rec = record_to_delivery_item(rec_rec)
        self.assertTrue(item_rec.is_recovery)


class TestAlertDeliveryQueries(unittest.TestCase):
    """Tests query layer and store integration."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "alert_delivery.sqlite3"
        self.store = AlertDeliveryStore(self.db_path, app_id="test-app")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp_dir.cleanup()

    def _seed_records(self) -> None:
        _insert_record(
            self.store,
            app_id="test-app",
            platform="android",
            version="1.0.0",
            status="sent",
            gate_status="fail",
            attempted_at="2026-09-08T08:00:00Z",
            delivered_at="2026-09-08T08:00:01Z",
            http_status=200,
            reasons=["r1"],
        )
        _insert_record(
            self.store,
            app_id="test-app",
            platform="android",
            version="1.0.0",
            status="suppressed",
            gate_status="fail",
            attempted_at="2026-09-08T08:30:00Z",
            error_message="in cooldown",
            reasons=["r1"],
        )
        _insert_record(
            self.store,
            app_id="test-app",
            platform="ios",
            version="2.0.0",
            status="failed",
            gate_status="fail",
            attempted_at="2026-09-08T09:00:00Z",
            attempt_count=3,
            http_status=503,
            error_code="HTTP_503",
            error_message="Service Unavailable",
            reasons=["r2"],
        )
        _insert_record(
            self.store,
            app_id="other-app",
            platform="android",
            version="1.0.0",
            status="sent",
            gate_status="fail",
            attempted_at="2026-09-08T09:30:00Z",
            delivered_at="2026-09-08T09:30:01Z",
            http_status=200,
            reasons=["r3"],
        )

    def test_query_ordering_and_isolation(self) -> None:
        self._seed_records()
        items = get_recent_alert_deliveries("test-app", store=self.store)
        self.assertEqual(len(items), 3)
        # Verify app isolation (other-app excluded)
        for it in items:
            self.assertNotEqual(it.reasons, ["r3"])
        # Verify ordering attempted_at DESC, id DESC
        self.assertEqual(items[0].attempted_at, "2026-09-08T09:00:00Z")
        self.assertEqual(items[1].attempted_at, "2026-09-08T08:30:00Z")
        self.assertEqual(items[2].attempted_at, "2026-09-08T08:00:00Z")

    def test_platform_and_version_filtering(self) -> None:
        self._seed_records()
        android_items = get_recent_alert_deliveries("test-app", platform="android", store=self.store)
        self.assertEqual(len(android_items), 2)
        for it in android_items:
            self.assertEqual(it.platform, "android")

        ver_items = get_release_alert_history("test-app", platform="android", version="1.0.0", store=self.store)
        self.assertEqual(len(ver_items), 2)

        latest_rel = get_latest_release_delivery("test-app", platform="android", version="1.0.0", store=self.store)
        self.assertIsNotNone(latest_rel)
        self.assertEqual(latest_rel.attempted_at, "2026-09-08T08:30:00Z")


class TestAlertDeliveryHealth(unittest.TestCase):
    """Tests deterministic health summarization."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "alert_delivery.sqlite3"
        self.store = AlertDeliveryStore(self.db_path, app_id="test-app")
        self.ref_now = dt.datetime(2026, 9, 8, 12, 0, 0, tzinfo=dt.UTC)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp_dir.cleanup()

    def test_health_no_data_empty_store(self) -> None:
        h = summarize_alert_delivery_health("test-app", store=self.store, now=self.ref_now)
        self.assertEqual(h.status, "no_data")
        self.assertEqual(h.sent_24h, 0)
        self.assertEqual(h.failed_24h, 0)
        self.assertEqual(h.suppressed_24h, 0)
        self.assertIsNone(h.latest_success_at)
        self.assertIsNone(h.latest_failure_at)
        self.assertEqual(h.unresolved_failures, 0)

    def test_health_dry_run_strictly_excluded(self) -> None:
        # Insert only dry-run records
        _insert_record(
            self.store,
            app_id="test-app",
            platform="android",
            version="1.0.0",
            status="sent",
            gate_status="fail",
            attempted_at="2026-09-08T10:00:00Z",
            http_status=200,
            dry_run=True,
        )
        h = summarize_alert_delivery_health("test-app", store=self.store, now=self.ref_now)
        # Even though status is sent, it is dry-run, so health status MUST be no_data
        self.assertEqual(h.status, "no_data")
        self.assertEqual(h.sent_24h, 0)

    def test_health_healthy_when_latest_sent(self) -> None:
        _insert_record(
            self.store,
            app_id="test-app",
            platform="android",
            version="1.0.0",
            status="sent",
            gate_status="fail",
            attempted_at="2026-09-08T10:00:00Z",
            delivered_at="2026-09-08T10:00:01Z",
            http_status=200,
            dry_run=False,
        )
        h = summarize_alert_delivery_health("test-app", store=self.store, now=self.ref_now)
        self.assertEqual(h.status, "healthy")
        self.assertEqual(h.sent_24h, 1)
        self.assertEqual(h.failed_24h, 0)
        self.assertEqual(h.unresolved_failures, 0)
        self.assertEqual(h.latest_success_at, "2026-09-08T10:00:01Z")

    def test_health_degraded_when_latest_failed(self) -> None:
        _insert_record(
            self.store,
            app_id="test-app",
            platform="android",
            version="1.0.0",
            status="failed",
            gate_status="fail",
            attempted_at="2026-09-08T11:00:00Z",
            attempt_count=3,
            http_status=500,
            error_code="HTTP_500",
            error_message="Internal Error",
            dry_run=False,
        )
        h = summarize_alert_delivery_health("test-app", store=self.store, now=self.ref_now)
        self.assertEqual(h.status, "degraded")
        self.assertEqual(h.failed_24h, 1)
        self.assertEqual(h.unresolved_failures, 1)
        self.assertEqual(h.latest_failure_at, "2026-09-08T11:00:00Z")

    def test_health_suppressed_does_not_degrade_health(self) -> None:
        # Succeeded first
        _insert_record(
            self.store,
            app_id="test-app",
            platform="android",
            version="1.0.0",
            status="sent",
            gate_status="fail",
            attempted_at="2026-09-08T09:00:00Z",
            delivered_at="2026-09-08T09:00:01Z",
            http_status=200,
            dry_run=False,
        )
        # Then multiple suppressed attempts
        for t in ("09:30:00Z", "10:00:00Z", "10:30:00Z"):
            _insert_record(
                self.store,
                app_id="test-app",
                platform="android",
                version="1.0.0",
                status="suppressed",
                gate_status="fail",
                attempted_at=f"2026-09-08T{t}",
                error_message="in cooldown",
                dry_run=False,
            )

        h = summarize_alert_delivery_health("test-app", store=self.store, now=self.ref_now)
        # Must stay healthy because suppressed is normal policy behavior, not failure!
        self.assertEqual(h.status, "healthy")
        self.assertEqual(h.sent_24h, 1)
        self.assertEqual(h.suppressed_24h, 3)
        self.assertEqual(h.failed_24h, 0)
        self.assertEqual(h.unresolved_failures, 0)

    def test_health_recovery_transition(self) -> None:
        # Failure first
        _insert_record(
            self.store,
            app_id="test-app",
            platform="android",
            version="1.0.0",
            status="failed",
            gate_status="fail",
            attempted_at="2026-09-08T09:00:00Z",
            attempt_count=3,
            http_status=500,
            error_code="HTTP_500",
            error_message="Internal Error",
            dry_run=False,
        )
        # Then successful recovery alert
        _insert_record(
            self.store,
            app_id="test-app",
            platform="android",
            version="1.0.0",
            status="sent",
            gate_status="pass",
            attempted_at="2026-09-08T11:00:00Z",
            delivered_at="2026-09-08T11:00:01Z",
            http_status=200,
            error_message="Quality recovered to PASS",
            dry_run=False,
        )
        h = summarize_alert_delivery_health("test-app", store=self.store, now=self.ref_now)
        self.assertEqual(h.status, "healthy")
        self.assertEqual(h.unresolved_failures, 0)
        self.assertEqual(h.sent_24h, 1)
        self.assertEqual(h.failed_24h, 1)
        self.assertEqual(h.latest_success_at, "2026-09-08T11:00:01Z")
        self.assertEqual(h.latest_failure_at, "2026-09-08T09:00:00Z")


class TestStoreResilience(unittest.TestCase):
    """Tests non-blocking resilience against missing or corrupted stores."""

    def test_missing_database_file(self) -> None:
        non_existent = Path("/path/to/definitely/non_existent_alert_delivery.sqlite3")
        bundle = get_alert_observability_bundle("dummy-app", custom_path=non_existent)
        self.assertEqual(bundle["health"]["status"], "no_data")
        self.assertEqual(bundle["recent"], [])

        items = get_recent_alert_deliveries("dummy-app", custom_path=non_existent)
        self.assertEqual(items, [])

    def test_corrupted_database_file(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            f.write(b"NOT A VALID SQLITE DATABASE FILE GARBAGE BYTES")
            corrupt_path = Path(f.name)

        try:
            bundle = get_alert_observability_bundle("dummy-app", custom_path=corrupt_path)
            self.assertEqual(bundle["health"]["status"], "no_data")
            self.assertEqual(bundle["recent"], [])

            items = get_recent_alert_deliveries("dummy-app", custom_path=corrupt_path)
            self.assertEqual(items, [])
        finally:
            corrupt_path.unlink(missing_ok=True)


class TestAlertObservabilityCLI(unittest.TestCase):
    """Tests CLI querying with --history, --json, and filtering."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "alert_delivery.sqlite3"
        self.store = AlertDeliveryStore(self.db_path, app_id="test-app")
        _insert_record(
            self.store,
            app_id="test-app",
            platform="android",
            version="3.4.1",
            status="sent",
            gate_status="pass",
            attempted_at="2026-09-08T10:00:00Z",
            delivered_at="2026-09-08T10:00:01Z",
            http_status=200,
            dry_run=False,
        )
        _insert_record(
            self.store,
            app_id="test-app",
            platform="ios",
            version="2.1.0",
            status="failed",
            gate_status="fail",
            attempted_at="2026-09-08T09:00:00Z",
            attempt_count=2,
            http_status=500,
            error_code="HTTP_500",
            error_message="Internal Server Error",
            dry_run=False,
            reasons=["crash_free_drop"],
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tmp_dir.cleanup()

    def test_cli_history_table(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            with patch(
                "sys.argv",
                [
                    "alerts",
                    "--app",
                    "test-app",
                    "--history",
                    "--db-path",
                    str(self.db_path),
                ],
            ):
                with self.assertRaises(SystemExit) as cm:
                    alerts_cli_main()
                self.assertEqual(cm.exception.code, 0)

        output = out.getvalue()
        self.assertIn("[Alert Delivery History]", output)
        self.assertIn("3.4.1", output)
        self.assertIn("2.1.0", output)
        self.assertIn("sent", output)
        self.assertIn("failed", output)

    def test_cli_history_json(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            with patch(
                "sys.argv",
                [
                    "alerts",
                    "--app",
                    "test-app",
                    "--history",
                    "--json",
                    "--db-path",
                    str(self.db_path),
                ],
            ):
                with self.assertRaises(SystemExit) as cm:
                    alerts_cli_main()
                self.assertEqual(cm.exception.code, 0)

        data = json.loads(out.getvalue())
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]["version"], "3.4.1")
        self.assertEqual(data[0]["status"], "sent")
        self.assertTrue(data[0]["is_recovery"])

    def test_cli_history_filtering(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            with patch(
                "sys.argv",
                [
                    "alerts",
                    "--app",
                    "test-app",
                    "--history",
                    "--platform",
                    "ios",
                    "--json",
                    "--db-path",
                    str(self.db_path),
                ],
            ):
                with self.assertRaises(SystemExit) as cm:
                    alerts_cli_main()
                self.assertEqual(cm.exception.code, 0)

        data = json.loads(out.getvalue())
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["platform"], "ios")
        self.assertEqual(data[0]["version"], "2.1.0")


class TestSchemaV2AlertDelivery(unittest.TestCase):
    """Tests Schema V2 runtime validation with alert delivery data."""

    def test_validate_alert_delivery_valid(self) -> None:
        ad = {
            "provider": "google_chat",
            "health": {
                "status": "healthy",
                "provider": "google_chat",
                "sent_24h": 5,
                "failed_24h": 0,
                "suppressed_24h": 2,
                "latest_success_at": "2026-09-08T10:00:00Z",
                "latest_failure_at": None,
                "unresolved_failures": 0,
            },
            "recent": [
                {
                    "id": 1,
                    "provider": "google_chat",
                    "platform": "android",
                    "version": "1.0.0",
                    "status": "sent",
                    "gate_status": "fail",
                    "attempted_at": "2026-09-08T10:00:00Z",
                    "delivered_at": "2026-09-08T10:00:01Z",
                    "attempt_count": 1,
                    "http_status": 200,
                    "error_code": None,
                    "error_message": None,
                    "suppression_reason": None,
                    "thread_key": None,
                    "message_name": None,
                    "reasons": ["rule1"],
                    "dry_run": False,
                    "is_recovery": False,
                }
            ],
        }
        errors: list[str] = []
        validate_alert_delivery(ad, errors)
        self.assertEqual(errors, [])

    def test_validate_alert_delivery_invalid_status(self) -> None:
        ad = {
            "health": {
                "status": "invalid_status",
                "sent_24h": -1,
            }
        }
        errors: list[str] = []
        validate_alert_delivery(ad, errors)
        self.assertTrue(any("status must be one of" in e for e in errors))
        self.assertTrue(any("sent_24h must be a non-negative integer" in e for e in errors))

    def test_validate_release_catalog_alert_deliveries(self) -> None:
        cat = [
            {
                "version": "1.0.0",
                "platform": "android",
                "first_seen": "2026-09-01T00:00:00Z",
                "last_seen": "2026-09-08T00:00:00Z",
                "release_date": "2026-09-01",
                "status": "latest",
                "lifetime_crashes": 10,
                "lifetime_issues": 2,
                "lifetime_affected_users": 8,
                "lifetime_fatal": 0,
                "lifetime_anr": 0,
                "recent_health": {},
                "alert_deliveries": [
                    {
                        "id": 1,
                        "provider": "google_chat",
                        "platform": "android",
                        "version": "1.0.0",
                        "status": "sent",
                        "gate_status": "pass",
                        "attempted_at": "2026-09-08T10:00:00Z",
                        "attempt_count": 1,
                        "is_recovery": True,
                        "dry_run": False,
                    }
                ],
            }
        ]
        errors: list[str] = []
        validate_release_catalog(cat, errors)
        self.assertEqual(errors, [])

    def test_validate_app_dashboard_v2_with_alert_delivery(self) -> None:
        app_data = {
            "metadata": {
                "app_id": "test_app",
                "display_name": "Test App",
                "firebase_project_id": "test-proj",
                "platforms": ["android"],
                "source_repo": None,
                "custom_keys_monitored": [],
            },
            "period": {
                "days": 7,
                "start_time": "2026-09-01T00:00:00Z",
                "end_time": "2026-09-08T00:00:00Z",
                "comparison_period": None,
            },
            "sources": {},
            "kpi": {
                "crash_free_users_rate": 0.99,
                "crash_free_sessions_rate": 0.999,
                "total_crashes": 10,
                "total_fatal": 0,
                "total_anr": 0,
                "total_affected_users": 5,
                "total_sessions": 10000,
            },
            "daily_trend": [],
            "version_health": [],
            "distributions": {
                "platform": [],
                "device_models": [],
                "os_versions": [],
                "app_versions": [],
            },
            "top_issues": [],
            "ai_summary": {
                "overall_health_assessment": "Good",
                "trend_analysis": "Stable",
                "distribution_insights": "None",
                "recommended_actions": [],
                "data_limitations": None,
            },
            "limitations": [],
            "alert_delivery": {
                "provider": "google_chat",
                "health": {
                    "status": "healthy",
                    "provider": "google_chat",
                    "sent_24h": 1,
                    "failed_24h": 0,
                    "suppressed_24h": 0,
                    "latest_success_at": "2026-09-08T00:00:00Z",
                    "latest_failure_at": None,
                },
                "recent": [],
            },
        }
        errors = validate_app_dashboard_v2(app_data)
        self.assertEqual(errors, [])


class TestDashboardUIIntegration(unittest.TestCase):
    """Tests HTML and JS rendering components."""

    def test_sources_html_contains_alert_section(self) -> None:
        html = get_sources_html()
        self.assertIn('id="alertDeliverySection"', html)
        self.assertIn('id="alertDeliveryHealthGrid"', html)
        self.assertIn('id="alertDeliveryRecentCard"', html)
        self.assertIn('id="alertDeliveryTableContainer"', html)

    def test_sources_js_contains_alert_renderer(self) -> None:
        js = get_sources_js()
        self.assertIn("function renderAlertDeliveryObservability()", js)
        self.assertIn("renderAlertDeliveryObservability();", js)
        self.assertIn("服務連線健康度", js)
        self.assertIn("過去 24 小時統計", js)

    def test_releases_js_contains_alert_timeline(self) -> None:
        js = get_releases_js()
        self.assertIn("alert_deliveries", js)
        self.assertIn("通知發送紀錄時間軸 (Alert Delivery Timeline)", js)
        self.assertIn("復原通知 ↗", js)


if __name__ == "__main__":
    unittest.main()
