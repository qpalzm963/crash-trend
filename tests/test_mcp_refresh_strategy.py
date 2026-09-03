"""Tests for Issue #18: MCP Refresh Strategy (off / manual / weekly & cache freshness)."""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crash_trend"))

from crash_trend.config import get_mcp_config, is_mcp_cache_fresh
from crash_trend.fetch_issue_details import (
    fetch_issue_details,
    get_mcp_source_status,
)
from crash_trend.fetch_stacktraces import McpError


class TestMcpConfigParsing(unittest.TestCase):
    def test_default_config_is_manual_with_7_days(self) -> None:
        cfg = {"firebase_project": "test-proj"}
        mcp = get_mcp_config(cfg)
        self.assertEqual(mcp["mode"], "manual")
        self.assertEqual(mcp["max_age_days"], 7)

    def test_data_sources_shorthand_string(self) -> None:
        # 1. off
        cfg1 = {"data_sources": {"mcp": "off"}}
        self.assertEqual(get_mcp_config(cfg1)["mode"], "off")

        # 2. weekly
        cfg2 = {"data_sources": {"mcp": "weekly"}}
        self.assertEqual(get_mcp_config(cfg2)["mode"], "weekly")

        # 3. optional (maps to manual)
        cfg3 = {"data_sources": {"mcp": "optional"}}
        self.assertEqual(get_mcp_config(cfg3)["mode"], "manual")

        # 4. disabled
        cfg4 = {"data_sources": {"mcp": "disabled"}}
        self.assertEqual(get_mcp_config(cfg4)["mode"], "off")

    def test_dict_config_with_custom_max_age_days(self) -> None:
        cfg = {
            "mcp": {
                "mode": "weekly",
                "max_age_days": 14,
            }
        }
        mcp = get_mcp_config(cfg)
        self.assertEqual(mcp["mode"], "weekly")
        self.assertEqual(mcp["max_age_days"], 14)

    def test_boolean_mcp_flags(self) -> None:
        self.assertEqual(get_mcp_config({"mcp": False})["mode"], "off")
        self.assertEqual(get_mcp_config({"mcp": True})["mode"], "manual")
        self.assertEqual(get_mcp_config({"mcp": {"enabled": False}})["mode"], "off")


class TestMcpCacheFreshness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmppath = Path(self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_nonexistent_cache_returns_not_fresh(self) -> None:
        fresh, age, gen_at = is_mcp_cache_fresh(self.tmppath / "missing.json")
        self.assertFalse(fresh)
        self.assertIsNone(age)
        self.assertIsNone(gen_at)

    def test_fresh_cache_within_max_age(self) -> None:
        cache_file = self.tmppath / "stacktraces.json"
        now = dt.datetime(2026, 9, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
        # Cached 2 days ago
        cached_time = (now - dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cache_file.write_text(json.dumps({"generated_at": cached_time, "issues": {}}), encoding="utf-8")

        fresh, age, gen_at = is_mcp_cache_fresh(cache_file, max_age_days=7, now=now)
        self.assertTrue(fresh)
        self.assertAlmostEqual(age, 2.0, delta=0.1)
        self.assertEqual(gen_at, cached_time)

    def test_stale_cache_exceeding_max_age(self) -> None:
        cache_file = self.tmppath / "stacktraces.json"
        now = dt.datetime(2026, 9, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
        # Cached 9 days ago
        cached_time = (now - dt.timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cache_file.write_text(json.dumps({"generated_at": cached_time, "issues": {}}), encoding="utf-8")

        fresh, age, gen_at = is_mcp_cache_fresh(cache_file, max_age_days=7, now=now)
        self.assertFalse(fresh)
        self.assertAlmostEqual(age, 9.0, delta=0.1)
        self.assertEqual(gen_at, cached_time)

    def test_corrupt_cache_handled_gracefully(self) -> None:
        cache_file = self.tmppath / "corrupt.json"
        cache_file.write_text("invalid json", encoding="utf-8")
        fresh, age, gen_at = is_mcp_cache_fresh(cache_file)
        self.assertFalse(fresh)
        self.assertIsNone(age)
        self.assertIsNone(gen_at)


class TestFetchStacktracesModes(unittest.TestCase):
    @patch("crash_trend.fetch_stacktraces.McpClient")
    @patch("crash_trend.fetch_stacktraces.get_app")
    def test_weekly_check_skips_when_mode_is_manual(self, mock_get_app, mock_client_cls) -> None:
        from crash_trend.fetch_stacktraces import main
        mock_get_app.return_value = {
            "firebase_project": "proj-1",
            "mcp": {"mode": "manual"},
        }
        with patch("sys.argv", ["fetch_stacktraces.py", "--app", "app1", "--weekly-check"]):
            # Should exit early without creating McpClient
            main()
        mock_client_cls.assert_not_called()

    @patch("crash_trend.fetch_stacktraces.McpClient")
    @patch("crash_trend.fetch_stacktraces.get_app")
    def test_mode_off_skips_execution(self, mock_get_app, mock_client_cls) -> None:
        from crash_trend.fetch_stacktraces import main
        mock_get_app.return_value = {
            "firebase_project": "proj-1",
            "mcp": "off",
        }
        with patch("sys.argv", ["fetch_stacktraces.py", "--app", "app1"]):
            main()
        mock_client_cls.assert_not_called()

    @patch("crash_trend.fetch_stacktraces.is_mcp_cache_fresh")
    @patch("crash_trend.fetch_stacktraces.McpClient")
    @patch("crash_trend.fetch_stacktraces.get_app")
    def test_weekly_check_skips_when_cache_is_fresh(
        self, mock_get_app, mock_client_cls, mock_fresh
    ) -> None:
        from crash_trend.fetch_stacktraces import main
        mock_get_app.return_value = {
            "firebase_project": "proj-1",
            "mcp": {"mode": "weekly", "max_age_days": 7},
        }
        mock_fresh.return_value = (True, 3.2, "2026-08-31T00:00:00Z")
        with patch("sys.argv", ["fetch_stacktraces.py", "--app", "app1", "--weekly-check"]):
            main()
        mock_client_cls.assert_not_called()

    @patch("crash_trend.fetch_stacktraces.write_json")
    @patch("crash_trend.fetch_stacktraces.McpClient")
    @patch("crash_trend.fetch_stacktraces.get_app")
    def test_mcp_client_failure_exits_cleanly_without_breaking_pipeline(
        self, mock_get_app, mock_client_cls, mock_write_json
    ) -> None:
        from crash_trend.fetch_stacktraces import main
        mock_get_app.return_value = {
            "firebase_project": "proj-1",
            "mcp": {"mode": "manual"},
            "app_ids": {"android": "1:123:android:abc"},
        }
        mock_client_cls.side_effect = McpError("firebase login required")
        with patch("sys.argv", ["fetch_stacktraces.py", "--app", "app1"]):
            # Must not raise exception
            main()
        mock_write_json.assert_called()


class TestFetchIssueDetailsSupplementalMerge(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmppath = Path(self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    @patch("crash_trend.fetch_issue_details.safe_get_app")
    def test_mcp_mode_off_marks_source_status_disabled(self, mock_get_app) -> None:
        mock_get_app.return_value = {
            "firebase_project": "test-p",
            "mcp": "off",
        }
        status = get_mcp_source_status("app_off")
        self.assertEqual(status["status"], "disabled")
        self.assertIsNone(status["last_sync_timestamp"])
        self.assertIn("disabled", status["error_message"].lower())

    @patch("crash_trend.fetch_issue_details.safe_get_app")
    def test_mcp_stale_cache_records_warning_in_source_status(self, mock_get_app) -> None:
        mock_get_app.return_value = {
            "firebase_project": "test-p",
            "mcp": {"mode": "manual", "max_age_days": 7},
        }
        with patch("crash_trend.fetch_issue_details.is_mcp_cache_fresh") as mock_fresh:
            with patch("pathlib.Path.exists", return_value=True):
                mock_fresh.return_value = (False, 12.5, "2026-08-20T10:00:00Z")
                status = get_mcp_source_status("app_stale")
                self.assertEqual(status["status"], "available")
                self.assertEqual(status["last_sync_timestamp"], "2026-08-20T10:00:00Z")
                self.assertIn("過期", status["error_message"])
                self.assertIn("12.5", status["error_message"])

    @patch("crash_trend.fetch_issue_details.load_issue_details_from_stacktraces_cache")
    @patch("crash_trend.fetch_issue_details.safe_get_app")
    def test_bigquery_complete_data_not_overwritten_by_mcp(
        self, mock_get_app, mock_load_cache
    ) -> None:
        mock_get_app.return_value = {"firebase_project": "test-p", "mcp": "manual"}
        mock_load_cache.return_value = {
            "ISSUE_1": {
                "blame_frame": {"file": "McpFile.kt", "line": 99, "symbol": "mcpFunc", "blamed": True},
                "detail": {
                    "stack_trace": "MCP stack trace line",
                    "breadcrumbs": [{"category": "ui", "message": "tap", "timestamp": "2026-09-01T00:00:00Z"}],
                    "logs": ["mcp log"],
                    "custom_keys": {"key": "val"},
                },
            }
        }

        # Mock BigQuery returning full detail
        with patch("crash_trend.fetch_issue_details.fetch_issue_details_from_bq") as mock_bq:
            mock_bq.return_value = {
                "ISSUE_1": {
                    "blame_frame": {"file": "BQFile.kt", "line": 42, "symbol": "bqFunc", "blamed": True},
                    "detail": {
                        "stack_trace": "BQ complete stack trace",
                        "breadcrumbs": [],  # Missing in BQ
                        "logs": ["bq log"],  # Present in BQ
                        "custom_keys": {},  # Missing in BQ
                    },
                }
            }

            mock_bq_client = MagicMock()
            mock_bq_client.list_tables.return_value = [MagicMock(table_id="app_events")]

            results = fetch_issue_details("app1", ["ISSUE_1"], bq_client=mock_bq_client)

            # Blame frame: BQ must be preserved!
            self.assertEqual(results["ISSUE_1"]["blame_frame"]["file"], "BQFile.kt")
            # Stack trace: BQ must be preserved!
            self.assertEqual(results["ISSUE_1"]["detail"]["stack_trace"], "BQ complete stack trace")
            # Logs: BQ must be preserved!
            self.assertEqual(results["ISSUE_1"]["detail"]["logs"], ["bq log"])
            # Breadcrumbs & Custom Keys: Missing in BQ, so supplemented from MCP!
            self.assertEqual(len(results["ISSUE_1"]["detail"]["breadcrumbs"]), 1)
            self.assertEqual(results["ISSUE_1"]["detail"]["custom_keys"], {"key": "val"})


if __name__ == "__main__":
    unittest.main()
