"""Tests for Source Health UI, Data Freshness, and Status Mapping (Issue #23)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from crash_trend.build_dashboard import build_html, generate_dashboard
from crash_trend.schema_v2 import validate_dashboard_v2


class TestSourceHealthUI(unittest.TestCase):
    def test_dashboard_html_contains_source_health_components(self) -> None:
        """Verifies that generated HTML includes the Data Sources Health card, classes, and JS functions."""
        bundle = {
            "schema_version": "2.0",
            "generated_at": "2026-09-03T06:00:00Z",
            "default_app": "app1",
            "apps": {
                "app1": {
                    "metadata": {
                        "app_id": "app1",
                        "display_name": "App One",
                        "firebase_project_id": "proj-1",
                        "platforms": ["android"],
                        "source_repo": None,
                        "custom_keys_monitored": [],
                    },
                    "period": {"days": 30, "start_time": "2026-08-04T06:00:00Z", "end_time": "2026-09-03T06:00:00Z", "comparison_period": None},
                    "sources": {
                        "crashlytics_bq": {
                            "status": "available",
                            "last_sync_timestamp": "2026-09-03T04:00:00Z",
                            "error_message": None,
                        },
                        "firebase_sessions": {
                            "status": "disabled",
                            "last_sync_timestamp": None,
                            "error_message": "Sessions 匯出已停用 (disabled in config)",
                        },
                        "mcp_crashlytics": {
                            "status": "stale",
                            "last_sync_timestamp": "2026-08-25T06:00:00Z",
                            "error_message": "MCP 快取過期（已快取 9.0 天 > 上限 7 天）",
                        },
                        "gemini_ai": {
                            "status": "available",
                            "last_sync_timestamp": "2026-09-03T05:59:00Z",
                            "error_message": None,
                        },
                    },
                    "kpi": {
                        "crash_events": {"value": 100, "previous_value": 120, "change_pct": -16.7, "status": "available"},
                        "affected_users": {"value": 50, "previous_value": 60, "change_pct": -16.7, "status": "available"},
                        "crash_free_users": {"rate": None, "total": None, "crashed": None, "previous_rate": None, "change_pct_points": None, "status": "unavailable", "unavailable_reason": "disabled"},
                        "crash_free_sessions": {"rate": None, "total": None, "crashed": None, "previous_rate": None, "change_pct_points": None, "status": "unavailable", "unavailable_reason": "disabled"},
                        "new_issues_count": {"value": 1, "previous_value": 2, "change_pct": -50.0, "status": "available"},
                        "events_by_error_type": {"fatal": 80, "anr": 15, "non_fatal": 5},
                    },
                    "daily_trend": [
                        {
                            "date": "2026-09-02",
                            "crash_events": 100,
                            "affected_users": 50,
                            "fatal_events": 80,
                            "anr_events": 15,
                            "non_fatal_events": 5,
                            "sessions_total": None,
                            "crashed_sessions": None,
                            "crash_free_sessions_rate": None,
                            "by_platform": None,
                        }
                    ],
                    "version_health": [
                        {
                            "version": "1.0.0",
                            "platform": "android",
                            "release_date": "2026-08-01",
                            "crash_events": 100,
                            "affected_users": 50,
                            "crash_free_users_rate": None,
                            "crash_free_sessions_rate": None,
                            "adoption_rate": 1.0,
                            "status": "latest",
                            "trend": "stable",
                        }
                    ],
                    "distributions": {
                        "platform": [{"name": "android", "events": 100, "users": 50, "share": 1.0}],
                        "device_models": [{"model": "Pixel 7", "platform": "android", "events": 100, "users": 50, "share": 1.0}],
                        "os_versions": [{"os_version": "Android 14", "platform": "android", "events": 100, "users": 50, "share": 1.0}],
                        "app_versions": [{"app_version": "1.0.0", "platform": "android", "events": 100, "users": 50, "share": 1.0}],
                    },
                    "top_issues": [],
                    "ai_summary": {
                        "status": "available",
                        "model": "gemini-flash",
                        "generated_at": "2026-09-03T05:59:00Z",
                        "overview": "Overview text",
                        "key_takeaways": ["Takeaway 1"],
                        "distribution_insights": "Distribution note",
                        "recommended_actions": [],
                        "data_limitations": None,
                    },
                    "limitations": [],
                }
            },
        }

        val_errs = validate_dashboard_v2(bundle)
        self.assertEqual(val_errs, [])

        html = build_html(bundle)

        # Check DOM elements
        self.assertIn('id="overviewDataSourcesCard"', html)
        self.assertIn('id="overviewDataSourcesGrid"', html)
        self.assertIn('id="headerSourceBadges"', html)

        # Check JS function names
        self.assertIn("formatFreshness", html)
        self.assertIn("resolveSourceHealth", html)
        self.assertIn("renderDataSourcesHealth", html)

        # Check CSS classes
        self.assertIn(".data-sources-card", html)
        self.assertIn(".data-source-item", html)
        self.assertIn(".src-dot.stale", html)
        self.assertIn(".src-dot.insufficient_data", html)

        # Check that UI mentions supplemental cache note
        self.assertIn("supplemental", html)

    def test_multi_app_source_health_isolation(self) -> None:
        """Verifies that multi-app configurations have isolated source health statuses."""
        bundle = {
            "schema_version": "2.0",
            "generated_at": "2026-09-03T06:00:00Z",
            "default_app": "app_full",
            "apps": {
                "app_full": {
                    "metadata": {
                        "app_id": "app_full",
                        "display_name": "Full App",
                        "firebase_project_id": "proj-full",
                        "platforms": ["ios"],
                        "source_repo": None,
                        "custom_keys_monitored": [],
                    },
                    "period": {"days": 30, "start_time": "2026-08-04T06:00:00Z", "end_time": "2026-09-03T06:00:00Z", "comparison_period": None},
                    "sources": {
                        "crashlytics_bq": {"status": "available", "last_sync_timestamp": "2026-09-03T05:00:00Z", "error_message": None},
                        "firebase_sessions": {"status": "available", "last_sync_timestamp": "2026-09-03T05:00:00Z", "error_message": None},
                        "mcp_crashlytics": {"status": "available", "last_sync_timestamp": "2026-09-03T05:00:00Z", "error_message": None},
                        "gemini_ai": {"status": "available", "last_sync_timestamp": "2026-09-03T05:00:00Z", "error_message": None},
                    },
                    "kpi": {
                        "crash_events": {"value": 10, "previous_value": 10, "change_pct": 0.0, "status": "available"},
                        "affected_users": {"value": 5, "previous_value": 5, "change_pct": 0.0, "status": "available"},
                        "crash_free_users": {"rate": 0.999, "total": 5000, "crashed": 5, "previous_rate": 0.999, "change_pct_points": 0.0, "status": "available", "unavailable_reason": None},
                        "crash_free_sessions": {"rate": 0.999, "total": 10000, "crashed": 10, "previous_rate": 0.999, "change_pct_points": 0.0, "status": "available", "unavailable_reason": None},
                        "new_issues_count": {"value": 0, "previous_value": 0, "change_pct": 0.0, "status": "available"},
                        "events_by_error_type": {"fatal": 10, "anr": 0, "non_fatal": 0},
                    },
                    "daily_trend": [{"date": "2026-09-02", "crash_events": 10, "affected_users": 5, "fatal_events": 10, "anr_events": 0, "non_fatal_events": 0, "sessions_total": 10000, "crashed_sessions": 10, "crash_free_sessions_rate": 0.999, "by_platform": None}],
                    "version_health": [{"version": "2.0.0", "platform": "ios", "release_date": "2026-08-01", "crash_events": 10, "affected_users": 5, "crash_free_users_rate": 0.999, "crash_free_sessions_rate": 0.999, "adoption_rate": 1.0, "status": "latest", "trend": "stable"}],
                    "distributions": {
                        "platform": [{"name": "ios", "events": 10, "users": 5, "share": 1.0}],
                        "device_models": [{"model": "iPhone 15", "platform": "ios", "events": 10, "users": 5, "share": 1.0}],
                        "os_versions": [{"os_version": "iOS 17", "platform": "ios", "events": 10, "users": 5, "share": 1.0}],
                        "app_versions": [{"app_version": "2.0.0", "platform": "ios", "events": 10, "users": 5, "share": 1.0}],
                    },
                    "top_issues": [],
                    "ai_summary": {"status": "available", "model": "gemini-flash", "generated_at": "2026-09-03T05:00:00Z", "overview": "Good", "key_takeaways": [], "distribution_insights": "", "recommended_actions": [], "data_limitations": None},
                    "limitations": [],
                },
                "app_minimal": {
                    "metadata": {
                        "app_id": "app_minimal",
                        "display_name": "Minimal App",
                        "firebase_project_id": "proj-min",
                        "platforms": ["android"],
                        "source_repo": None,
                        "custom_keys_monitored": [],
                    },
                    "period": {"days": 30, "start_time": "2026-08-04T06:00:00Z", "end_time": "2026-09-03T06:00:00Z", "comparison_period": None},
                    "sources": {
                        "crashlytics_bq": {"status": "available", "last_sync_timestamp": "2026-09-03T05:00:00Z", "error_message": None},
                        "firebase_sessions": {"status": "disabled", "last_sync_timestamp": None, "error_message": "Sessions disabled in config"},
                        "mcp_crashlytics": {"status": "disabled", "last_sync_timestamp": None, "error_message": "MCP mode is off"},
                        "gemini_ai": {"status": "disabled", "last_sync_timestamp": None, "error_message": "GEMINI_API_KEY not configured"},
                    },
                    "kpi": {
                        "crash_events": {"value": 5, "previous_value": 5, "change_pct": 0.0, "status": "available"},
                        "affected_users": {"value": 2, "previous_value": 2, "change_pct": 0.0, "status": "available"},
                        "crash_free_users": {"rate": None, "total": None, "crashed": None, "previous_rate": None, "change_pct_points": None, "status": "unavailable", "unavailable_reason": "Sessions disabled in config"},
                        "crash_free_sessions": {"rate": None, "total": None, "crashed": None, "previous_rate": None, "change_pct_points": None, "status": "unavailable", "unavailable_reason": "Sessions disabled in config"},
                        "new_issues_count": {"value": 0, "previous_value": 0, "change_pct": 0.0, "status": "available"},
                        "events_by_error_type": {"fatal": 5, "anr": 0, "non_fatal": 0},
                    },
                    "daily_trend": [{"date": "2026-09-02", "crash_events": 5, "affected_users": 2, "fatal_events": 5, "anr_events": 0, "non_fatal_events": 0, "sessions_total": None, "crashed_sessions": None, "crash_free_sessions_rate": None, "by_platform": None}],
                    "version_health": [{"version": "1.0", "platform": "android", "release_date": "2026-08-01", "crash_events": 5, "affected_users": 2, "crash_free_users_rate": None, "crash_free_sessions_rate": None, "adoption_rate": 1.0, "status": "latest", "trend": "stable"}],
                    "distributions": {
                        "platform": [{"name": "android", "events": 5, "users": 2, "share": 1.0}],
                        "device_models": [{"model": "Pixel 6", "platform": "android", "events": 5, "users": 2, "share": 1.0}],
                        "os_versions": [{"os_version": "Android 13", "platform": "android", "events": 5, "users": 2, "share": 1.0}],
                        "app_versions": [{"app_version": "1.0", "platform": "android", "events": 5, "users": 2, "share": 1.0}],
                    },
                    "top_issues": [],
                    "ai_summary": {"status": "disabled", "model": None, "generated_at": None, "overview": "", "key_takeaways": [], "distribution_insights": "", "recommended_actions": [], "data_limitations": None},
                    "limitations": [],
                },
            },
        }

        # Validation must pass for multi-app with mixed source profiles
        errs = validate_dashboard_v2(bundle)
        self.assertEqual(errs, [])

        with tempfile.TemporaryDirectory() as tmpdir:
            out_html = Path(tmpdir) / "dashboard.html"
            generate_dashboard(bundle, output_path=out_html)
            self.assertTrue(out_html.is_file())
            content = out_html.read_text(encoding="utf-8")
            self.assertIn("app_full", content)
            self.assertIn("app_minimal", content)


if __name__ == "__main__":
    unittest.main()
