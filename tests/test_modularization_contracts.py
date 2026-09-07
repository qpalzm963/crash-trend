"""Modularization and Backward Compatibility Contract Tests (Issue #53).

Validates:
1. Public API symbol parity between legacy entrypoints and new packages:
   - `crash_trend.lifecycle` <=> `crash_trend.catalog`
   - `crash_trend.build_dashboard` <=> `crash_trend.dashboard`
2. Direct CLI script execution parity:
   - `python crash_trend/lifecycle.py --help`
   - `python crash_trend/build_dashboard.py --help`
3. Package module CLI execution parity:
   - `python -m crash_trend.lifecycle --help`
   - `python -m crash_trend.build_dashboard --help`
   - `python -m crash_trend.catalog --help`
   - `python -m crash_trend.dashboard --help`
4. Clean imports without circular dependency deadlocks or reverse coupling.
5. Strict fallback sources contract (gemini_ai and ai present, error_message is None).
6. Unidirectional dependency injection: legacy shim passes ROOT to modular renderer
   without renderer inspecting sys.modules.
7. HTML rendering structural contract and pre-refactor parity on key markers.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import crash_trend.build_dashboard as legacy_dashboard
import crash_trend.catalog as catalog_pkg
import crash_trend.dashboard as dashboard_pkg
import crash_trend.dashboard.renderer as dashboard_renderer
import crash_trend.lifecycle as legacy_lifecycle


class TestModularizationContracts(unittest.TestCase):
    """Verifies that modularization maintains strict parity and zero breaking changes."""

    def test_lifecycle_and_catalog_export_parity(self):
        """All symbols exported by legacy crash_trend.lifecycle must exist in crash_trend.catalog."""
        legacy_symbols = set(legacy_lifecycle.__all__)
        catalog_symbols = set(catalog_pkg.__all__)

        missing_in_catalog = legacy_symbols - catalog_symbols
        self.assertEqual(
            missing_in_catalog,
            set(),
            f"crash_trend.catalog is missing symbols exported by lifecycle.py: {missing_in_catalog}",
        )

        for sym in legacy_symbols:
            self.assertTrue(
                hasattr(legacy_lifecycle, sym),
                f"lifecycle.py missing promised symbol: {sym}",
            )
            self.assertTrue(
                hasattr(catalog_pkg, sym),
                f"catalog package missing promised symbol: {sym}",
            )

    def test_dashboard_export_parity(self):
        """All symbols exported by legacy crash_trend.build_dashboard must exist in crash_trend.dashboard."""
        legacy_symbols = set(legacy_dashboard.__all__)
        dashboard_symbols = set(dashboard_pkg.__all__)

        missing_in_pkg = legacy_symbols - dashboard_symbols
        self.assertEqual(
            missing_in_pkg,
            set(),
            f"crash_trend.dashboard is missing symbols exported by build_dashboard.py: {missing_in_pkg}",
        )

        for sym in legacy_symbols:
            self.assertTrue(
                hasattr(legacy_dashboard, sym),
                f"build_dashboard.py missing promised symbol: {sym}",
            )
            self.assertTrue(
                hasattr(dashboard_pkg, sym),
                f"dashboard package missing promised symbol: {sym}",
            )
            legacy_obj = getattr(legacy_dashboard, sym)
            dashboard_obj = getattr(dashboard_pkg, sym)
            # Verify callables have compatible signatures rather than requiring identical object identity
            if callable(legacy_obj) and callable(dashboard_obj):
                leg_params = set(inspect.signature(legacy_obj).parameters.keys())
                new_params = set(inspect.signature(dashboard_obj).parameters.keys())
                self.assertTrue(
                    leg_params.issubset(new_params) or new_params.issubset(leg_params),
                    f"Signature mismatch for {sym}: legacy={leg_params}, new={new_params}",
                )

    def test_fallback_sources_contract(self):
        """Regression test for Review Issue 1: Fallback bundle must preserve exact sources contract.

        Specifically:
        - sources must contain both 'gemini_ai' and 'ai'
        - gemini_ai.error_message must be None (not 'AI analysis disabled')
        - ai.error_message must be None
        - status must be 'disabled'
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            empty_root = Path(tmp_dir)
            bundle = dashboard_renderer.collect_data(root_dir=empty_root)
            self.assertIn("apps", bundle)
            self.assertIn("default_app", bundle["apps"])

            app = bundle["apps"]["default_app"]
            self.assertIn("sources", app)
            sources = app["sources"]

            # Must contain both gemini_ai and ai
            self.assertIn("gemini_ai", sources, "Fallback sources must contain gemini_ai")
            self.assertIn("ai", sources, "Fallback sources must contain ai")

            gemini_ai = sources["gemini_ai"]
            self.assertEqual(gemini_ai["status"], "disabled")
            self.assertIsNone(gemini_ai["error_message"], "gemini_ai error_message must be None")
            self.assertIsNone(gemini_ai["last_sync_timestamp"])

            ai = sources["ai"]
            self.assertEqual(ai["status"], "disabled")
            self.assertEqual(ai["provider"], "gemini")
            self.assertIsNone(ai["error_message"], "ai error_message must be None")
            self.assertIsNone(ai["model"])

    def test_no_reverse_dependency_from_renderer_to_shim(self):
        """Review Issue 2: Renderer must not inspect or import crash_trend.build_dashboard."""
        renderer_source = Path(dashboard_renderer.__file__).read_text(encoding="utf-8")
        self.assertNotIn(
            "crash_trend.build_dashboard",
            renderer_source,
            "Renderer implementation must have no awareness of legacy build_dashboard module",
        )
        self.assertNotIn(
            "_get_root",
            renderer_source,
            "Renderer must not use runtime sys.modules monkeypatch inspection",
        )

    def test_legacy_shim_delegates_with_root_injection(self):
        """Legacy shim passing its patched ROOT must affect output without reverse coupling."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            fixture_path = Path(__file__).parent / "fixtures" / "dashboard_v2.json"
            custom_data = json.loads(fixture_path.read_text(encoding="utf-8"))
            custom_data["apps"]["shop_app"]["metadata"]["display_name"] = "Patched App via Shim"

            # Write custom bundle in temp_root/out/dashboard_v2.json
            out_dir = temp_root / "out"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "dashboard_v2.json").write_text(json.dumps(custom_data), encoding="utf-8")

            # Patch ROOT on legacy shim
            with mock.patch.object(legacy_dashboard, "ROOT", temp_root):
                collected = legacy_dashboard.collect_data()
                self.assertIn("shop_app", collected.get("apps", {}))

                # Generate dashboard via legacy shim without explicit output path
                generated_path = legacy_dashboard.generate_dashboard(collected)
                self.assertEqual(generated_path, temp_root / "dashboard.html")
                self.assertTrue(generated_path.is_file())
                html_content = generated_path.read_text(encoding="utf-8")
                self.assertIn("Patched App via Shim", html_content)

    def test_no_circular_imports_in_isolated_process(self):
        """Importing modules in fresh Python processes must succeed without cycle or attribute errors."""
        test_scripts = [
            "import crash_trend.catalog",
            "import crash_trend.dashboard",
            "import crash_trend.lifecycle",
            "import crash_trend.build_dashboard",
            "import crash_trend.catalog; import crash_trend.dashboard",
            "import crash_trend.dashboard; import crash_trend.catalog",
            "from crash_trend.build_dashboard import build_html, generate_dashboard, ROOT",
            "from crash_trend.lifecycle import IssueHistoricalCatalog, detect_issue_lifecycle",
        ]

        for script in test_scripts:
            with self.subTest(script=script):
                res = subprocess.run(
                    [sys.executable, "-c", script],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    res.returncode,
                    0,
                    f"Script '{script}' failed with stderr:\n{res.stderr}",
                )

    def test_cli_direct_script_invocations(self):
        """Direct execution of python <path_to_script>.py --help must succeed with code 0."""
        repo_root = Path(__file__).resolve().parent.parent

        scripts = [
            repo_root / "crash_trend" / "lifecycle.py",
            repo_root / "crash_trend" / "build_dashboard.py",
        ]

        for script_path in scripts:
            with self.subTest(script=script_path.name):
                res = subprocess.run(
                    [sys.executable, str(script_path), "--help"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    res.returncode,
                    0,
                    f"{script_path.name} direct execution failed with:\n{res.stderr}",
                )
                self.assertIn("options:", res.stdout)

    def test_cli_module_invocations(self):
        """Package execution with python -m <module> --help must succeed with code 0."""
        modules = [
            "crash_trend.lifecycle",
            "crash_trend.build_dashboard",
            "crash_trend.catalog",
            "crash_trend.dashboard",
        ]

        for mod in modules:
            with self.subTest(module=mod):
                res = subprocess.run(
                    [sys.executable, "-m", mod, "--help"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    res.returncode,
                    0,
                    f"python -m {mod} --help failed with:\n{res.stderr}",
                )
                self.assertIn("options:", res.stdout)

    def test_rendering_structural_parity_and_key_contracts(self):
        """Validates that rendered HTML preserves all required sections, IDs, and client functions."""
        sample_bundle = {
            "schema_version": "2.0",
            "generated_at": "2026-03-31T12:00:00Z",
            "default_app": "demo_app",
            "apps": {
                "demo_app": {
                    "metadata": {
                        "app_id": "demo_app",
                        "display_name": "Demo App",
                        "platforms": ["android", "ios"],
                    },
                    "period": {"days": 30, "start_time": "2026-03-01", "end_time": "2026-03-31"},
                    "kpi": {
                        "crash_events": {"value": 150},
                        "affected_users": {"value": 75},
                        "crash_free_users": {"status": "available", "rate": 0.992},
                    },
                    "daily_trend": [],
                    "version_health": [],
                    "distributions": {},
                    "top_issues": [],
                }
            },
        }

        html_legacy = legacy_dashboard.build_html(sample_bundle)
        html_modular = dashboard_pkg.build_html(sample_bundle)

        # Both legacy shim and modular package must render identical HTML output
        self.assertEqual(html_legacy, html_modular)

        # 1. Structural View Containers
        required_views = [
            'id="view-overview"',
            'id="view-issues"',
            'id="view-version_health"',
            'id="view-devices"',
            'id="view-releases"',
            'id="view-notifications"',
            'id="view-ai_insights"',
            'id="view-settings"',
        ]
        for v in required_views:
            self.assertIn(v, html_modular, f"Missing required view container: {v}")

        # 2. Key Components & Cards
        required_elements = [
            'id="sidebar"',
            'id="appSelector"',
            'id="cardCrashFreeUsers"',
            'id="cardCrashEvents"',
            'id="cardAffectedUsers"',
            'id="cardNewIssues"',
            'id="overviewDataSourcesGrid"',
            'id="pipelineCardsGrid"',
            'id="aiPolicyCard"',
            'id="aiObservabilityCard"',
            'id="settingsTableBody"',
            'id="topIssuesPreviewBody"',
            'id="issuesListContainer"',
            'id="filterVersion"',
            'id="filterPlatform"',
        ]
        for el in required_elements:
            self.assertIn(el, html_modular, f"Missing required element: {el}")

        # 3. Essential Client Functions in Script
        required_js_functions = [
            "function renderAll()",
            "function renderHeader()",
            "function renderDataSourcesHealth()",
            "function renderKPIs()",
            "function renderAISummaries()",
            "function renderCharts()",
            "function renderOverviewTopIssuesPreview()",
            "function updateVersionFilterOptions(",
            "function renderIssuesList()",
            "function renderVersionHealth()",
            "function renderDevicesTable()",
            "function renderReleasesTable()",
            "function renderPipelines()",
            "function renderSettings()",
            "function handlePaidModelToggle(",
            "function saveAiPolicyFromUI()",
        ]
        for fn in required_js_functions:
            self.assertIn(fn, html_modular, f"Missing essential client function: {fn}")


if __name__ == "__main__":
    unittest.main()
