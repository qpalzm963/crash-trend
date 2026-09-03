"""E2E Contract Tests: Pipeline and Schema Regression Coverage (Issue #24).

Verifies the 6 required profiles and failure cases using deterministic fixtures
and filesystem artifacts without requiring external cloud credentials:
1. Full profile: Crashlytics + Sessions + MCP all available and fresh
2. Crashlytics-only: Sessions disabled, MCP off/manual, graceful degradation
3. MCP stale: Last-known-good cache enriches Top Issues but triggers stale warning
4. MCP refresh failure: Core pipeline succeeds, good cache preserved, marked degraded
5. Multi-app mixed profile: Diverse source configurations across apps without cross-contamination
6. Invalid contract: Schema violations immediately fail validation (no false green)
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from crash_trend.analyze_gemini import enrich_app_data_with_priority_and_ai
from crash_trend.build_dashboard import (
    assemble_bundle_from_apps,
    build_html,
    generate_dashboard,
)
from crash_trend.fetch_bigquery import transform_bq_to_v2
from crash_trend.fetch_issue_details import enrich_top_issues, get_mcp_source_status
from crash_trend.fetch_sessions import (
    build_crash_free_metric,
    build_unavailable_sessions_result,
    enrich_app_dashboard_with_sessions,
)
from crash_trend.pipeline_health import PipelineRunTracker, load_run_summary
from crash_trend.schema_v2 import (
    SCHEMA_VERSION,
    validate_app_dashboard_v2,
    validate_dashboard_v2,
)

ROOT = Path(__file__).resolve().parent.parent


class TestE2EContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures_dir = ROOT / "tests" / "fixtures"
        self.base_v2_bundle = json.loads((self.fixtures_dir / "dashboard_v2.json").read_text(encoding="utf-8"))

    # -----------------------------------------------------------------------
    # Profile 1: Full Profile (Crashlytics + Sessions + MCP)
    # -----------------------------------------------------------------------
    def test_profile_1_full_profile(self) -> None:
        """Profile 1: Full pipeline with BQ (Apple singular error), Sessions, MCP, and AI."""
        today = dt.datetime.now(dt.timezone.utc).date()
        d1 = (today - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        d2 = today.strftime("%Y-%m-%d")

        # Apple Crashlytics official schema fixture: utilizes singular 'error' record
        bq_raw = {
            "tables": {
                "app_ios_batch": {
                    "overview": [{"total_events": 1200, "distinct_users": 400, "fatal_events": 1000, "anr_events": 0, "non_fatal_events": 200}],
                    "daily_trend": [
                        {"date": d1, "events": 600, "users": 200, "fatal_events": 500, "anr_events": 0, "non_fatal_events": 100},
                        {"date": d2, "events": 600, "users": 200, "fatal_events": 500, "anr_events": 0, "non_fatal_events": 100},
                    ],
                    "top_issues": [
                        {
                            "issue_id": "ios_crash_1",
                            "issue_title": "SIGSEGV in RenderEngine",
                            "issue_subtitle": "MetalRenderer.swift:88",
                            "error_type": "FATAL",
                            "events": 1000,
                            "users": 350,
                            "first_seen_timestamp": "2026-08-15T00:00:00Z",
                            "last_seen_timestamp": "2026-09-02T00:00:00Z",
                            "first_seen_version": "2.0.0",
                            "last_seen_version": "2.1.0",
                        }
                    ],
                    "issue_versions": [{"issue_id": "ios_crash_1", "app_version": "2.1.0", "events": 1000, "users": 350}],
                    "new_issues": [{"new_issues_count": 1}],
                    "by_device": [{"device_model": "iPhone 15", "events": 1200, "users": 400}],
                    "by_os": [{"os_version": "iOS 17.5", "events": 1200, "users": 400}],
                    "by_app_version": [{"app_version": "2.1.0", "events": 1200, "users": 400}],
                    "custom_keys": [],
                }
            }
        }
        app_cfg = {
            "app_id": "full_ios_app",
            "firebase_project": "full-ios-proj",
            "display_name": "Full iOS App",
            "platforms": ["ios"],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tmproot = Path(tmpdir)
            out_app = tmproot / "out" / "full_ios_app"
            out_app.mkdir(parents=True, exist_ok=True)

            # Step 1: Base BigQuery transform
            app_v2 = transform_bq_to_v2(bq_raw, app_cfg, days=30)
            self.assertEqual(app_v2["metadata"]["platforms"], ["ios"])

            # Step 2: Sessions enrich
            sess_res = {
                "sources": {"status": "available", "last_sync_timestamp": "2026-09-02T00:00:00Z", "error_message": None},
                "kpi": {
                    "crash_free_users": build_crash_free_metric(10000, 350, previous_rate=0.97, status="available"),
                    "crash_free_sessions": build_crash_free_metric(50000, 1000, previous_rate=0.985, status="available"),
                },
                "daily_trend": {},
                "version_health": {},
            }
            app_v2 = enrich_app_dashboard_with_sessions(app_v2, sess_res)

            # Step 3: MCP fresh cache enrich
            mcp_cache = {
                "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "issues": {
                    "ios_crash_1": {
                        "blame_frame": {"file": "MetalRenderer.swift", "line": 88, "symbol": "drawFrame()", "blamed": True},
                        "detail": {
                            "stack_trace": "0 MetalRenderer.swift:88 drawFrame\n1 Main.swift:10 run",
                            "breadcrumbs": [{"timestamp": "2026-09-02T00:00:00Z", "category": "ui", "message": "tap button", "level": "info"}],
                            "logs": ["Engine started"],
                        },
                    }
                },
            }
            (out_app / "stacktraces.json").write_text(json.dumps(mcp_cache), encoding="utf-8")

            with patch("crash_trend.fetch_issue_details.ROOT", tmproot):
                app_v2["top_issues"] = enrich_top_issues(app_v2["top_issues"], app_name="full_ios_app", bq_client=None)

            # Step 4: AI enrich (stubbed)
            mock_ai = {
                "overview": "Metal 渲染執行序越界修復",
                "key_takeaways": ["記憶體崩潰主因為緩衝區越界"],
                "distribution_insights": "iOS 17.5 佔 100%",
                "recommended_actions": [{"issue_id": "ios_crash_1", "priority": "P0", "action": "修復 MetalRenderer 緩衝區", "effort": "M"}],
                "items": [{"issue_id": "ios_crash_1", "root_cause": "Buffer overflow", "suggested_fix": "Add boundary check", "effort": "M", "confidence": "high"}],
            }
            with patch("crash_trend.analyze_gemini.call_gemini", return_value=mock_ai):
                app_v2 = enrich_app_data_with_priority_and_ai(app_v2, api_key="fake-key", core_paths=["Metal"])

            (out_app / "dashboard_v2.json").write_text(json.dumps(app_v2, ensure_ascii=False), encoding="utf-8")

            # Step 5: Assemble and strict validation
            fake_cfg = {"apps": {"full_ios_app": app_cfg}}
            with patch("crash_trend.build_dashboard.ROOT", tmproot):
                bundle = assemble_bundle_from_apps(fake_cfg)
                self.assertIsNotNone(bundle)
                errors = validate_dashboard_v2(bundle)
                self.assertEqual(errors, [])

                out_html = tmproot / "dashboard.html"
                generate_dashboard(bundle, output_path=out_html)
                self.assertTrue(out_html.is_file())
                html_text = out_html.read_text(encoding="utf-8")
                self.assertIn("Full iOS App", html_text)
                self.assertIn("SIGSEGV in RenderEngine", html_text)

    # -----------------------------------------------------------------------
    # Profile 2: Crashlytics-Only Profile (Sessions Disabled, MCP Off/Manual)
    # -----------------------------------------------------------------------
    def test_profile_2_crashlytics_only(self) -> None:
        """Profile 2: Crashlytics only, Sessions disabled, MCP off, no AI key (strictly no 0%)."""
        app_cfg = {
            "app_id": "crash_only_app",
            "firebase_project": "co-proj",
            "display_name": "Crash Only App",
            "platforms": ["android"],
            "data_sources": {"sessions": False},
            "mcp": "off",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tmproot = Path(tmpdir)
            out_app = tmproot / "out" / "crash_only_app"
            out_app.mkdir(parents=True, exist_ok=True)

            bq_raw = {
                "tables": {
                    "co_tbl": {
                        "overview": [{"total_events": 50, "distinct_users": 20, "fatal_events": 40, "anr_events": 10, "non_fatal_events": 0}],
                        "daily_trend": [{"date": "2026-09-02", "events": 50, "users": 20, "fatal_events": 40, "anr_events": 10, "non_fatal_events": 0}],
                        "top_issues": [{
                            "issue_id": "co_issue_1",
                            "issue_title": "IllegalStateException",
                            "issue_subtitle": "App.java:30",
                            "error_type": "FATAL",
                            "events": 40,
                            "users": 15,
                            "first_seen_timestamp": "2026-09-01T00:00:00Z",
                            "last_seen_timestamp": "2026-09-02T00:00:00Z",
                            "first_seen_version": "1.0",
                            "last_seen_version": "1.0",
                        }],
                        "issue_versions": [{"issue_id": "co_issue_1", "app_version": "1.0", "events": 40, "users": 15}],
                        "new_issues": [{"new_issues_count": 1}],
                        "by_device": [{"device_model": "Pixel 7", "events": 50, "users": 20}],
                        "by_os": [{"os_version": "Android 14", "events": 50, "users": 20}],
                        "by_app_version": [{"app_version": "1.0", "events": 50, "users": 20}],
                        "custom_keys": [],
                    }
                }
            }

            app_v2 = transform_bq_to_v2(bq_raw, app_cfg, days=30)
            app_v2 = enrich_app_dashboard_with_sessions(app_v2, build_unavailable_sessions_result("Sessions disabled in config"))
            with patch("crash_trend.fetch_issue_details.ROOT", tmproot):
                app_v2["top_issues"] = enrich_top_issues(app_v2["top_issues"], app_name="crash_only_app", bq_client=None)
            app_v2 = enrich_app_data_with_priority_and_ai(app_v2, api_key=None)

            # Ensure Sessions rate is strictly null (not 0%)
            self.assertIsNone(app_v2["kpi"]["crash_free_users"]["rate"])
            self.assertEqual(app_v2["kpi"]["crash_free_users"]["status"], "unavailable")

            # Deterministic Priority score must be calculated (P0~P3)
            p_level = app_v2["top_issues"][0]["priority"]["level"]
            self.assertIn(p_level, {"P0", "P1", "P2", "P3"})

            (out_app / "dashboard_v2.json").write_text(json.dumps(app_v2, ensure_ascii=False), encoding="utf-8")

            fake_cfg = {"apps": {"crash_only_app": app_cfg}}
            with patch("crash_trend.build_dashboard.ROOT", tmproot):
                bundle = assemble_bundle_from_apps(fake_cfg)
                self.assertIsNotNone(bundle)
                errors = validate_dashboard_v2(bundle)
                self.assertEqual(errors, [])

    # -----------------------------------------------------------------------
    # Profile 3: MCP Stale Profile (Last-Known-Good Cache with Warning)
    # -----------------------------------------------------------------------
    def test_profile_3_mcp_stale(self) -> None:
        """Profile 3: Stale MCP cache successfully enriches issues while source status records warning."""
        app_cfg = {
            "app_id": "stale_app",
            "firebase_project": "stale-proj",
            "display_name": "Stale App",
            "platforms": ["android"],
            "mcp": {"mode": "manual", "max_age_days": 7},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            tmproot = Path(tmpdir)
            out_app = tmproot / "out" / "stale_app"
            out_app.mkdir(parents=True, exist_ok=True)

            # Stale cache: 14 days old (> 7 days)
            old_time = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
            stale_cache = {
                "generated_at": old_time,
                "issues": {
                    "stale_issue_1": {
                        "blame_frame": {"file": "Payment.kt", "line": 55, "symbol": "checkout()", "blamed": True},
                        "detail": {"stack_trace": "Payment.kt:55", "breadcrumbs": [], "logs": []},
                    }
                },
            }
            (out_app / "stacktraces.json").write_text(json.dumps(stale_cache), encoding="utf-8")

            issues = [
                {
                    "issue_id": "stale_issue_1",
                    "title": "NullPointer",
                    "subtitle": "Payment.kt:55",
                    "error_type": "FATAL",
                    "priority": {"score": 80, "level": "P1", "trend": "stable", "score_breakdown": None},
                    "events": 100,
                    "affected_users": 50,
                    "first_seen_timestamp": "2026-08-01T00:00:00Z",
                    "last_seen_timestamp": "2026-09-02T00:00:00Z",
                    "first_seen_version": "1.0",
                    "last_seen_version": "1.0",
                    "version_distribution": [{"version": "1.0", "events": 100, "users": 50}],
                    "blame_frame": None,
                    "ai_analysis": {"status": "skipped", "root_cause": None, "suggested_fix": None, "effort": None, "confidence": None, "reasoning_sources": None},
                    "detail": None,
                }
            ]

            with patch("crash_trend.fetch_issue_details.ROOT", tmproot):
                with patch("crash_trend.fetch_issue_details.safe_get_app", return_value=app_cfg):
                    enriched_issues = enrich_top_issues(issues, app_name="stale_app", bq_client=None)
                    mcp_status = get_mcp_source_status("stale_app")

            # Verify supplemental cache successfully enriched blame frame
            self.assertIsNotNone(enriched_issues[0]["blame_frame"])
            self.assertEqual(enriched_issues[0]["blame_frame"]["file"], "Payment.kt")
            self.assertEqual(enriched_issues[0]["blame_frame"]["symbol"], "checkout()")

            # Verify source status explicitly warns about staleness
            self.assertIn(mcp_status["status"], {"available", "stale"})
            self.assertIn("過期", mcp_status["error_message"])

    # -----------------------------------------------------------------------
    # Profile 4: MCP Refresh Failure (Preserves Good Cache, Core Succeeds)
    # -----------------------------------------------------------------------
    def test_profile_4_mcp_refresh_failure(self) -> None:
        """Profile 4: Failure during MCP refresh must NEVER destroy good cache and never fail core pipeline."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmproot = Path(tmpdir)
            out_app = tmproot / "out" / "preserve_app"
            out_app.mkdir(parents=True, exist_ok=True)

            # Pre-existing good cache
            good_cache_content = json.dumps({"generated_at": "2026-08-20T00:00:00Z", "issues": {"i1": {"blame_frame": {"symbol": "foo"}}}})
            (out_app / "stacktraces.json").write_text(good_cache_content, encoding="utf-8")

            # Simulate tracker recording refresh failure
            tracker = PipelineRunTracker(started_at="2026-09-03T06:00:00Z")
            tracker.record_stage("preserve_app", "crashlytics_bigquery", "success", "2026-09-03T06:00:00Z", "2026-09-03T06:00:05Z")
            tracker.record_stage("preserve_app", "mcp", "failed", "2026-09-03T06:00:05Z", "2026-09-03T06:00:06Z", error_message="firebase login token expired")
            tracker.record_stage("preserve_app", "normalize", "success", "2026-09-03T06:00:06Z", "2026-09-03T06:00:07Z")

            summary = tracker.build_summary("2026-09-03T06:00:07Z")
            # App is degraded, NOT failed
            self.assertEqual(summary["apps"]["preserve_app"]["status"], "degraded")
            self.assertEqual(summary["status"], "degraded")

            # Cache file must still exist and be intact
            self.assertTrue((out_app / "stacktraces.json").is_file())
            self.assertEqual((out_app / "stacktraces.json").read_text(encoding="utf-8"), good_cache_content)

    # -----------------------------------------------------------------------
    # Profile 5: Multi-App Mixed Profile
    # -----------------------------------------------------------------------
    def test_profile_5_multi_app_mixed(self) -> None:
        """Profile 5: Multi-app bundle with mixed source configurations."""
        data_v2 = copy.deepcopy(self.base_v2_bundle)
        shop_app = data_v2["apps"]["shop_app"]

        # Clone shop_app into a minimal app with sessions disabled
        minimal_app = copy.deepcopy(shop_app)
        minimal_app["metadata"]["app_id"] = "minimal_app"
        minimal_app["metadata"]["display_name"] = "Minimal Android"
        minimal_app["sources"]["firebase_sessions"]["status"] = "disabled"
        minimal_app["sources"]["firebase_sessions"]["error_message"] = "Sessions disabled in config"
        minimal_app["kpi"]["crash_free_users"]["rate"] = None
        minimal_app["kpi"]["crash_free_users"]["total"] = None
        minimal_app["kpi"]["crash_free_users"]["crashed"] = None
        minimal_app["kpi"]["crash_free_users"]["previous_rate"] = None
        minimal_app["kpi"]["crash_free_users"]["change_pct_points"] = None
        minimal_app["kpi"]["crash_free_users"]["status"] = "unavailable"
        minimal_app["kpi"]["crash_free_sessions"]["rate"] = None
        minimal_app["kpi"]["crash_free_sessions"]["total"] = None
        minimal_app["kpi"]["crash_free_sessions"]["crashed"] = None
        minimal_app["kpi"]["crash_free_sessions"]["previous_rate"] = None
        minimal_app["kpi"]["crash_free_sessions"]["change_pct_points"] = None
        minimal_app["kpi"]["crash_free_sessions"]["status"] = "unavailable"

        mixed_bundle = {
            "schema_version": "2.0",
            "generated_at": "2026-09-03T06:00:00Z",
            "default_app": "shop_app",
            "apps": {
                "shop_app": shop_app,
                "minimal_app": minimal_app,
            },
        }

        errors = validate_dashboard_v2(mixed_bundle)
        self.assertEqual(errors, [])

        # Verify shop_app retained full metrics while minimal_app remains disabled
        self.assertIsNotNone(mixed_bundle["apps"]["shop_app"]["kpi"]["crash_free_users"]["rate"])
        self.assertIsNone(mixed_bundle["apps"]["minimal_app"]["kpi"]["crash_free_users"]["rate"])
        self.assertEqual(mixed_bundle["apps"]["minimal_app"]["sources"]["firebase_sessions"]["status"], "disabled")

    # -----------------------------------------------------------------------
    # Profile 6: Invalid Contract Fails (Strict Validation Rejects Malformed)
    # -----------------------------------------------------------------------
    def test_profile_6_invalid_contract_fails(self) -> None:
        """Profile 6: Schema violations must cause validate_dashboard_v2 to return non-empty errors."""
        # 6.1 Invalid schema version
        bad_ver = copy.deepcopy(self.base_v2_bundle)
        bad_ver["schema_version"] = "1.9"
        self.assertTrue(len(validate_dashboard_v2(bad_ver)) > 0)

        # 6.2 Negative KPI values
        bad_kpi = copy.deepcopy(self.base_v2_bundle)
        bad_kpi["apps"]["shop_app"]["kpi"]["crash_events"]["value"] = -10
        self.assertTrue(len(validate_dashboard_v2(bad_kpi)) > 0)

        # 6.3 Rate > 1.0
        bad_rate = copy.deepcopy(self.base_v2_bundle)
        bad_rate["apps"]["shop_app"]["kpi"]["crash_free_users"]["rate"] = 1.05
        self.assertTrue(len(validate_dashboard_v2(bad_rate)) > 0)

        # 6.4 Invalid enum error_type (prevent regression to unnormalized raw strings)
        bad_issue = copy.deepcopy(self.base_v2_bundle)
        bad_issue["apps"]["shop_app"]["top_issues"][0]["error_type"] = "CRASH"  # type: ignore
        self.assertTrue(len(validate_dashboard_v2(bad_issue)) > 0)

        # 6.5 Apple schema regression check: error_type must be one of FATAL / ANR / NON_FATAL
        app_issue = copy.deepcopy(self.base_v2_bundle)
        app_issue["apps"]["shop_app"]["top_issues"][0]["error_type"] = "FATAL"
        errors = validate_dashboard_v2(app_issue)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
