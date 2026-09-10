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
    is_recovery: bool = False,
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
            is_recovery=is_recovery,
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
        is_recovery=is_recovery,
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

    def test_record_to_delivery_item_authoritative_recovery(self) -> None:
        # 1. Normal PASS suppression (PASS with no prior failure) -> is_recovery MUST be False
        rec_pass_no_prior = DeliveryRecord(
            id=3,
            app_id="app1",
            platform="ios",
            version="2.0.0",
            provider="google_chat",
            alert_fingerprint="fp3",
            status="suppressed",
            attempt_count=0,
            attempted_at="2026-09-08T12:00:00Z",
            delivered_at=None,
            http_status=None,
            error_code="SUPPRESSED",
            error_message="Status 'pass' is not in configured notify_on list ['warn', 'fail']",
            thread_key=None,
            message_name=None,
            gate_status="pass",
            reasons=[],
            dry_run=False,
            is_recovery=False,
        )
        item_pass = record_to_delivery_item(rec_pass_no_prior)
        self.assertFalse(item_pass.is_recovery)

        # 2. Genuine recovery -> is_recovery MUST be True
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
            error_message="Quality recovered to pass after previous FAIL alert",
            thread_key=None,
            message_name=None,
            gate_status="pass",
            reasons=[],
            dry_run=False,
            is_recovery=True,
        )
        item_rec = record_to_delivery_item(rec_rec)
        self.assertTrue(item_rec.is_recovery)

    def test_legacy_dirty_record_projection_defensive_sanitization(self) -> None:
        rec = DeliveryRecord(
            id=99,
            app_id="app-audit",
            platform="ios",
            version="1.2.3",
            provider="google_chat",
            alert_fingerprint="fp99",
            status="failed",
            attempt_count=3,
            attempted_at="2026-09-08T10:00:00Z",
            delivered_at=None,
            http_status=401,
            error_code="Bearer ya29.SecretBearerToken12345",
            error_message="Authorization: Bearer secret-auth-token and key=secret_key_123 and token=secret_tok_456 and https://hooks.slack.com/services/T00/B00/X00 and user_id=12345678-1234-1234-1234-123456789abc",
            thread_key="spaces/AAA?token=secret_thread_token&installation_id=87654321-4321-4321-4321-cba987654321",
            message_name="spaces/AAA/messages/CCC?key=mysecret_msg_key",
            gate_status="fail",
            reasons=[
                "crash drop for device 11112222-3333-4444-5555-666677778888",
                "api_key=exposed_api_key_val",
            ],
            dry_run=False,
            is_recovery=False,
        )
        item = record_to_delivery_item(rec)
        item_dict = item.to_dict()

        # Verify no raw secrets, auth headers, URLs, or UUIDs exist anywhere in exported dict
        dict_json = json.dumps(item_dict)
        self.assertNotIn("ya29.SecretBearerToken12345", dict_json)
        self.assertNotIn("secret-auth-token", dict_json)
        self.assertNotIn("secret_key_123", dict_json)
        self.assertNotIn("secret_tok_456", dict_json)
        self.assertNotIn("hooks.slack.com", dict_json)
        self.assertNotIn("12345678-1234-1234-1234-123456789abc", dict_json)
        self.assertNotIn("secret_thread_token", dict_json)
        self.assertNotIn("87654321-4321-4321-4321-cba987654321", dict_json)
        self.assertNotIn("mysecret_msg_key", dict_json)
        self.assertNotIn("11112222-3333-4444-5555-666677778888", dict_json)
        self.assertNotIn("exposed_api_key_val", dict_json)

        # Verify safe replacements
        self.assertIn("<redacted>", str(item.error_code))
        self.assertIn("<redacted>", str(item.error_message))
        self.assertIn("<redacted-webhook-url>", str(item.error_message))
        self.assertIn("<redacted-id>", str(item.error_message))
        self.assertIn("<redacted-id>", item.reasons[0])
        self.assertIn("api_key=<redacted>", item.reasons[1])

    def test_authorization_header_sanitization_schemes(self) -> None:
        """Tests that Basic, ApiKey, Digest, and custom auth header credentials do not leak."""
        test_cases = [
            ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("Authorization: ApiKey SECRET_API_KEY_12345", "SECRET_API_KEY_12345"),
            ('Authorization: Digest username="Mufasa", realm="myrealm", nonce="dcd98b7102dd2f0e8b11d0f600bfb0c093"', "Mufasa"),
            ('{"Authorization": "Basic c2VjcmV0dXNlcjpwYXNz", "Content-Type": "application/json"}', "c2VjcmV0dXNlcjpwYXNz"),
        ]
        for header_text, sensitive_token in test_cases:
            rec = DeliveryRecord(
                id=101,
                app_id="app-auth-test",
                platform="ios",
                version="1.0.0",
                provider="google_chat",
                alert_fingerprint="fp101",
                status="failed",
                attempt_count=1,
                attempted_at="2026-09-08T10:00:00Z",
                delivered_at=None,
                http_status=401,
                error_code="UNAUTHORIZED",
                error_message=f"Request failed. {header_text}",
                thread_key=None,
                message_name=None,
                gate_status="fail",
                reasons=[],
                dry_run=False,
                is_recovery=False,
            )
            item = record_to_delivery_item(rec)
            d = item.to_dict()
            self.assertNotIn(sensitive_token, json.dumps(d))
            self.assertIn("<redacted>", str(item.error_message))


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
            is_recovery=True,
        )
        h = summarize_alert_delivery_health("test-app", store=self.store, now=self.ref_now)
        self.assertEqual(h.status, "healthy")
        self.assertEqual(h.unresolved_failures, 0)
        self.assertEqual(h.sent_24h, 1)
        self.assertEqual(h.failed_24h, 1)
        self.assertEqual(h.latest_success_at, "2026-09-08T11:00:01Z")
        self.assertEqual(h.latest_failure_at, "2026-09-08T09:00:00Z")

    def test_health_large_volume_no_limit_500_truncation(self) -> None:
        # Seed 600 records within 24h: 550 suppressed, 30 failed, 20 sent
        # With limit=500, old code would truncate at 500 records and give wrong counts
        for i in range(550):
            _insert_record(
                self.store,
                app_id="test-app",
                platform="android",
                version=f"1.0.{i}",
                status="suppressed",
                gate_status="warn",
                attempted_at="2026-09-08T10:00:00Z",
                error_message="in cooldown",
            )
        for i in range(30):
            _insert_record(
                self.store,
                app_id="test-app",
                platform="android",
                version=f"2.0.{i}",
                status="failed",
                gate_status="fail",
                attempted_at="2026-09-08T10:30:00Z",
                error_code="HTTP_500",
                error_message="Internal Error",
            )
        for i in range(20):
            _insert_record(
                self.store,
                app_id="test-app",
                platform="android",
                version=f"3.0.{i}",
                status="sent",
                gate_status="pass",
                attempted_at="2026-09-08T11:00:00Z",
                delivered_at="2026-09-08T11:00:01Z",
                http_status=200,
            )

        h = summarize_alert_delivery_health("test-app", store=self.store, now=self.ref_now)
        # Total records = 600. All 600 must be accurately counted without 500 truncation!
        self.assertEqual(h.suppressed_24h, 550)
        self.assertEqual(h.failed_24h, 30)
        self.assertEqual(h.sent_24h, 20)
        self.assertEqual(h.status, "healthy")
        self.assertEqual(h.unresolved_failures, 0)
        self.assertEqual(h.latest_success_at, "2026-09-08T11:00:01Z")
        self.assertEqual(h.latest_failure_at, "2026-09-08T10:30:00Z")

    def test_health_unresolved_failures_out_of_order_insert_uses_attempted_at_authority(self) -> None:
        """Tests that unresolved failure count follows (attempted_at DESC, id DESC) authority,
        even when a later attempt has a smaller autoincrement ID due to worker delay.
        """
        # 1. 11:00 FAILED is inserted first (gets id=1)
        _insert_record(
            self.store,
            app_id="test-app",
            platform="android",
            version="1.0.0",
            status="failed",
            gate_status="fail",
            attempted_at="2026-09-08T11:00:00Z",
            error_code="HTTP_500",
            error_message="Gateway Timeout",
        )
        # 2. 10:00 SENT was delayed and committed second (gets id=2)
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
        )

        # Under (attempted_at DESC, id DESC), 11:00 FAILED (id=1) is strictly newer than 10:00 SENT (id=2).
        h = summarize_alert_delivery_health("test-app", store=self.store, now=self.ref_now)
        self.assertEqual(h.status, "degraded")
        # Unresolved failures must be 1, NOT 0 (which occurred when comparing id > MAX(id of sent))
        self.assertEqual(h.unresolved_failures, 1)
        self.assertEqual(h.latest_failure_at, "2026-09-08T11:00:00Z")
        self.assertEqual(h.latest_success_at, "2026-09-08T10:00:01Z")


class TestStoreResilience(unittest.TestCase):
    """Tests non-blocking resilience against missing or corrupted stores."""

    def test_missing_database_file(self) -> None:
        non_existent = Path("/path/to/definitely/non_existent_alert_delivery.sqlite3")
        bundle = get_alert_observability_bundle("dummy-app", custom_path=non_existent)
        self.assertEqual(bundle["health"]["status"], "no_data")
        self.assertIsNone(bundle["health"].get("error_diagnostic"))
        self.assertEqual(bundle["recent"], [])

        items = get_recent_alert_deliveries("dummy-app", custom_path=non_existent)
        self.assertEqual(items, [])

    def test_corrupted_database_file(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            f.write(b"NOT A VALID SQLITE DATABASE FILE GARBAGE BYTES")
            corrupt_path = Path(f.name)

        try:
            bundle = get_alert_observability_bundle("dummy-app", custom_path=corrupt_path)
            self.assertEqual(bundle["health"]["status"], "unavailable")
            self.assertIsNotNone(bundle["health"].get("error_diagnostic"))
            self.assertEqual(bundle["recent"], [])

            items = get_recent_alert_deliveries("dummy-app", custom_path=corrupt_path)
            self.assertEqual(items, [])
        finally:
            corrupt_path.unlink(missing_ok=True)

    def test_readonly_store_and_legacy_db_without_is_recovery(self) -> None:
        """Verifies that observability layer reads legacy databases in read-only mode (chmod 444)
        without attempting WAL pragma, table creation, or ALTER TABLE migrations.
        """
        import os
        import sqlite3

        with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
            legacy_db_path = Path(f.name)

        conn = sqlite3.connect(str(legacy_db_path))
        conn.execute("""
            CREATE TABLE alert_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                app_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                version TEXT NOT NULL,
                provider TEXT NOT NULL,
                alert_fingerprint TEXT NOT NULL,
                gate_status TEXT NOT NULL,
                attempted_at TEXT NOT NULL,
                delivered_at TEXT,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 1,
                http_status INTEGER,
                error_code TEXT,
                error_message TEXT,
                thread_key TEXT,
                message_name TEXT,
                reasons_json TEXT,
                dry_run INTEGER NOT NULL DEFAULT 0
            );
        """)
        conn.execute("""
            INSERT INTO alert_deliveries (
                app_id, platform, version, provider, alert_fingerprint,
                gate_status, attempted_at, delivered_at, status, attempt_count, dry_run
            ) VALUES (
                'legacy-app', 'android', '1.0.0', 'google_chat', 'fp_legacy',
                'pass', '2026-09-08T10:00:00Z', '2026-09-08T10:00:01Z', 'sent', 1, 0
            );
        """)
        conn.commit()
        conn.close()

        # Set read-only filesystem permission (chmod 444)
        os.chmod(legacy_db_path, 0o444)

        try:
            # Observability layer should succeed in read-only mode without error
            # 注入參考時間：本測試寫入的紀錄為硬編的 2026-09-08T10:00Z，
            # 若改用系統時鐘，滾動 24 小時視窗會在 2026-09-09T10:00Z 後過期，
            # 使斷言變成掛在時鐘上的定時炸彈（Issue #85）。
            bundle = get_alert_observability_bundle(
                "legacy-app",
                custom_path=legacy_db_path,
                now=dt.datetime(2026, 9, 8, 12, 0, 0, tzinfo=dt.UTC),
            )
            self.assertEqual(bundle["health"]["status"], "healthy")
            self.assertEqual(bundle["health"]["sent_24h"], 1)
            self.assertEqual(len(bundle["recent"]), 1)
            # Legacy table has no is_recovery column -> must safely fallback to False
            self.assertFalse(bundle["recent"][0]["is_recovery"])

            # Verify no ALTER TABLE was attempted and schema is still legacy without is_recovery
            read_conn = sqlite3.connect(f"file:{legacy_db_path.resolve().as_posix()}?mode=ro", uri=True)
            cols = [col[1] for col in read_conn.execute("PRAGMA table_info(alert_deliveries)").fetchall()]
            read_conn.close()
            self.assertNotIn("is_recovery", cols)
        finally:
            os.chmod(legacy_db_path, 0o666)
            legacy_db_path.unlink(missing_ok=True)


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
            is_recovery=True,
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
        fixture_path = ROOT / "tests" / "fixtures" / "dashboard_v2.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        shop_app = data["apps"]["shop_app"]
        shop_app["alert_delivery"] = {
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
        }
        errors = validate_app_dashboard_v2(shop_app)
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


class TestDispatcherRecoveryAuthority(unittest.TestCase):
    """Tests end-to-end authoritative is_recovery handling by AlertDispatcher and storage."""

    def test_pass_with_no_prior_failure_persists_is_recovery_false(self) -> None:
        from unittest.mock import MagicMock

        from crash_trend.alerts.dispatcher import AlertDispatcher
        from crash_trend.alerts.policy import AlertPolicy
        from tests.test_alerts import make_sample_artifact

        store = AlertDeliveryStore(db_path=":memory:")
        artifact = make_sample_artifact(
            app_id="demo-auth",
            platform="android",
            version="1.0.0",
            gate_status="pass",
            rule_results=[],
        )
        policy = AlertPolicy(enabled=True, notify_on=("fail", "warn"), notify_recovery=True)
        mock_provider = MagicMock()

        dispatcher = AlertDispatcher(store=store, provider=mock_provider)
        summary = dispatcher.dispatch(
            app_id="demo-auth",
            artifact=artifact,
            policy=policy,
            dry_run=False,
        )

        self.assertEqual(summary.total_suppressed, 1)
        self.assertFalse(summary.decisions["android"].is_recovery)

        # Inspect SQLite row directly
        records = store.get_history("demo-auth")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "suppressed")
        self.assertEqual(records[0].gate_status, "pass")
        self.assertFalse(records[0].is_recovery)

        # Inspect projected AlertDeliveryItem
        items = get_recent_alert_deliveries("demo-auth", store=store)
        self.assertEqual(len(items), 1)
        self.assertFalse(items[0].is_recovery)
        self.assertFalse(items[0].to_dict()["is_recovery"])

    def test_pass_with_prior_failure_persists_is_recovery_true(self) -> None:
        from unittest.mock import MagicMock

        from crash_trend.alerts.dispatcher import AlertDispatcher
        from crash_trend.alerts.models import DeliveryResult
        from crash_trend.alerts.policy import AlertPolicy
        from tests.test_alerts import make_sample_artifact

        store = AlertDeliveryStore(db_path=":memory:")
        policy = AlertPolicy(enabled=True, notify_on=("fail", "warn"), notify_recovery=True)
        mock_provider = MagicMock()
        mock_provider.send.return_value = DeliveryResult(
            status="sent",
            attempt_count=1,
            delivered_at="2026-09-08T10:00:01Z",
            http_status=200,
        )

        # 1. First evaluation: FAIL -> sent
        art_fail = make_sample_artifact(
            app_id="demo-auth",
            platform="android",
            version="1.0.0",
            gate_status="fail",
        )
        dispatcher = AlertDispatcher(store=store, provider=mock_provider)
        summary1 = dispatcher.dispatch(
            app_id="demo-auth",
            artifact=art_fail,
            policy=policy,
            dry_run=False,
        )
        self.assertEqual(summary1.total_sent, 1)
        self.assertFalse(summary1.decisions["android"].is_recovery)

        # 2. Second evaluation: PASS -> Recovery sent
        art_pass = make_sample_artifact(
            app_id="demo-auth",
            platform="android",
            version="1.0.0",
            gate_status="pass",
            rule_results=[],
        )
        summary2 = dispatcher.dispatch(
            app_id="demo-auth",
            artifact=art_pass,
            policy=policy,
            dry_run=False,
        )
        self.assertEqual(summary2.total_sent, 1)
        self.assertTrue(summary2.decisions["android"].is_recovery)

        # Inspect SQLite row
        records = store.get_history("demo-auth")
        self.assertEqual(len(records), 2)
        # records are ordered latest first
        latest = records[0]
        self.assertEqual(latest.status, "sent")
        self.assertEqual(latest.gate_status, "pass")
        self.assertTrue(latest.is_recovery)

        # Inspect projected AlertDeliveryItem
        items = get_recent_alert_deliveries("demo-auth", store=store)
        self.assertTrue(items[0].is_recovery)
        self.assertTrue(items[0].to_dict()["is_recovery"])


if __name__ == "__main__":
    unittest.main()
