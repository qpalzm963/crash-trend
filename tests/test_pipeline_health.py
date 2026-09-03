"""Unit tests for Pipeline Health, Sanitization, and Run Summary (Issue #22)."""

from __future__ import annotations

import json
import os
import tempfile
import time
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

    def test_graceful_failures_exit_zero_detected_from_artifacts(self) -> None:
        """Regression test for Blocking 1: Subprocesses exit 0 but artifacts indicate error."""
        from unittest.mock import patch
        from crash_trend.pipeline_run import run_pipeline

        fake_cfg = {
            "apps": {
                "graceful_app": {
                    "firebase_project": "p1",
                    "sessions_dataset": "sessions",
                    "mcp": {"mode": "weekly"},
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tmproot = Path(tmpdir)
            out_app = tmproot / "out" / "graceful_app"
            out_app.mkdir(parents=True, exist_ok=True)

            # Artifacts with graceful failure (exit code was 0, but content contains error)
            (out_app / "dashboard_v2.json").write_text(json.dumps({
                "sources": {
                    "crashlytics_bq": {"status": "available"},
                    "firebase_sessions": {"status": "error", "error_message": "Sessions query returned 404 table not found"},
                    "gemini_ai": {"status": "error", "error_message": "Gemini quota exceeded"},
                },
                "ai_summary": {"status": "error", "data_limitations": "Gemini quota exceeded"},
            }), encoding="utf-8")

            (out_app / "sessions.json").write_text(json.dumps({
                "sources": {"status": "error", "error_message": "Sessions query returned 404 table not found"}
            }), encoding="utf-8")

            # MCP wrote stacktraces_last_error.json and exited 0
            (out_app / "stacktraces_last_error.json").write_text(json.dumps({
                "error_message": "Firebase login required",
                "errors": [{"stage": "mcp_handshake", "message": "Firebase login required"}]
            }), encoding="utf-8")

            sum_path = tmproot / "out" / "pipeline_run.json"

            with patch("crash_trend.pipeline_run.ROOT", tmproot):
                with patch("crash_trend.pipeline_run.load_config", return_value=fake_cfg):
                    with patch("crash_trend.pipeline_run.get_app", return_value=fake_cfg["apps"]["graceful_app"]):
                        with patch("crash_trend.pipeline_run.run_stage_process", return_value=(0, "ok", "")):
                            with patch.dict("os.environ", {"GEMINI_API_KEY": "fake-key"}):
                                summary = run_pipeline(
                                    app_names=["graceful_app"],
                                    summary_path=sum_path,
                                    skip_dashboard=True,
                                    verbose=False,
                                )

            app_sum = summary["apps"]["graceful_app"]
            # Sessions must be recorded as failed, NOT success!
            self.assertEqual(app_sum["stages"]["sessions"]["status"], "failed")
            self.assertIn("404", app_sum["stages"]["sessions"]["error_message"])

            # MCP must be recorded as failed, NOT success!
            self.assertEqual(app_sum["stages"]["mcp"]["status"], "failed")
            self.assertIn("Firebase login required", app_sum["stages"]["mcp"]["error_message"])

            # AI must be recorded as failed, NOT success!
            self.assertEqual(app_sum["stages"]["ai"]["status"], "failed")
            self.assertIn("quota", app_sum["stages"]["ai"]["error_message"])

            # App status must be degraded (since BQ was success, but optional stages failed)
            self.assertEqual(app_sum["status"], "degraded")
            self.assertEqual(summary["status"], "degraded")

    def test_ai_enabled_with_gemini_key_url(self) -> None:
        """Regression test for High Priority issue: AI enabled detection supports GEMINI_KEY_URL."""
        from unittest.mock import patch
        from crash_trend.pipeline_run import run_pipeline

        fake_cfg = {
            "apps": {
                "ai_url_app": {
                    "firebase_project": "p1",
                    "data_sources": {"sessions": False},
                    "mcp": "off",
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tmproot = Path(tmpdir)
            out_app = tmproot / "out" / "ai_url_app"
            out_app.mkdir(parents=True, exist_ok=True)

            (out_app / "dashboard_v2.json").write_text(json.dumps({
                "sources": {
                    "crashlytics_bq": {"status": "available"},
                    "gemini_ai": {"status": "available"},
                },
                "ai_summary": {"status": "available"},
            }), encoding="utf-8")

            sum_path = tmproot / "out" / "pipeline_run.json"

            # No GEMINI_API_KEY in env, but resolve_api_key returns a key from GEMINI_KEY_URL
            with patch.dict("os.environ", {}, clear=True):
                with patch("crash_trend.pipeline_run.ROOT", tmproot):
                    with patch("crash_trend.pipeline_run.load_config", return_value=fake_cfg):
                        with patch("crash_trend.pipeline_run.get_app", return_value=fake_cfg["apps"]["ai_url_app"]):
                            with patch("crash_trend.pipeline_run.run_stage_process", return_value=(0, "ok", "")):
                                with patch("crash_trend.ai_provider.resolve_gemini_key", return_value="key-from-url"):
                                    summary = run_pipeline(
                                        app_names=["ai_url_app"],
                                        summary_path=sum_path,
                                        skip_dashboard=True,
                                        verbose=False,
                                    )

            app_sum = summary["apps"]["ai_url_app"]
            # AI stage must be success, NOT disabled!
            self.assertEqual(app_sum["stages"]["ai"]["status"], "success")
            self.assertEqual(app_sum["status"], "success")

    def test_provisional_summary_does_not_freeze_finished_at(self) -> None:
        """Regression test for Blocking 1: Provisional save must NOT prematurely freeze finished_at."""
        tracker = PipelineRunTracker(started_at="2026-09-03T06:00:00Z")
        tracker.record_stage("app1", "crashlytics_bigquery", "success", "2026-09-03T06:00:00Z", "2026-09-03T06:00:05Z")

        # 1. Provisional save before build_dashboard
        prov = tracker.build_summary("2026-09-03T06:00:05Z", finalize=False)
        self.assertEqual(prov["finished_at"], "2026-09-03T06:00:05Z")
        self.assertEqual(prov["duration_sec"], 5.0)
        # tracker.finished_at must NOT be set permanently!
        self.assertIsNone(tracker.finished_at)

        # 2. build_dashboard stage executes from 06:00:05 to 06:00:15
        tracker.record_stage(None, "build_dashboard", "success", "2026-09-03T06:00:05Z", "2026-09-03T06:00:15Z")

        # 3. Finalized summary
        final = tracker.build_summary("2026-09-03T06:00:15Z", finalize=True)
        self.assertEqual(final["finished_at"], "2026-09-03T06:00:15Z")
        self.assertEqual(final["duration_sec"], 15.0)
        self.assertGreaterEqual(final["finished_at"], final["build_dashboard"]["finished_at"])

    def test_mcp_fresh_cache_skip_not_failed_by_historical_error_file(self) -> None:
        """Regression test for High Priority: Historical stacktraces_last_error.json must not fail fresh cache skip."""
        from unittest.mock import patch
        from crash_trend.pipeline_run import run_pipeline

        fake_cfg = {
            "apps": {
                "mcp_skip_app": {
                    "firebase_project": "p1",
                    "data_sources": {"sessions": False},
                    "mcp": {"mode": "weekly", "max_age_days": 7},
                }
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tmproot = Path(tmpdir)
            out_app = tmproot / "out" / "mcp_skip_app"
            out_app.mkdir(parents=True, exist_ok=True)

            (out_app / "dashboard_v2.json").write_text(json.dumps({
                "sources": {
                    "crashlytics_bq": {"status": "available"},
                    "firebase_sessions": {"status": "disabled"},
                    "mcp_crashlytics": {"status": "available"},
                    "gemini_ai": {"status": "disabled"},
                },
                "ai_summary": {"status": "disabled"},
            }), encoding="utf-8")

            # Historical error file from a past failed run
            err_file = out_app / "stacktraces_last_error.json"
            err_file.write_text(json.dumps({
                "error_message": "Past Firebase login expired error",
                "errors": [{"stage": "auth", "message": "Expired"}]
            }), encoding="utf-8")
            # Set mtime back in the past
            past_mtime = time.time() - 3600
            os.utime(err_file, (past_mtime, past_mtime))

            sum_path = tmproot / "out" / "pipeline_run.json"

            def fake_stage_exec(cmd, cwd=None, env=None):
                cmd_str = " ".join(cmd)
                if "fetch_stacktraces.py" in cmd_str:
                    # Returns 0 with fresh cache message, does NOT touch stacktraces_last_error.json
                    return 0, "（App「mcp_skip_app」MCP 快取仍有效（3.2 天 < 7 天），略過重新抓取）", ""
                return 0, "stage ok", ""

            with patch("crash_trend.pipeline_run.ROOT", tmproot):
                with patch("crash_trend.pipeline_run.load_config", return_value=fake_cfg):
                    with patch("crash_trend.pipeline_run.get_app", return_value=fake_cfg["apps"]["mcp_skip_app"]):
                        with patch("crash_trend.pipeline_run.run_stage_process", side_effect=fake_stage_exec):
                            summary = run_pipeline(
                                app_names=["mcp_skip_app"],
                                summary_path=sum_path,
                                skip_dashboard=True,
                                verbose=False,
                            )

            app_sum = summary["apps"]["mcp_skip_app"]
            # MCP stage must be skipped, NOT failed!
            self.assertEqual(app_sum["stages"]["mcp"]["status"], "skipped")
            self.assertEqual(app_sum["status"], "success")
            self.assertEqual(summary["status"], "success")


if __name__ == "__main__":
    unittest.main()



