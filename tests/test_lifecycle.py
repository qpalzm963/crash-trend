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
9. Schema validation for IssueLifecycle across top_issues and period snapshots.
10. Production pipeline order: Sessions adoption evidence enables resolved state.
11. Intermediate version gap sample sufficiency verification for regressed.
12. Filter/sort copy prompt stability by issue_id.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from crash_trend.analyze_gemini import calculate_priority
from crash_trend.build_dashboard import build_html
from crash_trend.fetch_sessions import enrich_app_dashboard_with_sessions
from crash_trend.lifecycle import (
    IssueHistoricalCatalog,
    bootstrap_catalog_from_disk,
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
            known_version_sufficiency={"1.0.8": True, "1.0.9": True, "1.0.10": True},
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
                "version_health": [{"version": "1.0.10", "status": "latest", "adoption_rate": 0.5}],
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


class TestProductionPipelineAndEvidenceTiming(unittest.TestCase):
    """Verifies that Sessions evidence injected after BigQuery properly promotes resolved state (Must Fix 1 & 2)."""

    def test_production_pipeline_order_sessions_enables_resolved(self):
        # 1. Simulate BigQuery output where latest release 1.0.10 has 0 crashes and is absent from Crashlytics
        app_bq = {
            "metadata": {"app_id": "demo", "display_name": "Demo", "firebase_project_id": "p", "platforms": ["android"]},
            "period": {"days": 30, "start_time": "2026-08-05T00:00:00Z", "end_time": "2026-09-03T23:59:59Z"},
            "sources": {"crashlytics_bq": {"status": "available"}},
            "kpi": {"crash_events": {"value": 15}, "affected_users": {"value": 8}},
            "daily_trend": [],
            "version_health": [
                {"version": "1.0.8", "status": "active", "crash_events": 10},
                {"version": "1.0.9", "status": "latest", "crash_events": 5},
                # Notice: 1.0.10 is NOT in BigQuery because 0 crashes occurred!
            ],
            "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [{"app_version": "1.0.8"}, {"app_version": "1.0.9"}], "custom_keys": []},
            "top_issues": [
                {
                    "issue_id": "iss_fixed_in_10",
                    "title": "Old Bug",
                    "first_seen_version": "1.0.8",
                    "last_seen_version": "1.0.9",
                    "version_distribution": [
                        {"version": "1.0.8", "events": 10, "users": 5},
                        {"version": "1.0.9", "events": 5, "users": 3},
                    ],
                }
            ],
            "periods": {},
        }

        # Initial BigQuery lifecycle: latest_version was 1.0.9, issue occurred in 1.0.9 => persistent
        enriched_bq = enrich_app_data_with_lifecycle(app_bq, app_name="demo")
        iss_bq = enriched_bq["top_issues"][0]
        self.assertEqual(iss_bq["lifecycle"]["status"], "persistent")
        self.assertEqual(iss_bq["lifecycle"]["latest_version"], "1.0.9")

        # 2. Stage 2: Firebase Sessions runs and injects 1.0.10 with high adoption and sessions
        sessions_result = {
            "sources": {"status": "available", "last_sync_timestamp": "2026-09-03T12:00:00Z"},
            "kpi": {"crash_free_users": {"rate": 0.999, "total": 10000, "crashed": 10}, "crash_free_sessions": {"rate": 0.999, "total": 50000, "crashed": 15}},
            "daily_trend": {},
            "version_health": {
                "1.0.8": {"adoption_rate": 0.1, "sessions_total": 5000, "crash_free_users_rate": 0.98},
                "1.0.9": {"adoption_rate": 0.4, "sessions_total": 20000, "crash_free_users_rate": 0.99},
                "1.0.10": {"adoption_rate": 0.5, "sessions_total": 25000, "crash_free_users_rate": 1.0},
            },
        }

        # Run Sessions enrichment
        enriched_sessions = enrich_app_dashboard_with_sessions(app_bq, sessions_result)

        # 1.0.10 must be materialized into version_health and marked as latest
        v_10 = next((v for v in enriched_sessions["version_health"] if v.get("version") == "1.0.10"), None)
        self.assertIsNotNone(v_10)
        self.assertEqual(v_10.get("status"), "latest")
        self.assertEqual(v_10.get("adoption_rate"), 0.5)

        # Issue lifecycle must now be RESOLVED in 1.0.10!
        iss_after = enriched_sessions["top_issues"][0]
        self.assertEqual(iss_after["lifecycle"]["status"], "resolved")
        self.assertEqual(iss_after["lifecycle"]["latest_version"], "1.0.10")
        self.assertEqual(iss_after["lifecycle"]["confidence"], "high")

    def test_latest_version_from_sessions_materialized(self):
        app_data = {
            "version_health": [{"version": "1.0.0", "status": "active"}],
            "top_issues": [],
            "periods": {},
        }
        sessions_data = {
            "sources": {"status": "available"},
            "version_health": {
                "1.0.0": {"adoption_rate": 0.1, "sessions_total": 100},
                "2.0.0": {"adoption_rate": 0.9, "sessions_total": 9000},
            },
        }
        enriched = enrich_app_dashboard_with_sessions(app_data, sessions_data)
        latest_v = get_latest_app_version(enriched)
        self.assertEqual(latest_v, "2.0.0")


class TestRegressionGapSampleSufficiency(unittest.TestCase):
    """Verifies that intermediate absence gap requires sample sufficiency to declare regressed (Must Fix 3)."""

    def test_intermediate_version_sample_insufficient_not_regressed(self):
        # 1.0.8 (present) -> 1.0.9 (absent, but sample insufficient) -> 1.0.10 (present)
        all_versions = ["1.0.8", "1.0.9", "1.0.10"]
        events = {"1.0.8": 5, "1.0.9": 0, "1.0.10": 2}
        sufficiency = {"1.0.8": True, "1.0.9": False, "1.0.10": True}  # 1.0.9 is NOT sufficient!

        lc = detect_issue_lifecycle(
            issue_id="iss_test",
            historical_versions=["1.0.8", "1.0.10"],
            all_known_versions=all_versions,
            latest_version="1.0.10",
            sample_sufficient=True,
            current_version_events=events,
            known_version_sufficiency=sufficiency,
        )

        # Must NOT be judged as regressed; must be persistent!
        self.assertEqual(lc["status"], "persistent")
        self.assertIsNone(lc["previously_absent_since"])
        self.assertIn("樣本不足", lc["reason"])

    def test_intermediate_version_sample_sufficient_is_regressed(self):
        # 1.0.8 (present) -> 1.0.9 (absent, and sample sufficient) -> 1.0.10 (present)
        all_versions = ["1.0.8", "1.0.9", "1.0.10"]
        events = {"1.0.8": 5, "1.0.9": 0, "1.0.10": 2}
        sufficiency = {"1.0.8": True, "1.0.9": True, "1.0.10": True}  # 1.0.9 IS sufficient!

        lc = detect_issue_lifecycle(
            issue_id="iss_test",
            historical_versions=["1.0.8", "1.0.10"],
            all_known_versions=all_versions,
            latest_version="1.0.10",
            sample_sufficient=True,
            current_version_events=events,
            known_version_sufficiency=sufficiency,
        )

        # Correctly regressed!
        self.assertEqual(lc["status"], "regressed")
        self.assertEqual(lc["previously_absent_since"], "1.0.9")
        self.assertEqual(lc["reappeared_version"], "1.0.10")


class TestCopyPromptStability(unittest.TestCase):
    """Verifies that copyFixPrompt passes issue_id and not filtered index (Must Fix 4)."""

    def test_copy_prompt_by_issue_id_in_html(self):
        bundle = {
            "schema_version": "2.3.0",
            "generated_at": "2026-09-03T12:00:00Z",
            "default_app": "demo",
            "apps": {
                "demo": {
                    "metadata": {"app_id": "demo", "display_name": "Demo", "firebase_project_id": "p", "platforms": ["android"]},
                    "period": {"days": 30, "start_time": "2026-08-05T00:00:00Z", "end_time": "2026-09-03T23:59:59Z"},
                    "sources": {"crashlytics_bq": {"status": "available"}},
                    "kpi": {"crash_events": {"value": 10}, "affected_users": {"value": 5}},
                    "daily_trend": [],
                    "version_health": [{"version": "1.0.10", "status": "latest"}],
                    "distributions": {"platform": [], "device_models": [], "os_versions": [], "app_versions": [], "custom_keys": []},
                    "top_issues": [
                        {
                            "issue_id": "specific_issue_uuid_12345",
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
                            "ai_analysis": None,
                            "detail": None,
                            "lifecycle": None,
                        }
                    ],
                    "ai_summary": {"status": "unavailable", "overview": ""},
                    "periods": {},
                }
            },
        }

        html = build_html(bundle)
        # Verify button template calls copyFixPrompt with iss.issue_id
        self.assertIn("copyFixPrompt('${esc(iss.issue_id)}')", html)
        # Verify copyFixPrompt definition looks up by issueId
        self.assertIn("function copyFixPrompt(issueId)", html)
        self.assertIn("i.issue_id === issueId", html)
        self.assertIn("specific_issue_uuid_12345", html)


class TestSchemaValidationLifecycle(unittest.TestCase):
    """Verifies schema validation rules for IssueLifecycle across top_issues and period snapshots."""

    def setUp(self):
        fixture_path = Path(__file__).resolve().parent / "fixtures" / "dashboard_v2.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.app_data = data["apps"]["shop_app"]

    def test_valid_lifecycle_passes(self):
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

    def test_schema_validation_covers_periods_lifecycle(self):
        """Verifies that invalid lifecycle in period snapshots is caught by validator (Should Fix 6)."""
        app = copy.deepcopy(self.app_data)
        app["periods"] = {
            "30": {
                "period": {"days": 30, "start_time": "2026-08-05T00:00:00Z", "end_time": "2026-09-03T23:59:59Z"},
                "kpi": copy.deepcopy(app["kpi"]),
                "version_health": copy.deepcopy(app["version_health"]),
                "distributions": copy.deepcopy(app["distributions"]),
                "top_issues": [
                    {
                        **copy.deepcopy(app["top_issues"][0]),
                        "lifecycle": {
                            "status": "bogus_status_in_period",  # INVALID!
                            "latest_version": "1.0.10",
                            "first_seen_version": "1.0.8",
                            "last_seen_version": "1.0.10",
                            "versions_seen": 2,
                            "confidence": "high",
                        },
                    }
                ],
            }
        }
        errors = validate_app_dashboard_v2(app)
        self.assertTrue(any("periods['30'].top_issues[0].lifecycle.status must be one of" in e for e in errors))


class TestPlatformIsolation(unittest.TestCase):
    """Verifies strict isolation between Android and iOS version streams and issue lifecycles."""

    def test_platform_isolated_versions_and_lifecycle(self):
        # App with both Android (1.0.8, 1.0.9, 1.0.10) and iOS (2.0.0, 2.1.0)
        app_data: AppDashboardV2Data = {
            "metadata": {"app_id": "shop_app", "display_name": "Shop", "firebase_project_id": "p", "platforms": ["android", "ios"]},
            "period": {"days": 30, "start_time": "2026-08-05T00:00:00Z", "end_time": "2026-09-03T23:59:59Z"},
            "sources": {"crashlytics_bq": {"status": "available"}},
            "kpi": {"crash_events": {"value": 100}, "affected_users": {"value": 50}},
            "daily_trend": [],
            "version_health": [
                {"version": "1.0.8", "platform": "android", "status": "active", "crash_events": 10},
                {"version": "1.0.9", "platform": "android", "status": "active", "crash_events": 20},
                {"version": "1.0.10", "platform": "android", "status": "latest", "crash_events": 15, "adoption_rate": 0.4, "sessions_total": 5000},
                {"version": "2.0.0", "platform": "ios", "status": "active", "crash_events": 10},
                {"version": "2.1.0", "platform": "ios", "status": "latest", "crash_events": 0, "adoption_rate": 0.6, "sessions_total": 8000},
            ],
            "distributions": {
                "platform": [],
                "device_models": [],
                "os_versions": [],
                "app_versions": [
                    {"app_version": "1.0.8", "platform": "android"},
                    {"app_version": "1.0.9", "platform": "android"},
                    {"app_version": "1.0.10", "platform": "android"},
                    {"app_version": "2.0.0", "platform": "ios"},
                    {"app_version": "2.1.0", "platform": "ios"},
                ],
                "custom_keys": [],
            },
            "top_issues": [
                {
                    "issue_id": "android_crash_active",
                    "platform": "android",
                    "title": "NPE in Android Checkout",
                    "first_seen_version": "1.0.8",
                    "last_seen_version": "1.0.10",
                    "version_distribution": [
                        {"version": "1.0.8", "events": 5, "users": 3},
                        {"version": "1.0.9", "events": 5, "users": 3},
                        {"version": "1.0.10", "events": 5, "users": 3},
                    ],
                },
                {
                    "issue_id": "ios_crash_resolved",
                    "platform": "ios",
                    "title": "SIGSEGV in iOS Render",
                    "first_seen_version": "2.0.0",
                    "last_seen_version": "2.0.0",
                    "version_distribution": [
                        {"version": "2.0.0", "events": 10, "users": 5},
                        # 0 events in iOS 2.1.0!
                    ],
                },
            ],
            "periods": {},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            enriched = enrich_app_data_with_lifecycle(app_data, app_name="shop_app", out_dir=Path(tmpdir))
            iss_and = enriched["top_issues"][0]
            iss_ios = enriched["top_issues"][1]

            # Android issue must ONLY be compared against Android latest_version (1.0.10), NOT iOS (2.1.0)!
            self.assertEqual(iss_and["lifecycle"]["latest_version"], "1.0.10")
            self.assertEqual(iss_and["lifecycle"]["status"], "persistent")

            # iOS issue must ONLY be compared against iOS latest_version (2.1.0), NOT Android (1.0.10)!
            self.assertEqual(iss_ios["lifecycle"]["latest_version"], "2.1.0")
            self.assertEqual(iss_ios["lifecycle"]["status"], "resolved")

    def test_platform_same_issue_id_collision_prevention(self):
        """Identical issue IDs across Android and iOS must be maintained distinctly in catalog."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cat_path = Path(tmpdir) / "historical_catalog.json"
            catalog = IssueHistoricalCatalog(cat_path, app_id="multi_platform")

            # Both platforms share the same issue_id string (e.g. from common error hash)
            catalog.update_from_issues([
                {"issue_id": "common_hash_123", "platform": "android", "first_seen_version": "1.0.0", "last_seen_version": "1.2.0"},
                {"issue_id": "common_hash_123", "platform": "ios", "first_seen_version": "2.0.0", "last_seen_version": "2.5.0"},
            ])
            catalog.save()

            # Reload and verify no overwrite
            cat_reloaded = IssueHistoricalCatalog(cat_path, app_id="multi_platform")
            cat_reloaded.load()

            hist_and = cat_reloaded.get_issue_history("common_hash_123", platform="android")
            hist_ios = cat_reloaded.get_issue_history("common_hash_123", platform="ios")

            self.assertIsNotNone(hist_and)
            self.assertIsNotNone(hist_ios)
            self.assertEqual(hist_and["first_seen_version"], "1.0.0")
            self.assertEqual(hist_ios["first_seen_version"], "2.0.0")


class TestHistoricalBootstrap(unittest.TestCase):
    """Verifies true historical bootstrap from disk archives and retention queries."""

    def test_bootstrap_from_disk_archives(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            app_name = "shop_app"

            # 1. Create simulated monthly report: reports/data/shop_app/2026-07.json
            r_dir = root / "reports" / "data" / app_name
            r_dir.mkdir(parents=True)
            report_data = {
                "issues": [
                    {
                        "issue_id": "iss_old_from_july",
                        "platform": "android",
                        "first_seen_version": "0.9.0",
                        "last_seen_version": "0.9.5",
                        "version_distribution": [{"version": "0.9.0", "events": 5, "users": 2}],
                    }
                ]
            }
            (r_dir / "2026-07.json").write_text(json.dumps(report_data), encoding="utf-8")

            # 2. Create simulated unified.json: out/shop_app/unified.json
            out_d = root / "out" / app_name
            out_d.mkdir(parents=True)
            unified_data = {
                "issues": [
                    {
                        "issue_id": "iss_from_unified",
                        "platform": "ios",
                        "first_seen_version": "1.5.0",
                        "last_seen_version": "1.6.0",
                        "version_distribution": [{"version": "1.5.0", "events": 10, "users": 4}],
                    }
                ]
            }
            (out_d / "unified.json").write_text(json.dumps(unified_data), encoding="utf-8")

            # Run bootstrap
            cat = bootstrap_catalog_from_disk(app_name, root_dir=root)

            # Both historical issues must be indexed
            july_iss = cat.get_issue_history("iss_old_from_july", platform="android")
            self.assertIsNotNone(july_iss)
            self.assertEqual(july_iss["first_seen_version"], "0.9.0")

            uni_iss = cat.get_issue_history("iss_from_unified", platform="ios")
            self.assertIsNotNone(uni_iss)
            self.assertEqual(uni_iss["first_seen_version"], "1.5.0")


if __name__ == "__main__":
    unittest.main()
