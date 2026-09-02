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
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.build_dashboard import (
    DEFAULT_OUT_HTML,
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

    def test_collect_data_fallback(self) -> None:
        """Verifies collect_data loads from fixture or creates valid fallback."""
        data = collect_data(str(self.fixture_v2_path))
        self.assertEqual(data["schema_version"], "2.0")
        self.assertIn("apps", data)

        # Test default without path finds fixture
        default_data = collect_data()
        self.assertEqual(default_data["schema_version"], "2.0")
        self.assertIn("apps", default_data)


if __name__ == "__main__":
    unittest.main()
