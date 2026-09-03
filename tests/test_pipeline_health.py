"""Unit tests for Pipeline Health, Sanitization, and Run Summary (Issue #22)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from crash_trend.pipeline_health import (
    PipelineRunTracker,
    load_run_summary,
    now_utc_iso,
    sanitize_error_message,
)


class TestPipelineHealth(unittest.TestCase):
    def test_sanitize_error_message(self) -> None:
        # None and empty
        self.assertIsNone(sanitize_error_message(None))
        self.assertEqual(sanitize_error_message(""), "")

        # Google API Key
        raw_key = "Failed request with key AIzaSyD9876543210AbCdEfGhIjKlMnOpQrS and project"
        sanitized = sanitize_error_message(raw_key)
        self.assertNotIn("AIzaSyD9876543210AbCdEfGhIjKlMnOpQrS", sanitized)
        self.assertIn("AIza[REDACTED]", sanitized)

        # Bearer token
        raw_bearer = "HTTP 401 Unauthorized: Bearer ya29.a0AfH6SMBxyz123-abc"
        sanitized = sanitize_error_message(raw_bearer)
        self.assertNotIn("ya29.a0AfH6SMBxyz123-abc", sanitized)
        self.assertIn("Bearer [REDACTED]", sanitized)

        # Private Key block
        raw_pk = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...\n-----END RSA PRIVATE KEY-----"
        sanitized = sanitize_error_message(raw_pk)
        self.assertNotIn("MIIEowIBAAKCAQEA0", sanitized)
        self.assertIn("[PRIVATE KEY REDACTED]", sanitized)

        # Credentials & tokens in query params / assignments
        raw_param = "Connection refused: api_key=secret_12345678&token=tok_abcdefgh123"
        sanitized = sanitize_error_message(raw_param)
        self.assertNotIn("secret_12345678", sanitized)
        self.assertNotIn("tok_abcdefgh123", sanitized)
        self.assertIn("api_key=[REDACTED]", sanitized)
        self.assertIn("token=[REDACTED]", sanitized)

        # Truncation for oversized message
        huge_msg = "Error line\n" * 100
        sanitized_huge = sanitize_error_message(huge_msg, max_len=200)
        self.assertLessEqual(len(sanitized_huge), 250)
        self.assertIn("... (truncated)", sanitized_huge)

    def test_pipeline_run_tracker_all_success(self) -> None:
        tracker = PipelineRunTracker(started_at="2026-09-03T06:00:00Z")
        tracker.record_stage("app1", "crashlytics_bigquery", "success", "2026-09-03T06:00:01Z", "2026-09-03T06:00:05Z")
        tracker.record_stage("app1", "sessions", "success", "2026-09-03T06:00:05Z", "2026-09-03T06:00:07Z")
        tracker.record_stage("app1", "mcp", "skipped", "2026-09-03T06:00:07Z", "2026-09-03T06:00:07Z")
        tracker.record_stage("app1", "issue_details", "success", "2026-09-03T06:00:07Z", "2026-09-03T06:00:09Z")
        tracker.record_stage("app1", "normalize", "success", "2026-09-03T06:00:09Z", "2026-09-03T06:00:10Z")
        tracker.record_stage("app1", "ai", "success", "2026-09-03T06:00:10Z", "2026-09-03T06:00:15Z")
        tracker.record_stage(None, "build_dashboard", "success", "2026-09-03T06:00:15Z", "2026-09-03T06:00:18Z")

        summary = tracker.build_summary("2026-09-03T06:00:18Z")
        self.assertEqual(summary["schema_version"], "1.0")
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["apps"]["app1"]["status"], "success")
        self.assertEqual(summary["apps"]["app1"]["stages"]["crashlytics_bigquery"]["status"], "success")
        self.assertEqual(summary["apps"]["app1"]["stages"]["crashlytics_bigquery"]["duration_sec"], 4.0)
        self.assertEqual(summary["apps"]["app1"]["stages"]["mcp"]["status"], "skipped")
        self.assertEqual(summary["build_dashboard"]["status"], "success")
        self.assertEqual(summary["duration_sec"], 18.0)

    def test_pipeline_run_tracker_optional_failure_is_degraded(self) -> None:
        tracker = PipelineRunTracker(started_at="2026-09-03T06:00:00Z")
        # Core stage succeeds
        tracker.record_stage("app1", "crashlytics_bigquery", "success", "2026-09-03T06:00:00Z", "2026-09-03T06:00:05Z")
        # Optional stage MCP fails
        tracker.record_stage(
            "app1",
            "mcp",
            "failed",
            "2026-09-03T06:00:05Z",
            "2026-09-03T06:00:06Z",
            error_message="MCP server error: api_key=secret12345",
        )
        # Optional stage AI fails
        tracker.record_stage(
            "app1",
            "ai",
            "failed",
            "2026-09-03T06:00:06Z",
            "2026-09-03T06:00:08Z",
            error_message="Gemini quota exceeded",
        )

        summary = tracker.build_summary("2026-09-03T06:00:08Z")
        # Optional failure must result in degraded, NOT failed
        self.assertEqual(summary["status"], "degraded")
        self.assertEqual(summary["apps"]["app1"]["status"], "degraded")
        mcp_stage = summary["apps"]["app1"]["stages"]["mcp"]
        self.assertEqual(mcp_stage["status"], "failed")
        self.assertNotIn("secret12345", mcp_stage["error_message"])
        self.assertIn("api_key=[REDACTED]", mcp_stage["error_message"])

    def test_pipeline_run_tracker_core_failure_is_failed(self) -> None:
        tracker = PipelineRunTracker(started_at="2026-09-03T06:00:00Z")
        # Core stage BigQuery fails
        tracker.record_stage(
            "app1",
            "crashlytics_bigquery",
            "failed",
            "2026-09-03T06:00:00Z",
            "2026-09-03T06:00:02Z",
            error_message="Dataset not found",
        )

        summary = tracker.build_summary("2026-09-03T06:00:02Z")
        # Core failure must mark app and overall as failed
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["apps"]["app1"]["status"], "failed")

    def test_pipeline_run_tracker_disabled_and_skipped_remain_success(self) -> None:
        tracker = PipelineRunTracker(started_at="2026-09-03T06:00:00Z")
        tracker.record_stage("app1", "crashlytics_bigquery", "success", "2026-09-03T06:00:00Z", "2026-09-03T06:00:03Z")
        # Sessions disabled in config
        tracker.record_stage("app1", "sessions", "disabled", "2026-09-03T06:00:03Z", "2026-09-03T06:00:03Z")
        # MCP off or manual
        tracker.record_stage("app1", "mcp", "disabled", "2026-09-03T06:00:03Z", "2026-09-03T06:00:03Z")

        summary = tracker.build_summary("2026-09-03T06:00:04Z")
        # Disabled/skipped do NOT cause failure or degradation
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["apps"]["app1"]["status"], "success")
        self.assertEqual(summary["apps"]["app1"]["stages"]["sessions"]["status"], "disabled")
        self.assertEqual(summary["apps"]["app1"]["stages"]["mcp"]["status"], "disabled")

    def test_pipeline_run_tracker_multi_app_isolation(self) -> None:
        tracker = PipelineRunTracker(started_at="2026-09-03T06:00:00Z")
        # App 1: Has optional failure (degraded)
        tracker.record_stage("app1", "crashlytics_bigquery", "success", "2026-09-03T06:00:00Z", "2026-09-03T06:00:02Z")
        tracker.record_stage("app1", "ai", "failed", "2026-09-03T06:00:02Z", "2026-09-03T06:00:03Z", error_message="AI failed")

        # App 2: Fully clean (success)
        tracker.record_stage("app2", "crashlytics_bigquery", "success", "2026-09-03T06:00:03Z", "2026-09-03T06:00:05Z")
        tracker.record_stage("app2", "ai", "success", "2026-09-03T06:00:05Z", "2026-09-03T06:00:07Z")

        summary = tracker.build_summary("2026-09-03T06:00:07Z")
        self.assertEqual(summary["apps"]["app1"]["status"], "degraded")
        self.assertEqual(summary["apps"]["app2"]["status"], "success")
        # App 2 AI stage is success and not contaminated by App 1
        self.assertEqual(summary["apps"]["app2"]["stages"]["ai"]["status"], "success")
        # Overall run is degraded because app1 was degraded
        self.assertEqual(summary["status"], "degraded")

    def test_atomic_save_and_load_summary(self) -> None:
        tracker = PipelineRunTracker(started_at="2026-09-03T06:00:00Z")
        tracker.record_stage("shop_app", "crashlytics_bigquery", "success", "2026-09-03T06:00:00Z", "2026-09-03T06:00:02Z")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "out" / "pipeline_run.json"
            saved_path = tracker.save_summary(out_file)
            self.assertTrue(saved_path.is_file())

            loaded = load_run_summary(saved_path)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["schema_version"], "1.0")
            self.assertEqual(loaded["apps"]["shop_app"]["status"], "success")

    def test_run_pipeline_orchestration(self) -> None:
        from unittest.mock import patch
        from crash_trend.pipeline_run import run_pipeline

        fake_cfg = {
            "apps": {
                "mock_app1": {
                    "firebase_project": "p1",
                    "data_sources": {"sessions": False},
                    "mcp": "off",
                },
                "mock_app2": {
                    "firebase_project": "p2",
                    "sessions_dataset": "sessions",
                    "mcp": {"mode": "weekly"},
                },
            }
        }

        # Mock run_stage_process: let mock_app2's sessions return failure, but all others succeed
        def fake_run_stage(cmd, cwd=None, env=None):
            cmd_str = " ".join(cmd)
            if "fetch_sessions.py" in cmd_str and "mock_app2" in cmd_str:
                return 1, "", "BigQuery sessions 503 Service Unavailable"
            if "fetch_stacktraces.py" in cmd_str:
                return 0, "快取仍有效（2 天 < 7 天）", ""
            return 0, "success output", ""

        with tempfile.TemporaryDirectory() as tmpdir:
            sum_path = Path(tmpdir) / "pipeline_run.json"
            with patch("crash_trend.pipeline_run.load_config", return_value=fake_cfg):
                with patch("crash_trend.pipeline_run.get_app", side_effect=lambda a: fake_cfg["apps"][a]):
                    with patch("crash_trend.pipeline_run.run_stage_process", side_effect=fake_run_stage):
                        summary = run_pipeline(
                            app_names=["mock_app1", "mock_app2"],
                            summary_path=sum_path,
                            skip_dashboard=False,
                            verbose=False,
                        )

                        # mock_app1: Sessions disabled, MCP off -> success
                        self.assertEqual(summary["apps"]["mock_app1"]["status"], "success")
                        self.assertEqual(summary["apps"]["mock_app1"]["stages"]["sessions"]["status"], "disabled")
                        self.assertEqual(summary["apps"]["mock_app1"]["stages"]["mcp"]["status"], "disabled")

                        # mock_app2: Sessions failed (optional), MCP fresh skipped -> degraded
                        self.assertEqual(summary["apps"]["mock_app2"]["status"], "degraded")
                        self.assertEqual(summary["apps"]["mock_app2"]["stages"]["sessions"]["status"], "failed")
                        self.assertEqual(summary["apps"]["mock_app2"]["stages"]["mcp"]["status"], "skipped")

                        # Overall status should be degraded (not failed) because only optional stage failed
                        self.assertEqual(summary["status"], "degraded")
                        self.assertEqual(summary["build_dashboard"]["status"], "success")

                        # Verify written file on disk
                        self.assertTrue(sum_path.is_file())
                        loaded = json.loads(sum_path.read_text(encoding="utf-8"))
                        self.assertEqual(loaded["status"], "degraded")


if __name__ == "__main__":
    unittest.main()

