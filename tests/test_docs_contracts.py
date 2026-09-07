import json
import re
import unittest
from pathlib import Path

from crash_trend.lifecycle import IssueHistoricalCatalog, is_version_sample_sufficient
from crash_trend.schema_v2 import SUPPORTED_SCHEMA_VERSIONS, validate_historical_catalog


class TestDocsContracts(unittest.TestCase):
    """Verifies that documentation contracts, schemas, and implementations remain in sync."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parent.parent
        cls.readme_path = cls.repo_root / "README.md"
        cls.schema_doc_path = cls.repo_root / "docs" / "dashboard_v2_schema.md"

        cls.assertTrue(cls.readme_path.is_file(), f"README.md not found at {cls.readme_path}")
        cls.assertTrue(cls.schema_doc_path.is_file(), f"dashboard_v2_schema.md not found at {cls.schema_doc_path}")

        cls.readme_text = cls.readme_path.read_text(encoding="utf-8")
        cls.schema_doc_text = cls.schema_doc_path.read_text(encoding="utf-8")

    def test_canonical_paths_consistency(self) -> None:
        """Verifies canonical paths out/<app>/historical_catalog.json and out/<app>/catalog_authority.sqlite3."""
        # Check presence of canonical partitioned paths in README.md
        self.assertIn("out/<app>/historical_catalog.json", self.readme_text)
        self.assertIn("out/<app>/catalog_authority.sqlite3", self.readme_text)

        # Check presence of canonical partitioned paths in docs/dashboard_v2_schema.md
        self.assertIn("out/<app>/historical_catalog.json", self.schema_doc_text)
        self.assertIn("out/<app>/catalog_authority.sqlite3", self.schema_doc_text)

        # Verify no bare contract paths remain in contract declarations
        self.assertNotIn("`out/historical_catalog.json`", self.readme_text)
        self.assertNotIn("`out/catalog_authority.sqlite3`", self.readme_text)
        self.assertNotIn("`out/historical_catalog.json`", self.schema_doc_text)
        self.assertNotIn("`out/catalog_authority.sqlite3`", self.schema_doc_text)

    def test_schema_spec_json_blocks_are_valid_and_pass_validators(self) -> None:
        """Verifies that JSON blocks in schema documentation are valid parseable JSON without ellipses."""
        # Find JSON block under HistoricalCatalogData
        cat_match = re.search(r"#### `HistoricalCatalogData`\s+```json\s+(.*?)\s+```", self.schema_doc_text, re.DOTALL)
        self.assertIsNotNone(cat_match, "HistoricalCatalogData JSON block must exist in schema spec")
        cat_json_str = cat_match.group(1)

        # Must not contain placeholder ellipses like { ... }
        self.assertNotIn("...", cat_json_str, "JSON example in doc must not contain placeholder ellipses '...'")

        # Must be parseable JSON
        try:
            cat_data = json.loads(cat_json_str)
        except json.JSONDecodeError as err:
            self.fail(f"HistoricalCatalogData JSON in doc failed to parse: {err}")

        # Must pass schema validator with 0 errors
        validation_errors = validate_historical_catalog(cat_data)
        self.assertEqual(
            validation_errors,
            [],
            f"HistoricalCatalogData JSON in doc failed schema validation: {validation_errors}",
        )

    def test_sample_sufficient_threshold_matches_implementation(self) -> None:
        """Verifies that documented sample_sufficient threshold matches is_version_sample_sufficient()."""
        # Ensure doc explicitly references adoption_rate >= 0.05, sessions_total >= 1000, crash_events >= 20
        self.assertIn("adoption_rate >= 0.05", self.schema_doc_text)
        self.assertIn("sessions_total >= 1000", self.schema_doc_text)
        self.assertIn("crash_events >= 20", self.schema_doc_text)
        # Ensure obsolete affected_users >= 10 is not mentioned
        self.assertNotIn("affected_users >= 10", self.schema_doc_text)

        # Test implementation behavior corresponds to these exact thresholds
        self.assertTrue(is_version_sample_sufficient({"adoption_rate": 0.05}))
        self.assertFalse(is_version_sample_sufficient({"adoption_rate": 0.049}))
        self.assertTrue(is_version_sample_sufficient({"sessions_total": 1000}))
        self.assertFalse(is_version_sample_sufficient({"sessions_total": 999}))
        self.assertTrue(is_version_sample_sufficient({"crash_events": 20}))
        self.assertFalse(is_version_sample_sufficient({"crash_events": 19}))

    def test_producer_schema_version_matches_doc(self) -> None:
        """Verifies that Historical Catalog Producer emits '2.3.0' while validator supports compatibility."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            cat_path = Path(tmpdir) / "catalog.json"
            cat = IssueHistoricalCatalog(cat_path, app_id="test_app")
            cat.save()
            cat_dict = json.loads(cat_path.read_text(encoding="utf-8"))
            self.assertEqual(cat_dict.get("schema_version"), "2.3.0")

        # Document must mention Producer writes 2.3.0
        self.assertIn('"2.3.0"', self.schema_doc_text)
        self.assertIn("Producer", self.schema_doc_text)

        # Validator must accept 2.3.0 as well as 2.6.0
        self.assertIn("2.3.0", SUPPORTED_SCHEMA_VERSIONS)
        self.assertIn("2.6.0", SUPPORTED_SCHEMA_VERSIONS)

    def test_recursive_zero_raw_ids_enforcement(self) -> None:
        """Verifies that validate_historical_catalog() recursively rejects raw IDs anywhere in the structure."""
        nested_cat = {
            "schema_version": "2.3.0",
            "updated_at": "2026-09-02T14:00:00Z",
            "issues": {
                "android:8a7f1b2c": {
                    "issue_id": "8a7f1b2c",
                    "platform": "android",
                    "first_seen_version": "3.1.0",
                    "last_seen_version": "3.2.0",
                    "versions_seen": ["3.1.0", "3.2.0"],
                }
            },
            "app_versions": {
                "android": {
                    "3.2.0": {
                        "version": "3.2.0",
                        "platform": "android",
                        "recent_health": {
                            "30d": {
                                "nested_payload": {
                                    "user_ids": ["raw_user_leak"],
                                    "device_list": [{"installation_ids": ["raw_inst_leak"]}],
                                }
                            }
                        },
                    }
                }
            },
        }
        errs = validate_historical_catalog(nested_cat)
        self.assertEqual(len(errs), 2)
        self.assertTrue(any("user_ids is forbidden" in e for e in errs))
        self.assertTrue(any("installation_ids is forbidden" in e for e in errs))
        self.assertTrue(any("recent_health.30d.nested_payload.user_ids" in e for e in errs))
        self.assertTrue(any("recent_health.30d.nested_payload.device_list[0].installation_ids" in e for e in errs))


if __name__ == "__main__":
    unittest.main()
