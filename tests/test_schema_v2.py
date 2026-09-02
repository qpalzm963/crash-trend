"""Comprehensive unit tests for Dashboard V2 Data Schema validation, fixtures, and consistency."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.schema_v2 import (
    SCHEMA_VERSION,
    is_valid_date,
    is_valid_iso8601_utc,
    validate_app_dashboard_v2,
    validate_dashboard_v2,
)


class TestDashboardV2Schema(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures_dir = ROOT / "tests" / "fixtures"

    def test_full_fixture_validates_successfully(self) -> None:
        fixture_path = self.fixtures_dir / "dashboard_v2.json"
        self.assertTrue(fixture_path.exists(), f"Missing fixture: {fixture_path}")
        data = json.loads(fixture_path.read_text(encoding="utf-8"))

        errors = validate_dashboard_v2(data)
        self.assertEqual(
            errors,
            [],
            f"Expected dashboard_v2.json to pass validation, got errors:\n" + "\n".join(errors),
        )

        # Multi-app check
        self.assertIn("shop_app", data["apps"])
        self.assertIn("rider_app", data["apps"])
        self.assertEqual(data["default_app"], "shop_app")

        # Shop app check
        shop = data["apps"]["shop_app"]
        self.assertEqual(shop["kpi"]["crash_free_users"]["status"], "available")
        self.assertAlmostEqual(shop["kpi"]["crash_free_users"]["rate"], 0.9985)
        self.assertEqual(shop["kpi"]["crash_free_sessions"]["status"], "available")

    def test_no_sessions_fixture_validates_successfully(self) -> None:
        fixture_path = self.fixtures_dir / "dashboard_v2_no_sessions.json"
        self.assertTrue(fixture_path.exists(), f"Missing fixture: {fixture_path}")
        data = json.loads(fixture_path.read_text(encoding="utf-8"))

        errors = validate_dashboard_v2(data)
        self.assertEqual(
            errors,
            [],
            f"Expected dashboard_v2_no_sessions.json to pass validation, got errors:\n"
            + "\n".join(errors),
        )

        legacy = data["apps"]["legacy_app"]
        cf_users = legacy["kpi"]["crash_free_users"]
        self.assertEqual(cf_users["status"], "unavailable")
        self.assertIsNone(cf_users["rate"])
        self.assertIsNotNone(cf_users["unavailable_reason"])

        cf_sessions = legacy["kpi"]["crash_free_sessions"]
        self.assertEqual(cf_sessions["status"], "unavailable")
        self.assertIsNone(cf_sessions["rate"])

    def test_daily_trend_consistency_for_fixtures(self) -> None:
        """Verifies daily_trend consistency against KPI totals and period length."""
        fixture_path = self.fixtures_dir / "dashboard_v2.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))

        for app_id, app_data in data["apps"].items():
            days_count = app_data["period"]["days"]
            daily = app_data["daily_trend"]
            self.assertEqual(
                len(daily),
                days_count,
                f"{app_id}: daily_trend items ({len(daily)}) must match period.days ({days_count})",
            )

            # Sum of daily crash_events must equal KPI crash_events value
            total_events_daily = sum(d["crash_events"] for d in daily)
            self.assertEqual(
                total_events_daily,
                app_data["kpi"]["crash_events"]["value"],
                f"{app_id}: sum of daily crash_events ({total_events_daily}) must equal KPI ({app_data['kpi']['crash_events']['value']})",
            )

            # Sum of daily fatal/anr/non_fatal must equal KPI error type breakdown
            by_err = app_data["kpi"]["events_by_error_type"]
            self.assertEqual(sum(d["fatal_events"] for d in daily), by_err["fatal"])
            self.assertEqual(sum(d["anr_events"] for d in daily), by_err["anr"])
            self.assertEqual(sum(d["non_fatal_events"] for d in daily), by_err["non_fatal"])

    def test_strict_timestamp_validator(self) -> None:
        self.assertTrue(is_valid_iso8601_utc("2026-09-02T14:00:00Z"))
        self.assertTrue(is_valid_iso8601_utc("2026-09-02T14:00:00.123Z"))
        self.assertTrue(is_valid_iso8601_utc("2026-09-02T14:00:00+00:00"))

        # Invalid calendar dates or invalid timezone
        self.assertFalse(is_valid_iso8601_utc("2026-99-99T99:99:99Z"))
        self.assertFalse(is_valid_iso8601_utc("2026-02-30T10:00:00Z"))
        self.assertFalse(is_valid_iso8601_utc("2026-09-02T14:00:00"))  # missing tz
        self.assertFalse(is_valid_iso8601_utc("2026-09-02T14:00:00+08:00"))  # non-UTC tz
        self.assertFalse(is_valid_iso8601_utc("2026-09-02"))
        self.assertFalse(is_valid_iso8601_utc(None))
        self.assertFalse(is_valid_iso8601_utc(123456))

    def test_strict_date_validator(self) -> None:
        self.assertTrue(is_valid_date("2026-09-02"))
        self.assertTrue(is_valid_date("2024-02-29"))  # 2024 leap year
        self.assertFalse(is_valid_date("2023-02-29"))  # 2023 not leap year
        self.assertFalse(is_valid_date("2026-02-30"))
        self.assertFalse(is_valid_date("2026-13-01"))
        self.assertFalse(is_valid_date("2026-00-00"))
        self.assertFalse(is_valid_date("2026-09-02T14:00:00Z"))
        self.assertFalse(is_valid_date(None))

    def test_invalid_schema_version_rejected(self) -> None:
        fixture_path = self.fixtures_dir / "dashboard_v2.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        data["schema_version"] = "1.0"
        errors = validate_dashboard_v2(data)
        self.assertTrue(any("schema_version" in e for e in errors))

    def test_invalid_crash_free_rate_rejected(self) -> None:
        fixture_path = self.fixtures_dir / "dashboard_v2.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))

        # Rate out of range (> 1.0)
        data_bad = copy.deepcopy(data)
        data_bad["apps"]["shop_app"]["kpi"]["crash_free_users"]["rate"] = 1.5
        errors = validate_dashboard_v2(data_bad)
        self.assertTrue(any("crash_free_users.rate" in e for e in errors))

        # Unavailable status with non-null rate
        data_bad2 = copy.deepcopy(data)
        data_bad2["apps"]["shop_app"]["kpi"]["crash_free_users"]["status"] = "unavailable"
        data_bad2["apps"]["shop_app"]["kpi"]["crash_free_users"]["rate"] = 0.99
        errors = validate_dashboard_v2(data_bad2)
        self.assertTrue(any("crash_free_users.rate must be null" in e for e in errors))

    def test_invalid_error_type_rejected(self) -> None:
        fixture_path = self.fixtures_dir / "dashboard_v2.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        data["apps"]["shop_app"]["top_issues"][0]["error_type"] = "UNKNOWN_TYPE"
        errors = validate_dashboard_v2(data)
        self.assertTrue(any("error_type must be FATAL, ANR, or NON_FATAL" in e for e in errors))

    def test_malformed_input_handled_gracefully_without_crash(self) -> None:
        """Ensures validator does not throw unhandled exceptions on malformed structures."""
        malformed_cases = [
            None,
            "not a dict",
            [],
            {"schema_version": "2.0", "generated_at": "2026-09-02T14:00:00Z", "default_app": "app1", "apps": {"app1": None}},
            {"schema_version": "2.0", "generated_at": "2026-09-02T14:00:00Z", "default_app": "app1", "apps": {"app1": {
                "metadata": None,
                "period": "invalid",
                "sources": [1, 2, 3],
                "kpi": 12345,
                "daily_trend": ["not an object", None, 456],
                "version_health": [None, "invalid"],
                "distributions": "invalid",
                "top_issues": [None, 789, "string"],
                "ai_summary": None,
                "limitations": None
            }}},
        ]
        for idx, case in enumerate(malformed_cases):
            errors = validate_dashboard_v2(case)
            self.assertIsInstance(errors, list)
            self.assertTrue(len(errors) > 0, f"Case {idx} should produce validation errors")


if __name__ == "__main__":
    unittest.main()
