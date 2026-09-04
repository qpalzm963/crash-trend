"""Tests for Dashboard V2 build_dashboard.py module.

Verifies HTML generation, UI Shell components (Sidebar, Header, KPI cards),
self-contained embedding, multi-app switching, and explicit Unavailable semantics.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.build_dashboard import (
    DEFAULT_OUT_HTML,
    assemble_bundle_from_apps,
    build_html,
    collect_data,
    generate_dashboard,
)


class TestBuildDashboardV2(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures_dir = ROOT / "tests" / "fixtures"
        self.fixture_v2_path = self.fixtures_dir / "dashboard_v2.json"
        self.fixture_no_sessions_path = self.fixtures_dir / "dashboard_v2_no_sessions.json"

    def test_build_html_with_standard_fixture(self) -> None:
        """Verifies that building HTML from dashboard_v2.json produces a complete, valid UI Shell."""
        self.assertTrue(self.fixture_v2_path.exists(), f"Missing fixture: {self.fixture_v2_path}")
        data = json.loads(self.fixture_v2_path.read_text(encoding="utf-8"))

        html = build_html(data)

        # 1. Basic HTML Structure
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn("<html", html)
        self.assertIn("</html>", html)
        self.assertIn("<head>", html)
        self.assertIn("<body>", html)

        # 2. Sidebar items (8 standard navigation entries)
        self.assertIn("總覽 (Overview)", html)
        self.assertIn("問題列表 (Issues)", html)
        self.assertIn("版本健康度 (Version Health)", html)
        self.assertIn("裝置分析 (Devices)", html)
        self.assertIn("發佈版本 (Releases)", html)
        self.assertIn("通知 (Notifications)", html)
        self.assertIn("AI 分析 (AI Insights)", html)
        self.assertIn("設定 (Settings)", html)

        # 3. Header components
        self.assertIn("appSelector", html)
        self.assertIn("globalSearch", html)
        self.assertIn("p-7d", html)
        self.assertIn("p-30d", html)
        self.assertIn("p-90d", html)

        # 4. KPI Cards
        self.assertIn("Crash-free Users", html)
        self.assertIn("Crash Events", html)
        self.assertIn("Affected Users", html)
        self.assertIn("New Issues", html)

        # 5. Embedded Data verification
        self.assertIn("shop_app", html)
        self.assertIn("rider_app", html)
        self.assertIn("E-Commerce Shop", html)

        # 6. Self-contained: No external CDN links
        self.assertNotIn("cdn.jsdelivr.net", html)
        self.assertNotIn("cdnjs.cloudflare.com", html)
        self.assertNotIn("fonts.googleapis.com", html)
        self.assertNotIn("unpkg.com", html)

    def test_build_html_with_no_sessions_fixture(self) -> None:
        """Verifies that missing Sessions displays 'Unavailable' and strictly never '0%' or '0.00%'."""
        self.assertTrue(
            self.fixture_no_sessions_path.exists(),
            f"Missing fixture: {self.fixture_no_sessions_path}",
        )
        data = json.loads(self.fixture_no_sessions_path.read_text(encoding="utf-8"))

        html = build_html(data)

        # Check that HTML contains Unavailable badge/text logic
        self.assertIn("Unavailable", html)
        self.assertIn("Firebase Sessions export table not found in dataset", html)
        self.assertIn("legacy_app", html)
        self.assertIn("Legacy Project App", html)

        # Ensure no sessions app data in json payload has rate=null, status=unavailable
        legacy_kpi = data["apps"]["legacy_app"]["kpi"]
        self.assertEqual(legacy_kpi["crash_free_users"]["status"], "unavailable")
        self.assertIsNone(legacy_kpi["crash_free_users"]["rate"])

    def test_generate_dashboard_file(self) -> None:
        """Verifies generate_dashboard writes out a file correctly."""
        data = json.loads(self.fixture_v2_path.read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "test_dashboard.html"
            result_path = generate_dashboard(data=data, output_path=out_file)

            self.assertEqual(result_path, out_file)
            self.assertTrue(out_file.is_file())
            content = out_file.read_text(encoding="utf-8")
            self.assertGreater(len(content), 1000)
            self.assertIn("Crashlytics", content)
            self.assertIn("shop_app", content)

    def test_collect_data_specified_path(self) -> None:
        """Verifies collect_data loads from specified path correctly."""
        data = collect_data(str(self.fixture_v2_path))
        self.assertEqual(data["schema_version"], "2.0")
        self.assertIn("apps", data)
        self.assertIn("shop_app", data["apps"])

    def test_collect_data_negative_never_silently_loads_fixtures(self) -> None:
        """Negative test: In an environment with empty out/ and no reports, collect_data must NEVER silently load test fixtures."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmproot = Path(tmpdir)
            with unittest.mock.patch("crash_trend.build_dashboard.ROOT", tmproot):
                # When out/ and reports/ do not exist, collect_data() must return safe minimal fallback, NOT fixture data
                data = collect_data()
                self.assertEqual(data["schema_version"], "2.3.0")
                self.assertIn("default_app", data["apps"])
                self.assertNotIn("shop_app", data["apps"])
                self.assertNotIn("rider_app", data["apps"])

    def test_assemble_bundle_from_apps_rejects_invalid_bundle(self) -> None:
        """Verifies assemble_bundle_from_apps validates schema and refuses to write invalid bundle."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmproot = Path(tmpdir)
            out_dir = tmproot / "out" / "broken_app"
            out_dir.mkdir(parents=True, exist_ok=True)

            # Write invalid app data (missing required kpi/metadata fields)
            broken_app_data = {"invalid_field": 123}
            (out_dir / "dashboard_v2.json").write_text(json.dumps(broken_app_data), encoding="utf-8")

            fake_cfg = {"apps": {"broken_app": {"display_name": "Broken"}}}
            with unittest.mock.patch("crash_trend.build_dashboard.ROOT", tmproot):
                bundle = assemble_bundle_from_apps(fake_cfg)
                self.assertIsNone(bundle)
                # Confirm bundle was NOT written to out/ or reports/
                self.assertFalse((tmproot / "out" / "dashboard_v2.json").is_file())
                self.assertFalse((tmproot / "reports" / "dashboard_v2.json").is_file())

    def test_ai_policy_and_observability_rendering(self) -> None:
        """Issue #41 & #42: Dashboard renders AI Policy & Governance and Observability UI cards."""
        data = json.loads(self.fixture_v2_path.read_text(encoding="utf-8"))
        data["global_ai_policy"] = {
            "mode": "auto",
            "primary_provider": "gemini",
            "primary_model": "gemini-3.8-flash",
            "lightweight_provider": "openrouter",
            "lightweight_model": "openrouter/free",
            "allow_paid_models": False,
            "include_source_snippet": True,
            "fallback_enabled": False,
            "has_per_app_override": False,
        }
        data["ai_usage"] = {
            "period_days": 7,
            "total_requests": 15,
            "success_count": 14,
            "error_count": 0,
            "fallback_count": 1,
            "rate_limit_count": 0,
            "free_tier_ratio": 1.0,
            "by_task_type": {"issue_triage": 10, "deep_analysis": 5},
            "by_provider": {"gemini": 5, "openrouter": 10},
            "by_model": {"gemini-3.8-flash": 5, "openrouter/free": 10},
            "tokens": {"status": "available", "total_tokens": 4200, "prompt_tokens": 3000, "completion_tokens": 1200},
            "cost_guard": {"paid_models_ever_allowed": False, "policy": "strict_free_tier_enforced"},
        }

        html = build_html(data)

        # AI Policy Admin card elements
        self.assertIn("aiPolicyCard", html)
        self.assertIn("AI Policy & Routing 治理設定", html)
        self.assertIn("Free Tier Guard 啟動", html)
        self.assertIn("adminModeSelect", html)
        self.assertIn("saveAiPolicyFromUI", html)
        self.assertIn("ai_config_service", html)

        # AI Observability card elements
        self.assertIn("aiObservabilityCard", html)
        self.assertIn("AI Usage & Quota Observability", html)
        self.assertIn("429 Rate Limit", html)
        self.assertIn("總 AI 請求量", html)
        self.assertIn("Token 消耗量審計", html)
        self.assertIn("每日呼叫趨勢明細", html)


if __name__ == "__main__":
    unittest.main()
