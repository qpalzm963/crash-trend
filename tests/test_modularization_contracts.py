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
4. Clean imports without circular dependency deadlocks.
5. Functional rendering parity between legacy shim and modular package.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

import crash_trend.build_dashboard as legacy_dashboard
import crash_trend.catalog as catalog_pkg
import crash_trend.dashboard as dashboard_pkg
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
            # Verify they resolve to the exact same underlying object
            legacy_obj = getattr(legacy_lifecycle, sym)
            catalog_obj = getattr(catalog_pkg, sym)
            self.assertIs(
                legacy_obj,
                catalog_obj,
                f"Symbol {sym} does not resolve to the same underlying entity",
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
            # ROOT might be rebound during tests, but functions/classes must match
            if callable(legacy_obj):
                self.assertIs(
                    legacy_obj,
                    dashboard_obj,
                    f"Callable {sym} does not resolve to the same function",
                )

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

    def test_rendering_functional_parity(self):
        """Rendering HTML via legacy build_dashboard vs new dashboard package must produce identical output."""
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

        self.assertEqual(html_legacy, html_modular)
        self.assertIn("<!DOCTYPE html>", html_modular)
        self.assertIn("Demo App", html_modular)
        self.assertIn("0.992", html_modular)


if __name__ == "__main__":
    unittest.main()
