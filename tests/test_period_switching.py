"""Tests for Issue #28: [Dashboard V2.3] Period Switching: 7d / 30d / 90d Authoritative Metrics.

Covers:
1. Schema V2.3 periods validation (numeric day keys, period.days consistency, strictness).
2. Distinct user overcounting regression test (authoritative distinct users vs daily sum).
3. Multi-period BigQuery data transformation (transform_bq_to_v2 with periods).
4. Firebase Sessions multi-period enrichment and unavailable semantics.
5. Issue details and AI enrichment across period snapshots.
6. Dashboard UI contract: getCurPeriodSnapshot(), setPeriod(days), and period button state.
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from unittest import mock
from pathlib import Path

from crash_trend.schema_v2 import (
    AppDashboardV2Data,
    AppPeriodSnapshot,
    validate_app_dashboard_v2,
)
from crash_trend.fetch_bigquery import (
    transform_bq_to_v2,
    transform_bq_period_snapshot,
)
from crash_trend.fetch_sessions import (
    enrich_app_dashboard_with_sessions,
    build_unavailable_sessions_result,
)
from crash_trend.fetch_issue_details import (
    enrich_top_issues,
)
from crash_trend.analyze_gemini import (
    enrich_app_data_with_priority_and_ai,
)
from crash_trend.build_dashboard import (
    build_html,
    assemble_bundle_from_apps,
)


class TestPeriodSwitchingSchema(unittest.TestCase):
    """Verifies Schema V2.3 periods field validation."""

    def setUp(self):
        self.fixed_end = dt.datetime(2026, 9, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
        self.app_cfg = {
            "app_id": "test_app",
            "display_name": "Test App",
            "firebase_project": "test-proj",
            "platforms": ["android"],
        }
        self.mock_bq = {
            "tables": {
                "com_example_app_ANDROID": {
                    "overview": [{"total_events": 10, "distinct_users": 5, "fatal_events": 2, "anr_events": 1, "non_fatal_events": 7}],
                    "daily_trend": [{"date": "2026-09-03", "events": 10, "users": 5, "fatal_events": 2, "anr_events": 1, "non_fatal_events": 7}],
                    "top_issues": [],
                    "issue_versions": [],
                    "by_device": [],
                    "by_os": [],
                    "by_app_version": [],
                }
            }
        }

    def test_default_single_period_populates_periods_dict(self):
        """When queried for 30 days, transform_bq_to_v2 creates periods['30']."""
        app_data = transform_bq_to_v2(self.mock_bq, self.app_cfg, days=30, end_time=self.fixed_end)
        self.assertIn("periods", app_data)
        self.assertIn("30", app_data["periods"])
        self.assertEqual(app_data["periods"]["30"]["period"]["days"], 30)
        self.assertEqual(app_data["periods"]["30"]["kpi"]["crash_events"]["value"], 10)
        self.assertEqual(app_data["periods"]["30"]["kpi"]["affected_users"]["value"], 5)
        errors = validate_app_dashboard_v2(app_data)
        self.assertEqual(errors, [])

    def test_periods_validation_rejects_non_numeric_keys(self):
        """Periods dictionary keys must be numeric strings matching period.days."""
        app_data = transform_bq_to_v2(self.mock_bq, self.app_cfg, days=30, end_time=self.fixed_end)
        snap = app_data["periods"]["30"]
        app_data["periods"]["invalid_key"] = snap
        errors = validate_app_dashboard_v2(app_data)
        self.assertTrue(any("must be a numeric string" in e for e in errors), f"Actual errors: {errors}")

    def test_periods_validation_rejects_mismatched_period_days(self):
        """Key '7' must match snapshot['period']['days'] == 7."""
        app_data = transform_bq_to_v2(self.mock_bq, self.app_cfg, days=30, end_time=self.fixed_end)
        snap = dict(app_data["periods"]["30"])
        app_data["periods"]["7"] = snap
        errors = validate_app_dashboard_v2(app_data)
        self.assertTrue(any("must match key" in e for e in errors), f"Actual errors: {errors}")


class TestDistinctUserOvercountingRegression(unittest.TestCase):
    """Regression test ensuring distinct users are NOT summed across daily trend points."""

    def test_authoritative_distinct_users_prevents_overcounting(self):
        """
        Scenario:
          Day 1: User A (2 events)
          Day 2: User A (1 event)
          Day 3: User B (1 event)

        Authoritative 7d Metrics:
          Crash Events = 4
          Affected Users = 2 (User A, User B)

        Summing daily trend users would incorrectly yield 1 + 1 + 1 = 3.
        """
        mock_bq_regression = {
            "tables": {
                "com_example_app_ANDROID": {
                    "overview": [{
                        "total_events": 4,
                        "distinct_users": 2,  # Authoritative COUNT(DISTINCT installation_uuid)
                        "fatal_events": 1,
                        "anr_events": 0,
                        "non_fatal_events": 3,
                    }],
                    "daily_trend": [
                        {"date": "2026-09-01", "events": 2, "users": 1, "fatal_events": 1, "anr_events": 0, "non_fatal_events": 1},
                        {"date": "2026-09-02", "events": 1, "users": 1, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 1},
                        {"date": "2026-09-03", "events": 1, "users": 1, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 1},
                    ],
                    "top_issues": [],
                    "issue_versions": [],
                    "by_device": [],
                    "by_os": [],
                    "by_app_version": [],
                }
            }
        }

        app_cfg = {"app_id": "demo", "firebase_project": "demo-proj", "platforms": ["android"]}
        end_time = dt.datetime(2026, 9, 3, 23, 59, 59, tzinfo=dt.timezone.utc)
        res = transform_bq_to_v2(mock_bq_regression, app_cfg, days=7, end_time=end_time)

        # Total events is 4
        self.assertEqual(res["kpi"]["crash_events"]["value"], 4)
        # Authoritative distinct users is 2, NOT daily sum of 3!
        self.assertEqual(res["kpi"]["affected_users"]["value"], 2)
        # Daily trend points still preserve daily counts
        daily_users_sum = sum(p["affected_users"] for p in res["daily_trend"])
        self.assertEqual(daily_users_sum, 3)
        # Snapshot for 7d also preserves authoritative 2
        self.assertEqual(res["periods"]["7"]["kpi"]["affected_users"]["value"], 2)


class TestMultiPeriodTransformation(unittest.TestCase):
    """Tests multi-period transformation from BigQuery periods dictionary."""

    def setUp(self):
        self.end_time = dt.datetime(2026, 9, 3, 23, 59, 59, tzinfo=dt.timezone.utc)
        self.app_cfg = {"app_id": "clock_in", "firebase_project": "mp-clockin", "platforms": ["android"]}

        self.mock_multi_period_bq = {
            "periods": {
                "7": {
                    "tables": {
                        "com_mp_clockin_ANDROID": {
                            "overview": [{"total_events": 5, "distinct_users": 2, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 5}],
                            "daily_trend": [{"date": "2026-09-03", "events": 5, "users": 2, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 5}],
                            "top_issues": [{"issue_id": "iss_7", "events": 5, "users": 2, "error_type": "NON_FATAL", "issue_title": "Network Error"}],
                            "issue_versions": [{"issue_id": "iss_7", "app_version": "1.2.0", "events": 5, "users": 2}],
                            "by_device": [],
                            "by_os": [],
                            "by_app_version": [{"app_version": "1.2.0", "events": 5, "users": 2}],
                        }
                    }
                },
                "30": {
                    "tables": {
                        "com_mp_clockin_ANDROID": {
                            "overview": [{"total_events": 25, "distinct_users": 8, "fatal_events": 1, "anr_events": 0, "non_fatal_events": 24}],
                            "daily_trend": [
                                {"date": "2026-08-15", "events": 20, "users": 6, "fatal_events": 1, "anr_events": 0, "non_fatal_events": 19},
                                {"date": "2026-09-03", "events": 5, "users": 2, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 5},
                            ],
                            "top_issues": [
                                {"issue_id": "iss_fatal", "events": 10, "users": 3, "error_type": "FATAL", "issue_title": "Null Pointer"},
                                {"issue_id": "iss_7", "events": 15, "users": 5, "error_type": "NON_FATAL", "issue_title": "Network Error"},
                            ],
                            "issue_versions": [
                                {"issue_id": "iss_fatal", "app_version": "1.1.0", "events": 10, "users": 3},
                                {"issue_id": "iss_7", "app_version": "1.2.0", "events": 15, "users": 5},
                            ],
                            "by_device": [],
                            "by_os": [],
                            "by_app_version": [
                                {"app_version": "1.2.0", "events": 15, "users": 5},
                                {"app_version": "1.1.0", "events": 10, "users": 3},
                            ],
                        }
                    }
                },
                "90": {
                    "tables": {
                        "com_mp_clockin_ANDROID": {
                            "overview": [{"total_events": 100, "distinct_users": 20, "fatal_events": 5, "anr_events": 2, "non_fatal_events": 93}],
                            "daily_trend": [
                                {"date": "2026-06-15", "events": 75, "users": 15, "fatal_events": 4, "anr_events": 2, "non_fatal_events": 69},
                                {"date": "2026-08-15", "events": 20, "users": 6, "fatal_events": 1, "anr_events": 0, "non_fatal_events": 19},
                                {"date": "2026-09-03", "events": 5, "users": 2, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 5},
                            ],
                            "top_issues": [
                                {"issue_id": "iss_fatal", "events": 40, "users": 10, "error_type": "FATAL", "issue_title": "Null Pointer"},
                                {"issue_id": "iss_7", "events": 60, "users": 15, "error_type": "NON_FATAL", "issue_title": "Network Error"},
                            ],
                            "issue_versions": [
                                {"issue_id": "iss_fatal", "app_version": "1.1.0", "events": 40, "users": 10},
                                {"issue_id": "iss_7", "app_version": "1.2.0", "events": 60, "users": 15},
                            ],
                            "by_device": [],
                            "by_os": [],
                            "by_app_version": [
                                {"app_version": "1.2.0", "events": 60, "users": 15},
                                {"app_version": "1.1.0", "events": 40, "users": 10},
                            ],
                        }
                    }
                },
            }
        }

    def test_multi_period_snapshots_created_and_valid(self):
        """All 3 periods are transformed with independent authoritative metrics."""
        app_data = transform_bq_to_v2(self.mock_multi_period_bq, self.app_cfg, days=90, end_time=self.end_time)
        self.assertIn("periods", app_data)
        self.assertEqual(sorted(list(app_data["periods"].keys()), key=int), ["7", "30", "90"])

        snap_7 = app_data["periods"]["7"]
        snap_30 = app_data["periods"]["30"]
        snap_90 = app_data["periods"]["90"]

        # 7d snapshot assertions
        self.assertEqual(snap_7["period"]["days"], 7)
        self.assertEqual(snap_7["kpi"]["crash_events"]["value"], 5)
        self.assertEqual(snap_7["kpi"]["affected_users"]["value"], 2)
        self.assertEqual(len(snap_7["top_issues"]), 1)
        self.assertEqual(snap_7["top_issues"][0]["issue_id"], "iss_7")

        # 30d snapshot assertions
        self.assertEqual(snap_30["period"]["days"], 30)
        self.assertEqual(snap_30["kpi"]["crash_events"]["value"], 25)
        self.assertEqual(snap_30["kpi"]["affected_users"]["value"], 8)
        self.assertEqual(len(snap_30["top_issues"]), 2)

        # 90d snapshot assertions
        self.assertEqual(snap_90["period"]["days"], 90)
        self.assertEqual(snap_90["kpi"]["crash_events"]["value"], 100)
        self.assertEqual(snap_90["kpi"]["affected_users"]["value"], 20)

        # Top-level reflects active period (90)
        self.assertEqual(app_data["period"]["days"], 90)
        self.assertEqual(app_data["kpi"]["crash_events"]["value"], 100)

        # Full Schema V2 strict validation
        errors = validate_app_dashboard_v2(app_data)
        self.assertEqual(errors, [])


class TestSessionsMultiPeriodEnrichment(unittest.TestCase):
    """Tests Sessions enrichment across period snapshots."""

    def test_disabled_sessions_marks_all_periods_unavailable(self):
        """When Sessions is disabled, all period snapshots must be unavailable (never 0%)."""
        app_cfg = {"app_id": "app1", "firebase_project": "p1", "platforms": ["android"]}
        mock_bq = {
            "periods": {
                "7": {"tables": {}},
                "30": {"tables": {}},
                "90": {"tables": {}},
            }
        }
        app_data = transform_bq_to_v2(mock_bq, app_cfg, days=90)
        sessions_unavail = build_unavailable_sessions_result("Sessions export disabled")

        enriched = enrich_app_dashboard_with_sessions(app_data, sessions_unavail)

        for p_key in ["7", "30", "90"]:
            snap = enriched["periods"][p_key]
            cfu = snap["kpi"]["crash_free_users"]
            cfs = snap["kpi"]["crash_free_sessions"]
            self.assertEqual(cfu["status"], "unavailable")
            self.assertIsNone(cfu["rate"])
            self.assertEqual(cfs["status"], "unavailable")
            self.assertIsNone(cfs["rate"])


class TestIssueDetailsAndAIEnrichment(unittest.TestCase):
    """Tests top_issues and AI summary enrichment across period snapshots."""

    def test_ai_enrichment_propagates_to_all_period_snapshots(self):
        """Priority score is computed for all period top_issues and AI analyses are linked."""
        app_cfg = {"app_id": "app1", "firebase_project": "p1", "platforms": ["android"]}
        mock_bq = {
            "periods": {
                "7": {
                    "tables": {
                        "t1": {
                            "overview": [{"total_events": 10, "distinct_users": 5}],
                            "top_issues": [{"issue_id": "iss_1", "events": 10, "users": 5, "issue_title": "Crash A"}],
                            "issue_versions": [{"issue_id": "iss_1", "app_version": "1.0.0", "events": 10, "users": 5}],
                        }
                    }
                },
                "90": {
                    "tables": {
                        "t1": {
                            "overview": [{"total_events": 50, "distinct_users": 20}],
                            "top_issues": [{"issue_id": "iss_1", "events": 50, "users": 20, "issue_title": "Crash A"}],
                            "issue_versions": [{"issue_id": "iss_1", "app_version": "1.0.0", "events": 50, "users": 20}],
                        }
                    }
                },
            }
        }
        app_data = transform_bq_to_v2(mock_bq, app_cfg, days=90)

        # Enrich with AI (disabled mode test without network call)
        enriched = enrich_app_data_with_priority_and_ai(app_data)

        for p_key in ["7", "90"]:
            snap = enriched["periods"][p_key]
            self.assertTrue(len(snap["top_issues"]) > 0)
            iss = snap["top_issues"][0]
            self.assertIn("priority", iss)
            self.assertIn("score", iss["priority"])
            self.assertIn("ai_analysis", iss)
            self.assertIn("ai_summary", snap)


class TestDashboardUIContract(unittest.TestCase):
    """Tests that build_dashboard generates client-side script with multi-period support."""

    def test_html_contains_period_snapshot_logic(self):
        """The bundled HTML must include getCurPeriodSnapshot and period-sensitive rendering."""
        bundle_data = {
            "version": "2.3.0",
            "generated_at": "2026-09-03T12:00:00Z",
            "default_app": "demo",
            "apps": {
                "demo": {
                    "metadata": {"app_id": "demo", "display_name": "Demo App", "platforms": ["android"]},
                    "period": {"days": 90, "start_time": "2026-06-05T00:00:00Z", "end_time": "2026-09-03T23:59:59Z"},
                    "sources": {
                        "crashlytics_bq": {"status": "available", "last_sync_timestamp": "2026-09-03T12:00:00Z"},
                        "firebase_sessions": {"status": "unavailable", "last_sync_timestamp": None},
                        "mcp_crashlytics": {"status": "unavailable", "last_sync_timestamp": None},
                        "gemini_ai": {"status": "unavailable", "last_sync_timestamp": None},
                    },
                    "kpi": {
                        "crash_events": {"value": 100, "status": "available"},
                        "affected_users": {"value": 20, "status": "available"},
                        "crash_free_users": {"status": "unavailable"},
                        "crash_free_sessions": {"status": "unavailable"},
                        "new_issues_count": {"value": 0, "status": "available"},
                        "events_by_error_type": {"fatal": 10, "anr": 0, "non_fatal": 90},
                    },
                    "daily_trend": [],
                    "version_health": [],
                    "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [], "custom_keys": []},
                    "top_issues": [],
                    "ai_summary": {"status": "unavailable", "overview": ""},
                    "periods": {
                        "7": {
                            "period": {"days": 7, "start_time": "2026-08-28T00:00:00Z", "end_time": "2026-09-03T23:59:59Z"},
                            "kpi": {
                                "crash_events": {"value": 10, "status": "available"},
                                "affected_users": {"value": 2, "status": "available"},
                                "crash_free_users": {"status": "unavailable"},
                                "crash_free_sessions": {"status": "unavailable"},
                                "new_issues_count": {"value": 0, "status": "available"},
                                "events_by_error_type": {"fatal": 1, "anr": 0, "non_fatal": 9},
                            },
                            "daily_trend": [],
                            "version_health": [],
                            "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [], "custom_keys": []},
                            "top_issues": [],
                        }
                    }
                }
            }
        }
        html = build_html(bundle_data)
        self.assertIn("function getCurPeriodSnapshot()", html)
        self.assertIn("function setPeriod(days)", html)
        self.assertIn('id="p-7d"', html)
        self.assertIn('id="p-30d"', html)
        self.assertIn('id="p-90d"', html)
        self.assertIn("app.periods", html)


class TestPeriodSwitchingReviewRegressions(unittest.TestCase):
    """Regression tests addressing PR #31 review requirements:
    1. Sessions multi-period isolation without cross-contamination.
    2. Single period overview failure prevents distinct-user overcounting and marks period invalid.
    3. Priority trend uses same-period previous snapshot baseline.
    4. Issue details caching and deduplication.
    """

    def setUp(self):
        self.fixed_end = dt.datetime(2026, 9, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
        self.app_cfg = {
            "app_id": "test_app",
            "display_name": "Test App",
            "firebase_project": "test-proj",
            "platforms": ["android"],
        }

    def test_sessions_multi_period_isolation_no_cross_contamination(self):
        """Regression 1: Sessions 7d/30d/90d metrics must NOT contaminate each other or fallback to main period."""
        app_data: AppDashboardV2Data = {
            "metadata": {"app_id": "test_app", "display_name": "Test App", "firebase_project_id": "p", "platforms": ["android"], "source_repo": None, "custom_keys_monitored": []},
            "period": {"days": 30, "start_time": "2026-08-05T00:00:00Z", "end_time": "2026-09-03T23:59:59Z", "comparison_period": None},
            "sources": {
                "crashlytics_bq": {"status": "available", "last_sync_timestamp": None, "error_message": None},
                "firebase_sessions": {"status": "unavailable", "last_sync_timestamp": None, "error_message": None},
                "mcp_crashlytics": {"status": "unavailable", "last_sync_timestamp": None, "error_message": None},
                "gemini_ai": {"status": "unavailable", "last_sync_timestamp": None, "error_message": None},
            },
            "kpi": {
                "crash_events": {"value": 100, "previous_value": None, "change_pct": None, "status": "available"},
                "affected_users": {"value": 20, "previous_value": None, "change_pct": None, "status": "available"},
                "crash_free_users": {"rate": None, "total": None, "crashed": None, "previous_rate": None, "change_pct_points": None, "status": "unavailable", "unavailable_reason": None},
                "crash_free_sessions": {"rate": None, "total": None, "crashed": None, "previous_rate": None, "change_pct_points": None, "status": "unavailable", "unavailable_reason": None},
                "new_issues_count": {"value": 1, "previous_value": None, "change_pct": None, "status": "available"},
                "events_by_error_type": {"fatal": 10, "anr": 0, "non_fatal": 90},
            },
            "daily_trend": [{"date": "2026-09-03", "crash_events": 5, "affected_users": 2, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 5, "sessions_total": None, "crashed_sessions": None, "crash_free_sessions_rate": None, "by_platform": None}],
            "version_health": [{"version": "1.0.0", "platform": "android", "release_date": None, "crash_events": 10, "affected_users": 2, "crash_free_users_rate": None, "crash_free_sessions_rate": None, "adoption_rate": None, "status": "latest", "trend": "stable"}],
            "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [], "custom_keys": []},
            "top_issues": [],
            "ai_summary": {"status": "unavailable", "model": None, "generated_at": None, "overview": "", "key_takeaways": [], "distribution_insights": "", "recommended_actions": [], "data_limitations": None},
            "limitations": [],
            "periods": {
                "7": {
                    "period": {"days": 7, "start_time": "2026-08-28T00:00:00Z", "end_time": "2026-09-03T23:59:59Z", "comparison_period": None},
                    "kpi": {
                        "crash_events": {"value": 10, "previous_value": None, "change_pct": None, "status": "available"},
                        "affected_users": {"value": 2, "previous_value": None, "change_pct": None, "status": "available"},
                        "crash_free_users": {"rate": None, "total": None, "crashed": None, "previous_rate": None, "change_pct_points": None, "status": "unavailable", "unavailable_reason": None},
                        "crash_free_sessions": {"rate": None, "total": None, "crashed": None, "previous_rate": None, "change_pct_points": None, "status": "unavailable", "unavailable_reason": None},
                        "new_issues_count": {"value": 0, "previous_value": None, "change_pct": None, "status": "available"},
                        "events_by_error_type": {"fatal": 1, "anr": 0, "non_fatal": 9},
                    },
                    "daily_trend": [{"date": "2026-09-03", "crash_events": 5, "affected_users": 2, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 5, "sessions_total": None, "crashed_sessions": None, "crash_free_sessions_rate": None, "by_platform": None}],
                    "version_health": [{"version": "1.0.0", "platform": "android", "release_date": None, "crash_events": 10, "affected_users": 2, "crash_free_users_rate": None, "crash_free_sessions_rate": None, "adoption_rate": None, "status": "latest", "trend": "stable"}],
                    "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [], "custom_keys": []},
                    "top_issues": [],
                },
                "30": {
                    "period": {"days": 30, "start_time": "2026-08-05T00:00:00Z", "end_time": "2026-09-03T23:59:59Z", "comparison_period": None},
                    "kpi": {
                        "crash_events": {"value": 100, "previous_value": None, "change_pct": None, "status": "available"},
                        "affected_users": {"value": 20, "previous_value": None, "change_pct": None, "status": "available"},
                        "crash_free_users": {"rate": None, "total": None, "crashed": None, "previous_rate": None, "change_pct_points": None, "status": "unavailable", "unavailable_reason": None},
                        "crash_free_sessions": {"rate": None, "total": None, "crashed": None, "previous_rate": None, "change_pct_points": None, "status": "unavailable", "unavailable_reason": None},
                        "new_issues_count": {"value": 1, "previous_value": None, "change_pct": None, "status": "available"},
                        "events_by_error_type": {"fatal": 10, "anr": 0, "non_fatal": 90},
                    },
                    "daily_trend": [{"date": "2026-09-03", "crash_events": 5, "affected_users": 2, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 5, "sessions_total": None, "crashed_sessions": None, "crash_free_sessions_rate": None, "by_platform": None}],
                    "version_health": [{"version": "1.0.0", "platform": "android", "release_date": None, "crash_events": 10, "affected_users": 2, "crash_free_users_rate": None, "crash_free_sessions_rate": None, "adoption_rate": None, "status": "latest", "trend": "stable"}],
                    "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [], "custom_keys": []},
                    "top_issues": [],
                },
                "90": {
                    "period": {"days": 90, "start_time": "2026-06-05T00:00:00Z", "end_time": "2026-09-03T23:59:59Z", "comparison_period": None},
                    "kpi": {
                        "crash_events": {"value": 300, "previous_value": None, "change_pct": None, "status": "available"},
                        "affected_users": {"value": 50, "previous_value": None, "change_pct": None, "status": "available"},
                        "crash_free_users": {"rate": None, "total": None, "crashed": None, "previous_rate": None, "change_pct_points": None, "status": "unavailable", "unavailable_reason": None},
                        "crash_free_sessions": {"rate": None, "total": None, "crashed": None, "previous_rate": None, "change_pct_points": None, "status": "unavailable", "unavailable_reason": None},
                        "new_issues_count": {"value": 5, "previous_value": None, "change_pct": None, "status": "available"},
                        "events_by_error_type": {"fatal": 30, "anr": 0, "non_fatal": 270},
                    },
                    "daily_trend": [{"date": "2026-09-03", "crash_events": 5, "affected_users": 2, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 5, "sessions_total": None, "crashed_sessions": None, "crash_free_sessions_rate": None, "by_platform": None}],
                    "version_health": [{"version": "1.0.0", "platform": "android", "release_date": None, "crash_events": 10, "affected_users": 2, "crash_free_users_rate": None, "crash_free_sessions_rate": None, "adoption_rate": None, "status": "latest", "trend": "stable"}],
                    "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [], "custom_keys": []},
                    "top_issues": [],
                },
            },
        }

        # Construct sessions_result with intentionally distinct values across 7d, 30d, 90d
        sessions_result = {
            "sources": {"status": "available", "last_sync_timestamp": "2026-09-03T12:00:00Z", "error_message": None},
            "kpi": {
                "crash_free_users": {"rate": 0.99, "total": 1000, "crashed": 10, "status": "available"},
                "crash_free_sessions": {"rate": 0.98, "total": 5000, "crashed": 100, "status": "available"},
            },
            "daily_trend": {"2026-09-03": {"sessions_total": 200, "crashed_sessions": 4, "crash_free_sessions_rate": 0.98}},
            "version_health": {"1.0.0": {"crash_free_users_rate": 0.99, "crash_free_sessions_rate": 0.98, "adoption_rate": 1.0}},
            "periods": {
                "7": {
                    "sources": {"status": "available", "last_sync_timestamp": "2026-09-03T12:00:00Z"},
                    "kpi": {
                        "crash_free_users": {"rate": 0.95, "total": 200, "crashed": 10, "status": "available"},
                        "crash_free_sessions": {"rate": 0.94, "total": 1000, "crashed": 60, "status": "available"},
                    },
                    "daily_trend": {"2026-09-03": {"sessions_total": 200, "crashed_sessions": 12, "crash_free_sessions_rate": 0.94}},
                    "version_health": {"1.0.0": {"crash_free_users_rate": 0.95, "crash_free_sessions_rate": 0.94, "adoption_rate": 1.0}},
                },
                "30": {
                    "sources": {"status": "available", "last_sync_timestamp": "2026-09-03T12:00:00Z"},
                    "kpi": {
                        "crash_free_users": {"rate": 0.99, "total": 1000, "crashed": 10, "status": "available"},
                        "crash_free_sessions": {"rate": 0.98, "total": 5000, "crashed": 100, "status": "available"},
                    },
                    "daily_trend": {"2026-09-03": {"sessions_total": 200, "crashed_sessions": 4, "crash_free_sessions_rate": 0.98}},
                    "version_health": {"1.0.0": {"crash_free_users_rate": 0.99, "crash_free_sessions_rate": 0.98, "adoption_rate": 1.0}},
                },
                "90": {
                    "sources": {"status": "available", "last_sync_timestamp": "2026-09-03T12:00:00Z"},
                    "kpi": {
                        "crash_free_users": {"rate": 0.91, "total": 3000, "crashed": 270, "status": "available"},
                        "crash_free_sessions": {"rate": 0.90, "total": 15000, "crashed": 1500, "status": "available"},
                    },
                    "daily_trend": {"2026-09-03": {"sessions_total": 200, "crashed_sessions": 20, "crash_free_sessions_rate": 0.90}},
                    "version_health": {"1.0.0": {"crash_free_users_rate": 0.91, "crash_free_sessions_rate": 0.90, "adoption_rate": 1.0}},
                },
            },
        }

        enriched = enrich_app_dashboard_with_sessions(app_data, sessions_result)

        # Verify strict isolation: 7d, 30d, 90d preserve their own unique metrics
        self.assertEqual(enriched["periods"]["7"]["kpi"]["crash_free_users"]["rate"], 0.95)
        self.assertEqual(enriched["periods"]["30"]["kpi"]["crash_free_users"]["rate"], 0.99)
        self.assertEqual(enriched["periods"]["90"]["kpi"]["crash_free_users"]["rate"], 0.91)

        self.assertEqual(enriched["periods"]["7"]["version_health"][0]["crash_free_users_rate"], 0.95)
        self.assertEqual(enriched["periods"]["30"]["version_health"][0]["crash_free_users_rate"], 0.99)
        self.assertEqual(enriched["periods"]["90"]["version_health"][0]["crash_free_users_rate"], 0.91)

        # Negative isolation test: if 90d has no sessions, it must be marked unavailable, NOT fallback to 30d
        sessions_partial = {
            "sources": {"status": "available"},
            "kpi": {"crash_free_users": {"rate": 0.99, "status": "available"}},
            "daily_trend": {},
            "version_health": {"1.0.0": {"crash_free_users_rate": 0.99}},
            "periods": {
                "30": {"sources": {"status": "available"}, "kpi": {"crash_free_users": {"rate": 0.99, "status": "available"}}, "version_health": {"1.0.0": {"crash_free_users_rate": 0.99}}},
                "90": {"sources": {"status": "unavailable", "error_message": "No 90d sessions"}, "kpi": {"crash_free_users": {"rate": None, "status": "unavailable"}}, "version_health": {}},
            }
        }
        enriched_partial = enrich_app_dashboard_with_sessions(app_data, sessions_partial)
        snap_90 = enriched_partial["periods"]["90"]
        self.assertEqual(snap_90["kpi"]["crash_free_users"]["status"], "unavailable")
        self.assertIsNone(snap_90["kpi"]["crash_free_users"]["rate"])
        # version_health in 90d must be None, NOT fallback to 0.99 from 30d!
        self.assertIsNone(snap_90["version_health"][0]["crash_free_users_rate"])

    def test_single_period_overview_failure_prevents_overcounting_and_disables_period(self):
        """Regression 2: When overview fails for one period, affected_users must NOT sum daily users, and UI must disable period button."""
        mock_bq = {
            "periods": {
                "7": {
                    "tables": {
                        "com_app_ANDROID": {
                            "overview": [],  # FAILS!
                            "daily_trend": [
                                {"date": "2026-09-01", "events": 5, "users": 3, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 5},
                                {"date": "2026-09-02", "events": 5, "users": 3, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 5},
                                {"date": "2026-09-03", "events": 5, "users": 3, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 5},
                            ],
                            "top_issues": [],
                            "issue_versions": [],
                            "by_device": [],
                            "by_os": [],
                            "by_app_version": [],
                        }
                    }
                },
                "30": {
                    "tables": {
                        "com_app_ANDROID": {
                            "overview": [{"total_events": 30, "distinct_users": 10, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 30}],
                            "daily_trend": [
                                {"date": "2026-09-01", "events": 5, "users": 3, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 5},
                                {"date": "2026-09-02", "events": 5, "users": 3, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 5},
                                {"date": "2026-09-03", "events": 5, "users": 3, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 5},
                            ],
                            "top_issues": [],
                            "issue_versions": [],
                            "by_device": [],
                            "by_os": [],
                            "by_app_version": [],
                        }
                    }
                },
            }
        }

        app_data = transform_bq_to_v2(mock_bq, self.app_cfg, days=30, end_time=self.fixed_end)
        snap_7 = app_data["periods"]["7"]

        # 1. Total events can be derived from daily trend (15)
        self.assertEqual(snap_7["kpi"]["crash_events"]["value"], 15)
        # 2. Distinct users MUST NOT sum daily users (3+3+3 = 9)! Value is 0 and status is insufficient_data
        self.assertEqual(snap_7["kpi"]["affected_users"]["value"], 0)
        self.assertEqual(snap_7["kpi"]["affected_users"]["status"], "insufficient_data")
        self.assertEqual(snap_7["status"], "insufficient_data")
        self.assertIn("Overview 權威彙總缺失", snap_7.get("error_message", ""))

        # 3. Verify HTML output contains disabled logic for button
        bundle = {
            "schema_version": "2.3.0",
            "generated_at": "2026-09-03T12:00:00Z",
            "default_app": "test_app",
            "apps": {"test_app": app_data},
        }
        html = build_html(bundle)
        self.assertIn("isUsablePeriodSnapshot", html)
        self.assertIn('usStatus === "insufficient_data"', html)

    def test_priority_trend_uses_same_period_baseline(self):
        """Regression 3: Priority trend must compare against same-period baseline (prev_app_data.periods[p_key])."""
        current_data = {
            "metadata": {"app_id": "demo", "display_name": "Demo", "firebase_project_id": "p", "platforms": ["android"], "source_repo": None, "custom_keys_monitored": []},
            "period": {"days": 30, "start_time": "2026-08-05T00:00:00Z", "end_time": "2026-09-03T23:59:59Z", "comparison_period": None},
            "sources": {"crashlytics_bq": {"status": "available"}, "firebase_sessions": {"status": "unavailable"}, "mcp_crashlytics": {"status": "unavailable"}, "gemini_ai": {"status": "unavailable"}},
            "kpi": {"crash_events": {"value": 100}, "affected_users": {"value": 20}, "crash_free_users": {"status": "unavailable"}, "crash_free_sessions": {"status": "unavailable"}, "new_issues_count": {"value": 0}, "events_by_error_type": {"fatal": 0, "anr": 0, "non_fatal": 100}},
            "daily_trend": [],
            "version_health": [{"version": "2.0.0", "status": "latest"}],
            "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [{"app_version": "2.0.0"}], "custom_keys": []},
            "top_issues": [{"issue_id": "iss_common", "events": 100, "users": 20, "error_type": "NON_FATAL", "title": "Common Error"}],
            "ai_summary": {"status": "unavailable", "overview": ""},
            "limitations": [],
            "periods": {
                "7": {
                    "period": {"days": 7, "start_time": "2026-08-28T00:00:00Z", "end_time": "2026-09-03T23:59:59Z"},
                    "version_health": [{"version": "2.0.0", "status": "latest"}],
                    "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [{"app_version": "2.0.0"}], "custom_keys": []},
                    "top_issues": [{"issue_id": "iss_common", "events": 10, "users": 5, "error_type": "NON_FATAL", "title": "Common Error"}],
                },
                "30": {
                    "period": {"days": 30, "start_time": "2026-08-05T00:00:00Z", "end_time": "2026-09-03T23:59:59Z"},
                    "version_health": [{"version": "2.0.0", "status": "latest"}],
                    "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [{"app_version": "2.0.0"}], "custom_keys": []},
                    "top_issues": [{"issue_id": "iss_common", "events": 100, "users": 20, "error_type": "NON_FATAL", "title": "Common Error"}],
                },
            }
        }

        # Previous period snapshot:
        # In 7d baseline, iss_common had 5 events (Current 10 > 5 * 1.2 -> worsening)
        # In 30d baseline, iss_common had 200 events (Current 100 < 200 * 0.8 -> improving)
        prev_data = {
            "period": {"days": 30},
            "top_issues": [{"issue_id": "iss_common", "events": 200}],
            "periods": {
                "7": {"top_issues": [{"issue_id": "iss_common", "events": 5}]},
                "30": {"top_issues": [{"issue_id": "iss_common", "events": 200}]},
            }
        }

        enriched = enrich_app_data_with_priority_and_ai(current_data, prev_app_data=prev_data)

        trend_7d = enriched["periods"]["7"]["top_issues"][0]["priority"]["trend"]
        trend_30d = enriched["periods"]["30"]["top_issues"][0]["priority"]["trend"]

        # 7d must be worsening (10 vs 5), NOT improving (10 vs 200)!
        self.assertEqual(trend_7d, "worsening")
        # 30d must be improving (100 vs 200)
        self.assertEqual(trend_30d, "improving")

        # Test period without same-period baseline does not apply worsening/improving boost
        prev_data_no_7d = {
            "period": {"days": 30},
            "top_issues": [{"issue_id": "iss_common", "events": 200}],
            "periods": {
                "30": {"top_issues": [{"issue_id": "iss_common", "events": 200}]},
            }
        }
        enriched_no_7d = enrich_app_data_with_priority_and_ai(current_data, prev_app_data=prev_data_no_7d)
        prio_7d_no_base = enriched_no_7d["periods"]["7"]["top_issues"][0]["priority"]
        self.assertEqual(prio_7d_no_base["trend"], "stable")
        self.assertEqual(prio_7d_no_base["score_breakdown"]["worsening_boost"], 0)

    def test_enrich_top_issues_uses_cache_and_deduplicates_queries(self):
        """Regression 4: enrich_top_issues must reuse cache to avoid redundant BigQuery calls."""
        issues = [
            {"issue_id": "iss_cached", "title": "Cached Error", "subtitle": ""},
            {"issue_id": "iss_new", "title": "New Error", "subtitle": ""},
        ]
        cache = {
            "iss_cached": {
                "blame_frame": {"file": "Cached.kt", "line": 10, "is_blame": True, "source_available": False},
                "detail": {"stack_trace": "line 10", "breadcrumbs": [], "logs": [], "custom_keys": {}, "top_devices": [], "top_os": []},
            }
        }

        with mock.patch("crash_trend.fetch_issue_details.fetch_issue_details") as mock_fetch:
            mock_fetch.return_value = {
                "iss_new": {
                    "blame_frame": {"file": "New.kt", "line": 20, "is_blame": True, "source_available": False},
                    "detail": {"stack_trace": "line 20", "breadcrumbs": [], "logs": [], "custom_keys": {}, "top_devices": [], "top_os": []},
                }
            }

            enriched = enrich_top_issues(issues, app_name="test_app", days=30, details_cache=cache)

            # fetch_issue_details must be called ONLY with ["iss_new"], NOT ["iss_cached"]!
            mock_fetch.assert_called_once()
            call_args = mock_fetch.call_args[0]
            self.assertEqual(call_args[1], ["iss_new"])

            # Cached issue retains its blame_frame and detail
            cached_item = next(i for i in enriched if i["issue_id"] == "iss_cached")
            self.assertEqual(cached_item["blame_frame"]["file"], "Cached.kt")

            # New issue gets populated into cache
            self.assertIn("iss_new", cache)

    def test_is_usable_period_snapshot_logic_in_dashboard(self):
        """Regression 5: Dashboard JS must include isUsablePeriodSnapshot and guard switchApp/renderHeader/getCurPeriodSnapshot."""
        from crash_trend.schema_v2 import SnapshotStatus, AppPeriodSnapshot

        # Verify SnapshotStatus typing and AppPeriodSnapshot contract
        self.assertIn("insufficient_data", SnapshotStatus.__args__)
        self.assertIn("error", SnapshotStatus.__args__)
        self.assertIn("available", SnapshotStatus.__args__)

        snap_data: AppPeriodSnapshot = {
            "period": {"days": 7, "start_time": "2026-08-28T00:00:00Z", "end_time": "2026-09-03T23:59:59Z", "comparison_period": None},
            "kpi": {
                "crash_events": {"value": 10, "previous_value": None, "change_pct": None, "status": "available"},
                "affected_users": {"value": 0, "previous_value": None, "change_pct": None, "status": "insufficient_data"},
                "crash_free_users": {"rate": None, "total": None, "crashed": None, "previous_rate": None, "change_pct_points": None, "status": "unavailable", "unavailable_reason": None},
                "crash_free_sessions": {"rate": None, "total": None, "crashed": None, "previous_rate": None, "change_pct_points": None, "status": "unavailable", "unavailable_reason": None},
                "new_issues_count": {"value": 0, "previous_value": None, "change_pct": None, "status": "available"},
                "events_by_error_type": {"fatal": 0, "anr": 0, "non_fatal": 10},
            },
            "version_health": [],
            "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [], "custom_keys": []},
            "top_issues": [],
            "status": "insufficient_data",
            "error_message": "Overview 權威彙總缺失",
        }
        self.assertEqual(snap_data["status"], "insufficient_data")

        # Verify HTML contains unified isUsablePeriodSnapshot logic guarding switchApp and getCurPeriodSnapshot
        bundle = {
            "schema_version": "2.3.0",
            "generated_at": "2026-09-03T12:00:00Z",
            "default_app": "demo",
            "apps": {
                "demo": {
                    "metadata": {"app_id": "demo", "display_name": "Demo", "firebase_project_id": "p", "platforms": ["android"]},
                    "period": {"days": 30, "start_time": "2026-08-05T00:00:00Z", "end_time": "2026-09-03T23:59:59Z"},
                    "sources": {"crashlytics_bq": {"status": "available"}},
                    "kpi": {"crash_events": {"value": 10}, "affected_users": {"value": 5}},
                    "daily_trend": [],
                    "version_health": [],
                    "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [], "custom_keys": []},
                    "top_issues": [],
                    "ai_summary": {"status": "unavailable", "overview": ""},
                    "periods": {"7": snap_data},
                }
            },
        }
        html = build_html(bundle)
        self.assertIn("function isUsablePeriodSnapshot(snap)", html)
        self.assertIn("isUsablePeriodSnapshot", html)
        self.assertIn("snap.status === \"insufficient_data\"", html)


if __name__ == "__main__":
    unittest.main()
