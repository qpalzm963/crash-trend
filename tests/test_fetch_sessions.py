"""Unit tests for Firebase Sessions fetching, metric calculation, graceful degradation, and Schema V2 validation."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.fetch_sessions import (
    DEFAULT_UNAVAILABLE_REASON,
    SQLS,
    build_crash_free_metric,
    build_unavailable_sessions_result,
    calculate_adoption_rate,
    calculate_change_pct_points,
    calculate_crash_free_rate,
    compute_daily_sessions,
    compute_version_sessions,
    enrich_app_dashboard_with_sessions,
    fetch_sessions_data,
    list_session_tables,
)
from crash_trend.schema_v2 import validate_app_dashboard_v2, validate_dashboard_v2


class TestFetchSessionsMetrics(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures_dir = ROOT / "tests" / "fixtures"

    # -----------------------------------------------------------------------
    # 1. Calculation & Pure Metric Tests
    # -----------------------------------------------------------------------

    def test_calculate_crash_free_rate(self) -> None:
        # Standard examples
        self.assertAlmostEqual(calculate_crash_free_rate(520000, 780), 0.9985, places=4)
        self.assertAlmostEqual(calculate_crash_free_rate(3400000, 2720), 0.9992, places=4)

        # 0 crashes = 100% crash free
        self.assertEqual(calculate_crash_free_rate(1000, 0), 1.0)

        # 100% crashed = 0.0
        self.assertEqual(calculate_crash_free_rate(100, 100), 0.0)

        # Crashed exceeds total (anomaly clamped to 0.0)
        self.assertEqual(calculate_crash_free_rate(100, 150), 0.0)

        # Crashed is negative (anomaly clamped to 0)
        self.assertEqual(calculate_crash_free_rate(100, -10), 1.0)

        # Total is 0 or None -> returns None
        self.assertIsNone(calculate_crash_free_rate(0, 0))
        self.assertIsNone(calculate_crash_free_rate(None, 10))
        self.assertIsNone(calculate_crash_free_rate(100, None))
        self.assertIsNone(calculate_crash_free_rate(-50, 5))

    def test_calculate_change_pct_points(self) -> None:
        # Rate improved by 0.13 percentage points
        self.assertAlmostEqual(calculate_change_pct_points(0.9985, 0.9972), 0.13, places=2)

        # Rate improved by 0.04 percentage points
        self.assertAlmostEqual(calculate_change_pct_points(0.9992, 0.9988), 0.04, places=2)

        # Rate degraded by 0.50 percentage points
        self.assertAlmostEqual(calculate_change_pct_points(0.9850, 0.9900), -0.50, places=2)

        # None handling
        self.assertIsNone(calculate_change_pct_points(0.99, None))
        self.assertIsNone(calculate_change_pct_points(None, 0.99))
        self.assertIsNone(calculate_change_pct_points(None, None))

    def test_calculate_adoption_rate(self) -> None:
        self.assertAlmostEqual(calculate_adoption_rate(58000, 100000), 0.58, places=4)
        self.assertAlmostEqual(calculate_adoption_rate(32000, 100000), 0.32, places=4)
        self.assertIsNone(calculate_adoption_rate(0, 0))
        self.assertIsNone(calculate_adoption_rate(None, 100))
        self.assertIsNone(calculate_adoption_rate(10, None))

    def test_build_crash_free_metric_available(self) -> None:
        metric = build_crash_free_metric(520000, 780, previous_rate=0.9972, status="available")
        self.assertEqual(metric["status"], "available")
        self.assertAlmostEqual(metric["rate"], 0.9985, places=4)
        self.assertEqual(metric["total"], 520000)
        self.assertEqual(metric["crashed"], 780)
        self.assertAlmostEqual(metric["previous_rate"], 0.9972, places=4)
        self.assertAlmostEqual(metric["change_pct_points"], 0.13, places=2)
        self.assertIsNone(metric["unavailable_reason"])

    # -----------------------------------------------------------------------
    # 2. SQL Templates & Official Schema Conformance
    # -----------------------------------------------------------------------

    def test_sqls_conform_to_official_sessions_and_crashlytics_schema(self) -> None:
        for sql_key in ("kpi_joined", "kpi_previous_joined", "daily_joined", "versions_joined"):
            sql = SQLS[sql_key]
            # Official Sessions columns
            self.assertIn("session_id", sql)
            self.assertNotIn("session_info", sql)
            self.assertNotIn("TIMESTAMP_MICROS", sql)
            self.assertNotIn("user_pseudo_id", sql)
            # Official Crashlytics join columns
            self.assertIn("firebase_session_id", sql)
            self.assertIn("UPPER(error_type) = 'FATAL'", sql)

    # -----------------------------------------------------------------------
    # 3. Graceful Degradation & Unavailable Semantics (No Fake 0%)
    # -----------------------------------------------------------------------

    def test_build_unavailable_sessions_result_strict_no_fake_zeros(self) -> None:
        reason = "Firebase Sessions export not enabled in project"
        res = build_unavailable_sessions_result(reason)

        # Sources
        self.assertEqual(res["sources"]["status"], "unavailable")
        self.assertIsNone(res["sources"]["last_sync_timestamp"])
        self.assertEqual(res["sources"]["error_message"], reason)
        self.assertIsNone(res["sources"]["tables_queried"])

        # KPI Users
        cf_users = res["kpi"]["crash_free_users"]
        self.assertEqual(cf_users["status"], "unavailable")
        self.assertIsNone(cf_users["rate"], "Rate must be null/None when unavailable (Strictly NO fake 0%!)")
        self.assertNotEqual(cf_users["rate"], 0.0)
        self.assertNotEqual(cf_users["rate"], 0)
        self.assertIsNone(cf_users["total"])
        self.assertIsNone(cf_users["crashed"])
        self.assertIsNone(cf_users["previous_rate"])
        self.assertIsNone(cf_users["change_pct_points"])
        self.assertEqual(cf_users["unavailable_reason"], reason)

        # KPI Sessions
        cf_sess = res["kpi"]["crash_free_sessions"]
        self.assertEqual(cf_sess["status"], "unavailable")
        self.assertIsNone(cf_sess["rate"], "Rate must be null/None when unavailable (Strictly NO fake 0%!)")
        self.assertNotEqual(cf_sess["rate"], 0.0)
        self.assertNotEqual(cf_sess["rate"], 0)
        self.assertIsNone(cf_sess["total"])
        self.assertIsNone(cf_sess["crashed"])
        self.assertEqual(cf_sess["unavailable_reason"], reason)

    def test_compute_daily_sessions_aggregation(self) -> None:
        raw_rows = [
            {"date": "2026-09-01", "sessions_total": 10000, "crashed_sessions": 10},
            {"date": "2026-09-01", "sessions_total": 5000, "crashed_sessions": 5},
            {"date": "2026-09-02", "sessions_total": 20000, "crashed_sessions": 40},
        ]
        daily = compute_daily_sessions(raw_rows)
        self.assertIn("2026-09-01", daily)
        self.assertEqual(daily["2026-09-01"]["sessions_total"], 15000)
        self.assertEqual(daily["2026-09-01"]["crashed_sessions"], 15)
        self.assertAlmostEqual(daily["2026-09-01"]["crash_free_sessions_rate"], 0.9990, places=4)

        self.assertIn("2026-09-02", daily)
        self.assertEqual(daily["2026-09-02"]["sessions_total"], 20000)
        self.assertEqual(daily["2026-09-02"]["crashed_sessions"], 40)
        self.assertAlmostEqual(daily["2026-09-02"]["crash_free_sessions_rate"], 0.9980, places=4)

    def test_compute_version_sessions_aggregation(self) -> None:
        raw_rows = [
            {
                "version": "3.2.0",
                "sessions_total": 58000,
                "crashed_sessions": 58,
                "users_total": 20000,
                "crashed_users": 40,
            },
            {
                "version": "3.1.2",
                "sessions_total": 32000,
                "crashed_sessions": 16,
                "users_total": 10000,
                "crashed_users": 10,
            },
            {
                "version": "3.1.0",
                "sessions_total": 10000,
                "crashed_sessions": 20,
                "users_total": 3000,
                "crashed_users": 15,
            },
        ]
        ver_map = compute_version_sessions(raw_rows, overall_total_sessions=100000)
        self.assertIn("3.2.0", ver_map)
        v320 = ver_map["3.2.0"]
        self.assertEqual(v320["sessions_total"], 58000)
        self.assertAlmostEqual(v320["crash_free_sessions_rate"], 0.9990, places=4)
        self.assertAlmostEqual(v320["crash_free_users_rate"], 0.9980, places=4)
        self.assertAlmostEqual(v320["adoption_rate"], 0.58, places=4)

        v312 = ver_map["3.1.2"]
        self.assertAlmostEqual(v312["adoption_rate"], 0.32, places=4)
        self.assertAlmostEqual(v312["crash_free_sessions_rate"], 0.9995, places=4)

    # -----------------------------------------------------------------------
    # 4. Schema V2 Integration & Validation
    # -----------------------------------------------------------------------

    def test_enrich_app_dashboard_with_sessions_available_passes_validation(self) -> None:
        fixture_path = self.fixtures_dir / "dashboard_v2_no_sessions.json"
        bundle = json.loads(fixture_path.read_text(encoding="utf-8"))
        app_data = bundle["apps"]["legacy_app"]

        sessions_result = {
            "sources": {
                "status": "available",
                "last_sync_timestamp": "2026-09-02T14:00:00Z",
                "error_message": None,
                "tables_queried": ["com_example_legacy_ANDROID"],
            },
            "kpi": {
                "crash_free_users": build_crash_free_metric(50000, 100, previous_rate=0.9970, status="available"),
                "crash_free_sessions": build_crash_free_metric(200000, 200, previous_rate=0.9985, status="available"),
            },
            "daily_trend": {
                pt["date"]: {
                    "sessions_total": 6000,
                    "crashed_sessions": 6,
                    "crash_free_sessions_rate": 0.9990,
                }
                for pt in app_data["daily_trend"]
            },
            "version_health": {
                "1.0.4": {
                    "version": "1.0.4",
                    "crash_free_users_rate": 0.9980,
                    "crash_free_sessions_rate": 0.9990,
                    "adoption_rate": 1.0,
                }
            },
        }

        enriched_app = enrich_app_dashboard_with_sessions(app_data, sessions_result)
        errors = validate_app_dashboard_v2(enriched_app, prefix="apps['legacy_app']")
        self.assertEqual(errors, [], f"Enriched available app data failed validation: {errors}")

        self.assertEqual(enriched_app["sources"]["firebase_sessions"]["status"], "available")
        self.assertEqual(enriched_app["kpi"]["crash_free_users"]["status"], "available")
        self.assertAlmostEqual(enriched_app["kpi"]["crash_free_users"]["rate"], 0.9980)
        self.assertEqual(enriched_app["kpi"]["crash_free_sessions"]["status"], "available")
        self.assertAlmostEqual(enriched_app["kpi"]["crash_free_sessions"]["rate"], 0.9990)
        self.assertEqual(enriched_app["daily_trend"][0]["sessions_total"], 6000)
        self.assertEqual(enriched_app["version_health"][0]["crash_free_sessions_rate"], 0.9990)

    def test_enrich_app_dashboard_with_sessions_unavailable_passes_validation(self) -> None:
        fixture_path = self.fixtures_dir / "dashboard_v2.json"
        bundle = json.loads(fixture_path.read_text(encoding="utf-8"))
        app_data = copy.deepcopy(bundle["apps"]["shop_app"])

        unavailable_res = build_unavailable_sessions_result("Sessions table not found")
        enriched_app = enrich_app_dashboard_with_sessions(app_data, unavailable_res)

        errors = validate_app_dashboard_v2(enriched_app, prefix="apps['shop_app']")
        self.assertEqual(errors, [], f"Enriched unavailable app data failed validation: {errors}")

        self.assertEqual(enriched_app["sources"]["firebase_sessions"]["status"], "unavailable")
        self.assertEqual(enriched_app["kpi"]["crash_free_users"]["status"], "unavailable")
        self.assertIsNone(enriched_app["kpi"]["crash_free_users"]["rate"])
        self.assertIsNone(enriched_app["kpi"]["crash_free_sessions"]["rate"])
        self.assertIsNone(enriched_app["daily_trend"][0]["sessions_total"])
        self.assertIsNone(enriched_app["daily_trend"][0]["crashed_sessions"])
        self.assertIsNone(enriched_app["daily_trend"][0]["crash_free_sessions_rate"])
        self.assertIsNone(enriched_app["version_health"][0]["crash_free_users_rate"])
        self.assertIsNone(enriched_app["version_health"][0]["crash_free_sessions_rate"])

    def test_full_bundle_passes_validate_dashboard_v2(self) -> None:
        fixture_path = self.fixtures_dir / "dashboard_v2.json"
        bundle = json.loads(fixture_path.read_text(encoding="utf-8"))

        shop = bundle["apps"]["shop_app"]
        enrich_app_dashboard_with_sessions(shop, build_unavailable_sessions_result("Table expired"))

        errors = validate_dashboard_v2(bundle)
        self.assertEqual(errors, [], f"Dashboard bundle failed validation: {errors}")

    # -----------------------------------------------------------------------
    # 5. BigQuery Client Mocking & Table Filtering Tests
    # -----------------------------------------------------------------------

    def test_list_session_tables_handles_exception_gracefully(self) -> None:
        mock_client = MagicMock()
        mock_client.list_tables.side_effect = Exception("Dataset not found 404")

        tables = list_session_tables(mock_client, "dummy-proj", "firebase_sessions")
        self.assertEqual(tables, [])

    def test_list_session_tables_filters_by_app_and_returns_empty_on_mismatch(self) -> None:
        mock_client = MagicMock()
        t1 = MagicMock()
        t1.table_id = "com_example_rider_ANDROID"
        mock_client.list_tables.return_value = [t1]

        shop_cfg = {"package_name": "com.example.shop", "app_id": "shop_app"}
        res = list_session_tables(mock_client, "proj", "dataset", app_config=shop_cfg)
        self.assertEqual(res, [])

    def test_fetch_sessions_data_graceful_on_missing_tables(self) -> None:
        mock_client = MagicMock()
        mock_client.list_tables.return_value = []

        res = fetch_sessions_data("dummy-proj", "firebase_sessions", client=mock_client)
        self.assertEqual(res["sources"]["status"], "unavailable")
        self.assertIn("not found", res["sources"]["error_message"])
        self.assertEqual(res["kpi"]["crash_free_users"]["status"], "unavailable")
        self.assertIsNone(res["kpi"]["crash_free_users"]["rate"])
        self.assertEqual(res["kpi"]["crash_free_sessions"]["status"], "unavailable")
        self.assertIsNone(res["kpi"]["crash_free_sessions"]["rate"])

    def test_fetch_sessions_data_with_mocked_success(self) -> None:
        mock_client = MagicMock()

        t1 = MagicMock()
        t1.table_id = "events_20260901"
        mock_client.list_tables.return_value = [t1]

        mock_kpi_row = {
            "total_sessions": 100000,
            "total_users": 20000,
            "crashed_sessions": 150,
            "crashed_users": 50,
        }
        mock_daily_rows = [
            {"date": "2026-09-01", "sessions_total": 50000, "crashed_sessions": 75},
            {"date": "2026-09-02", "sessions_total": 50000, "crashed_sessions": 75},
        ]
        mock_ver_rows = [
            {"version": "1.0.0", "sessions_total": 70000, "crashed_sessions": 90, "users_total": 14000, "crashed_users": 30},
            {"version": "0.9.0", "sessions_total": 30000, "crashed_sessions": 60, "users_total": 6000, "crashed_users": 20},
        ]

        def query_side_effect(sql: str):
            mock_res = MagicMock()
            if "s.version" in sql or "version" in sql and "GROUP BY 1" in sql and "FORMAT_TIMESTAMP" not in sql:
                mock_res.result.return_value = mock_ver_rows
            elif "FORMAT_TIMESTAMP" in sql or "session_date" in sql:
                mock_res.result.return_value = mock_daily_rows
            elif "total_sessions" in sql:
                mock_res.result.return_value = [mock_kpi_row]
            else:
                mock_res.result.return_value = []
            return mock_res

        mock_client.query.side_effect = query_side_effect

        res = fetch_sessions_data("test-proj", client=mock_client, days=30)
        self.assertEqual(res["sources"]["status"], "available")
        self.assertEqual(res["sources"]["tables_queried"], ["events_20260901"])

        cf_users = res["kpi"]["crash_free_users"]
        self.assertEqual(cf_users["status"], "available")
        self.assertEqual(cf_users["total"], 20000)
        self.assertEqual(cf_users["crashed"], 50)
        self.assertAlmostEqual(cf_users["rate"], 0.9975, places=4)

        cf_sess = res["kpi"]["crash_free_sessions"]
        self.assertEqual(cf_sess["status"], "available")
        self.assertEqual(cf_sess["total"], 100000)
        self.assertEqual(cf_sess["crashed"], 150)
        self.assertAlmostEqual(cf_sess["rate"], 0.9985, places=4)

        # Daily trend
        self.assertIn("2026-09-01", res["daily_trend"])
        self.assertEqual(res["daily_trend"]["2026-09-01"]["sessions_total"], 50000)

        # Version health
        self.assertIn("1.0.0", res["version_health"])
        self.assertAlmostEqual(res["version_health"]["1.0.0"]["adoption_rate"], 0.70, places=2)

    def test_fetch_sessions_data_with_query_exception_returns_error_gracefully(self) -> None:
        mock_client = MagicMock()
        t1 = MagicMock()
        t1.table_id = "events_20260901"
        mock_client.list_tables.return_value = [t1]
        mock_client.query.side_effect = Exception("Permission denied on table")

        res = fetch_sessions_data("test-proj", client=mock_client)
        self.assertEqual(res["sources"]["status"], "error")
        self.assertIn("Permission denied", res["sources"]["error_message"])
        self.assertEqual(res["kpi"]["crash_free_users"]["status"], "error")
        self.assertIsNone(res["kpi"]["crash_free_users"]["rate"])


class TestSessionsPlatformIsolationAndSchema(unittest.TestCase):
    """Verifies Must Fix 1 & Must Fix 2: Materialized 0-crash VersionHealthItem conforms to Schema V2.3,
    and Sessions version evidence strictly isolates Android and iOS.
    """

    def setUp(self) -> None:
        self.fixtures_dir = ROOT / "tests" / "fixtures"
        base_bundle = json.loads((self.fixtures_dir / "dashboard_v2.json").read_text(encoding="utf-8"))
        self.base_app = base_bundle["apps"]["shop_app"]

    def test_materialized_0_crash_version_conforms_to_schema_v2_3(self):
        """Must Fix 1: Sessions materialize 0-crash latest -> validate_app_dashboard_v2(enriched) == []."""
        app_data = copy.deepcopy(self.base_app)
        # Add a 30-day period snapshot to test both top-level and periods
        app_data["periods"] = {
            "30": {
                "period": {"days": 30, "start_time": "2026-08-05T00:00:00Z", "end_time": "2026-09-03T23:59:59Z"},
                "kpi": copy.deepcopy(app_data["kpi"]),
                "version_health": copy.deepcopy(app_data["version_health"]),
                "distributions": copy.deepcopy(app_data["distributions"]),
                "top_issues": copy.deepcopy(app_data["top_issues"]),
            }
        }

        # 1.0.11 is a new version only present in Sessions with 0 crashes
        sessions_res = {
            "sources": {"status": "available", "last_sync_timestamp": "2026-09-03T12:00:00Z"},
            "kpi": {
                "crash_free_users": build_crash_free_metric(10000, 10, previous_rate=0.98, status="available"),
                "crash_free_sessions": build_crash_free_metric(50000, 5, previous_rate=0.998, status="available"),
            },
            "daily_trend": {},
            "version_health": {
                "android:1.0.11": {
                    "version": "1.0.11",
                    "platform": "android",
                    "crash_free_users_rate": 1.0,
                    "crash_free_sessions_rate": 1.0,
                    "adoption_rate": 0.25,
                }
            },
            "periods": {
                "30": {
                    "sources": {"status": "available"},
                    "kpi": {
                        "crash_free_users": build_crash_free_metric(10000, 10, previous_rate=0.98, status="available"),
                        "crash_free_sessions": build_crash_free_metric(50000, 5, previous_rate=0.998, status="available"),
                    },
                    "daily_trend": {},
                    "version_health": {
                        "android:1.0.11": {
                            "version": "1.0.11",
                            "platform": "android",
                            "crash_free_users_rate": 1.0,
                            "crash_free_sessions_rate": 1.0,
                            "adoption_rate": 0.25,
                        }
                    },
                }
            },
        }

        enriched = enrich_app_dashboard_with_sessions(app_data, sessions_res)

        # 1.0.11 must be in top-level version_health with release_date, trend, status, platform
        v_top = next((v for v in enriched["version_health"] if v.get("version") == "1.0.11"), None)
        self.assertIsNotNone(v_top)
        self.assertIn("release_date", v_top)
        self.assertIn("trend", v_top)
        self.assertEqual(v_top["trend"], "new")
        self.assertEqual(v_top["status"], "latest")
        self.assertEqual(v_top["platform"], "android")

        # 1.0.11 must be in period 30 version_health with release_date, trend, status, platform
        v_snap = next((v for v in enriched["periods"]["30"]["version_health"] if v.get("version") == "1.0.11"), None)
        self.assertIsNotNone(v_snap)
        self.assertIn("release_date", v_snap)
        self.assertIn("trend", v_snap)
        self.assertEqual(v_snap["trend"], "new")
        self.assertEqual(v_snap["status"], "latest")
        self.assertEqual(v_snap["platform"], "android")

        # Strict Schema validation must succeed with 0 errors!
        errs = validate_app_dashboard_v2(enriched, require_lifecycle=True)
        self.assertEqual(errs, [])

    def test_sessions_version_evidence_multi_platform_isolation(self):
        """Must Fix 2:
        - Android 1.0.10 + iOS 2.1.0 (iOS 0 crash) -> 2.1.0 materialize as ios latest, not android.
        - Android/iOS both have 1.0.10, but different adoption -> sample evidence preserves respective values.
        """
        app_data = copy.deepcopy(self.base_app)
        app_data["metadata"]["platforms"] = ["android", "ios"]
        app_data["version_health"] = [
            {
                "version": "1.0.10",
                "platform": "android",
                "release_date": None,
                "crash_events": 10,
                "affected_users": 2,
                "crash_free_users_rate": None,
                "crash_free_sessions_rate": None,
                "adoption_rate": None,
                "status": "active",
                "trend": "stable",
            },
            {
                "version": "1.0.10",
                "platform": "ios",
                "release_date": None,
                "crash_events": 5,
                "affected_users": 1,
                "crash_free_users_rate": None,
                "crash_free_sessions_rate": None,
                "adoption_rate": None,
                "status": "active",
                "trend": "stable",
            },
        ]

        # Sessions has Android 1.0.10 (adoption 0.35), iOS 1.0.10 (adoption 0.85), and iOS 2.1.0 (0 crashes, adoption 0.10)
        session_rows = [
            {"version": "1.0.10", "_platform": "android", "sessions_total": 3500, "crashed_sessions": 10, "users_total": 500, "crashed_users": 2},
            {"version": "1.0.9", "_platform": "android", "sessions_total": 6500, "crashed_sessions": 20, "users_total": 1000, "crashed_users": 5},
            {"version": "1.0.10", "_platform": "ios", "sessions_total": 8500, "crashed_sessions": 5, "users_total": 1200, "crashed_users": 1},
            {"version": "2.1.0", "_platform": "ios", "sessions_total": 1500, "crashed_sessions": 0, "users_total": 300, "crashed_users": 0},
        ]

        computed = compute_version_sessions(session_rows)

        # Verify computed metrics are separate for android:1.0.10 and ios:1.0.10
        self.assertAlmostEqual(computed["android:1.0.10"]["adoption_rate"], 0.35, places=2)
        self.assertAlmostEqual(computed["ios:1.0.10"]["adoption_rate"], 0.85, places=2)
        self.assertEqual(computed["ios:2.1.0"]["platform"], "ios")

        sessions_res = {
            "sources": {"status": "available"},
            "kpi": {"crash_free_users": {"rate": 0.99, "status": "available"}, "crash_free_sessions": {"rate": 0.999, "status": "available"}},
            "daily_trend": {},
            "version_health": computed,
        }

        enriched = enrich_app_dashboard_with_sessions(app_data, sessions_res)

        # Android 1.0.10 must get 0.35 adoption, iOS 1.0.10 must get 0.85 adoption
        v_and_10 = next((v for v in enriched["version_health"] if v.get("version") == "1.0.10" and v.get("platform") == "android"), None)
        v_ios_10 = next((v for v in enriched["version_health"] if v.get("version") == "1.0.10" and v.get("platform") == "ios"), None)
        self.assertIsNotNone(v_and_10)
        self.assertIsNotNone(v_ios_10)
        self.assertAlmostEqual(v_and_10["adoption_rate"], 0.35, places=2)
        self.assertAlmostEqual(v_ios_10["adoption_rate"], 0.85, places=2)

        # iOS 2.1.0 must be materialized with platform = 'ios', NOT 'android'!
        v_ios_21 = next((v for v in enriched["version_health"] if v.get("version") == "2.1.0"), None)
        self.assertIsNotNone(v_ios_21)
        self.assertEqual(v_ios_21["platform"], "ios")
        self.assertEqual(v_ios_21["status"], "latest")

        # Android 1.0.10 must still be marked latest for Android!
        self.assertEqual(v_and_10["status"], "latest")


if __name__ == "__main__":
    unittest.main()
