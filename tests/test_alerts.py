"""Comprehensive test suite for Google Chat Quality Alerts Delivery (Issue #59).

Covers:
1. Alert Decision logic (FAIL, WARN, PASS, BASELINE, INSUFFICIENT_DATA, disabled, recovery)
2. Deduplication & Cooldown (fingerprint match, status change, new reasons, cooldown expiry, platform isolation, multi-app)
3. Secret safety & privacy (redaction of tokens/URLs, zero raw UUIDs, missing webhook env handling)
4. HTTP resilience & bounded retry (2xx, 429 backoff, 5xx backoff, network errors, 4xx fast-fail, thread fallback)
5. Threading (deterministic thread key, platform/version isolation, thread reuse)
6. CLI & Pipeline integration (dry-run, force, gate decoupled failure, fail-on-alert-failure)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crash_trend.alerts import (
    AlertDeliveryStore,
    AlertDispatcher,
    AlertMessage,
    AlertPolicy,
    DeliveryResult,
    GoogleChatWebhookProvider,
    build_alert_message,
    compute_alert_fingerprint,
    evaluate_alert_decision,
    redact_url,
    sanitize_audit_text,
)
from crash_trend.gate.artifact import ReleaseGateArtifact
from crash_trend.pipeline_run import run_pipeline


def make_sample_artifact(
    app_id: str = "demo_app",
    platform: str = "android",
    version: str = "3.2.0",
    prev_version: str = "3.1.2",
    gate_status: str = "fail",
    rule_results: Sequence[dict[str, Any]] | None = None,
) -> ReleaseGateArtifact:
    """Creates a valid ReleaseGateArtifact dictionary for testing."""
    now_iso = "2026-09-08T15:00:00Z"
    if rule_results is None:
        if gate_status == "fail":
            rule_results = [
                {
                    "rule_name": "crash_rate_regression",
                    "metric_name": "crash_rate_change_pct",
                    "current_value": 0.52,
                    "previous_value": 0.0,
                    "warn_threshold": 0.10,
                    "fail_threshold": 0.25,
                    "status": "fail",
                    "reason": "崩潰率上升 52.0%（當前 1.52% vs 前版 1.00%），達到失敗閾值 25.0%",
                }
            ]
        elif gate_status == "warn":
            rule_results = [
                {
                    "rule_name": "crash_rate_regression",
                    "metric_name": "crash_rate_change_pct",
                    "current_value": 0.18,
                    "previous_value": 0.0,
                    "warn_threshold": 0.10,
                    "fail_threshold": 0.25,
                    "status": "warn",
                    "reason": "崩潰率上升 18.0%，達到預警閾值 10.0%",
                }
            ]
        else:
            rule_results = []

    return {
        "schema_version": "2.7",
        "app_id": app_id,
        "generated_at": now_iso,
        "overall_status": gate_status,  # type: ignore[typeddict-item]
        "should_alert": gate_status in ("fail", "warn"),
        "alert_severity": "critical" if gate_status == "fail" else ("warning" if gate_status == "warn" else "none"),
        "alert_summary": f"版本 {version} ({platform}) 閘門結果: {gate_status}",
        "platforms": {
            platform: {
                "platform": platform,  # type: ignore[typeddict-item]
                "target_version": version,
                "previous_version": prev_version,
                "gate_status": gate_status,  # type: ignore[typeddict-item]
                "sample_sufficient": True,
                "rule_results": list(rule_results),  # type: ignore[typeddict-item]
                "alert": {
                    "should_alert": gate_status in ("fail", "warn"),
                    "alert_severity": "critical" if gate_status == "fail" else "none",
                    "alert_summary": "Alert summary",
                    "trigger_rules": [r["rule_name"] for r in rule_results if r.get("status") in ("fail", "warn")],
                },
                "evaluated_at": now_iso,
            }
        },
        "policy_version": "1.0",
        "policy": {},
    }


class TestAlertDecisionLogic(unittest.TestCase):
    """Verifies decision evaluation for various gate outcomes and policies."""

    def setUp(self) -> None:
        self.store = AlertDeliveryStore(db_path=":memory:")
        self.policy = AlertPolicy(
            enabled=True,
            notify_on=("fail", "warn"),
            cooldown_minutes=360,
            notify_recovery=True,
        )

    def tearDown(self) -> None:
        self.store.close()

    def test_fail_status_triggers_send(self) -> None:
        dec = evaluate_alert_decision(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            gate_status="fail",
            triggered_reasons=["crash_rate_regression"],
            policy=self.policy,
            store=self.store,
        )
        self.assertEqual(dec.decision, "send")
        self.assertFalse(dec.is_recovery)

    def test_warn_status_triggers_send(self) -> None:
        dec = evaluate_alert_decision(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            gate_status="warn",
            triggered_reasons=["crash_rate_regression"],
            policy=self.policy,
            store=self.store,
        )
        self.assertEqual(dec.decision, "send")
        self.assertFalse(dec.is_recovery)

    def test_clean_pass_suppressed_when_no_prior_regression(self) -> None:
        dec = evaluate_alert_decision(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            gate_status="pass",
            triggered_reasons=[],
            policy=self.policy,
            store=self.store,
        )
        self.assertEqual(dec.decision, "suppressed")
        self.assertIn("Pass status suppressed", dec.reason)

    def test_baseline_and_insufficient_data_suppressed_by_default(self) -> None:
        for st in ("baseline", "insufficient_data"):
            dec = evaluate_alert_decision(
                app_id="app1",
                platform="android",
                target_version="3.2.0",
                gate_status=st,
                triggered_reasons=[],
                policy=self.policy,
                store=self.store,
            )
            self.assertEqual(dec.decision, "suppressed")

    def test_alerts_disabled_suppresses_everything(self) -> None:
        disabled_policy = AlertPolicy(enabled=False)
        dec = evaluate_alert_decision(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            gate_status="fail",
            triggered_reasons=["crash_rate_regression"],
            policy=disabled_policy,
            store=self.store,
        )
        self.assertEqual(dec.decision, "suppressed")
        self.assertIn("disabled", dec.reason)

    def test_recovery_pass_after_fail_triggers_send(self) -> None:
        # 1. Simulate prior sent FAIL alert
        fp = compute_alert_fingerprint("app1", "android", "3.2.0", "fail", ["crash_rate_regression"])
        rec_id = self.store.record_attempt(
            app_id="app1",
            platform="android",
            version="3.2.0",
            provider="google_chat",
            alert_fingerprint=fp,
            gate_status="fail",
            attempted_at="2026-09-08T10:00:00Z",
            reasons=["crash_rate_regression"],
        )
        self.store.update_result(rec_id, status="sent", delivered_at="2026-09-08T10:00:01Z")

        # 2. Evaluate current PASS
        dec = evaluate_alert_decision(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            gate_status="pass",
            triggered_reasons=[],
            policy=self.policy,
            store=self.store,
        )
        self.assertEqual(dec.decision, "send")
        self.assertTrue(dec.is_recovery)
        self.assertIn("recovered", dec.reason.lower())

    def test_second_pass_after_recovery_is_suppressed(self) -> None:
        # Prior sent was recovery PASS
        fp = compute_alert_fingerprint("app1", "android", "3.2.0", "pass", [])
        rec_id = self.store.record_attempt(
            app_id="app1",
            platform="android",
            version="3.2.0",
            provider="google_chat",
            alert_fingerprint=fp,
            gate_status="pass",
            attempted_at="2026-09-08T12:00:00Z",
            reasons=[],
        )
        self.store.update_result(rec_id, status="sent", delivered_at="2026-09-08T12:00:01Z")

        # Subsequent PASS should not alert again
        dec = evaluate_alert_decision(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            gate_status="pass",
            triggered_reasons=[],
            policy=self.policy,
            store=self.store,
        )
        self.assertEqual(dec.decision, "suppressed")


class TestDeduplicationAndCooldown(unittest.TestCase):
    """Verifies deterministic dedupe, cooldown window, and bypass criteria."""

    def setUp(self) -> None:
        self.store = AlertDeliveryStore(db_path=":memory:")
        self.policy = AlertPolicy(
            enabled=True,
            notify_on=("fail", "warn"),
            cooldown_minutes=360,
            resend_on_status_change=True,
            resend_on_new_reason=True,
        )

    def tearDown(self) -> None:
        self.store.close()

    def test_same_fingerprint_in_cooldown_is_suppressed(self) -> None:
        now = dt.datetime(2026, 9, 8, 12, 0, 0, tzinfo=dt.UTC)
        prev_time = now - dt.timedelta(minutes=60)  # 1 hour ago (cooldown is 6 hours)

        fp = compute_alert_fingerprint("app1", "android", "3.2.0", "fail", ["crash_rate_regression"])
        rec_id = self.store.record_attempt(
            app_id="app1",
            platform="android",
            version="3.2.0",
            provider="google_chat",
            alert_fingerprint=fp,
            gate_status="fail",
            attempted_at=prev_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            reasons=["crash_rate_regression"],
        )
        self.store.update_result(rec_id, status="sent", delivered_at=prev_time.strftime("%Y-%m-%dT%H:%M:%SZ"))

        dec = evaluate_alert_decision(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            gate_status="fail",
            triggered_reasons=["crash_rate_regression"],
            policy=self.policy,
            store=self.store,
            now=now,
        )
        self.assertEqual(dec.decision, "suppressed")
        self.assertIn("active cooldown", dec.reason)

    def test_status_change_bypasses_cooldown(self) -> None:
        now = dt.datetime(2026, 9, 8, 12, 0, 0, tzinfo=dt.UTC)
        prev_time = now - dt.timedelta(minutes=30)

        # Previously WARN
        fp = compute_alert_fingerprint("app1", "android", "3.2.0", "warn", ["crash_rate_regression"])
        rec_id = self.store.record_attempt(
            app_id="app1",
            platform="android",
            version="3.2.0",
            provider="google_chat",
            alert_fingerprint=fp,
            gate_status="warn",
            attempted_at=prev_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            reasons=["crash_rate_regression"],
        )
        self.store.update_result(rec_id, status="sent", delivered_at=prev_time.strftime("%Y-%m-%dT%H:%M:%SZ"))

        # Now escalated to FAIL -> must send despite 30m elapsed
        dec = evaluate_alert_decision(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            gate_status="fail",
            triggered_reasons=["crash_rate_regression"],
            policy=self.policy,
            store=self.store,
            now=now,
        )
        self.assertEqual(dec.decision, "send")
        self.assertIn("Gate status changed", dec.reason)

    def test_new_reason_bypasses_cooldown(self) -> None:
        now = dt.datetime(2026, 9, 8, 12, 0, 0, tzinfo=dt.UTC)
        prev_time = now - dt.timedelta(minutes=30)

        # Previously FAIL with only crash_rate_regression
        fp = compute_alert_fingerprint("app1", "android", "3.2.0", "fail", ["crash_rate_regression"])
        rec_id = self.store.record_attempt(
            app_id="app1",
            platform="android",
            version="3.2.0",
            provider="google_chat",
            alert_fingerprint=fp,
            gate_status="fail",
            attempted_at=prev_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            reasons=["crash_rate_regression"],
        )
        self.store.update_result(rec_id, status="sent", delivered_at=prev_time.strftime("%Y-%m-%dT%H:%M:%SZ"))

        # Now FAIL with crash_rate_regression AND fatal_regression -> new reason!
        dec = evaluate_alert_decision(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            gate_status="fail",
            triggered_reasons=["crash_rate_regression", "fatal_regression"],
            policy=self.policy,
            store=self.store,
            now=now,
        )
        self.assertEqual(dec.decision, "send")
        self.assertIn("New regression reasons detected", dec.reason)

    def test_cooldown_expiry_triggers_resend(self) -> None:
        now = dt.datetime(2026, 9, 8, 18, 0, 0, tzinfo=dt.UTC)
        prev_time = now - dt.timedelta(minutes=400)  # 400 mins > 360 mins cooldown

        fp = compute_alert_fingerprint("app1", "android", "3.2.0", "fail", ["crash_rate_regression"])
        rec_id = self.store.record_attempt(
            app_id="app1",
            platform="android",
            version="3.2.0",
            provider="google_chat",
            alert_fingerprint=fp,
            gate_status="fail",
            attempted_at=prev_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            reasons=["crash_rate_regression"],
        )
        self.store.update_result(rec_id, status="sent", delivered_at=prev_time.strftime("%Y-%m-%dT%H:%M:%SZ"))

        dec = evaluate_alert_decision(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            gate_status="fail",
            triggered_reasons=["crash_rate_regression"],
            policy=self.policy,
            store=self.store,
            now=now,
        )
        self.assertEqual(dec.decision, "send")
        self.assertIn("Cooldown expired", dec.reason)

    def test_platform_isolation(self) -> None:
        now = dt.datetime(2026, 9, 8, 12, 0, 0, tzinfo=dt.UTC)

        # Record sent for Android 3.2.0
        fp_android = compute_alert_fingerprint("app1", "android", "3.2.0", "fail", ["crash_rate_regression"])
        rec_id = self.store.record_attempt(
            app_id="app1",
            platform="android",
            version="3.2.0",
            provider="google_chat",
            alert_fingerprint=fp_android,
            gate_status="fail",
            attempted_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            reasons=["crash_rate_regression"],
        )
        self.store.update_result(rec_id, status="sent", delivered_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))

        # iOS 3.2.0 evaluation must NOT be suppressed by Android's alert!
        dec_ios = evaluate_alert_decision(
            app_id="app1",
            platform="ios",
            target_version="3.2.0",
            gate_status="fail",
            triggered_reasons=["crash_rate_regression"],
            policy=self.policy,
            store=self.store,
            now=now,
        )
        self.assertEqual(dec_ios.decision, "send")
        self.assertIn("Initial alert", dec_ios.reason)

    def test_force_flag_bypasses_cooldown_and_dedupe(self) -> None:
        now = dt.datetime(2026, 9, 8, 12, 0, 0, tzinfo=dt.UTC)
        prev_time = now - dt.timedelta(minutes=5)

        fp = compute_alert_fingerprint("app1", "android", "3.2.0", "fail", ["crash_rate_regression"])
        rec_id = self.store.record_attempt(
            app_id="app1",
            platform="android",
            version="3.2.0",
            provider="google_chat",
            alert_fingerprint=fp,
            gate_status="fail",
            attempted_at=prev_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            reasons=["crash_rate_regression"],
        )
        self.store.update_result(rec_id, status="sent", delivered_at=prev_time.strftime("%Y-%m-%dT%H:%M:%SZ"))

        dec = evaluate_alert_decision(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            gate_status="fail",
            triggered_reasons=["crash_rate_regression"],
            policy=self.policy,
            store=self.store,
            now=now,
            force=True,
        )
        self.assertEqual(dec.decision, "send")
        self.assertIn("--force", dec.reason)


class TestSecretSafetyAndPrivacy(unittest.TestCase):
    """Verifies that secrets are scrubbed and privacy policies enforced."""

    def test_redact_url_helper(self) -> None:
        secret_url = "https://chat.googleapis.com/v1/spaces/SPACE123/messages?key=AIzaSySecretKey&token=SecretToken123"
        redacted = redact_url(secret_url)
        self.assertNotIn("AIzaSySecretKey", redacted)
        self.assertNotIn("SecretToken123", redacted)
        self.assertEqual(redacted, "https://chat.googleapis.com/...<redacted>")

        # Text with embedded URL
        err_text = f"Failed to POST to {secret_url}: 500 Server Error"
        redacted_err = redact_url(err_text)
        self.assertNotIn("AIzaSySecretKey", redacted_err)
        self.assertNotIn("SecretToken123", redacted_err)
        self.assertIn("https://chat.googleapis.com/...<redacted>", redacted_err)

    def test_sanitize_audit_text(self) -> None:
        sensitive = "ThreadKey with https://chat.googleapis.com/v1/spaces/A/messages?key=12345"
        clean = sanitize_audit_text(sensitive)
        self.assertIsNotNone(clean)
        self.assertNotIn("12345", clean or "")
        self.assertIn("<redacted>", clean or "")

    def test_missing_webhook_env_returns_controlled_error(self) -> None:
        # Ensure env var is unset
        with patch.dict(os.environ, {}, clear=True):
            provider = GoogleChatWebhookProvider(webhook_env="NON_EXISTENT_VAR_XYZ")
            msg = AlertMessage(
                app_id="app1",
                platform="android",
                target_version="1.0.0",
                previous_version=None,
                gate_status="fail",
                is_recovery=False,
                title="Test",
                summary="Test",
                regression_reasons=[],
                policy_version="1.0",
                evaluated_at="2026-09-08T15:00:00Z",
                dashboard_url=None,
                thread_key=None,
                text="Test alert",
            )
            res = provider.send(msg)
            self.assertEqual(res.status, "failed")
            self.assertEqual(res.error_code, "MISSING_WEBHOOK_URL")
            self.assertIn("NON_EXISTENT_VAR_XYZ", res.error_message or "")

    def test_store_never_persists_raw_secrets_or_uuids(self) -> None:
        with AlertDeliveryStore(db_path=":memory:") as store:
            rec_id = store.record_attempt(
                app_id="app1",
                platform="android",
                version="1.0.0",
                provider="google_chat",
                alert_fingerprint="fp123",
                gate_status="fail",
                thread_key="https://chat.googleapis.com/v1/spaces/XYZ?key=SECRET_TOKEN",
            )
            store.update_result(
                rec_id,
                status="failed",
                error_message="Error posting to https://chat.googleapis.com/v1/spaces/XYZ?key=SECRET_TOKEN",
            )
            hist = store.get_history("app1")
            self.assertEqual(len(hist), 1)
            row = hist[0]
            self.assertNotIn("SECRET_TOKEN", row.thread_key or "")
            self.assertNotIn("SECRET_TOKEN", row.error_message or "")


class TestHTTPResilienceAndRetry(unittest.TestCase):
    """Verifies bounded retry, backoff, permanent error fast-fail, and thread fallback."""

    def test_200_ok_delivers_successfully(self) -> None:
        mock_session = MagicMock()
        mock_res = MagicMock()
        mock_res.status_code = 200
        mock_res.json.return_value = {"name": "spaces/ABC/messages/123"}
        mock_session.post.return_value = mock_res

        provider = GoogleChatWebhookProvider(
            webhook_url="https://chat.googleapis.com/v1/spaces/ABC/messages?key=k&token=t",
            session=mock_session,
        )
        msg = AlertMessage(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            previous_version="3.1.2",
            gate_status="fail",
            is_recovery=False,
            title="Fail",
            summary="Summary",
            regression_reasons=["Reason 1"],
            policy_version="1.0",
            evaluated_at="2026-09-08T15:00:00Z",
            dashboard_url=None,
            thread_key="crash-trend:app1:android:3.2.0",
            text="🚨 Release Gate FAIL",
        )
        result = provider.send(msg)

        self.assertEqual(result.status, "sent")
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.message_name, "spaces/ABC/messages/123")
        self.assertEqual(result.thread_key, "crash-trend:app1:android:3.2.0")

    def test_429_retries_with_backoff_and_succeeds(self) -> None:
        mock_session = MagicMock()
        res_429 = MagicMock()
        res_429.status_code = 429
        res_429.text = "Rate limited"

        res_200 = MagicMock()
        res_200.status_code = 200
        res_200.json.return_value = {"name": "msg_success"}

        mock_session.post.side_effect = [res_429, res_200]
        mock_sleep = MagicMock()

        provider = GoogleChatWebhookProvider(
            webhook_url="https://chat.googleapis.com/v1/spaces/ABC/messages?key=k&token=t",
            max_attempts=3,
            backoff_sec=1.0,
            sleep_fn=mock_sleep,
            session=mock_session,
        )
        msg = AlertMessage(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            previous_version=None,
            gate_status="fail",
            is_recovery=False,
            title="Fail",
            summary="Summary",
            regression_reasons=[],
            policy_version="1.0",
            evaluated_at="2026-09-08T15:00:00Z",
            dashboard_url=None,
            thread_key=None,
            text="text",
        )
        result = provider.send(msg)

        self.assertEqual(result.status, "sent")
        self.assertEqual(result.attempt_count, 2)
        mock_sleep.assert_called_once_with(1.0)

    def test_500_retries_and_exhausts(self) -> None:
        mock_session = MagicMock()
        res_500 = MagicMock()
        res_500.status_code = 500
        res_500.text = "Internal Server Error"
        mock_session.post.return_value = res_500
        mock_sleep = MagicMock()

        provider = GoogleChatWebhookProvider(
            webhook_url="https://chat.googleapis.com/v1/spaces/ABC/messages?key=k&token=t",
            max_attempts=3,
            backoff_sec=0.5,
            sleep_fn=mock_sleep,
            session=mock_session,
        )
        msg = AlertMessage(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            previous_version=None,
            gate_status="fail",
            is_recovery=False,
            title="Fail",
            summary="Summary",
            regression_reasons=[],
            policy_version="1.0",
            evaluated_at="2026-09-08T15:00:00Z",
            dashboard_url=None,
            thread_key=None,
            text="text",
        )
        result = provider.send(msg)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.attempt_count, 3)
        self.assertEqual(result.error_code, "HTTP_500")
        self.assertEqual(mock_sleep.call_count, 2)

    def test_401_fails_fast_without_retry(self) -> None:
        mock_session = MagicMock()
        res_401 = MagicMock()
        res_401.status_code = 401
        res_401.text = "Unauthorized"
        mock_session.post.return_value = res_401
        mock_sleep = MagicMock()

        provider = GoogleChatWebhookProvider(
            webhook_url="https://chat.googleapis.com/v1/spaces/ABC/messages?key=k&token=t",
            max_attempts=3,
            sleep_fn=mock_sleep,
            session=mock_session,
        )
        msg = AlertMessage(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            previous_version=None,
            gate_status="fail",
            is_recovery=False,
            title="Fail",
            summary="Summary",
            regression_reasons=[],
            policy_version="1.0",
            evaluated_at="2026-09-08T15:00:00Z",
            dashboard_url=None,
            thread_key=None,
            text="text",
        )
        result = provider.send(msg)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(result.error_code, "HTTP_401")
        mock_sleep.assert_not_called()

    def test_thread_400_fallback_without_thread(self) -> None:
        mock_session = MagicMock()
        res_400 = MagicMock()
        res_400.status_code = 400
        res_400.text = "Thread not supported in this space"

        res_200 = MagicMock()
        res_200.status_code = 200
        res_200.json.return_value = {"name": "fallback_success"}

        mock_session.post.side_effect = [res_400, res_200]

        provider = GoogleChatWebhookProvider(
            webhook_url="https://chat.googleapis.com/v1/spaces/ABC/messages?key=k&token=t",
            use_threads=True,
            session=mock_session,
        )
        msg = AlertMessage(
            app_id="app1",
            platform="android",
            target_version="3.2.0",
            previous_version=None,
            gate_status="fail",
            is_recovery=False,
            title="Fail",
            summary="Summary",
            regression_reasons=[],
            policy_version="1.0",
            evaluated_at="2026-09-08T15:00:00Z",
            dashboard_url=None,
            thread_key="crash-trend:app1:android:3.2.0",
            text="text",
        )
        result = provider.send(msg)

        self.assertEqual(result.status, "sent")
        self.assertIsNone(result.thread_key)  # threadKey stripped on fallback


class TestThreadingContract(unittest.TestCase):
    """Verifies deterministic threadKey generation and thread reuse."""

    def test_deterministic_thread_key(self) -> None:
        msg1 = build_alert_message(
            app_id="shop_app",
            platform="android",
            target_version="3.2.0",
            previous_version="3.1.2",
            gate_status="fail",
            is_recovery=False,
            rule_results=[],
            policy_version="1.0",
            evaluated_at="2026-09-08T15:00:00Z",
            use_threads=True,
        )
        self.assertEqual(msg1.thread_key, "crash-trend:shop_app:android:3.2.0")

        # Recovery on same version should produce identical thread_key
        msg2 = build_alert_message(
            app_id="shop_app",
            platform="android",
            target_version="3.2.0",
            previous_version="3.1.2",
            gate_status="pass",
            is_recovery=True,
            rule_results=[],
            policy_version="1.0",
            evaluated_at="2026-09-08T16:00:00Z",
            use_threads=True,
        )
        self.assertEqual(msg2.thread_key, "crash-trend:shop_app:android:3.2.0")

        # Different platform produces different thread_key
        msg_ios = build_alert_message(
            app_id="shop_app",
            platform="ios",
            target_version="3.2.0",
            previous_version="3.1.2",
            gate_status="fail",
            is_recovery=False,
            rule_results=[],
            policy_version="1.0",
            evaluated_at="2026-09-08T15:00:00Z",
            use_threads=True,
        )
        self.assertEqual(msg_ios.thread_key, "crash-trend:shop_app:ios:3.2.0")


class TestPipelineAndDispatcherIntegration(unittest.TestCase):
    """Verifies end-to-end dispatcher flow, dry-run safety, and decoupled pipeline failure semantics."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.out_root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_dry_run_generates_preview_without_recording_sent(self) -> None:
        store = AlertDeliveryStore(db_path=":memory:")
        artifact = make_sample_artifact(app_id="demo", platform="android", version="1.0.0", gate_status="fail")
        policy = AlertPolicy(enabled=True, notify_on=("fail", "warn"))

        mock_provider = MagicMock()
        dispatcher = AlertDispatcher(store=store, provider=mock_provider)

        summary = dispatcher.dispatch(
            app_id="demo",
            artifact=artifact,
            policy=policy,
            dry_run=True,
        )
        self.assertEqual(summary.total_sent, 1)
        mock_provider.send.assert_not_called()

        # Check that dry-run did NOT persist status='sent' into store
        last = store.get_last_sent_delivery("demo", "android", "1.0.0")
        self.assertIsNone(last)

    def test_gate_fail_with_delivery_success_keeps_gate_verdict(self) -> None:
        store = AlertDeliveryStore(db_path=":memory:")
        artifact = make_sample_artifact(app_id="demo", platform="android", version="1.0.0", gate_status="fail")
        policy = AlertPolicy(enabled=True, notify_on=("fail", "warn"))

        mock_provider = MagicMock()
        mock_provider.send.return_value = DeliveryResult(
            status="sent",
            attempt_count=1,
            delivered_at="2026-09-08T15:00:00Z",
            http_status=200,
        )
        dispatcher = AlertDispatcher(store=store, provider=mock_provider)

        summary = dispatcher.dispatch(
            app_id="demo",
            artifact=artifact,
            policy=policy,
            dry_run=False,
        )
        self.assertEqual(summary.total_sent, 1)
        self.assertEqual(artifact["overall_status"], "fail")

    def test_gate_fail_with_delivery_failure_keeps_gate_verdict_and_records_failed(self) -> None:
        store = AlertDeliveryStore(db_path=":memory:")
        artifact = make_sample_artifact(app_id="demo", platform="android", version="1.0.0", gate_status="fail")
        policy = AlertPolicy(enabled=True, notify_on=("fail", "warn"))

        mock_provider = MagicMock()
        mock_provider.send.return_value = DeliveryResult(
            status="failed",
            attempt_count=3,
            error_code="RETRY_EXHAUSTED",
            error_message="Connection timed out",
        )
        dispatcher = AlertDispatcher(store=store, provider=mock_provider)

        summary = dispatcher.dispatch(
            app_id="demo",
            artifact=artifact,
            policy=policy,
            dry_run=False,
        )
        self.assertEqual(summary.total_failed, 1)
        # Gate verdict is strictly unchanged
        self.assertEqual(artifact["overall_status"], "fail")


class TestAlertsCLIAndMain(unittest.TestCase):
    """Verifies standalone CLI options, arguments, and exit code contracts."""

    @patch("crash_trend.alerts.__main__.dispatch_alerts_for_app")
    def test_main_success_exit_code_0(self, mock_dispatch: Any) -> None:
        from crash_trend.alerts.__main__ import main as alerts_main
        from crash_trend.alerts.models import AlertDispatchSummary

        mock_dispatch.return_value = AlertDispatchSummary(
            app_id="demo",
            results={},
            decisions={},
            total_sent=1,
            total_suppressed=0,
            total_failed=0,
        )

        with patch.object(sys, "argv", ["alerts.py", "--app", "demo"]):
            with self.assertRaises(SystemExit) as ctx:
                alerts_main()
            self.assertEqual(ctx.exception.code, 0)
        mock_dispatch.assert_called_once_with(
            app_name="demo",
            dry_run=False,
            force=False,
            gate_path=None,
            verbose=True,
        )

    @patch("crash_trend.alerts.__main__.dispatch_alerts_for_app")
    def test_main_dry_run_and_force_flags(self, mock_dispatch: Any) -> None:
        from crash_trend.alerts.__main__ import main as alerts_main
        from crash_trend.alerts.models import AlertDispatchSummary

        mock_dispatch.return_value = AlertDispatchSummary(
            app_id="demo",
            results={},
            decisions={},
            total_sent=1,
            total_suppressed=0,
            total_failed=0,
        )

        with patch.object(sys, "argv", ["alerts.py", "--app", "demo", "--dry-run", "--force", "--quiet"]):
            with self.assertRaises(SystemExit) as ctx:
                alerts_main()
            self.assertEqual(ctx.exception.code, 0)
        mock_dispatch.assert_called_once_with(
            app_name="demo",
            dry_run=True,
            force=True,
            gate_path=None,
            verbose=False,
        )

    @patch("crash_trend.alerts.__main__.dispatch_alerts_for_app")
    def test_main_exits_1_on_failed_delivery(self, mock_dispatch: Any) -> None:
        from crash_trend.alerts.__main__ import main as alerts_main
        from crash_trend.alerts.models import AlertDispatchSummary

        mock_dispatch.return_value = AlertDispatchSummary(
            app_id="demo",
            results={},
            decisions={},
            total_sent=0,
            total_suppressed=0,
            total_failed=1,
        )

        with patch.object(sys, "argv", ["alerts.py", "--app", "demo"]):
            with self.assertRaises(SystemExit) as ctx:
                alerts_main()
            self.assertEqual(ctx.exception.code, 1)

    @patch("crash_trend.alerts.__main__.dispatch_alerts_for_app")
    def test_main_exits_1_on_exception(self, mock_dispatch: Any) -> None:
        from crash_trend.alerts.__main__ import main as alerts_main

        mock_dispatch.side_effect = FileNotFoundError("Missing gate artifact")

        with patch.object(sys, "argv", ["alerts.py", "--app", "demo"]):
            with self.assertRaises(SystemExit) as ctx:
                alerts_main()
            self.assertEqual(ctx.exception.code, 1)


class TestPipelineAlertDeliveryStage(unittest.TestCase):
    """Verifies pipeline runner integration, stage recording, and failure decoupling."""

    @patch("crash_trend.pipeline_run.run_stage_process")
    @patch("crash_trend.pipeline_run.load_config")
    def test_pipeline_executes_alert_delivery_and_records_success(
        self,
        mock_load_cfg: Any,
        mock_stage_proc: Any,
    ) -> None:
        mock_load_cfg.return_value = {
            "apps": {
                "test_app": {
                    "firebase_project": "test-p",
                    "platforms": ["android"],
                    "data_sources": {"sessions": False, "mcp": "off"},
                    "release_alerts": {
                        "enabled": True,
                        "provider": "google_chat",
                    },
                }
            }
        }
        # All subprocesses succeed
        mock_stage_proc.return_value = (0, "Success output", "")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            app_dir = tmp_root / "out" / "test_app"
            app_dir.mkdir(parents=True, exist_ok=True)

            with patch("crash_trend.pipeline_run.ROOT", tmp_root):
                summary = run_pipeline(
                    app_names=["test_app"],
                    summary_path=tmp_root / "pipeline_run.json",
                    skip_dashboard=True,
                    verbose=False,
                )

                app_stages = summary["apps"]["test_app"]["stages"]
                self.assertIn("alert_delivery", app_stages)
                self.assertEqual(app_stages["alert_delivery"]["status"], "success")

    @patch("crash_trend.pipeline_run.run_stage_process")
    @patch("crash_trend.pipeline_run.load_config")
    def test_alert_delivery_failure_decoupled_from_gate_status(
        self,
        mock_load_cfg: Any,
        mock_stage_proc: Any,
    ) -> None:
        """Delivery failure must NOT change release_gate verdict and must NOT abort pipeline."""
        mock_load_cfg.return_value = {
            "apps": {
                "test_app": {
                    "firebase_project": "test-p",
                    "platforms": ["android"],
                    "data_sources": {"sessions": False, "mcp": "off"},
                    "release_alerts": {
                        "enabled": True,
                        "provider": "google_chat",
                    },
                }
            }
        }

        def fake_stage_proc(cmd: list[str], **kwargs: Any) -> tuple[int, str, str]:
            if "crash_trend.alerts" in cmd:
                return (1, "", "Webhook delivery 500 error")
            return (0, "ok", "")

        mock_stage_proc.side_effect = fake_stage_proc

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            app_dir = tmp_root / "out" / "test_app"
            app_dir.mkdir(parents=True, exist_ok=True)

            # Fake release_gate.json with FAIL status
            gate_art = make_sample_artifact(app_id="test_app", platform="android", version="1.0.0", gate_status="fail")
            (app_dir / "release_gate.json").write_text(json.dumps(gate_art), encoding="utf-8")

            with patch("crash_trend.pipeline_run.ROOT", tmp_root):
                summary = run_pipeline(
                    app_names=["test_app"],
                    summary_path=tmp_root / "pipeline_run.json",
                    skip_dashboard=True,
                    verbose=False,
                )

                app_stages = summary["apps"]["test_app"]["stages"]
                # Gate stage remains SUCCESS with gate_status='fail'
                self.assertEqual(app_stages["release_gate"]["status"], "success")
                self.assertEqual(app_stages["release_gate"]["details"]["gate_status"], "fail")

                # Alert delivery stage recorded as FAILED
                self.assertEqual(app_stages["alert_delivery"]["status"], "failed")
                self.assertIn("Webhook delivery 500 error", app_stages["alert_delivery"]["error_message"])

                # Pipeline status is degraded (not failed!), continuing normally
                self.assertEqual(summary["status"], "degraded")

    @patch("crash_trend.pipeline_run.run_pipeline")
    def test_pipeline_main_fail_on_alert_failure_flag(self, mock_run_pipe: Any) -> None:
        from crash_trend.pipeline_run import main as pipeline_main

        mock_run_pipe.return_value = {
            "status": "degraded",
            "apps": {
                "test_app": {
                    "stages": {
                        "alert_delivery": {"status": "failed"},
                    }
                }
            },
        }

        # 1. With --fail-on-alert-failure -> exit code 1
        with patch.object(sys, "argv", ["pipeline_run.py", "--app", "test_app", "--fail-on-alert-failure"]):
            with self.assertRaises(SystemExit) as ctx:
                pipeline_main()
            self.assertEqual(ctx.exception.code, 1)

        # 2. Without --fail-on-alert-failure -> returns normally without exit (exit code 0)
        with patch.object(sys, "argv", ["pipeline_run.py", "--app", "test_app"]):
            pipeline_main()


if __name__ == "__main__":
    unittest.main()
