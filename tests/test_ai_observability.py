"""Tests for AI Usage & Quota Observability (Dashboard V2.5 - Issue #42)."""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from crash_trend.ai_telemetry import (
    aggregate_ai_usage,
    load_ai_usage_history,
    record_ai_call,
)


class TestAIObservability(unittest.TestCase):
    """Test suite for Issue #42 AI Usage & Quota Observability."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.history_path = Path(self.tmp_dir.name) / "ai_usage_history.json"

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_1_record_ai_call_and_secret_sanitization(self) -> None:
        """Test 1: record_ai_call sanitizes secrets in error messages and writes valid record."""
        rec = record_ai_call(
            app_id="shop_app",
            task_type="deep_analysis",
            provider="gemini",
            model="gemini-3.8-flash",
            status="error",
            http_status=401,
            error_message="Invalid key: AIzaSySecretKeyXYZ1234567890123456 with token Bearer secret-token-abc",
            history_path=self.history_path,
        )
        self.assertEqual(rec["app_id"], "shop_app")
        self.assertEqual(rec["status"], "error")
        # Ensure secret tokens are sanitized
        self.assertNotIn("AIzaSySecretKeyXYZ1234567890123456", rec["error_message"])
        self.assertIn("AIza[REDACTED]", rec["error_message"])
        self.assertNotIn("Bearer secret-token-abc", rec["error_message"])

        # Check persistence
        loaded = load_ai_usage_history(history_path=self.history_path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["model"], "gemini-3.8-flash")

    def test_2_token_accounting_strict_no_hallucination(self) -> None:
        """Test 2: Token metrics only recorded when integers are present; no guessing."""
        # Case A: Provider returned real token usage
        rec1 = record_ai_call(
            app_id="shop_app",
            task_type="deep_analysis",
            provider="gemini",
            model="gemini-3.8-flash",
            tokens={"prompt_tokens": 1200, "completion_tokens": 350, "total_tokens": 1550},
            history_path=self.history_path,
        )
        self.assertEqual(rec1["tokens"]["total_tokens"], 1550)

        # Case B: Provider did not return token usage
        rec2 = record_ai_call(
            app_id="shop_app",
            task_type="issue_triage",
            provider="openrouter",
            model="openrouter/free",
            tokens=None,
            history_path=self.history_path,
        )
        self.assertIsNone(rec2["tokens"])

        # Case C: Provider returned empty dictionary or non-ints
        rec3 = record_ai_call(
            app_id="shop_app",
            task_type="issue_triage",
            provider="openrouter",
            model="openrouter/free",
            tokens={"prompt_tokens": None, "total_tokens": "invalid"},
            history_path=self.history_path,
        )
        self.assertIsNone(rec3["tokens"])

    def test_3_aggregate_ai_usage_metrics(self) -> None:
        """Test 3: aggregate_ai_usage groups by task, provider, model, status, and daily trend."""
        now = dt.datetime(2026, 9, 4, 10, 0, 0, tzinfo=dt.timezone.utc)
        sample_records = [
            # Day 1: Gemini deep_analysis success (with tokens)
            {
                "timestamp": "2026-09-02T10:00:00Z",
                "app_id": "shop_app",
                "task_type": "deep_analysis",
                "provider": "gemini",
                "model": "gemini-3.8-flash",
                "status": "success",
                "paid_model_allowed": False,
                "tokens": {"prompt_tokens": 500, "completion_tokens": 200, "total_tokens": 700},
            },
            # Day 2: OpenRouter Free triage success
            {
                "timestamp": "2026-09-03T11:00:00Z",
                "app_id": "shop_app",
                "task_type": "issue_triage",
                "provider": "openrouter",
                "model": "openrouter/free",
                "status": "success",
                "paid_model_allowed": False,
                "tokens": None,
            },
            # Day 2: OpenRouter Free rate-limit 429
            {
                "timestamp": "2026-09-03T11:05:00Z",
                "app_id": "shop_app",
                "task_type": "issue_triage",
                "provider": "openrouter",
                "model": "openrouter/free",
                "status": "rate_limit",
                "http_status": 429,
                "paid_model_allowed": False,
                "tokens": None,
                "error_message": "429 Rate limit exceeded",
            },
            # Day 3: Fallback event
            {
                "timestamp": "2026-09-04T08:00:00Z",
                "app_id": "custom_app",
                "task_type": "deep_analysis",
                "provider": "gemini",
                "model": "gemini-3.8-flash",
                "status": "fallback",
                "paid_model_allowed": False,
                "tokens": None,
            },
        ]

        summary = aggregate_ai_usage(sample_records, days=7, now=now)

        self.assertEqual(summary["total_requests"], 4)
        self.assertEqual(summary["success_count"], 2)
        self.assertEqual(summary["rate_limit_count"], 1)
        self.assertEqual(summary["fallback_count"], 1)
        self.assertEqual(summary["error_count"], 0)

        # 100% of requests were on free tier eligible models
        self.assertEqual(summary["free_tier_ratio"], 1.0)
        self.assertEqual(summary["cost_guard"]["policy"], "strict_free_guard_active")
        self.assertIn("Google", summary["cost_guard"]["disclaimer"])

        # Distribution breakdown
        self.assertEqual(summary["by_task_type"]["deep_analysis"], 2)
        self.assertEqual(summary["by_task_type"]["issue_triage"], 2)
        self.assertEqual(summary["by_provider"]["gemini"], 2)
        self.assertEqual(summary["by_provider"]["openrouter"], 2)
        self.assertEqual(summary["by_app"]["shop_app"], 3)
        self.assertEqual(summary["by_app"]["custom_app"], 1)

        # Token usage
        self.assertEqual(summary["tokens"]["status"], "available")
        self.assertEqual(summary["tokens"]["total_tokens"], 700)
        self.assertEqual(summary["tokens"]["prompt_tokens"], 500)
        self.assertEqual(summary["tokens"]["completion_tokens"], 200)

        # Daily trend
        self.assertEqual(len(summary["daily_trend"]), 3)
        day_dates = [d["date"] for d in summary["daily_trend"]]
        self.assertIn("2026-09-02", day_dates)
        self.assertIn("2026-09-03", day_dates)
        self.assertIn("2026-09-04", day_dates)

    def test_4_aggregate_empty_history_handles_gracefully(self) -> None:
        """Test 4: aggregate_ai_usage with empty history returns clean schema without errors."""
        summary = aggregate_ai_usage([], days=7)
        self.assertEqual(summary["total_requests"], 0)
        self.assertIsNone(summary["free_tier_ratio"])
        self.assertEqual(summary["tokens"]["status"], "unavailable")
        self.assertIsNone(summary["tokens"]["total_tokens"])
        self.assertEqual(summary["daily_trend"], [])

    def test_5_gemini_paid_model_not_counted_in_free_tier(self) -> None:
        """Test 5: Gemini Pro / preview models are strictly classified as paid, not Free Tier."""
        records = [
            # Call 1: Gemini Free Tier eligible Flash model
            {
                "timestamp": "2026-09-04T10:00:00Z",
                "app_id": "test_app",
                "task_type": "deep_analysis",
                "provider": "gemini",
                "model": "gemini-3.8-flash",
                "status": "success",
            },
            # Call 2: Gemini Pro model (Standard Free Tier is NOT available)
            {
                "timestamp": "2026-09-04T11:00:00Z",
                "app_id": "test_app",
                "task_type": "deep_analysis",
                "provider": "gemini",
                "model": "gemini-3.1-pro-preview",
                "status": "success",
                "paid_model_allowed": True,
            },
            # Call 3: OpenRouter free model
            {
                "timestamp": "2026-09-04T12:00:00Z",
                "app_id": "test_app",
                "task_type": "issue_triage",
                "provider": "openrouter",
                "model": "openrouter/free",
                "status": "success",
            },
            # Call 4: Gemini Flash-image model (paid-only, Standard Free Tier not available)
            {
                "timestamp": "2026-09-04T13:00:00Z",
                "app_id": "test_app",
                "task_type": "deep_analysis",
                "provider": "gemini",
                "model": "gemini-2.5-flash-image",
                "status": "success",
                "paid_model_allowed": True,
            },
        ]
        summary = aggregate_ai_usage(records, days=7)
        self.assertEqual(summary["total_requests"], 4)
        # Only call 1 and call 3 are free tier (2 / 4 = 0.5), call 2 (Pro) and call 4 (Flash-image) are NOT counted!
        self.assertEqual(summary["free_tier_count"], 2)
        self.assertEqual(summary["free_tier_ratio"], 0.5)
        self.assertTrue(summary["cost_guard"]["paid_models_ever_allowed"])


if __name__ == "__main__":
    unittest.main()
