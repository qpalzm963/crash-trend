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


if __name__ == "__main__":
    unittest.main()
