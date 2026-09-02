"""Unit tests for Dashboard V2 Data Schema validation and fixtures."""

from __future__ import annotations

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
    is_valid_iso8601,
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

        # Check multi-app presence
        self.assertIn("shop_app", data["apps"])
        self.assertIn("rider_app", data["apps"])
        self.assertEqual(data["default_app"], "shop_app")

        # Check shop_app values
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

    def test_timestamp_validator(self) -> None:
        self.assertTrue(is_valid_iso8601("2026-09-02T14:00:00Z"))
        self.assertTrue(is_valid_iso8601("2026-09-02T14:00:00.123Z"))
        self.assertTrue(is_valid_iso8601("2026-09-02T14:00:00+08:00"))
        self.assertFalse(is_valid_iso8601("2026-09-02"))
        self.assertFalse(is_valid_iso8601("1.0.4"))
        self.assertFalse(is_valid_iso8601(None))

    def test_date_validator(self) -> None:
        self.assertTrue(is_valid_date("2026-09-02"))
        self.assertFalse(is_valid_date("2026-09-02T14:00:00Z"))
        self.assertFalse(is_valid_date("09/02/2026"))

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
        data["apps"]["shop_app"]["kpi"]["crash_free_users"]["rate"] = 1.5
        errors = validate_dashboard_v2(data)
        self.assertTrue(any("crash_free_users.rate" in e for e in errors))

        # Unavailable status with non-null rate
        data["apps"]["shop_app"]["kpi"]["crash_free_users"]["status"] = "unavailable"
        data["apps"]["shop_app"]["kpi"]["crash_free_users"]["rate"] = 0.99
        errors = validate_dashboard_v2(data)
        self.assertTrue(any("crash_free_users.rate must be null" in e for e in errors))

    def test_invalid_error_type_rejected(self) -> None:
        fixture_path = self.fixtures_dir / "dashboard_v2.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        data["apps"]["shop_app"]["top_issues"][0]["error_type"] = "UNKNOWN_TYPE"
        errors = validate_dashboard_v2(data)
        self.assertTrue(any("error_type must be FATAL, ANR, or NON_FATAL" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
