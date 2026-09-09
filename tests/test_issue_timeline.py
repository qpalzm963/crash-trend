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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.catalog.historical import IssueHistoricalCatalog, enrich_app_data_with_lifecycle
from crash_trend.dashboard.issues import get_issues_js
from crash_trend.fetch_bigquery import build_issue_occurrence_summary, transform_bq_period_snapshot
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
        """Test 2: Same-day multi-version aggregation."""
        raw_rows = [
            {
                "issue_id": "ISSUE_202",
                "date": "2026-09-08",
                "app_version": "3.14.0",
                "events": 9,
                "users": 5,
                "fatal_events": 2,
                "anr_events": 0,
                "non_fatal_events": 7,
            },
            {
                "issue_id": "ISSUE_202",
                "date": "2026-09-08",
                "app_version": "3.13.2",
                "events": 3,
                "users": 2,
                "fatal_events": 0,
                "anr_events": 0,
                "non_fatal_events": 3,
            },
        ]
        summary = build_issue_occurrence_summary(raw_rows, platform="android")

        self.assertEqual(len(summary["daily"]), 1)
        point = summary["daily"][0]
        self.assertEqual(point["date"], "2026-09-08")
        self.assertEqual(point["events"], 12)
        self.assertEqual(point["affected_users"], 7)
        self.assertEqual(point["fatal_events"], 2)
        self.assertEqual(point["anr_events"], 0)
        self.assertEqual(point["non_fatal_events"], 10)
        self.assertEqual(point["versions"], {"3.14.0": 9, "3.13.2": 3})

        errors: list[str] = []
        validate_issue_occurrence_summary(summary, errors)
        self.assertEqual(errors, [])

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
            self.assertNotIn("user_ids", d)


if __name__ == "__main__":
    unittest.main()

