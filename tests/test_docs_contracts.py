import hashlib
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from crash_trend.gate.artifact import validate_release_gate_artifact
from crash_trend.gate.history import ReleaseGateHistoryStore, compute_evaluation_key
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
        """Verifies canonical paths for all V2.7 stores and artifacts in README and schema docs."""
        canonical_paths = [
            "out/<app>/historical_catalog.json",
            "out/<app>/catalog_authority.sqlite3",
            "out/<app>/release_gate.json",
            "out/<app>/release_gate_history.sqlite3",
            "out/<app>/alert_delivery.sqlite3",
        ]
        for cp in canonical_paths:
            self.assertIn(cp, self.readme_text, f"Missing {cp} in README.md")
            self.assertIn(cp, self.schema_doc_text, f"Missing {cp} in docs/dashboard_v2_schema.md")

        # Verify no bare unpartitioned catalog paths remain in contract declarations
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

        # Document must mention Producer writes 2.3.0 and Bundle writes 2.7.0
        self.assertIn('"2.3.0"', self.schema_doc_text)
        self.assertIn('"2.7.0"', self.schema_doc_text)
        self.assertIn("Producer", self.schema_doc_text)

        # Validator must accept 2.3.0, 2.6.0 as well as 2.7.0
        self.assertIn("2.3.0", SUPPORTED_SCHEMA_VERSIONS)
        self.assertIn("2.6.0", SUPPORTED_SCHEMA_VERSIONS)
        self.assertIn("2.7.0", SUPPORTED_SCHEMA_VERSIONS)

    def test_exit_codes_and_cli_contracts_documented(self) -> None:
        """Verifies that Exit Code contracts (0, 1, 2) and CLI flags are clearly documented."""
        self.assertIn("--fail-on-regression", self.readme_text)
        self.assertIn("--fail-on-alert-failure", self.readme_text)
        self.assertIn("Exit Code", self.readme_text)
        self.assertIn("`0`", self.readme_text)
        self.assertIn("`1`", self.readme_text)
        self.assertIn("`2`", self.readme_text)

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

    def test_schema_spec_release_gate_artifact_passes_validator(self) -> None:
        """Verifies that ReleaseGateArtifact JSON in docs parses and passes validate_release_gate_artifact."""
        gate_match = re.search(r"#### `ReleaseGateArtifact`\s+```json\s+(.*?)\s+```", self.schema_doc_text, re.DOTALL)
        self.assertIsNotNone(gate_match, "ReleaseGateArtifact JSON block must exist in schema spec")
        assert gate_match is not None
        gate_json_str = gate_match.group(1)

        self.assertNotIn("...", gate_json_str, "JSON example in doc must not contain placeholder ellipses '...'")

        try:
            gate_data = json.loads(gate_json_str)
        except json.JSONDecodeError as err:
            self.fail(f"ReleaseGateArtifact JSON in doc failed to parse: {err}")

        validation_errors = validate_release_gate_artifact(gate_data)
        self.assertEqual(
            validation_errors,
            [],
            f"ReleaseGateArtifact JSON in doc failed schema validation: {validation_errors}",
        )
        self.assertIn("android", gate_data.get("platforms", {}))
        self.assertEqual(gate_data.get("schema_version"), "1.0")

    def test_gate_history_ddl_and_evaluation_key_contracts(self) -> None:
        """Verifies Gate History SQLite DDL and evaluation_key formula match implementations."""
        # DDL columns check
        for col in (
            "app_id TEXT NOT NULL",
            "platform TEXT NOT NULL",
            "version TEXT NOT NULL",
            "previous_version TEXT",
            "gate_status TEXT NOT NULL",
            "sample_sufficient INTEGER NOT NULL",
            "policy_version TEXT NOT NULL",
            "policy_identity TEXT NOT NULL DEFAULT ''",
            "comparison_window TEXT",
            "evaluated_at TEXT NOT NULL",
            "evaluation_key TEXT NOT NULL UNIQUE",
            "triggered_reasons_json TEXT NOT NULL",
            "rule_results_json TEXT NOT NULL",
            "normalized_metrics_json TEXT NOT NULL",
            "summary TEXT NOT NULL",
            "schema_version TEXT NOT NULL DEFAULT '1.0'",
            "created_at TEXT NOT NULL",
        ):
            self.assertIn(col, self.schema_doc_text, f"Missing column '{col}' in docs/dashboard_v2_schema.md DDL")
            self.assertIn(col, ReleaseGateHistoryStore.SCHEMA_SQL, f"Missing column '{col}' in SCHEMA_SQL")

        # Ensure obsolete metrics_digest is absent from both docs
        self.assertNotIn("metrics_digest", self.schema_doc_text)
        self.assertNotIn("metrics_digest", self.readme_text)

        # Ensure correct evaluation_key formula is documented
        self.assertIn("app_id:platform:version:evaluated_at:policy_version:policy_identity", self.schema_doc_text)
        self.assertIn("(app_id, platform, version, evaluated_at, policy_version, policy_identity)", self.readme_text)

        # Test compute_evaluation_key() produces expected deterministic SHA-256
        key = compute_evaluation_key("shop_app", "android", "3.2.0", "2026-09-08T10:00:00Z", "1.0", "policy_123")
        expected_raw = "shop_app:android:3.2.0:2026-09-08T10:00:00Z:1.0:policy_123"
        expected_hash = hashlib.sha256(expected_raw.encode("utf-8")).hexdigest()
        self.assertEqual(key, expected_hash)

    def test_cli_flags_and_parsers_consistency(self) -> None:
        """Verifies documented CLI flags against implementations and parser help outputs."""
        # 1. crash_trend.pipeline_run
        res_pipe = subprocess.run(
            [sys.executable, "-m", "crash_trend.pipeline_run", "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        for flag in ("--fail-on-regression", "--fail-on-alert-failure", "--app", "--days", "--skip-dashboard"):
            self.assertIn(flag, res_pipe.stdout)

        # 2. crash_trend.release_gate
        res_gate = subprocess.run(
            [sys.executable, "-m", "crash_trend.release_gate", "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        for flag in ("--app", "--fail-on-regression", "--policy", "--out", "--history", "--trend", "--platform", "--version"):
            self.assertIn(flag, res_gate.stdout)

        # 3. crash_trend.alerts
        res_alerts = subprocess.run(
            [sys.executable, "-m", "crash_trend.alerts", "--help"],
            capture_output=True,
            text=True,
            check=True,
        )
        for flag in ("--app", "--history", "--platform", "--version", "--limit", "--json", "--db-path", "--dry-run", "--force"):
            self.assertIn(flag, res_alerts.stdout)

        # Verify --fail-on-alert-failure is strictly in pipeline_run, not in alerts CLI
        self.assertNotIn("--fail-on-alert-failure", res_alerts.stdout)

        # Verify README CLI table does not have --platform on normal standalone evaluation row of release_gate
        self.assertIn(
            "`python3 -m crash_trend.release_gate --app shop_app --fail-on-regression`",
            self.readme_text,
        )
        self.assertNotIn(
            "release_gate --app shop_app --platform android --fail-on-regression",
            self.readme_text,
        )


if __name__ == "__main__":
    unittest.main()

