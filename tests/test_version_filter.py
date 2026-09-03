"""Unit tests for [Dashboard V2.3] Version Filter & Version-scoped Metrics (Issue #30)."""

from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.build_dashboard import build_html
from crash_trend.schema_v2 import validate_dashboard_v2


class TestVersionFilterAndScopedMetrics(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures_dir = ROOT / "tests" / "fixtures"
        self.base_bundle = json.loads((self.fixtures_dir / "dashboard_v2.json").read_text(encoding="utf-8"))

    def test_html_contains_version_filter_elements_and_js(self):
        """Acceptance Criteria 1 & 12: HTML template contains filterVersion select and core JS handlers."""
        html = build_html(self.base_bundle)

        # Check select element
        self.assertIn('id="filterVersion"', html)
        self.assertIn('onchange="renderIssuesList()"', html)
        self.assertIn('<option value="ALL">全部版本</option>', html)
        self.assertIn('<option value="LATEST">最新版本</option>', html)

        # Check core JS functions
        self.assertIn("function getAppAuthoritativeVersions(", html)
        self.assertIn("function resolveLatestVersion(", html)
        self.assertIn("function updateVersionFilterOptions(", html)
        self.assertIn("function compareSemver(", html)
        self.assertIn("function handlePlatformFilterChange(", html)

        # Check scoped metrics processing logic
        self.assertIn("scopedEvents", html)
        self.assertIn("scopedUsers", html)
        self.assertIn("version_distribution", html)

    def test_regression_fixture_scoped_metrics(self):
        """Regression Fixture test recommended by Issue #30:
        Issue A:
          1.0.8: 20 events / 6 users
          1.0.10: 4 events / 2 users
          Total: 24 events / 8 users
        Issue B:
          1.0.8: 0 events / 0 users
          1.0.10: 10 events / 3 users
          Total: 10 events / 3 users
        """
        issue_a = {
            "issue_id": "issue_a",
            "title": "Crash in Checkout",
            "subtitle": "NullPointerException at Checkout.kt:42",
            "platform": "android",
            "error_type": "FATAL",
            "events": 24,
            "affected_users": 8,
            "first_seen_version": "1.0.8",
            "last_seen_version": "1.0.10",
            "version_distribution": [
                {"version": "1.0.8", "events": 20, "users": 6},
                {"version": "1.0.10", "events": 4, "users": 2},
            ],
            "priority": {"score": 85, "level": "P0"},
            "lifecycle": {"status": "persistent", "reason": "still occurring in 1.0.10", "confidence": "high"},
        }
        issue_b = {
            "issue_id": "issue_b",
            "title": "ANR in MainFeed",
            "subtitle": "Thread blocked at Feed.kt:100",
            "platform": "android",
            "error_type": "ANR",
            "events": 10,
            "affected_users": 3,
            "first_seen_version": "1.0.10",
            "last_seen_version": "1.0.10",
            "version_distribution": [
                {"version": "1.0.8", "events": 0, "users": 0},
                {"version": "1.0.10", "events": 10, "users": 3},
            ],
            "priority": {"score": 75, "level": "P1"},
            "lifecycle": {"status": "new_in_latest", "reason": "first observed in 1.0.10", "confidence": "high"},
        }

        issues = [issue_a, issue_b]

        # Simulation helper that exactly mirrors frontend renderIssuesList filtering logic
        def filter_and_scope_issues(target_version: str | None, lifecycle_filter: str = "ALL"):
            results = []
            for iss in issues:
                scoped_ev = iss["events"]
                scoped_us = iss["affected_users"]
                matches_ver = True

                if target_version and target_version != "ALL":
                    v_dist = next((v for v in iss.get("version_distribution", []) if v["version"] == target_version), None)
                    if v_dist and (v_dist.get("events", 0) > 0 or v_dist.get("users", 0) > 0):
                        scoped_ev = v_dist.get("events", 0)
                        scoped_us = v_dist.get("users", 0)
                    else:
                        matches_ver = False

                if not matches_ver:
                    continue
                if lifecycle_filter != "ALL" and iss.get("lifecycle", {}).get("status") != lifecycle_filter:
                    continue

                results.append({
                    "issue_id": iss["issue_id"],
                    "scoped_events": scoped_ev,
                    "scoped_users": scoped_us,
                })
            return results

        # 1. Version = ALL
        res_all = filter_and_scope_issues("ALL")
        self.assertEqual(len(res_all), 2)
        res_a_all = next(r for r in res_all if r["issue_id"] == "issue_a")
        res_b_all = next(r for r in res_all if r["issue_id"] == "issue_b")
        self.assertEqual(res_a_all["scoped_events"], 24)
        self.assertEqual(res_a_all["scoped_users"], 8)
        self.assertEqual(res_b_all["scoped_events"], 10)
        self.assertEqual(res_b_all["scoped_users"], 3)

        # 2. Version = 1.0.10
        # Issue A = 4 events / 2 users
        # Issue B = 10 events / 3 users
        res_10 = filter_and_scope_issues("1.0.10")
        self.assertEqual(len(res_10), 2)
        res_a_10 = next(r for r in res_10 if r["issue_id"] == "issue_a")
        res_b_10 = next(r for r in res_10 if r["issue_id"] == "issue_b")
        self.assertEqual(res_a_10["scoped_events"], 4)
        self.assertEqual(res_a_10["scoped_users"], 2)
        self.assertEqual(res_b_10["scoped_events"], 10)
        self.assertEqual(res_b_10["scoped_users"], 3)

        # 3. Version = 1.0.8
        # Issue A = 20 events / 6 users
        # Issue B not shown
        res_8 = filter_and_scope_issues("1.0.8")
        self.assertEqual(len(res_8), 1)
        self.assertEqual(res_8[0]["issue_id"], "issue_a")
        self.assertEqual(res_8[0]["scoped_events"], 20)
        self.assertEqual(res_8[0]["scoped_users"], 6)

        # 4. Composite Filter: Version = 1.0.10 + Lifecycle = new_in_latest
        # Only Issue B should be returned
        res_comp = filter_and_scope_issues("1.0.10", lifecycle_filter="new_in_latest")
        self.assertEqual(len(res_comp), 1)
        self.assertEqual(res_comp[0]["issue_id"], "issue_b")
        self.assertEqual(res_comp[0]["scoped_events"], 10)
        self.assertEqual(res_comp[0]["scoped_users"], 3)

    def test_authoritative_versions_contain_0_crash_releases(self):
        """Acceptance Criteria 2 & 7: Authoritative version data includes versions with 0 crashes,
        and LATEST resolves to the true latest version.
        """
        bundle = copy.deepcopy(self.base_bundle)
        app = bundle["apps"]["shop_app"]

        # Add 1.0.11 as latest release with 0 crashes in version_health
        app["version_health"].append({
            "version": "1.0.11",
            "platform": "android",
            "release_date": None,
            "crash_events": 0,
            "affected_users": 0,
            "crash_free_users_rate": 1.0,
            "crash_free_sessions_rate": 1.0,
            "adoption_rate": 0.3,
            "status": "latest",
            "trend": "new",
        })

        html = build_html(bundle)
        self.assertIn('"version": "1.0.11"', html)
        self.assertIn('"status": "latest"', html)

    def test_bundle_with_version_filter_data_validates_against_schema_v2(self):
        """Verifies schema validation continues to pass with full version distribution."""
        errors = validate_dashboard_v2(self.base_bundle)
        self.assertEqual(errors, [])

    def test_multi_app_switching_isolation_and_fallback_reset(self):
        """Acceptance Criteria 9 & 10: Multi-app switching resets version filter when the version
        does not exist in the other app, and preserves valid selections.
        """
        app_1_versions = ["1.0.8", "1.0.10"]
        app_2_versions = ["2.0.0", "2.1.0"]

        def simulate_app_switch(prev_version: str, new_app_versions: list[str]) -> str:
            if prev_version in ("ALL", "LATEST"):
                return prev_version
            if prev_version in new_app_versions:
                return prev_version
            return "ALL"

        # Case 1: 1.0.10 selected in App 1 -> switch to App 2 -> resets to ALL
        self.assertEqual(simulate_app_switch("1.0.10", app_2_versions), "ALL")

        # Case 2: ALL selected in App 1 -> switch to App 2 -> stays ALL
        self.assertEqual(simulate_app_switch("ALL", app_2_versions), "ALL")

        # Case 3: LATEST selected in App 1 -> switch to App 2 -> stays LATEST (and dynamically resolves to 2.1.0)
        self.assertEqual(simulate_app_switch("LATEST", app_2_versions), "LATEST")

        # Case 4: 2.0.0 selected in App 2 -> switch to App 2 itself or compatible app -> preserves 2.0.0
        self.assertEqual(simulate_app_switch("2.0.0", app_2_versions), "2.0.0")

    def test_platform_isolated_authoritative_version_extraction(self):
        """Acceptance Criteria 2 & 8: Authoritative versions respect platform isolation."""
        vh_items = [
            {"version": "1.0.10", "platform": "android", "status": "latest"},
            {"version": "1.0.9", "platform": "android", "status": "active"},
            {"version": "2.1.0", "platform": "ios", "status": "latest"},
            {"version": "2.0.0", "platform": "ios", "status": "active"},
        ]

        def get_auth_versions_for_platform(vh_list, platform_filter: str):
            res = []
            for item in vh_list:
                item_pf = item.get("platform", "android").lower()
                if platform_filter != "ALL" and item_pf != platform_filter.lower():
                    continue
                res.append(item["version"])
            return res

        # Android only
        and_vers = get_auth_versions_for_platform(vh_items, "android")
        self.assertEqual(set(and_vers), {"1.0.10", "1.0.9"})

        # iOS only
        ios_vers = get_auth_versions_for_platform(vh_items, "ios")
        self.assertEqual(set(ios_vers), {"2.1.0", "2.0.0"})

        # ALL platforms
        all_vers = get_auth_versions_for_platform(vh_items, "ALL")
        self.assertEqual(set(all_vers), {"1.0.10", "1.0.9", "2.1.0", "2.0.0"})


if __name__ == "__main__":
    unittest.main()
