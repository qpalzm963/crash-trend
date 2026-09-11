"""Comprehensive unit and integration tests for Issue Occurrence Timeline (Issue #68).

Validates:
1. Single issue multi-day events aggregation
2. Same-day multi-version aggregation
3. Android / iOS platform isolation with identical issue_id
4. Multi-app isolation
5. Historical first seen authority preservation in 7-day snapshot
6. Out-of-order event timestamps sorting deterministically
7. Peak date tie-breaking (earliest date wins deterministically)
8. Empty / missing daily data handling in schema validation and dashboard
9. Period switching (7d / 30d / 90d snapshots)
10. Zero PII: no installation_uuid or raw user IDs in schema payload
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.build_dashboard import build_html
from crash_trend.catalog.historical import IssueHistoricalCatalog, enrich_app_data_with_lifecycle
from crash_trend.dashboard.issues import get_issues_js
from crash_trend.fetch_bigquery import build_issue_occurrence_summary, run_query, transform_bq_period_snapshot
from crash_trend.schema_v2 import (
    IssueSummary,
    validate_issue_occurrence_summary,
    validate_issue_summary,
)


class TestIssueOccurrenceTimeline(unittest.TestCase):
    def test_single_issue_multi_day_aggregation(self) -> None:
        """Test 1: Single issue multi-day events aggregation."""
        raw_rows = [
            {
                "issue_id": "ISSUE_101",
                "date": "2026-09-06",
                "app_version": "3.14.0",
                "events": 4,
                "users": 3,
                "fatal_events": 4,
                "anr_events": 0,
                "non_fatal_events": 0,
            },
            {
                "issue_id": "ISSUE_101",
                "date": "2026-09-07",
                "app_version": "3.14.0",
                "events": 8,
                "users": 5,
                "fatal_events": 2,
                "anr_events": 1,
                "non_fatal_events": 5,
            },
            {
                "issue_id": "ISSUE_101",
                "date": "2026-09-08",
                "app_version": "3.14.0",
                "events": 5,
                "users": 4,
                "fatal_events": 0,
                "anr_events": 0,
                "non_fatal_events": 5,
            },
        ]
        summary = build_issue_occurrence_summary(
            raw_rows,
            platform="android",
            first_seen_timestamp="2026-09-06T10:00:00Z",
            last_seen_timestamp="2026-09-08T18:00:00Z",
        )

        self.assertEqual(len(summary["daily"]), 3)
        self.assertEqual(summary["first_seen_date"], "2026-09-06")
        self.assertEqual(summary["last_seen_date"], "2026-09-08")
        self.assertEqual(summary["peak_date"], "2026-09-07")
        self.assertEqual(summary["peak_events"], 8)

        d1 = summary["daily"][0]
        self.assertEqual(d1["date"], "2026-09-06")
        self.assertEqual(d1["events"], 4)
        self.assertEqual(d1["affected_users"], 3)
        self.assertEqual(d1["fatal_events"], 4)
        self.assertEqual(d1["anr_events"], 0)
        self.assertEqual(d1["non_fatal_events"], 0)
        self.assertEqual(d1["versions"], {"3.14.0": 4})

        # Schema runtime validation
        errors: list[str] = []
        validate_issue_occurrence_summary(summary, errors)
        self.assertEqual(errors, [])

    def test_same_day_multi_version_aggregation(self) -> None:
        """Test 2: Same-day multi-version aggregation from BigQuery CTE output."""
        raw_rows = [
            {
                "issue_id": "ISSUE_202",
                "date": "2026-09-08",
                "events": 12,
                "users": 6,  # Authoritative distinct users computed at issue/day level in SQL
                "fatal_events": 2,
                "anr_events": 0,
                "non_fatal_events": 10,
                "versions_json": json.dumps([
                    {"version": "3.14.0", "events": 9},
                    {"version": "3.13.2", "events": 3},
                ]),
            }
        ]
        summary = build_issue_occurrence_summary(raw_rows, platform="android")

        self.assertEqual(len(summary["daily"]), 1)
        point = summary["daily"][0]
        self.assertEqual(point["date"], "2026-09-08")
        self.assertEqual(point["events"], 12)
        self.assertEqual(point["affected_users"], 6)
        self.assertEqual(point["fatal_events"], 2)
        self.assertEqual(point["anr_events"], 0)
        self.assertEqual(point["non_fatal_events"], 10)
        self.assertEqual(point["versions"], {"3.14.0": 9, "3.13.2": 3})

        errors: list[str] = []
        validate_issue_occurrence_summary(summary, errors)
        self.assertEqual(errors, [])

    def test_same_installation_multi_version_same_day_counts_as_single_user(self) -> None:
        """Regression test for [P1]: Same installation crashing across 2 versions on same day counts as 1 user."""
        raw_rows = [
            {
                "issue_id": "ISSUE_MULTI_VER",
                "date": "2026-09-08",
                "app_version": "3.13.2",
                "events": 1,
                "installation_uuid": "USER_CROSS_VER_123",
                "fatal_events": 1,
                "anr_events": 0,
                "non_fatal_events": 0,
            },
            {
                "issue_id": "ISSUE_MULTI_VER",
                "date": "2026-09-08",
                "app_version": "3.14.0",
                "events": 2,
                "installation_uuid": "USER_CROSS_VER_123",  # Same user upgraded and crashed on new version
                "fatal_events": 2,
                "anr_events": 0,
                "non_fatal_events": 0,
            },
            {
                "issue_id": "ISSUE_MULTI_VER",
                "date": "2026-09-08",
                "app_version": "3.14.0",
                "events": 1,
                "installation_uuid": "USER_OTHER_456",  # Distinct user
                "fatal_events": 1,
                "anr_events": 0,
                "non_fatal_events": 0,
            },
        ]
        summary = build_issue_occurrence_summary(raw_rows, platform="android")

        self.assertEqual(len(summary["daily"]), 1)
        point = summary["daily"][0]
        self.assertEqual(point["date"], "2026-09-08")
        self.assertEqual(point["events"], 4)
        # USER_CROSS_VER_123 (1) + USER_OTHER_456 (1) = 2 distinct users, NOT 3!
        self.assertEqual(point["affected_users"], 2)
        self.assertEqual(point["fatal_events"], 4)
        self.assertEqual(point["versions"], {"3.14.0": 3, "3.13.2": 1})

        # Zero PII guarantee: no temporary uuid tracking set leaked
        self.assertNotIn("_user_uuids", point)
        self.assertNotIn("installation_uuid", point)

    def test_platform_isolation_same_issue_id(self) -> None:
        """Test 3: Android / iOS platform isolation with identical issue_id."""
        start_d = dt.date(2026, 9, 2)
        end_d = dt.date(2026, 9, 8)
        tables_data = {
            "app_events_android": {
                "overview": [{"total_events": 20, "distinct_users": 10, "fatal_events": 20, "anr_events": 0, "non_fatal_events": 0}],
                "top_issues": [
                    {
                        "issue_id": "SHARED_ISSUE_ID",
                        "issue_title": "Android NullPointerException",
                        "issue_subtitle": "MainActivity.kt:42",
                        "error_type": "FATAL",
                        "events": 20,
                        "users": 10,
                        "first_seen_timestamp": "2026-09-03T00:00:00Z",
                        "last_seen_timestamp": "2026-09-08T00:00:00Z",
                        "first_seen_version": "3.14.0",
                        "last_seen_version": "3.14.0",
                    }
                ],
                "issue_daily_trend": [
                    {
                        "issue_id": "SHARED_ISSUE_ID",
                        "date": "2026-09-08",
                        "app_version": "3.14.0",
                        "events": 20,
                        "users": 10,
                        "fatal_events": 20,
                        "anr_events": 0,
                        "non_fatal_events": 0,
                    }
                ],
            },
            "app_events_ios": {
                "overview": [{"total_events": 5, "distinct_users": 3, "fatal_events": 5, "anr_events": 0, "non_fatal_events": 0}],
                "top_issues": [
                    {
                        "issue_id": "SHARED_ISSUE_ID",
                        "issue_title": "iOS FatalException",
                        "issue_subtitle": "AppDelegate.swift:15",
                        "error_type": "FATAL",
                        "events": 5,
                        "users": 3,
                        "first_seen_timestamp": "2026-09-05T00:00:00Z",
                        "last_seen_timestamp": "2026-09-07T00:00:00Z",
                        "first_seen_version": "2.1.0",
                        "last_seen_version": "2.1.0",
                    }
                ],
                "issue_daily_trend": [
                    {
                        "issue_id": "SHARED_ISSUE_ID",
                        "date": "2026-09-07",
                        "app_version": "2.1.0",
                        "events": 5,
                        "users": 3,
                        "fatal_events": 5,
                        "anr_events": 0,
                        "non_fatal_events": 0,
                    }
                ],
            },
        }

        snap = transform_bq_period_snapshot(
            tables_data,
            detected_platforms=["android", "ios"],
            days=7,
            start_date=start_d,
            end_date=end_d,
        )

        issues = snap["top_issues"]
        self.assertEqual(len(issues), 2)

        android_iss = next(i for i in issues if i["platform"] == "android")
        ios_iss = next(i for i in issues if i["platform"] == "ios")

        self.assertEqual(android_iss["issue_id"], "SHARED_ISSUE_ID")
        self.assertEqual(android_iss["events"], 20)
        self.assertEqual(android_iss["occurrence_timeline"]["peak_events"], 20)
        self.assertEqual(android_iss["occurrence_timeline"]["daily"][0]["platform"], "android")
        self.assertIn("3.14.0", android_iss["occurrence_timeline"]["daily"][0]["versions"])

        self.assertEqual(ios_iss["issue_id"], "SHARED_ISSUE_ID")
        self.assertEqual(ios_iss["events"], 5)
        self.assertEqual(ios_iss["occurrence_timeline"]["peak_events"], 5)
        self.assertEqual(ios_iss["occurrence_timeline"]["daily"][0]["platform"], "ios")
        self.assertIn("2.1.0", ios_iss["occurrence_timeline"]["daily"][0]["versions"])

    def test_multi_app_isolation(self) -> None:
        """Test 4: Different apps with same issue_id maintain isolated timelines."""
        cat_app1 = IssueHistoricalCatalog()
        cat_app1.issues["ISS_A"] = {
            "issue_id": "ISS_A",
            "platform": "android",
            "title": "App 1 Crash",
            "subtitle": "sub1",
            "error_type": "FATAL",
            "first_seen_version": "1.0.0",
            "last_seen_version": "1.0.0",
            "first_seen_timestamp": "2026-01-01T00:00:00Z",
            "last_seen_timestamp": "2026-01-02T00:00:00Z",
            "versions_seen": ["1.0.0"],
            "last_updated": "2026-01-02T00:00:00Z",
        }

        cat_app2 = IssueHistoricalCatalog()
        cat_app2.issues["ISS_A"] = {
            "issue_id": "ISS_A",
            "platform": "android",
            "title": "App 2 Crash",
            "subtitle": "sub2",
            "error_type": "ANR",
            "first_seen_version": "5.0.0",
            "last_seen_version": "5.0.0",
            "first_seen_timestamp": "2026-08-01T00:00:00Z",
            "last_seen_timestamp": "2026-08-02T00:00:00Z",
            "versions_seen": ["5.0.0"],
            "last_updated": "2026-08-02T00:00:00Z",
        }
        h1 = cat_app1.get_issue_history("ISS_A")
        h2 = cat_app2.get_issue_history("ISS_A")
        self.assertIsNotNone(h1)
        self.assertIsNotNone(h2)
        assert h1 is not None and h2 is not None
        self.assertEqual(h1["first_seen_version"], "1.0.0")
        self.assertEqual(h2["first_seen_version"], "5.0.0")

    def test_historical_first_seen_authority_in_short_window(self) -> None:
        """Test 5: 7-day snapshot uses historical first seen instead of window boundary."""
        catalog = IssueHistoricalCatalog()
        catalog.issues["android:ISS_HIST"] = {
            "issue_id": "ISS_HIST",
            "platform": "android",
            "title": "Historical Bug",
            "subtitle": "File.kt:1",
            "error_type": "FATAL",
            "first_seen_version": "1.0.0",
            "last_seen_version": "3.14.0",
            "first_seen_timestamp": "2026-05-01T12:00:00Z",
            "last_seen_timestamp": "2026-09-08T18:00:00Z",
            "versions_seen": ["1.0.0", "2.0.0", "3.14.0"],
            "last_updated": "2026-09-08T18:00:00Z",
        }

        app_data = {
            "top_issues": [
                {
                    "issue_id": "ISS_HIST",
                    "platform": "android",
                    "title": "Historical Bug",
                    "subtitle": "File.kt:1",
                    "error_type": "FATAL",
                    "priority": {"score": 10, "level": "P1", "trend": "stable", "score_breakdown": None},
                    "events": 25,
                    "affected_users": 15,
                    # Short 7-day query snapshot timestamps
                    "first_seen_timestamp": "2026-09-02T00:00:00Z",
                    "last_seen_timestamp": "2026-09-08T18:00:00Z",
                    "first_seen_version": "3.14.0",
                    "last_seen_version": "3.14.0",
                    "version_distribution": [{"version": "3.14.0", "events": 25, "users": 15}],
                    "blame_frame": None,
                    "ai_analysis": {"status": "unavailable", "root_cause": "", "suggested_fix": "", "effort": "M", "confidence": "low", "reasoning_sources": []},
                    "detail": None,
                    "occurrence_timeline": {
                        "first_seen_date": "2026-09-02",
                        "last_seen_date": "2026-09-08",
                        "peak_date": "2026-09-05",
                        "peak_events": 10,
                        "daily": [
                            {
                                "date": "2026-09-02",
                                "events": 5,
                                "affected_users": 3,
                                "fatal_events": 5,
                                "anr_events": 0,
                                "non_fatal_events": 0,
                                "versions": {"3.14.0": 5},
                            },
                            {
                                "date": "2026-09-05",
                                "events": 10,
                                "affected_users": 7,
                                "fatal_events": 10,
                                "anr_events": 0,
                                "non_fatal_events": 0,
                                "versions": {"3.14.0": 10},
                            },
                        ],
                    },
                }
            ],
            "periods": {
                "7": {
                    "top_issues": [
                        {
                            "issue_id": "ISS_HIST",
                            "platform": "android",
                            "title": "Historical Bug",
                            "subtitle": "File.kt:1",
                            "error_type": "FATAL",
                            "priority": {"score": 10, "level": "P1", "trend": "stable", "score_breakdown": None},
                            "events": 25,
                            "affected_users": 15,
                            "first_seen_timestamp": "2026-09-02T00:00:00Z",
                            "last_seen_timestamp": "2026-09-08T18:00:00Z",
                            "first_seen_version": "3.14.0",
                            "last_seen_version": "3.14.0",
                            "version_distribution": [{"version": "3.14.0", "events": 25, "users": 15}],
                            "blame_frame": None,
                            "ai_analysis": {"status": "unavailable", "root_cause": "", "suggested_fix": "", "effort": "M", "confidence": "low", "reasoning_sources": []},
                            "detail": None,
                            "occurrence_timeline": {
                                "first_seen_date": "2026-09-02",
                                "last_seen_date": "2026-09-08",
                                "peak_date": "2026-09-05",
                                "peak_events": 10,
                                "daily": [
                                    {
                                        "date": "2026-09-02",
                                        "events": 5,
                                        "affected_users": 3,
                                        "fatal_events": 5,
                                        "anr_events": 0,
                                        "non_fatal_events": 0,
                                        "versions": {"3.14.0": 5},
                                    }
                                ],
                            },
                        }
                    ]
                }
            },
        }

        enriched = enrich_app_data_with_lifecycle(app_data, catalog=catalog)
        top_iss = enriched["top_issues"][0]
        snap_iss = enriched["periods"]["7"]["top_issues"][0]

        # Top-level issue check
        self.assertEqual(top_iss["first_seen_timestamp"], "2026-05-01T12:00:00Z")
        self.assertEqual(top_iss["first_seen_version"], "1.0.0")
        self.assertEqual(top_iss["occurrence_timeline"]["first_seen_date"], "2026-05-01")

        # Snapshot issue check
        self.assertEqual(snap_iss["first_seen_timestamp"], "2026-05-01T12:00:00Z")
        self.assertEqual(snap_iss["first_seen_version"], "1.0.0")
        self.assertEqual(snap_iss["occurrence_timeline"]["first_seen_date"], "2026-05-01")

    def test_out_of_order_event_timestamps_sorted_deterministically(self) -> None:
        """Test 6: Out-of-order event timestamps are sorted deterministically date ASC."""
        raw_rows = [
            {"issue_id": "I1", "date": "2026-09-08", "app_version": "1.0.0", "events": 5, "users": 3},
            {"issue_id": "I1", "date": "2026-09-02", "app_version": "1.0.0", "events": 2, "users": 1},
            {"issue_id": "I1", "date": "2026-09-05", "app_version": "1.0.0", "events": 10, "users": 8},
        ]
        summary = build_issue_occurrence_summary(raw_rows, platform="android")

        dates = [d["date"] for d in summary["daily"]]
        self.assertEqual(dates, ["2026-09-02", "2026-09-05", "2026-09-08"])

    def test_peak_date_tie_breaking_deterministic(self) -> None:
        """Test 7: Peak date tie-breaking is deterministic (earliest date wins)."""
        raw_rows = [
            {"issue_id": "I1", "date": "2026-09-07", "app_version": "1.0.0", "events": 15, "users": 5},
            {"issue_id": "I1", "date": "2026-09-03", "app_version": "1.0.0", "events": 15, "users": 4},
            {"issue_id": "I1", "date": "2026-09-05", "app_version": "1.0.0", "events": 15, "users": 6},
        ]
        summary = build_issue_occurrence_summary(raw_rows, platform="android")

        self.assertEqual(summary["peak_events"], 15)
        # 2026-09-03 is the earliest date with 15 events
        self.assertEqual(summary["peak_date"], "2026-09-03")

    def test_empty_or_missing_daily_data(self) -> None:
        """Test 8: Empty / missing daily data handling in schema validation and dashboard."""
        empty_summary = build_issue_occurrence_summary([], platform="android")
        self.assertEqual(empty_summary["daily"], [])
        self.assertEqual(empty_summary["peak_events"], 0)
        self.assertIsNone(empty_summary["peak_date"])

        errors: list[str] = []
        validate_issue_occurrence_summary(empty_summary, errors)
        self.assertEqual(errors, [])

        # IssueSummary with empty occurrence timeline
        issue: IssueSummary = {
            "issue_id": "EMPTY_ISSUE",
            "platform": "android",
            "title": "Empty Daily Issue",
            "subtitle": "test",
            "error_type": "NON_FATAL",
            "priority": {"score": 0, "level": "P3", "trend": "stable", "score_breakdown": None},
            "events": 0,
            "affected_users": 0,
            "first_seen_timestamp": "2026-09-01T00:00:00Z",
            "last_seen_timestamp": "2026-09-01T00:00:00Z",
            "first_seen_version": "1.0.0",
            "last_seen_version": "1.0.0",
            "version_distribution": [],
            "blame_frame": None,
            "ai_analysis": {"status": "unavailable", "root_cause": None, "suggested_fix": None, "effort": None, "confidence": None, "reasoning_sources": []},
            "detail": None,
            "occurrence_timeline": empty_summary,
        }
        val_errors: list[str] = []
        validate_issue_summary(issue, 0, val_errors)
        self.assertEqual(val_errors, [])

        # Verify JS rendering contains graceful empty state markup
        js_code = get_issues_js()
        self.assertIn("尚無每日發生趨勢資料 (No daily occurrence data)", js_code)
        self.assertIn("renderOccurrenceTimelineHtml", js_code)

    def test_period_switching_snapshots(self) -> None:
        """Test 9: Period switching 7 / 30 / 90 days snapshots with occurrence timeline."""
        for days in (7, 30, 90):
            start_d = dt.date(2026, 9, 8) - dt.timedelta(days=days - 1)
            end_d = dt.date(2026, 9, 8)
            tables_data = {
                "app_events_android": {
                    "overview": [{"total_events": 10, "distinct_users": 5, "fatal_events": 10, "anr_events": 0, "non_fatal_events": 0}],
                    "top_issues": [
                        {
                            "issue_id": f"ISSUE_{days}D",
                            "issue_title": "Periodic Issue",
                            "issue_subtitle": "Test.kt:10",
                            "error_type": "FATAL",
                            "events": 10,
                            "users": 5,
                            "first_seen_timestamp": f"{start_d.isoformat()}T00:00:00Z",
                            "last_seen_timestamp": f"{end_d.isoformat()}T00:00:00Z",
                            "first_seen_version": "1.0.0",
                            "last_seen_version": "1.0.0",
                        }
                    ],
                    "issue_daily_trend": [
                        {
                            "issue_id": f"ISSUE_{days}D",
                            "date": end_d.isoformat(),
                            "app_version": "1.0.0",
                            "events": 10,
                            "users": 5,
                            "fatal_events": 10,
                            "anr_events": 0,
                            "non_fatal_events": 0,
                        }
                    ],
                }
            }

            snap = transform_bq_period_snapshot(
                tables_data,
                detected_platforms=["android"],
                days=days,
                start_date=start_d,
                end_date=end_d,
            )
            self.assertEqual(len(snap["top_issues"]), 1)
            timeline = snap["top_issues"][0].get("occurrence_timeline")
            self.assertIsNotNone(timeline)
            assert timeline is not None
            self.assertEqual(timeline["peak_events"], 10)
            self.assertEqual(timeline["peak_date"], end_d.isoformat())

    def test_zero_pii_no_raw_uuids_in_payload(self) -> None:
        """Test 10: Zero PII - No installation_uuid or raw user IDs in timeline payload."""
        raw_rows = [
            {
                "issue_id": "PII_CHECK",
                "date": "2026-09-08",
                "app_version": "3.14.0",
                "events": 10,
                "users": 6,
                "installation_uuid": "SECRET-UUID-DO-NOT-LEAK-1234",
                "user_id": "USER-PRIVATE-ID-9999",
            }
        ]
        summary = build_issue_occurrence_summary(raw_rows, platform="android")

        # Serialized JSON check
        json_str = json.dumps(summary)
        self.assertNotIn("SECRET-UUID", json_str)
        self.assertNotIn("USER-PRIVATE", json_str)
        self.assertNotIn("installation_uuid", json_str)
        self.assertNotIn("user_id", json_str)

        # Inspect dict keys
        for d in summary["daily"]:
            self.assertNotIn("installation_uuid", d)
            self.assertNotIn("user_id", d)
            self.assertNotIn("installation_ids", d)
    def test_over_5000_intermediate_rows_90_day_completeness(self) -> None:
        """Regression test for [P1]: Over 5000 intermediate rows across 90 days must not truncate recent dates."""
        start_d = dt.date(2026, 6, 11)
        end_d = dt.date(2026, 9, 8)
        num_days = (end_d - start_d).days + 1  # 90 days
        num_issues = 60  # 60 * 90 = 5400 intermediate rows (> 5000)

        # 1. Verify run_query does not cap at 5000
        mock_client = mock.MagicMock()
        mock_job = mock.MagicMock()
        mock_rows = [{"date": "2026-09-08", "events": 1} for _ in range(5400)]
        mock_job.result.return_value = mock_rows
        mock_client.query.return_value = mock_job

        res = run_query(mock_client, "SELECT 1", timeout=120)
        self.assertEqual(len(res), 5400)
        # Ensure result was called with no max_results limit（逾時上限不是列數上限：
        # bq_query 會帶 timeout，但**不得**順手帶上 max_results，否則 90 天資料會被截斷）
        _, result_kwargs = mock_job.result.call_args
        self.assertNotIn("max_results", result_kwargs)
        self.assertEqual(result_kwargs.get("timeout"), 120)

        # 2. Verify transform_bq_period_snapshot preserves full 90 days including latest date
        daily_rows: list[dict] = []
        for i_idx in range(num_issues):
            iid = f"ISSUE_{i_idx:03d}"
            for day_offset in range(num_days):
                cur_date = start_d + dt.timedelta(days=day_offset)
                daily_rows.append({
                    "issue_id": iid,
                    "date": cur_date.isoformat(),
                    "events": 2,
                    "users": 1,
                    "fatal_events": 2,
                    "anr_events": 0,
                    "non_fatal_events": 0,
                    "versions_json": json.dumps([{"version": "1.0.0", "events": 2}]),
                })
        self.assertEqual(len(daily_rows), 5400)

        top_issue_rows = [
            {
                "issue_id": "ISSUE_000",
                "issue_title": "Top Crash",
                "issue_subtitle": "Main.kt:10",
                "error_type": "FATAL",
                "events": 180,
                "users": 90,
                "first_seen_timestamp": "2026-06-11T00:00:00Z",
                "last_seen_timestamp": "2026-09-08T23:59:59Z",
                "first_seen_version": "1.0.0",
                "last_seen_version": "1.0.0",
            }
        ]

        tables_data = {
            "app_android": {
                "overview": [{"total_events": 10800, "distinct_users": 5400, "fatal_events": 10800, "anr_events": 0, "non_fatal_events": 0}],
                "top_issues": top_issue_rows,
                "issue_daily_trend": daily_rows,
            }
        }

        snap = transform_bq_period_snapshot(
            tables_data,
            detected_platforms=["android"],
            days=90,
            start_date=start_d,
            end_date=end_d,
        )

        self.assertEqual(len(snap["top_issues"]), 1)
        iss = snap["top_issues"][0]
        timeline = iss.get("occurrence_timeline")
        self.assertIsNotNone(timeline)
        assert timeline is not None

        # Full 90 days preserved without truncation
        self.assertEqual(len(timeline["daily"]), 90)
        self.assertEqual(timeline["first_seen_date"], "2026-06-11")
        # Most recent date (2026-09-08) is intact and NOT truncated by LIMIT 5000!
        self.assertEqual(timeline["last_seen_date"], "2026-09-08")
        self.assertEqual(timeline["daily"][-1]["date"], "2026-09-08")

    def test_copy_fix_prompt_platform_isolation(self) -> None:
        """Regression test for [P2]: copyFixPrompt passes platform to prevent cross-platform data mismatch."""
        shared_id = "SHARED_COLLISION_ID"
        android_iss = {
            "issue_id": shared_id,
            "platform": "android",
            "title": "Android Specific Crash",
            "subtitle": "AndroidActivity.kt:42",
            "error_type": "FATAL",
            "priority": {"score": 90, "level": "P0", "trend": "new", "score_breakdown": None},
            "events": 100,
            "affected_users": 50,
            "first_seen_timestamp": "2026-09-01T00:00:00Z",
            "last_seen_timestamp": "2026-09-08T00:00:00Z",
            "first_seen_version": "1.0.0",
            "last_seen_version": "1.1.0",
            "version_distribution": [{"version": "1.1.0", "events": 100, "users": 50}],
            "blame_frame": None,
            "ai_analysis": {"status": "unavailable", "root_cause": "", "suggested_fix": "", "effort": "M", "confidence": "low", "reasoning_sources": []},
            "detail": {"stack_trace": "java.lang.NullPointerException at AndroidActivity.kt:42"},
        }
        ios_iss = {
            "issue_id": shared_id,
            "platform": "ios",
            "title": "iOS Specific Crash",
            "subtitle": "IOSViewController.swift:99",
            "error_type": "FATAL",
            "priority": {"score": 75, "level": "P1", "trend": "stable", "score_breakdown": None},
            "events": 20,
            "affected_users": 15,
            "first_seen_timestamp": "2026-09-05T00:00:00Z",
            "last_seen_timestamp": "2026-09-08T00:00:00Z",
            "first_seen_version": "2.0.0",
            "last_seen_version": "2.0.1",
            "version_distribution": [{"version": "2.0.1", "events": 20, "users": 15}],
            "blame_frame": None,
            "ai_analysis": {"status": "unavailable", "root_cause": "", "suggested_fix": "", "effort": "M", "confidence": "low", "reasoning_sources": []},
            "detail": {"stack_trace": "fatal error: Unexpected nil at IOSViewController.swift:99"},
        }

        bundle = {
            "schema_version": "2.8.0",
            "generated_at": "2026-09-08T12:00:00Z",
            "default_app": "demo",
            "apps": {
                "demo": {
                    "metadata": {"app_id": "demo", "display_name": "Demo", "firebase_project_id": "p", "platforms": ["android", "ios"]},
                    "period": {"days": 30, "start_time": "2026-08-09T00:00:00Z", "end_time": "2026-09-08T23:59:59Z"},
                    "sources": {"crashlytics_bq": {"status": "available"}},
                    "kpi": {"crash_events": {"value": 120}, "affected_users": {"value": 65}},
                    "daily_trend": [],
                    "version_health": [],
                    "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [], "custom_keys": []},
                    "top_issues": [android_iss, ios_iss],
                    "ai_summary": {"status": "unavailable", "overview": ""},
                    "periods": {},
                }
            },
        }

        # 1. Verify HTML renders copyFixPrompt with both issue_id and platform in button call
        html = build_html(bundle)
        self.assertIn("copyFixPrompt('${esc(iss.issue_id)}', '${esc(iss.platform)}')", html)

        # 2. Verify JS implementation accepts (issueId, platform) and isolates by platform
        js = get_issues_js()
        self.assertIn("function copyFixPrompt(issueId, platform)", js)
        self.assertIn("i.issue_id === issueId && i.platform === platform", js)


if __name__ == "__main__":
    unittest.main()

