"""Tests for Data Source Profile (Crashlytics-only vs Sessions Optional) - Issue #17."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from crash_trend.config import get_data_sources, is_sessions_enabled
from crash_trend.fetch_sessions import (
    enrich_app_dashboard_with_sessions,
    fetch_sessions_for_app,
)
from crash_trend.schema_v2 import validate_dashboard_v2


class TestDataSourceProfile(unittest.TestCase):
    """Verifies data source profile resolution and Sessions-optional behavior."""

    def test_get_data_sources_defaults_and_explicit_flags(self) -> None:
        # 1. Defaults when app_cfg has no sessions_dataset
        cfg1 = {"firebase_project": "proj-1"}
        ds1 = get_data_sources(cfg1)
        self.assertTrue(ds1["crashlytics_bigquery"])
        self.assertFalse(ds1["sessions"])
        self.assertEqual(ds1["mcp"], "optional")
        self.assertFalse(is_sessions_enabled(cfg1))

        # 2. Explicit data_sources.sessions = false
        cfg2 = {
            "firebase_project": "proj-2",
            "sessions_dataset": "firebase_sessions",
            "data_sources": {"sessions": False},
        }
        ds2 = get_data_sources(cfg2)
        self.assertFalse(ds2["sessions"])
        self.assertFalse(is_sessions_enabled(cfg2))

        # 3. Explicit data_sources.sessions = true
        cfg3 = {
            "firebase_project": "proj-3",
            "data_sources": {"sessions": True},
        }
        ds3 = get_data_sources(cfg3)
        self.assertTrue(ds3["sessions"])
        self.assertTrue(is_sessions_enabled(cfg3))

        # 4. Shorthand top-level sessions: false
        cfg4 = {"firebase_project": "proj-4", "sessions": False}
        self.assertFalse(is_sessions_enabled(cfg4))

        # 5. Shorthand top-level sessions_enabled: false
        cfg5 = {"firebase_project": "proj-5", "sessions_enabled": False}
        self.assertFalse(is_sessions_enabled(cfg5))

        # 6. sessions_dataset present without explicit disable defaults to True
        cfg6 = {"firebase_project": "proj-6", "sessions_dataset": "firebase_sessions"}
        self.assertTrue(is_sessions_enabled(cfg6))

    @patch("crash_trend.fetch_sessions.get_app")
    def test_fetch_sessions_for_app_skips_bigquery_when_disabled(self, mock_get_app) -> None:
        mock_get_app.return_value = {
            "firebase_project": "disabled-proj",
            "data_sources": {"sessions": False},
        }
        mock_client = MagicMock()

        res = fetch_sessions_for_app("disabled_app", client=mock_client)

        # Ensure BigQuery client was NEVER called
        mock_client.list_tables.assert_not_called()
        mock_client.query.assert_not_called()

        self.assertEqual(res["sources"]["status"], "unavailable")
        self.assertIn("disabled", res["sources"]["error_message"].lower())
        self.assertIsNone(res["kpi"]["crash_free_users"]["rate"])
        self.assertIsNone(res["kpi"]["crash_free_sessions"]["rate"])

    def test_mixed_multi_app_bundle_passes_validation(self) -> None:
        import json
        from pathlib import Path
        from crash_trend.config import ROOT

        fixture_dir = ROOT / "tests" / "fixtures"
        f1 = json.loads((fixture_dir / "dashboard_v2.json").read_text(encoding="utf-8"))
        f2 = json.loads((fixture_dir / "dashboard_v2_no_sessions.json").read_text(encoding="utf-8"))

        # App 1: Full Sessions available (shop_app)
        app1_data = f1["apps"]["shop_app"]
        # App 2: Sessions unavailable/disabled (legacy_app)
        app2_data = f2["apps"]["legacy_app"]

        bundle = {
            "schema_version": "2.0",
            "generated_at": "2026-09-02T14:00:00Z",
            "default_app": "shop_app",
            "apps": {
                "shop_app": app1_data,
                "legacy_app": app2_data,
            },
        }

        # Must validate cleanly against Schema V2 with 0 errors
        errors = validate_dashboard_v2(bundle)
        self.assertEqual(errors, [], f"Validation errors found: {errors}")

        # Negative test: Ensure validator actually rejects invalid bundle
        invalid_bundle = dict(bundle)
        invalid_bundle["apps"] = {}
        errs = validate_dashboard_v2(invalid_bundle)
        self.assertTrue(len(errs) > 0, "Validator must reject empty apps bundle")


if __name__ == "__main__":
    unittest.main()
