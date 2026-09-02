"""Unit tests for Crashlytics BigQuery V2 Query assembly, data transformation, and Schema V2 compliance."""

from __future__ import annotations

import datetime as dt
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.fetch_bigquery import (
    SQLS,
    build_custom_keys_sql,
    extract_platform_from_table,
    format_iso_utc,
    list_crash_tables,
    norm_error_type,
    transform_bq_to_v2,
)
from crash_trend.schema_v2 import (
    DashboardV2Bundle,
    is_valid_date,
    is_valid_iso8601_utc,
    validate_app_dashboard_v2,
    validate_dashboard_v2,
)


class TestBigQuerySQLAssembly(unittest.TestCase):
    """驗證 BigQuery V2 SQL 語法組裝與欄位完整性。"""

    def test_sqls_structure_and_keys(self) -> None:
        required_sqls = [
            "overview",
            "daily_trend",
            "top_issues",
            "new_issues",
            "issue_versions",
            "by_device",
            "by_os",
            "by_app_version",
            "weekly_trend",
        ]
        for key in required_sqls:
            self.assertIn(key, SQLS, f"SQLS dictionary missing query key: {key}")
            self.assertTrue(len(SQLS[key].strip()) > 0)

    def test_overview_sql_contains_kpi_aggregations(self) -> None:
        sql = SQLS["overview"]
        self.assertIn("COUNT(*) AS total_events", sql)
        self.assertIn("COUNT(DISTINCT installation_uuid) AS distinct_users", sql)
        self.assertIn("COUNTIF(UPPER(error_type) = 'FATAL') AS fatal_events", sql)
        self.assertIn("COUNTIF(UPPER(error_type) = 'ANR') AS anr_events", sql)
        self.assertIn("COUNTIF(UPPER(error_type) NOT IN ('FATAL', 'ANR') OR error_type IS NULL) AS non_fatal_events", sql)
        self.assertIn("WHERE event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)", sql)

    def test_daily_trend_sql_format(self) -> None:
        sql = SQLS["daily_trend"]
        self.assertIn("FORMAT_TIMESTAMP('%Y-%m-%d', event_timestamp) AS date", sql)
        self.assertIn("COUNT(*) AS events", sql)
        self.assertIn("COUNT(DISTINCT installation_uuid) AS users", sql)
        self.assertIn("COUNTIF(UPPER(error_type) = 'FATAL') AS fatal_events", sql)
        self.assertIn("COUNTIF(UPPER(error_type) = 'ANR') AS anr_events", sql)
        self.assertIn("COUNTIF(UPPER(error_type) NOT IN ('FATAL', 'ANR') OR error_type IS NULL) AS non_fatal_events", sql)
        self.assertIn("GROUP BY 1", sql)
        self.assertIn("ORDER BY date ASC", sql)

    def test_top_issues_sql_utc_timestamp_and_exceptions_formatting(self) -> None:
        sql = SQLS["top_issues"]
        # Real Crashlytics schema checks
        self.assertIn("exceptions[SAFE_OFFSET(0)].type", sql)
        self.assertIn("exceptions[SAFE_OFFSET(0)].name", sql)
        self.assertIn("FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', MIN(event_timestamp)) AS first_seen_timestamp", sql)
        self.assertIn("FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', MAX(event_timestamp)) AS last_seen_timestamp", sql)
        self.assertIn("MIN(application.display_version) AS first_seen_version", sql)
        self.assertIn("MAX(application.display_version) AS last_seen_version", sql)
        self.assertIn("ORDER BY events DESC", sql)
        self.assertIn("LIMIT 50", sql)

    def test_new_issues_sql_format(self) -> None:
        sql = SQLS["new_issues"]
        self.assertIn("COUNT(DISTINCT issue_id) AS new_issues_count", sql)
        self.assertIn("NOT IN", sql)

    def test_issue_versions_sql_format(self) -> None:
        sql = SQLS["issue_versions"]
        self.assertIn("issue_id", sql)
        self.assertIn("application.display_version AS app_version", sql)
        self.assertIn("COUNT(*) AS events", sql)
        self.assertIn("COUNT(DISTINCT installation_uuid) AS users", sql)
        self.assertIn("GROUP BY 1, 2", sql)

    def test_custom_keys_sql_generation(self) -> None:
        table = "proj.dataset.com_example_app_ANDROID"
        days = 30
        keys = ["user_tier", "screen_name", "network_type"]
        sql = build_custom_keys_sql(table, days, keys)
        self.assertIsNotNone(sql)
        self.assertIn(f"`{table}`", sql)
        self.assertIn("INTERVAL 30 DAY", sql)
        self.assertIn("'user_tier', 'screen_name', 'network_type'", sql)
        self.assertIn("UNNEST(custom_keys) AS key", sql)

        # Invalid keys filtered safely
        bad_keys = ["user_tier", "screen; DROP TABLE--", "valid_key"]
        sql_safe = build_custom_keys_sql(table, days, bad_keys)
        self.assertIsNotNone(sql_safe)
        self.assertIn("'user_tier', 'valid_key'", sql_safe)
        self.assertNotIn("DROP TABLE", sql_safe)

        # Empty keys returns None
        self.assertIsNone(build_custom_keys_sql(table, days, []))
        self.assertIsNone(build_custom_keys_sql(table, days, ["invalid key with space", "###"]))


class TestTableFiltering(unittest.TestCase):
    """驗證 list_crash_tables 針對 App 設定過濾，避免跨 App 混淆。"""

    def test_list_crash_tables_filters_app_correctly(self) -> None:
        mock_client = MagicMock()
        t_shop_and = MagicMock()
        t_shop_and.table_id = "com_example_shop_ANDROID"
        t_shop_ios = MagicMock()
        t_shop_ios.table_id = "com_example_shop_IOS"
        t_rider_and = MagicMock()
        t_rider_and.table_id = "com_example_rider_ANDROID"
        t_realtime = MagicMock()
        t_realtime.table_id = "com_example_shop_ANDROID_REALTIME"

        mock_client.list_tables.return_value = [t_shop_and, t_shop_ios, t_rider_and, t_realtime]

        # Filter by shop_app package name
        shop_cfg = {"package_name": "com.example.shop", "app_id": "shop_app"}
        res = list_crash_tables(mock_client, "proj", "dataset", app_config=shop_cfg)
        self.assertIn("com_example_shop_ANDROID", res)
        self.assertIn("com_example_shop_IOS", res)
        self.assertNotIn("com_example_rider_ANDROID", res)
        self.assertNotIn("com_example_shop_ANDROID_REALTIME", res)


class TestTimestampAndDataHelpers(unittest.TestCase):
    """驗證時間戳格式轉換與輔助函式。"""

    def test_format_iso_utc_with_datetime(self) -> None:
        dt_utc = dt.datetime(2026, 9, 2, 14, 30, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(format_iso_utc(dt_utc), "2026-09-02T14:30:00Z")

        dt_naive = dt.datetime(2026, 9, 2, 14, 30, 0)
        self.assertEqual(format_iso_utc(dt_naive), "2026-09-02T14:30:00Z")

        tz_cst = dt.timezone(dt.timedelta(hours=8))
        dt_cst = dt.datetime(2026, 9, 2, 22, 30, 0, tzinfo=tz_cst)
        self.assertEqual(format_iso_utc(dt_cst), "2026-09-02T14:30:00Z")

    def test_format_iso_utc_with_date_and_strings(self) -> None:
        d = dt.date(2026, 9, 2)
        self.assertEqual(format_iso_utc(d), "2026-09-02T00:00:00Z")

        self.assertEqual(format_iso_utc("2026-09-02T14:00:00Z"), "2026-09-02T14:00:00Z")
        self.assertEqual(format_iso_utc("2026-09-02T14:00:00+00:00"), "2026-09-02T14:00:00Z")

        self.assertEqual(format_iso_utc("2026-09-02 14:00:00 UTC"), "2026-09-02T14:00:00Z")
        self.assertEqual(format_iso_utc("2026-09-02 14:00:00"), "2026-09-02T14:00:00Z")

        self.assertEqual(
            format_iso_utc(None, fallback="2026-08-01T00:00:00Z"),
            "2026-08-01T00:00:00Z",
        )

    def test_extract_platform_from_table(self) -> None:
        self.assertEqual(extract_platform_from_table("com_example_app_IOS"), "ios")
        self.assertEqual(extract_platform_from_table("com_example_app_ANDROID"), "android")
        self.assertEqual(extract_platform_from_table("app_ios_batch"), "ios")
        self.assertEqual(extract_platform_from_table("app_android_batch"), "android")
        self.assertEqual(extract_platform_from_table("my_table"), "android")

    def test_norm_error_type(self) -> None:
        self.assertEqual(norm_error_type("FATAL"), "FATAL")
        self.assertEqual(norm_error_type("fatal"), "FATAL")
        self.assertEqual(norm_error_type("ANR"), "ANR")
        self.assertEqual(norm_error_type("anr"), "ANR")
        self.assertEqual(norm_error_type("NON_FATAL"), "NON_FATAL")
        self.assertEqual(norm_error_type("non-fatal"), "NON_FATAL")
        self.assertEqual(norm_error_type("unknown", fatal_hint=True), "FATAL")
        self.assertEqual(norm_error_type(None, fatal_hint=False), "NON_FATAL")


class TestBigQueryDataTransformation(unittest.TestCase):
    """驗證 BigQuery 查詢結果轉換為 Schema V2 AppDashboardV2Data 的正確性。"""

    def setUp(self) -> None:
        self.fixed_end_time = dt.datetime(2026, 9, 2, 14, 0, 0, tzinfo=dt.timezone.utc)
        self.app_cfg = {
            "app_id": "shop_app",
            "display_name": "E-Commerce Shop",
            "firebase_project": "shop-app-prod-12345",
            "platforms": ["android"],
            "source_repo": "~/projects/shop_app",
            "custom_keys": ["user_tier", "screen_name"],
        }

    def test_single_platform_transformation(self) -> None:
        mock_bq = {
            "project": "shop-app-prod-12345",
            "dataset": "firebase_crashlytics",
            "tables": {
                "com_example_shop_ANDROID": {
                    "overview": [
                        {
                            "total_events": 1000,
                            "distinct_users": 650,
                            "fatal_events": 700,
                            "anr_events": 100,
                            "non_fatal_events": 200,
                        }
                    ],
                    "new_issues": [{"new_issues_count": 5}],
                    "daily_trend": [
                        {
                            "date": "2026-09-01",
                            "events": 400,
                            "users": 280,
                            "fatal_events": 280,
                            "anr_events": 40,
                            "non_fatal_events": 80,
                        },
                        {
                            "date": "2026-09-02",
                            "events": 600,
                            "users": 420,
                            "fatal_events": 420,
                            "anr_events": 60,
                            "non_fatal_events": 120,
                        },
                    ],
                    "top_issues": [
                        {
                            "issue_id": "issue_001",
                            "issue_title": "NullPointerException: CheckoutActivity.kt:142",
                            "issue_subtitle": "com.example.shop.CheckoutActivity.processPayment",
                            "error_type": "FATAL",
                            "events": 600,
                            "users": 400,
                            "first_seen_timestamp": "2026-08-15 08:30:00 UTC",
                            "last_seen_timestamp": "2026-09-02 13:50:00 UTC",
                            "first_seen_version": "1.0.0",
                            "last_seen_version": "1.2.0",
                        },
                        {
                            "issue_id": "issue_002",
                            "issue_title": "ANR: Input dispatching timed out",
                            "issue_subtitle": "com.example.shop.MainActivity",
                            "error_type": "ANR",
                            "events": 100,
                            "users": 80,
                            "first_seen_timestamp": "2026-08-20T10:00:00Z",
                            "last_seen_timestamp": "2026-09-01T12:00:00Z",
                            "first_seen_version": "1.1.0",
                            "last_seen_version": "1.2.0",
                        },
                    ],
                    "issue_versions": [
                        {"issue_id": "issue_001", "app_version": "1.2.0", "events": 400, "users": 280},
                        {"issue_id": "issue_001", "app_version": "1.1.0", "events": 150, "users": 90},
                        {"issue_id": "issue_001", "app_version": "1.0.0", "events": 50, "users": 30},
                        {"issue_id": "issue_002", "app_version": "1.2.0", "events": 100, "users": 80},
                    ],
                    "by_device": [
                        {"device_model": "Pixel 8", "events": 500, "users": 350},
                        {"device_model": "Galaxy S24", "events": 300, "users": 200},
                        {"device_model": "Redmi Note 12", "events": 200, "users": 100},
                    ],
                    "by_os": [
                        {"os_version": "Android 14", "events": 600, "users": 400},
                        {"os_version": "Android 13", "events": 400, "users": 250},
                    ],
                    "by_app_version": [
                        {"app_version": "1.2.0", "events": 700, "users": 480},
                        {"app_version": "1.1.0", "events": 250, "users": 140},
                        {"app_version": "1.0.0", "events": 50, "users": 30},
                    ],
                    "custom_keys": [
                        {"custom_key": "user_tier", "value": "vip", "events": 400},
                        {"custom_key": "screen_name", "value": "checkout", "events": 600},
                    ],
                }
            },
            "errors": {},
        }

        days = 30
        res = transform_bq_to_v2(mock_bq, self.app_cfg, days=days, end_time=self.fixed_end_time)

        # 1. Metadata check
        self.assertEqual(res["metadata"]["app_id"], "shop_app")
        self.assertEqual(res["metadata"]["platforms"], ["android"])
        self.assertEqual(res["period"]["days"], 30)
        self.assertTrue(is_valid_iso8601_utc(res["period"]["start_time"]))
        self.assertTrue(is_valid_iso8601_utc(res["period"]["end_time"]))

        # 2. Overview KPI check
        self.assertEqual(res["kpi"]["crash_events"]["value"], 1000)
        self.assertEqual(res["kpi"]["affected_users"]["value"], 650)
        self.assertEqual(res["kpi"]["new_issues_count"]["value"], 5)
        self.assertEqual(res["kpi"]["new_issues_count"]["status"], "available")
        self.assertEqual(res["kpi"]["events_by_error_type"]["fatal"], 700)
        self.assertEqual(res["kpi"]["events_by_error_type"]["anr"], 100)
        self.assertEqual(res["kpi"]["events_by_error_type"]["non_fatal"], 200)

        # 3. Daily trend check
        daily = res["daily_trend"]
        self.assertEqual(len(daily), 30, "Daily trend must have exactly 30 days")
        self.assertEqual(daily[-1]["date"], "2026-09-02")
        self.assertEqual(daily[-1]["crash_events"], 600)
        self.assertEqual(daily[-2]["date"], "2026-09-01")
        self.assertEqual(daily[-2]["crash_events"], 400)
        self.assertEqual(daily[0]["crash_events"], 0)
        self.assertEqual(daily[0]["fatal_events"], 0)

        self.assertEqual(sum(d["crash_events"] for d in daily), res["kpi"]["crash_events"]["value"])
        for d in daily:
            self.assertEqual(d["fatal_events"] + d["anr_events"] + d["non_fatal_events"], d["crash_events"])

        # 4. Top issues check
        issues = res["top_issues"]
        self.assertEqual(len(issues), 2)
        top = issues[0]
        self.assertEqual(top["issue_id"], "issue_001")
        self.assertEqual(top["error_type"], "FATAL")
        self.assertEqual(top["first_seen_timestamp"], "2026-08-15T08:30:00Z")
        self.assertEqual(top["last_seen_timestamp"], "2026-09-02T13:50:00Z")
        self.assertEqual(top["first_seen_version"], "1.0.0")
        self.assertEqual(top["last_seen_version"], "1.2.0")
        self.assertEqual(len(top["version_distribution"]), 3)
        self.assertEqual(top["version_distribution"][0]["version"], "1.2.0")

        # 5. Distributions check
        dists = res["distributions"]
        self.assertEqual(len(dists["platform"]), 1)
        self.assertEqual(dists["platform"][0]["name"], "android")
        self.assertEqual(dists["platform"][0]["events"], 1000)
        self.assertEqual(dists["platform"][0]["share"], 1.0)
        self.assertEqual(len(dists["device_models"]), 3)
        self.assertEqual(dists["device_models"][0]["model"], "Pixel 8")
        self.assertAlmostEqual(dists["device_models"][0]["share"], 0.5)

        # 6. Version health check
        self.assertTrue(len(res["version_health"]) >= 3)
        self.assertEqual(res["version_health"][0]["version"], "1.2.0")
        self.assertEqual(res["version_health"][0]["status"], "latest")

    def test_multi_platform_transformation(self) -> None:
        mock_bq_multi = {
            "project": "shop-app-prod-12345",
            "dataset": "firebase_crashlytics",
            "tables": {
                "com_example_shop_ANDROID": {
                    "overview": [
                        {
                            "total_events": 600,
                            "distinct_users": 400,
                            "fatal_events": 400,
                            "anr_events": 80,
                            "non_fatal_events": 120,
                        }
                    ],
                    "daily_trend": [
                        {
                            "date": "2026-09-02",
                            "events": 600,
                            "users": 400,
                            "fatal_events": 400,
                            "anr_events": 80,
                            "non_fatal_events": 120,
                        }
                    ],
                    "top_issues": [
                        {
                            "issue_id": "and_01",
                            "issue_title": "AndroidCrash",
                            "issue_subtitle": "Main.kt",
                            "error_type": "FATAL",
                            "events": 600,
                            "users": 400,
                            "first_seen_timestamp": "2026-09-01T00:00:00Z",
                            "last_seen_timestamp": "2026-09-02T12:00:00Z",
                            "first_seen_version": "1.0.0",
                            "last_seen_version": "1.0.0",
                        }
                    ],
                    "issue_versions": [
                        {"issue_id": "and_01", "app_version": "1.0.0", "events": 600, "users": 400}
                    ],
                    "by_device": [{"device_model": "Pixel 8", "events": 600, "users": 400}],
                    "by_os": [{"os_version": "Android 14", "events": 600, "users": 400}],
                    "by_app_version": [{"app_version": "1.0.0", "events": 600, "users": 400}],
                    "custom_keys": [],
                },
                "com_example_shop_IOS": {
                    "overview": [
                        {
                            "total_events": 400,
                            "distinct_users": 300,
                            "fatal_events": 300,
                            "anr_events": 0,
                            "non_fatal_events": 100,
                        }
                    ],
                    "daily_trend": [
                        {
                            "date": "2026-09-02",
                            "events": 400,
                            "users": 300,
                            "fatal_events": 300,
                            "anr_events": 0,
                            "non_fatal_events": 100,
                        }
                    ],
                    "top_issues": [
                        {
                            "issue_id": "ios_01",
                            "issue_title": "NSInvalidArgumentException",
                            "issue_subtitle": "ViewController.swift",
                            "error_type": "FATAL",
                            "events": 400,
                            "users": 300,
                            "first_seen_timestamp": "2026-09-01T00:00:00Z",
                            "last_seen_timestamp": "2026-09-02T12:00:00Z",
                            "first_seen_version": "1.0.0",
                            "last_seen_version": "1.0.0",
                        }
                    ],
                    "issue_versions": [
                        {"issue_id": "ios_01", "app_version": "1.0.0", "events": 400, "users": 300}
                    ],
                    "by_device": [{"device_model": "iPhone 15 Pro", "events": 400, "users": 300}],
                    "by_os": [{"os_version": "iOS 18.0", "events": 400, "users": 300}],
                    "by_app_version": [{"app_version": "1.0.0", "events": 400, "users": 300}],
                    "custom_keys": [],
                },
            },
            "errors": {},
        }

        multi_cfg = {**self.app_cfg, "platforms": ["android", "ios"]}
        res = transform_bq_to_v2(mock_bq_multi, multi_cfg, days=30, end_time=self.fixed_end_time)

        # 1. Total aggregation check
        self.assertEqual(res["metadata"]["platforms"], ["android", "ios"])
        self.assertEqual(res["kpi"]["crash_events"]["value"], 1000)
        self.assertEqual(res["kpi"]["affected_users"]["value"], 700)
        self.assertEqual(res["kpi"]["events_by_error_type"]["fatal"], 700)
        self.assertEqual(res["kpi"]["events_by_error_type"]["anr"], 80)
        self.assertEqual(res["kpi"]["events_by_error_type"]["non_fatal"], 220)

        # 2. Daily trend by_platform check
        last_day = res["daily_trend"][-1]
        self.assertEqual(last_day["crash_events"], 1000)
        self.assertEqual(last_day["by_platform"]["android"]["events"], 600)
        self.assertEqual(last_day["by_platform"]["ios"]["events"], 400)

        # 3. Platform distribution shares
        pf_dist = {p["name"]: p for p in res["distributions"]["platform"]}
        self.assertEqual(pf_dist["android"]["events"], 600)
        self.assertAlmostEqual(pf_dist["android"]["share"], 0.6)
        self.assertEqual(pf_dist["ios"]["events"], 400)
        self.assertAlmostEqual(pf_dist["ios"]["share"], 0.4)


class TestBigQuerySchemaV2Compliance(unittest.TestCase):
    """驗證轉換產出完全通過 Schema V2 驗證器。"""

    def test_transformed_data_passes_strict_schema_v2_validation(self) -> None:
        mock_bq = {
            "project": "shop-app-prod-12345",
            "dataset": "firebase_crashlytics",
            "tables": {
                "com_example_shop_ANDROID": {
                    "overview": [
                        {
                            "total_events": 500,
                            "distinct_users": 320,
                            "fatal_events": 350,
                            "anr_events": 50,
                            "non_fatal_events": 100,
                        }
                    ],
                    "daily_trend": [
                        {
                            "date": "2026-09-02",
                            "events": 500,
                            "users": 320,
                            "fatal_events": 350,
                            "anr_events": 50,
                            "non_fatal_events": 100,
                        }
                    ],
                    "top_issues": [
                        {
                            "issue_id": "8a7f1b2c",
                            "issue_title": "NullPointerException",
                            "issue_subtitle": "CheckoutActivity.kt:142",
                            "error_type": "FATAL",
                            "events": 500,
                            "users": 320,
                            "first_seen_timestamp": "2026-08-12T09:15:00Z",
                            "last_seen_timestamp": "2026-09-02T13:40:00Z",
                            "first_seen_version": "1.0.0",
                            "last_seen_version": "1.1.0",
                        }
                    ],
                    "issue_versions": [
                        {"issue_id": "8a7f1b2c", "app_version": "1.1.0", "events": 400, "users": 250},
                        {"issue_id": "8a7f1b2c", "app_version": "1.0.0", "events": 100, "users": 70},
                    ],
                    "by_device": [{"device_model": "Pixel 8", "events": 500, "users": 320}],
                    "by_os": [{"os_version": "Android 14", "events": 500, "users": 320}],
                    "by_app_version": [
                        {"app_version": "1.1.0", "events": 400, "users": 250},
                        {"app_version": "1.0.0", "events": 100, "users": 70},
                    ],
                    "custom_keys": [{"custom_key": "user_tier", "value": "vip", "events": 300}],
                }
            },
            "errors": {},
        }

        app_cfg = {
            "app_id": "shop_app",
            "display_name": "E-Commerce Shop",
            "firebase_project": "shop-app-prod-12345",
            "platforms": ["android"],
            "source_repo": "~/projects/shop_app",
            "custom_keys": ["user_tier"],
        }

        app_data = transform_bq_to_v2(mock_bq, app_cfg, days=30)
        errors = validate_app_dashboard_v2(app_data)
        self.assertEqual(errors, [], f"AppDashboardV2Data validation failed with errors:\n" + "\n".join(errors))

        # Bundle validation
        bundle: DashboardV2Bundle = {
            "schema_version": "2.0",
            "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "default_app": "shop_app",
            "apps": {"shop_app": app_data},
        }
        bundle_errors = validate_dashboard_v2(bundle)
        self.assertEqual(bundle_errors, [], f"DashboardV2Bundle validation failed:\n" + "\n".join(bundle_errors))


class TestBigQueryEdgeCases(unittest.TestCase):
    """驗證邊界與容錯情況。"""

    def test_empty_tables_transformation(self) -> None:
        empty_bq = {"project": "empty-proj", "dataset": "firebase_crashlytics", "tables": {}, "errors": {}}
        app_cfg = {
            "app_id": "empty_app",
            "display_name": "Empty App",
            "firebase_project": "empty-proj",
            "platforms": ["android"],
        }
        app_data = transform_bq_to_v2(empty_bq, app_cfg, days=14)

        self.assertEqual(app_data["kpi"]["crash_events"]["value"], 0)
        self.assertEqual(app_data["kpi"]["affected_users"]["value"], 0)
        self.assertEqual(app_data["kpi"]["new_issues_count"]["status"], "insufficient_data")
        self.assertEqual(len(app_data["daily_trend"]), 14)
        for dp in app_data["daily_trend"]:
            self.assertEqual(dp["crash_events"], 0)
            self.assertEqual(dp["fatal_events"] + dp["anr_events"] + dp["non_fatal_events"], 0)

        errors = validate_app_dashboard_v2(app_data)
        self.assertEqual(errors, [])

    def test_overview_fallback_from_daily_trend(self) -> None:
        mock_bq_no_ov = {
            "tables": {
                "com_example_app_ANDROID": {
                    "overview": [],
                    "daily_trend": [
                        {"date": "2026-09-01", "events": 200, "users": 150, "fatal_events": 150, "anr_events": 20, "non_fatal_events": 30},
                        {"date": "2026-09-02", "events": 300, "users": 200, "fatal_events": 200, "anr_events": 30, "non_fatal_events": 70},
                    ],
                    "top_issues": [],
                    "issue_versions": [],
                    "by_device": [],
                    "by_os": [],
                    "by_app_version": [],
                    "custom_keys": [],
                }
            }
        }
        res = transform_bq_to_v2(mock_bq_no_ov, {"app_id": "app1", "firebase_project": "p1"}, days=30)
        self.assertEqual(res["kpi"]["crash_events"]["value"], 500)
        self.assertEqual(res["kpi"]["events_by_error_type"]["fatal"], 350)
        self.assertEqual(res["kpi"]["events_by_error_type"]["anr"], 50)
        self.assertEqual(res["kpi"]["events_by_error_type"]["non_fatal"], 100)
        self.assertEqual(validate_app_dashboard_v2(res), [])

    def test_unusual_error_types_and_missing_versions(self) -> None:
        mock_bq = {
            "tables": {
                "com_example_app_IOS": {
                    "overview": [{"total_events": 100, "distinct_users": 80, "fatal_events": 100, "anr_events": 0, "non_fatal_events": 0}],
                    "daily_trend": [{"date": "2026-09-02", "events": 100, "users": 80, "fatal_events": 100, "anr_events": 0, "non_fatal_events": 0}],
                    "top_issues": [
                        {
                            "issue_id": "iss_strange",
                            "issue_title": "Strange Error",
                            "issue_subtitle": "unknown location",
                            "error_type": "custom_crash_type",
                            "events": 100,
                            "users": 80,
                            "first_seen_timestamp": None,
                            "last_seen_timestamp": "invalid-timestamp-string",
                            "first_seen_version": None,
                            "last_seen_version": None,
                        }
                    ],
                    "issue_versions": [],
                    "by_device": [],
                    "by_os": [],
                    "by_app_version": [],
                    "custom_keys": [],
                }
            }
        }
        res = transform_bq_to_v2(mock_bq, {"app_id": "app_ios", "firebase_project": "p1", "platforms": ["ios"]}, days=30)
        iss = res["top_issues"][0]
        self.assertEqual(iss["error_type"], "NON_FATAL")
        self.assertTrue(is_valid_iso8601_utc(iss["first_seen_timestamp"]))
        self.assertTrue(is_valid_iso8601_utc(iss["last_seen_timestamp"]))
        self.assertEqual(iss["first_seen_version"], "1.0.0")
        self.assertEqual(validate_app_dashboard_v2(res), [])

    def test_days_variations(self) -> None:
        for days in (7, 14, 30, 90):
            res = transform_bq_to_v2({"tables": {}}, {"app_id": "test_app", "firebase_project": "p1"}, days=days)
            self.assertEqual(res["period"]["days"], days)
            self.assertEqual(len(res["daily_trend"]), days)
            self.assertEqual(validate_app_dashboard_v2(res), [])


if __name__ == "__main__":
    unittest.main()
