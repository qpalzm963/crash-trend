"""Tests for Data Source Profile (Crashlytics-only vs Sessions Optional) - Issue #17."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from crash_trend.config import get_data_sources, is_sessions_enabled
from crash_trend.fetch_sessions import (
    enrich_app_dashboard_with_sessions,
    fetch_sessions_for_app,
)
from crash_trend.schema_v2 import validate_dashboard_v2


class TestDataSourceProfile(unittest.TestCase):
    """Verifies data source profile resolution and Sessions-optional behavior."""

    def test_get_data_sources_defaults_and_explicit_flags(self) -> None:
        # 1. Defaults when app_cfg has no sessions_dataset
        cfg1 = {"firebase_project": "proj-1"}
        ds1 = get_data_sources(cfg1)
        self.assertTrue(ds1["crashlytics_bigquery"])
        self.assertFalse(ds1["sessions"])
        self.assertEqual(ds1["mcp"], "optional")
        self.assertFalse(is_sessions_enabled(cfg1))

        # 2. Explicit data_sources.sessions = false
        cfg2 = {
            "firebase_project": "proj-2",
            "sessions_dataset": "firebase_sessions",
            "data_sources": {"sessions": False},
        }
        ds2 = get_data_sources(cfg2)
        self.assertFalse(ds2["sessions"])
        self.assertFalse(is_sessions_enabled(cfg2))

        # 3. Explicit data_sources.sessions = true
        cfg3 = {
            "firebase_project": "proj-3",
            "data_sources": {"sessions": True},
        }
        ds3 = get_data_sources(cfg3)
        self.assertTrue(ds3["sessions"])
        self.assertTrue(is_sessions_enabled(cfg3))

        # 4. Shorthand top-level sessions: false
        cfg4 = {"firebase_project": "proj-4", "sessions": False}
        self.assertFalse(is_sessions_enabled(cfg4))

        # 5. Shorthand top-level sessions_enabled: false
        cfg5 = {"firebase_project": "proj-5", "sessions_enabled": False}
        self.assertFalse(is_sessions_enabled(cfg5))

        # 6. sessions_dataset present without explicit disable defaults to True
        cfg6 = {"firebase_project": "proj-6", "sessions_dataset": "firebase_sessions"}
        self.assertTrue(is_sessions_enabled(cfg6))

    @patch("crash_trend.fetch_sessions.get_app")
    def test_fetch_sessions_for_app_skips_bigquery_when_disabled(self, mock_get_app) -> None:
        mock_get_app.return_value = {
            "firebase_project": "disabled-proj",
            "data_sources": {"sessions": False},
        }
        mock_client = MagicMock()

        res = fetch_sessions_for_app("disabled_app", client=mock_client)

        # Ensure BigQuery client was NEVER called
        mock_client.list_tables.assert_not_called()
        mock_client.query.assert_not_called()

        self.assertEqual(res["sources"]["status"], "unavailable")
        self.assertIn("disabled", res["sources"]["error_message"].lower())
        self.assertIsNone(res["kpi"]["crash_free_users"]["rate"])
        self.assertIsNone(res["kpi"]["crash_free_sessions"]["rate"])

    def test_mixed_multi_app_bundle_passes_validation(self) -> None:
        # App 1: Full Sessions available
        app1_data = {
            "app_name": "app1",
            "display_name": "App One",
            "schema_version": "2.0.0",
            "generated_at": "2026-09-03T00:00:00Z",
            "period": {
                "start_date": "2026-08-04",
                "end_date": "2026-09-02",
                "days": 30,
                "comparison_start_date": "2026-07-05",
                "comparison_end_date": "2026-08-03",
            },
            "sources": {
                "crashlytics_bq": {"status": "available", "last_sync_timestamp": "2026-09-03T00:00:00Z", "error_message": None},
                "firebase_sessions": {"status": "available", "last_sync_timestamp": "2026-09-03T00:00:00Z", "error_message": None},
                "mcp_crashlytics": {"status": "available", "last_sync_timestamp": "2026-09-03T00:00:00Z", "error_message": None},
                "gemini_ai": {"status": "available", "last_sync_timestamp": "2026-09-03T00:00:00Z", "error_message": None},
            },
            "kpi": {
                "crash_events": {"value": 10, "previous_value": 10, "change_pct": 0.0},
                "affected_users": {"value": 5, "previous_value": 5, "change_pct": 0.0},
                "crash_free_users": {"status": "available", "rate": 0.99, "previous_rate": 0.99, "change_pct_points": 0.0, "total": 500, "crashed": 5, "unavailable_reason": None},
                "crash_free_sessions": {"status": "available", "rate": 0.995, "previous_rate": 0.995, "change_pct_points": 0.0, "total": 1000, "crashed": 5, "unavailable_reason": None},
                "new_issues": {"value": 0, "previous_value": 0, "change_pct": 0.0},
            },
            "daily_trend": [{"date": "2026-08-04", "fatal": 1, "non_fatal": 0, "anr": 0, "total": 1, "users": 1, "sessions_total": 100, "crashed_sessions": 1, "crash_free_sessions_rate": 0.99}],
            "version_health": [{"version": "1.0.0", "crash_events": 10, "affected_users": 5, "fatal_events": 10, "anr_events": 0, "non_fatal_events": 0, "crash_free_users_rate": 0.99, "crash_free_sessions_rate": 0.995, "adoption_rate": 1.0}],
            "distributions": {"platform": [{"key": "ios", "events": 10, "share": 1.0}], "device_models": [], "os_versions": [], "app_versions": [], "custom_keys": {}},
            "top_issues": [],
            "ai_summary": {"status": "available", "overview": "Good", "key_takeaways": [], "distribution_insights": [], "action_items": []},
        }

        # App 2: Crashlytics-only (Sessions disabled)
        app2_data = {
            "app_name": "app2",
            "display_name": "App Two (Crashlytics-only)",
            "schema_version": "2.0.0",
            "generated_at": "2026-09-03T00:00:00Z",
            "period": {
                "start_date": "2026-08-04",
                "end_date": "2026-09-02",
                "days": 30,
                "comparison_start_date": "2026-07-05",
                "comparison_end_date": "2026-08-03",
            },
            "sources": {
                "crashlytics_bq": {"status": "available", "last_sync_timestamp": "2026-09-03T00:00:00Z", "error_message": None},
                "firebase_sessions": {"status": "unavailable", "last_sync_timestamp": None, "error_message": "Sessions 匯出已停用 (disabled in config)"},
                "mcp_crashlytics": {"status": "unavailable", "last_sync_timestamp": None, "error_message": None},
                "gemini_ai": {"status": "disabled", "last_sync_timestamp": None, "error_message": "Disabled"},
            },
            "kpi": {
                "crash_events": {"value": 8, "previous_value": 8, "change_pct": 0.0},
                "affected_users": {"value": 4, "previous_value": 4, "change_pct": 0.0},
                "crash_free_users": {"status": "unavailable", "rate": None, "previous_rate": None, "change_pct_points": None, "total": None, "crashed": None, "unavailable_reason": "Sessions 匯出已停用"},
                "crash_free_sessions": {"status": "unavailable", "rate": None, "previous_rate": None, "change_pct_points": None, "total": None, "crashed": None, "unavailable_reason": "Sessions 匯出已停用"},
                "new_issues": {"value": 0, "previous_value": 0, "change_pct": 0.0},
            },
            "daily_trend": [{"date": "2026-08-04", "fatal": 0, "non_fatal": 1, "anr": 0, "total": 1, "users": 1, "sessions_total": None, "crashed_sessions": None, "crash_free_sessions_rate": None}],
            "version_health": [{"version": "2.0.0", "crash_events": 8, "affected_users": 4, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 8, "crash_free_users_rate": None, "crash_free_sessions_rate": None, "adoption_rate": None}],
            "distributions": {"platform": [{"key": "android", "events": 8, "share": 1.0}], "device_models": [], "os_versions": [], "app_versions": [], "custom_keys": {}},
            "top_issues": [],
            "ai_summary": {"status": "disabled", "overview": "Deterministic score only", "key_takeaways": [], "distribution_insights": [], "action_items": []},
        }

        bundle = {
            "schema_version": "2.0.0",
            "generated_at": "2026-09-03T00:00:00Z",
            "apps": {
                "app1": app1_data,
                "app2": app2_data,
            },
        }

        # Must validate cleanly against Schema V2
        validate_dashboard_v2(bundle)


if __name__ == "__main__":
    unittest.main()
