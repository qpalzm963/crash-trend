"""Comprehensive integration tests for Dashboard V2 pipeline orchestration.

Verifies the 6 key acceptance scenarios required by Issue #9:
1. Crashlytics + Sessions both available (complete KPI, crash-free rates, daily trend, version health)
2. Crashlytics available, Sessions unavailable (graceful degradation, strictly no 0%)
3. Gemini AI key missing / disabled (graceful degradation, deterministic Priority score)
4. MCP / issue detail fallback failure (graceful fallback without pipeline interruption)
5. Multi-app pipeline (at least 2 apps bundled into DashboardV2Bundle and selectable in UI)
6. Empty data period (0 events, empty tables, safe division and valid schema)
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root and crash_trend to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crash_trend"))

from crash_trend.analyze_gemini import enrich_app_data_with_priority_and_ai
from crash_trend.build_dashboard import (
    assemble_bundle_from_apps,
    build_html,
    collect_data,
    generate_dashboard,
)
from crash_trend.check_surge import weekly_totals_from_daily
from crash_trend.fetch_bigquery import transform_bq_to_v2
from crash_trend.fetch_issue_details import enrich_top_issues
from crash_trend.fetch_sessions import (
    build_crash_free_metric,
    build_unavailable_sessions_result,
    enrich_app_dashboard_with_sessions,
)
from crash_trend.schema_v2 import (
    validate_app_dashboard_v2,
    validate_dashboard_v2,
)


class TestPipelineIntegrationScenarios(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures_dir = ROOT / "tests" / "fixtures"
        self.fixture_v2_path = self.fixtures_dir / "dashboard_v2.json"
        self.fixture_no_sessions_path = self.fixtures_dir / "dashboard_v2_no_sessions.json"

    # -----------------------------------------------------------------------
    # Scenario 1: Crashlytics + Sessions both available
    # -----------------------------------------------------------------------
    def test_scenario_1_crashlytics_and_sessions_available(self) -> None:
        """Scenario 1: Verifies complete pipeline when both Crashlytics and Sessions data are available."""
        today = dt.datetime.now(dt.timezone.utc).date()
        d1 = (today - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        d2 = today.strftime("%Y-%m-%d")

        # 1. Simulate BigQuery raw fetch
        bq_raw = {
            "tables": {
                "com_example_shop_ANDROID": {
                    "overview": [{"total_events": 5000, "distinct_users": 1200, "fatal_events": 4000, "anr_events": 800, "non_fatal_events": 200}],
                    "daily_trend": [
                        {"date": d1, "events": 200, "users": 50, "fatal_events": 160, "anr_events": 30, "non_fatal_events": 10},
                        {"date": d2, "events": 250, "users": 60, "fatal_events": 200, "anr_events": 40, "non_fatal_events": 10},
                    ],
                    "top_issues": [
                        {
                            "issue_id": "issue_crash_1",
                            "issue_title": "NullPointerException: payment failed",
                            "issue_subtitle": "CheckoutActivity.kt:142",
                            "error_type": "FATAL",
                            "events": 3000,
                            "users": 900,
                            "first_seen_timestamp": "2026-08-01T00:00:00Z",
                            "last_seen_timestamp": "2026-09-02T00:00:00Z",
                            "first_seen_version": "1.0.0",
                            "last_seen_version": "1.2.0",
                        }
                    ],
                    "issue_versions": [{"issue_id": "issue_crash_1", "app_version": "1.2.0", "events": 3000, "users": 900}],
                    "new_issues": [{"new_issues_count": 1}],
                    "by_device": [{"device_model": "Pixel 8", "events": 3000, "users": 700}],
                    "by_os": [{"os_version": "Android 14", "events": 4500, "users": 1100}],
                    "by_app_version": [{"app_version": "1.2.0", "events": 5000, "users": 1200}],
                    "custom_keys": [],
                }
            }
        }
        app_cfg = {
            "app_id": "shop_app",
            "firebase_project": "shop-prod",
            "display_name": "E-Commerce Shop",
            "platforms": ["android"],
        }
        app_v2 = transform_bq_to_v2(bq_raw, app_cfg, days=30)

        # 2. Simulate Sessions fetch & enrichment
        sessions_result = {
            "sources": {
                "status": "available",
                "last_sync_timestamp": "2026-09-02T00:00:00Z",
                "error_message": None,
                "tables_queried": ["events_20260902"],
            },
            "kpi": {
                "crash_free_users": build_crash_free_metric(50000, 1200, previous_rate=1 - (1400 / 48000), status="available"),
                "crash_free_sessions": build_crash_free_metric(100000, 4000, previous_rate=1 - (5000 / 95000), status="available"),
            },
            "daily_trend": {},
            "version_health": {
                "1.2.0": {
                    "version": "1.2.0",
                    "sessions_total": 80000,
                    "users_total": 40000,
                    "crashed_sessions": 3000,
                    "crashed_users": 900,
                    "crash_free_users_rate": 1 - (900 / 40000),
                    "crash_free_sessions_rate": 1 - (3000 / 80000),
                    "adoption_rate": 0.8,
                }
            },
        }
        app_v2 = enrich_app_dashboard_with_sessions(app_v2, sessions_result)

        # 3. Simulate Issue details enrichment
        app_v2["top_issues"] = enrich_top_issues(app_v2["top_issues"], app_name="shop_app", days=30)

        # 4. Simulate Gemini analysis (mocked)
        mock_ai_resp = {
            "overview": "整體 Crash 改善，主要集中在支付模組。",
            "key_takeaways": ["NullPointerException 為主要致命問題"],
            "distribution_insights": "Android 14 佔比 90%",
            "recommended_actions": [{"issue_id": "issue_crash_1", "priority": "P0", "action": "修復空指標", "effort": "S"}],
            "items": [{"issue_id": "issue_crash_1", "root_cause": "空指標", "suggested_fix": "加上非空判斷", "effort": "S", "confidence": "high"}],
        }
        with patch("crash_trend.analyze_gemini.call_gemini", return_value=mock_ai_resp):
            app_v2 = enrich_app_data_with_priority_and_ai(app_v2, api_key="test-key", core_paths=["checkout", "payment"])

        # Validate Schema
        errors = validate_app_dashboard_v2(app_v2)
        self.assertEqual(errors, [])

        # Check KPIs
        self.assertEqual(app_v2["kpi"]["crash_free_users"]["status"], "available")
        self.assertAlmostEqual(app_v2["kpi"]["crash_free_users"]["rate"], 1 - (1200 / 50000), places=4)
        self.assertEqual(app_v2["kpi"]["crash_events"]["value"], 5000)
        self.assertEqual(app_v2["top_issues"][0]["priority"]["level"], "P0")

        # Build bundle & HTML
        bundle = {
            "schema_version": "2.0",
            "generated_at": "2026-09-02T00:00:00Z",
            "default_app": "shop_app",
            "apps": {"shop_app": app_v2},
        }
        bundle_errors = validate_dashboard_v2(bundle)
        self.assertEqual(bundle_errors, [])

        html = build_html(bundle)
        self.assertIn("shop_app", html)
        self.assertIn("Crash-free Users", html)
        self.assertIn("NullPointerException", html)

    # -----------------------------------------------------------------------
    # Scenario 2: Crashlytics available, Sessions unavailable (Graceful Degradation)
    # -----------------------------------------------------------------------
    def test_scenario_2_sessions_unavailable_graceful_degradation(self) -> None:
        """Scenario 2: Verifies graceful degradation when Sessions export is not configured (strictly no 0%)."""
        bq_raw = {
            "tables": {
                "com_example_legacy_ANDROID": {
                    "overview": [{"total_events": 100, "distinct_users": 20, "fatal_events": 100, "anr_events": 0, "non_fatal_events": 0}],
                    "daily_trend": [],
                    "top_issues": [],
                    "new_issues": [],
                    "issue_versions": [],
                    "by_device": [],
                    "by_os": [],
                    "by_app_version": [],
                    "custom_keys": [],
                }
            }
        }
        app_cfg = {
            "app_id": "legacy_app",
            "firebase_project": "legacy-proj",
            "display_name": "Legacy Project App",
            "platforms": ["android"],
        }
        app_v2 = transform_bq_to_v2(bq_raw, app_cfg, days=30)

        # Enrich with Sessions as unavailable
        app_v2 = enrich_app_dashboard_with_sessions(
            app_v2,
            build_unavailable_sessions_result("Firebase Sessions export table not found"),
        )

        errors = validate_app_dashboard_v2(app_v2)
        self.assertEqual(errors, [])

        cfu = app_v2["kpi"]["crash_free_users"]
        self.assertEqual(cfu["status"], "unavailable")
        self.assertIsNone(cfu["rate"])
        self.assertIsNone(cfu["total"])
        self.assertIsNone(cfu["crashed"])
        self.assertEqual(cfu["unavailable_reason"], "Firebase Sessions export table not found")

        bundle = {
            "schema_version": "2.0",
            "generated_at": "2026-09-02T00:00:00Z",
            "default_app": "legacy_app",
            "apps": {"legacy_app": app_v2},
        }
        html = build_html(bundle)
        self.assertIn("Unavailable", html)
        self.assertIn("Firebase Sessions export table not found", html)

    # -----------------------------------------------------------------------
    # Scenario 3: Gemini AI key missing / disabled (Graceful Degradation)
    # -----------------------------------------------------------------------
    def test_scenario_3_gemini_ai_disabled_graceful_degradation(self) -> None:
        """Scenario 3: Verifies deterministic Priority Score works without Gemini API key and AI section is marked disabled."""
        data = json.loads(self.fixture_v2_path.read_text(encoding="utf-8"))
        app_data = data["apps"]["shop_app"]

        # Run enrich_app_data_with_priority_and_ai with api_key=None
        analyzed = enrich_app_data_with_priority_and_ai(app_data, api_key=None, core_paths=["checkout"])

        errors = validate_app_dashboard_v2(analyzed)
        self.assertEqual(errors, [])

        # AI summary must be disabled
        self.assertEqual(analyzed["sources"]["gemini_ai"]["status"], "disabled")
        self.assertEqual(analyzed["ai_summary"]["status"], "disabled")
        self.assertEqual(analyzed["top_issues"][0]["ai_analysis"]["status"], "unavailable")

        # But Priority Score must be calculated deterministically!
        prio = analyzed["top_issues"][0]["priority"]
        self.assertIn(prio["level"], ["P0", "P1", "P2", "P3"])
        self.assertGreaterEqual(prio["score"], 0)
        self.assertLessEqual(prio["score"], 100)

    # -----------------------------------------------------------------------
    # Scenario 4: MCP / issue detail fallback failure (Graceful Degradation)
    # -----------------------------------------------------------------------
    def test_scenario_4_mcp_fallback_failure_graceful_degradation(self) -> None:
        """Scenario 4: Verifies pipeline completes even if MCP / detail query fails or returns no data."""
        issues = [
            {
                "issue_id": "unreachable_issue_99",
                "platform": "android",
                "title": "Crash in UnknownModule",
                "subtitle": "UnknownModule.kt:999",
                "error_type": "FATAL",
                "priority": {"score": 60, "level": "P1", "trend": "new", "score_breakdown": None},
                "events": 50,
                "affected_users": 40,
                "first_seen_timestamp": "2026-09-01T00:00:00Z",
                "last_seen_timestamp": "2026-09-02T00:00:00Z",
                "first_seen_version": "1.0",
                "last_seen_version": "1.0",
                "version_distribution": [],
                "blame_frame": None,
                "ai_analysis": {
                    "status": "unavailable",
                    "root_cause": None,
                    "suggested_fix": None,
                    "effort": None,
                    "confidence": None,
                    "reasoning_sources": None,
                },
                "detail": None,
            }
        ]

        # enrich_top_issues when BQ client and MCP cache are both unavailable
        enriched = enrich_top_issues(issues, app_name="test_app", bq_client=None)
        self.assertEqual(len(enriched), 1)
        # Subtitle heuristic blame frame fallback
        self.assertIsNotNone(enriched[0]["blame_frame"])
        self.assertEqual(enriched[0]["blame_frame"]["file"], "UnknownModule.kt")
        self.assertEqual(enriched[0]["blame_frame"]["line"], 999)
        self.assertIsNone(enriched[0]["detail"])

    # -----------------------------------------------------------------------
    # Scenario 5: Multi-app pipeline (at least 2 apps bundled)
    # -----------------------------------------------------------------------
    def test_scenario_5_multi_app_pipeline_assembly(self) -> None:
        """Scenario 5: Verifies multi-app assembly into DashboardV2Bundle and selector rendering."""
        data_v2 = json.loads(self.fixture_v2_path.read_text(encoding="utf-8"))
        shop_app = data_v2["apps"]["shop_app"]
        rider_app = data_v2["apps"]["rider_app"]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmproot = Path(tmpdir)
            out_dir = tmproot / "out"
            (out_dir / "shop_app").mkdir(parents=True, exist_ok=True)
            (out_dir / "rider_app").mkdir(parents=True, exist_ok=True)

            (out_dir / "shop_app" / "dashboard_v2.json").write_text(json.dumps(shop_app), encoding="utf-8")
            (out_dir / "rider_app" / "dashboard_v2.json").write_text(json.dumps(rider_app), encoding="utf-8")

            fake_cfg = {
                "apps": {
                    "shop_app": {"display_name": "E-Commerce Shop"},
                    "rider_app": {"display_name": "Delivery Rider"},
                }
            }

            with patch("crash_trend.build_dashboard.ROOT", tmproot):
                bundle = assemble_bundle_from_apps(fake_cfg)
                self.assertIsNotNone(bundle)
                self.assertEqual(bundle["schema_version"], "2.0")
                self.assertIn("shop_app", bundle["apps"])
                self.assertIn("rider_app", bundle["apps"])

                # Check HTML output
                html = build_html(bundle)
                self.assertIn("E-Commerce Shop", html)
                self.assertIn("Delivery Rider", html)
                self.assertIn("appSelector", html)
                self.assertIn("shop_app", html)
                self.assertIn("rider_app", html)

                # Check generated bundle file
                self.assertTrue((out_dir / "dashboard_v2.json").is_file())
                self.assertTrue((tmproot / "reports" / "dashboard_v2.json").is_file())

    # -----------------------------------------------------------------------
    # Scenario 6: Empty data period (0 events / 0 users)
    # -----------------------------------------------------------------------
    def test_scenario_6_empty_data_period(self) -> None:
        """Scenario 6: Verifies that an empty data period handles division safely and produces valid Schema V2."""
        empty_bq = {
            "tables": {
                "com_example_empty_ANDROID": {
                    "overview": [{"total_events": 0, "distinct_users": 0, "fatal_events": 0, "anr_events": 0, "non_fatal_events": 0}],
                    "daily_trend": [],
                    "top_issues": [],
                    "new_issues": [],
                    "issue_versions": [],
                    "by_device": [],
                    "by_os": [],
                    "by_app_version": [],
                    "custom_keys": [],
                }
            }
        }
        app_cfg = {
            "app_id": "empty_app",
            "firebase_project": "empty-proj",
            "display_name": "Empty App",
            "platforms": ["android"],
        }

        app_v2 = transform_bq_to_v2(empty_bq, app_cfg, days=30)
        app_v2 = enrich_app_dashboard_with_sessions(
            app_v2,
            build_unavailable_sessions_result("No sessions in empty period"),
        )
        app_v2 = enrich_app_data_with_priority_and_ai(app_v2, api_key=None)

        errors = validate_app_dashboard_v2(app_v2)
        self.assertEqual(errors, [])

        # Check zeroed KPIs
        self.assertEqual(app_v2["kpi"]["crash_events"]["value"], 0)
        self.assertEqual(app_v2["kpi"]["affected_users"]["value"], 0)
        self.assertEqual(app_v2["kpi"]["crash_free_users"]["status"], "unavailable")

        # Check weekly surge totals with empty daily trend
        totals = weekly_totals_from_daily(app_v2["daily_trend"])
        self.assertEqual(sum(totals.values()), 0)

    # -----------------------------------------------------------------------
    # Scenario 7: End-to-End Pipeline Execution via Filesystem
    # -----------------------------------------------------------------------
    def test_scenario_7_e2e_disk_pipeline_execution(self) -> None:
        """Scenario 7: Verifies end-to-end pipeline execution and file I/O chaining across all modules."""
        data_v2 = json.loads(self.fixture_v2_path.read_text(encoding="utf-8"))
        shop_app = data_v2["apps"]["shop_app"]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmproot = Path(tmpdir)
            out_shop = tmproot / "out" / "shop_app"
            out_shop.mkdir(parents=True, exist_ok=True)

            # Step 1: Base V2 data written (simulating fetch_bigquery)
            (out_shop / "dashboard_v2.json").write_text(json.dumps(shop_app, ensure_ascii=False), encoding="utf-8")

            # Step 2: Enrich with Sessions (simulating fetch_sessions)
            sess_mock = {
                "sources": {"status": "available", "last_sync_timestamp": "2026-09-02T00:00:00Z", "error_message": None},
                "kpi": {
                    "crash_free_users": build_crash_free_metric(50000, 100, previous_rate=0.995, status="available"),
                    "crash_free_sessions": build_crash_free_metric(100000, 200, previous_rate=0.995, status="available"),
                },
                "daily_trend": {},
                "version_health": {},
            }
            app_data = json.loads((out_shop / "dashboard_v2.json").read_text(encoding="utf-8"))
            app_data = enrich_app_dashboard_with_sessions(app_data, sess_mock)
            (out_shop / "dashboard_v2.json").write_text(json.dumps(app_data, ensure_ascii=False), encoding="utf-8")

            # Step 3: Enrich with Issue details (simulating fetch_issue_details)
            app_data = json.loads((out_shop / "dashboard_v2.json").read_text(encoding="utf-8"))
            app_data["top_issues"] = enrich_top_issues(app_data["top_issues"], app_name="shop_app", bq_client=None)
            (out_shop / "dashboard_v2.json").write_text(json.dumps(app_data, ensure_ascii=False), encoding="utf-8")

            # Step 4: Enrich with Priority & AI (simulating analyze_gemini)
            app_data = json.loads((out_shop / "dashboard_v2.json").read_text(encoding="utf-8"))
            app_data = enrich_app_data_with_priority_and_ai(app_data, api_key=None, core_paths=["checkout"])
            (out_shop / "dashboard_v2.json").write_text(json.dumps(app_data, ensure_ascii=False), encoding="utf-8")

            # Step 5: Bundle assembly & HTML generation (simulating build_dashboard)
            fake_cfg = {"apps": {"shop_app": {"display_name": "E-Commerce Shop"}}}
            with patch("crash_trend.build_dashboard.ROOT", tmproot):
                bundle = assemble_bundle_from_apps(fake_cfg)
                self.assertIsNotNone(bundle)
                errors = validate_dashboard_v2(bundle)
                self.assertEqual(errors, [])

                out_html = tmproot / "dashboard.html"
                generate_dashboard(bundle, output_path=out_html)
                self.assertTrue(out_html.is_file())
                html_text = out_html.read_text(encoding="utf-8")
                self.assertIn("E-Commerce Shop", html_text)
                self.assertIn("shop_app", html_text)
                self.assertIn("Crash-free Users", html_text)


if __name__ == "__main__":
    unittest.main()
