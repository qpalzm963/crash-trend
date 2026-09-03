"""Unit and regression tests for Issue Lifecycle / Regression Detection (Issue #29).

Verifies:
1. 5 canonical lifecycle fixtures:
   - persistent
   - new_in_latest
   - regressed
   - not_observed_latest
   - resolved
2. Conservative resolved rules & sample sufficiency contracts.
3. Historical Catalog cross-window preservation (7d window doesn't truncate first_seen).
4. Catalog persistence & reload.
5. True latest version resolution (never from top_issues).
6. Multi-app catalog isolation.
7. Priority deterministic regressed_boost (+2 pts).
8. Dashboard UI contract (filters, badges, and prompt).
9. Schema validation for IssueLifecycle.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from crash_trend.analyze_gemini import calculate_priority
from crash_trend.build_dashboard import build_html
from crash_trend.lifecycle import (
    IssueHistoricalCatalog,
    detect_issue_lifecycle,
    enrich_app_data_with_lifecycle,
    get_latest_app_version,
    is_version_sample_sufficient,
)
from crash_trend.schema_v2 import (
    AppDashboardV2Data,
    IssueLifecycle,
    LifecycleStatus,
    validate_app_dashboard_v2,
)


class TestIssueLifecycleFixtures(unittest.TestCase):
    """Verifies the 5 canonical regression fixtures from Issue #29 specification."""

    def setUp(self):
        self.all_known_versions = ["1.0.8", "1.0.9", "1.0.10"]
        self.latest_version = "1.0.10"

    def test_fixture_persistent(self):
        """Persistent: 1.0.8: 4, 1.0.9: 2, 1.0.10: 3 => persistent."""
        events = {"1.0.8": 4, "1.0.9": 2, "1.0.10": 3}
        lc = detect_issue_lifecycle(
            issue_id="iss_p",
            historical_versions=["1.0.8", "1.0.9", "1.0.10"],
            all_known_versions=self.all_known_versions,
            latest_version=self.latest_version,
            sample_sufficient=True,
            current_version_events=events,
        )
        self.assertEqual(lc["status"], "persistent")
        self.assertEqual(lc["latest_version"], "1.0.10")
        self.assertEqual(lc["first_seen_version"], "1.0.8")
        self.assertEqual(lc["last_seen_version"], "1.0.10")
        self.assertEqual(lc["versions_seen"], 3)
        self.assertEqual(lc["confidence"], "high")
        self.assertIsNone(lc["previously_absent_since"])
        self.assertIsNone(lc["reappeared_version"])

    def test_fixture_new_in_latest(self):
        """New in latest: 1.0.8: 0, 1.0.9: 0, 1.0.10: 3 => new_in_latest."""
        events = {"1.0.8": 0, "1.0.9": 0, "1.0.10": 3}
        lc = detect_issue_lifecycle(
            issue_id="iss_new",
            historical_versions=["1.0.10"],
            all_known_versions=self.all_known_versions,
            latest_version=self.latest_version,
            sample_sufficient=True,
            current_version_events=events,
        )
        self.assertEqual(lc["status"], "new_in_latest")
        self.assertEqual(lc["first_seen_version"], "1.0.10")
        self.assertEqual(lc["last_seen_version"], "1.0.10")
        self.assertEqual(lc["versions_seen"], 1)
        self.assertEqual(lc["confidence"], "high")
        self.assertIsNone(lc["previously_absent_since"])
        self.assertIsNone(lc["reappeared_version"])

    def test_fixture_regressed(self):
        """Regressed: 1.0.8: 5, 1.0.9: 0, 1.0.10: 2 => regressed."""
        events = {"1.0.8": 5, "1.0.9": 0, "1.0.10": 2}
        lc = detect_issue_lifecycle(
            issue_id="iss_reg",
            historical_versions=["1.0.8", "1.0.10"],
            all_known_versions=self.all_known_versions,
            latest_version=self.latest_version,
            sample_sufficient=True,
            current_version_events=events,
        )
        self.assertEqual(lc["status"], "regressed")
        self.assertEqual(lc["first_seen_version"], "1.0.8")
        self.assertEqual(lc["last_seen_version"], "1.0.10")
        self.assertEqual(lc["versions_seen"], 2)
        self.assertEqual(lc["confidence"], "high")
        self.assertEqual(lc["previously_absent_since"], "1.0.9")
        self.assertEqual(lc["reappeared_version"], "1.0.10")
        self.assertIn("1.0.9", lc["reason"])
        self.assertIn("1.0.10", lc["reason"])

    def test_fixture_not_observed_latest(self):
        """Not observed latest: 1.0.8: 5, 1.0.9: 3, 1.0.10: 0, sample insufficient => not_observed_latest."""
        events = {"1.0.8": 5, "1.0.9": 3, "1.0.10": 0}
        lc = detect_issue_lifecycle(
            issue_id="iss_no_obs",
            historical_versions=["1.0.8", "1.0.9"],
            all_known_versions=self.all_known_versions,
            latest_version=self.latest_version,
            sample_sufficient=False,  # Insufficient sample in 1.0.10!
            current_version_events=events,
        )
        self.assertEqual(lc["status"], "not_observed_latest")
        self.assertEqual(lc["first_seen_version"], "1.0.8")
        self.assertEqual(lc["last_seen_version"], "1.0.9")
        self.assertEqual(lc["versions_seen"], 2)
        self.assertEqual(lc["confidence"], "medium")
        self.assertIsNone(lc["previously_absent_since"])
        self.assertIn("不足", lc["reason"])

    def test_fixture_resolved(self):
        """Resolved: 1.0.8: 5, 1.0.9: 3, 1.0.10: 0, sufficient sample => resolved."""
        events = {"1.0.8": 5, "1.0.9": 3, "1.0.10": 0}
        lc = detect_issue_lifecycle(
            issue_id="iss_res",
            historical_versions=["1.0.8", "1.0.9"],
            all_known_versions=self.all_known_versions,
            latest_version=self.latest_version,
            sample_sufficient=True,  # Sufficient sample in 1.0.10!
            current_version_events=events,
        )
        self.assertEqual(lc["status"], "resolved")
        self.assertEqual(lc["first_seen_version"], "1.0.8")
        self.assertEqual(lc["last_seen_version"], "1.0.9")
        self.assertEqual(lc["versions_seen"], 2)
        self.assertEqual(lc["confidence"], "high")
        self.assertEqual(lc["previously_absent_since"], "1.0.10")
        self.assertIn("未再觀察到", lc["reason"])


class TestSampleSufficiency(unittest.TestCase):
    """Verifies evidence thresholds for sample sufficiency."""

    def test_adoption_rate_threshold(self):
        self.assertTrue(is_version_sample_sufficient({"adoption_rate": 0.05}))
        self.assertTrue(is_version_sample_sufficient({"adoption_rate": 0.12}))
        self.assertFalse(is_version_sample_sufficient({"adoption_rate": 0.049}))

    def test_sessions_total_threshold(self):
        self.assertTrue(is_version_sample_sufficient({"sessions_total": 1000}))
        self.assertTrue(is_version_sample_sufficient({"sessions_total": 50000}))
        self.assertFalse(is_version_sample_sufficient({"sessions_total": 999}))

    def test_crash_events_threshold(self):
        self.assertTrue(is_version_sample_sufficient({"crash_events": 20}))
        self.assertTrue(is_version_sample_sufficient({"crash_events": 100}))
        self.assertFalse(is_version_sample_sufficient({"crash_events": 19}))

    def test_explicit_flag_and_none(self):
        self.assertTrue(is_version_sample_sufficient({"sample_sufficient": True}))
        self.assertFalse(is_version_sample_sufficient({"sample_sufficient": False}))
        self.assertFalse(is_version_sample_sufficient(None))
        self.assertFalse(is_version_sample_sufficient({}))


class TestHistoricalCatalog(unittest.TestCase):
    """Verifies catalog cross-window preservation and persistence."""

    def test_catalog_preserves_first_seen_across_short_query_window(self):
        """Historical first_seen_version must not be truncated when query window is only 7d."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cat_file = Path(tmpdir) / "historical_catalog.json"
            catalog = IssueHistoricalCatalog(cat_file)

            # Step 1: 90d window ran earlier, observing issue in 1.0.1, 1.0.3, 1.0.10
            initial_issues = [
                {
                    "issue_id": "iss_long_lived",
                    "title": "Legacy Memory Leak",
                    "first_seen_version": "1.0.1",
                    "last_seen_version": "1.0.10",
                    "version_distribution": [
                        {"version": "1.0.1", "events": 10, "users": 5},
                        {"version": "1.0.3", "events": 8, "users": 4},
                        {"version": "1.0.10", "events": 2, "users": 1},
                    ],
                }
            ]
            catalog.update_from_issues(initial_issues)
            catalog.save()

            # Step 2: 7d window runs later and only queries 1.0.10 events
            app_7d: AppDashboardV2Data = {
                "metadata": {"app_id": "app_test", "display_name": "Test App", "firebase_project_id": "p", "platforms": ["android"]},
                "period": {"days": 7, "start_time": "2026-08-28T00:00:00Z", "end_time": "2026-09-03T23:59:59Z"},
                "sources": {"crashlytics_bq": {"status": "available"}},
                "kpi": {"crash_events": {"value": 2}, "affected_users": {"value": 1}},
                "daily_trend": [],
                "version_health": [{"version": "1.0.10", "status": "latest"}],
                "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [{"app_version": "1.0.10"}], "custom_keys": []},
                "top_issues": [
                    {
                        "issue_id": "iss_long_lived",
                        "title": "Legacy Memory Leak",
                        # 7d BQ query alone only saw 1.0.10!
                        "first_seen_version": "1.0.10",
                        "last_seen_version": "1.0.10",
                        "version_distribution": [{"version": "1.0.10", "events": 2, "users": 1}],
                    }
                ],
                "ai_summary": {"status": "unavailable", "overview": ""},
                "periods": {},
            }

            # Reload catalog from disk
            cat_reloaded = IssueHistoricalCatalog(cat_file)
            cat_reloaded.load()

            enriched = enrich_app_data_with_lifecycle(app_7d, catalog=cat_reloaded)
            iss = enriched["top_issues"][0]

            # True historical first_seen_version remains 1.0.1, NOT truncated to 1.0.10!
            self.assertEqual(iss["first_seen_version"], "1.0.1")
            self.assertEqual(iss["last_seen_version"], "1.0.10")

            # Lifecycle is correctly persistent (not new_in_latest)!
            self.assertEqual(iss["lifecycle"]["status"], "persistent")
            self.assertEqual(iss["lifecycle"]["first_seen_version"], "1.0.1")

    def test_multi_app_catalog_isolation(self):
        """Catalogs for different apps must remain completely isolated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            cat_a = IssueHistoricalCatalog(out / "app_a" / "historical_catalog.json")
            cat_b = IssueHistoricalCatalog(out / "app_b" / "historical_catalog.json")

            cat_a.update_from_issues([{"issue_id": "iss_common_id", "first_seen_version": "1.0.0"}])
            cat_a.save()

            cat_b.update_from_issues([{"issue_id": "iss_common_id", "first_seen_version": "2.0.0"}])
            cat_b.save()

            # Verify isolated reload
            cat_a_reloaded = IssueHistoricalCatalog(out / "app_a" / "historical_catalog.json")
            cat_a_reloaded.load()
            cat_b_reloaded = IssueHistoricalCatalog(out / "app_b" / "historical_catalog.json")
            cat_b_reloaded.load()

            self.assertEqual(cat_a_reloaded.get_issue_history("iss_common_id")["first_seen_version"], "1.0.0")
            self.assertEqual(cat_b_reloaded.get_issue_history("iss_common_id")["first_seen_version"], "2.0.0")


class TestLatestVersionResolution(unittest.TestCase):
    """Verifies that latest version is determined from version_health/distributions, NEVER top_issues."""

    def test_latest_version_not_inferred_from_top_issues(self):
        app_data = {
            "version_health": [
                {"version": "1.0.0", "status": "deprecated"},
                {"version": "1.0.9", "status": "active"},
                {"version": "1.0.10", "status": "latest"},
            ],
            # Top issues only has crash in older version 1.0.0
            "top_issues": [
                {"issue_id": "iss_old", "last_seen_version": "1.0.0"}
            ],
        }
        latest = get_latest_app_version(app_data)
        self.assertEqual(latest, "1.0.10")

    def test_latest_version_max_semver_when_status_missing(self):
        app_data = {
            "version_health": [
                {"version": "1.0.8"},
                {"version": "1.0.10"},
                {"version": "1.0.9"},
            ],
            "top_issues": [],
        }
        latest = get_latest_app_version(app_data)
        self.assertEqual(latest, "1.0.10")


class TestPriorityRegressedBoost(unittest.TestCase):
    """Verifies deterministic Priority boost (+2 pts) for regressed issues."""

    def test_priority_scoring_regressed_boost(self):
        issue_regressed = {
            "issue_id": "iss_reg",
            "title": "Crash Recurring",
            "events": 100,
            "affected_users": 50,
            "fatal": False,
            "lifecycle": {
                "status": "regressed",
                "latest_version": "1.0.10",
                "first_seen_version": "1.0.8",
                "last_seen_version": "1.0.10",
                "versions_seen": 2,
                "confidence": "high",
                "previously_absent_since": "1.0.9",
                "reappeared_version": "1.0.10",
                "reason": "Regressed in 1.0.10",
            },
        }
        issue_normal = {
            "issue_id": "iss_norm",
            "title": "Crash Stable",
            "events": 100,
            "affected_users": 50,
            "fatal": False,
            "lifecycle": {
                "status": "persistent",
                "latest_version": "1.0.10",
                "first_seen_version": "1.0.8",
                "last_seen_version": "1.0.10",
                "versions_seen": 3,
                "confidence": "high",
                "previously_absent_since": None,
                "reappeared_version": None,
                "reason": "Persistent",
            },
        }

        prio_reg = calculate_priority(issue_regressed, max_users=100, max_events=100)
        prio_norm = calculate_priority(issue_normal, max_users=100, max_events=100)

        # Regressed issue must receive regressed_boost = 2
        self.assertEqual(prio_reg["score_breakdown"]["regressed_boost"], 2)
        self.assertEqual(prio_norm["score_breakdown"]["regressed_boost"], 0)
        # Score of regressed issue is higher than normal identical issue
        self.assertGreater(prio_reg["score"], prio_norm["score"])


class TestDashboardUIContractLifecycle(unittest.TestCase):
    """Verifies that the dashboard HTML contains lifecycle UI components."""

    def test_dashboard_ui_contract_lifecycle(self):
        bundle = {
            "schema_version": "2.3.0",
            "generated_at": "2026-09-03T12:00:00Z",
            "default_app": "demo",
            "apps": {
                "demo": {
                    "metadata": {"app_id": "demo", "display_name": "Demo App", "firebase_project_id": "p", "platforms": ["android"]},
                    "period": {"days": 30, "start_time": "2026-08-05T00:00:00Z", "end_time": "2026-09-03T23:59:59Z"},
                    "sources": {"crashlytics_bq": {"status": "available"}},
                    "kpi": {"crash_events": {"value": 10}, "affected_users": {"value": 5}},
                    "daily_trend": [],
                    "version_health": [{"version": "1.0.10", "status": "latest"}],
                    "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [], "custom_keys": []},
                    "top_issues": [
                        {
                            "issue_id": "iss_1",
                            "title": "Crash A",
                            "subtitle": "NullPointerException",
                            "platform": "android",
                            "error_type": "FATAL",
                            "priority": {"score": 85, "level": "P0", "trend": "new", "score_breakdown": None},
                            "events": 10,
                            "affected_users": 5,
                            "first_seen_version": "1.0.8",
                            "last_seen_version": "1.0.10",
                            "version_distribution": [],
                            "blame_frame": None,
                            "ai_analysis": {"status": "unavailable", "root_cause": None, "suggested_fix": None, "effort": None, "confidence": None, "reasoning_sources": None},
                            "detail": None,
                            "lifecycle": {
                                "status": "regressed",
                                "latest_version": "1.0.10",
                                "first_seen_version": "1.0.8",
                                "last_seen_version": "1.0.10",
                                "versions_seen": 2,
                                "confidence": "high",
                                "previously_absent_since": "1.0.9",
                                "reappeared_version": "1.0.10",
                                "reason": "於版本 1.0.9 消失後在 1.0.10 重新出現",
                            },
                        }
                    ],
                    "ai_summary": {"status": "unavailable", "overview": ""},
                    "periods": {},
                }
            },
        }

        html = build_html(bundle)

        # 1. Filter dropdown must exist
        self.assertIn('id="filterLifecycle"', html)
        self.assertIn('value="new_in_latest"', html)
        self.assertIn('value="regressed"', html)
        self.assertIn('value="persistent"', html)
        self.assertIn('value="resolved"', html)
        self.assertIn('value="not_observed_latest"', html)

        # 2. CSS styles for lifecycle badges
        self.assertIn(".badge-lifecycle-new", html)
        self.assertIn(".badge-lifecycle-regressed", html)
        self.assertIn(".badge-lifecycle-persistent", html)
        self.assertIn(".badge-lifecycle-resolved", html)
        self.assertIn(".badge-lifecycle-not-observed", html)

        # 3. JavaScript helper and filter logic
        self.assertIn("function getLifecycleBadgeHtml(lc)", html)
        self.assertIn("filterLife", html)
        self.assertIn("iss.lifecycle", html)


class TestSchemaValidationLifecycle(unittest.TestCase):
    """Verifies schema validation rules for IssueLifecycle."""

    def setUp(self):
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "dashboard_v2.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.app_data = data["apps"]["shop_app"]

    def test_valid_lifecycle_passes(self):
        import copy
        app = copy.deepcopy(self.app_data)
        app["top_issues"][0]["lifecycle"] = {
            "status": "persistent",
            "latest_version": "1.0.10",
            "first_seen_version": "1.0.8",
            "last_seen_version": "1.0.10",
            "versions_seen": 2,
            "confidence": "high",
            "previously_absent_since": None,
            "reappeared_version": None,
            "reason": "Persistent",
        }
        errors = validate_app_dashboard_v2(app)
        self.assertEqual(errors, [])

    def test_invalid_lifecycle_status_rejected(self):
        import copy
        app = copy.deepcopy(self.app_data)
        app["top_issues"][0]["lifecycle"] = {
            "status": "bogus_status",  # INVALID!
            "latest_version": "1.0.10",
            "first_seen_version": "1.0.8",
            "last_seen_version": "1.0.10",
            "versions_seen": 2,
            "confidence": "high",
        }
        errors = validate_app_dashboard_v2(app)
        self.assertTrue(any("lifecycle.status must be one of" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
