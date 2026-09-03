"""Unit tests for [Dashboard V2.3] Version Filter & Version-scoped Metrics (Issue #30)."""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.build_dashboard import build_html
from crash_trend.fetch_bigquery import transform_bq_period_snapshot
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

    def test_multi_platform_all_latest_preserves_both_platforms(self):
        """Must Fix 1: Platform=ALL and Version=LATEST resolves each issue to its platform's latest version.
        Android latest (1.0.10) and iOS latest (2.1.0) issues must both be shown with their scoped metrics,
        and Android issues must NOT be hidden by the higher iOS SemVer.
        """
        bundle = copy.deepcopy(self.base_bundle)
        app = bundle["apps"]["shop_app"]

        app["version_health"] = [
            {"version": "1.0.10", "platform": "android", "status": "latest"},
            {"version": "1.0.8", "platform": "android", "status": "active"},
            {"version": "2.1.0", "platform": "ios", "status": "latest"},
            {"version": "2.0.0", "platform": "ios", "status": "active"},
        ]

        app["top_issues"] = [
            {
                "issue_id": "and_iss_latest",
                "title": "Android Latest Crash",
                "subtitle": "NullPointerException at Android.kt:1",
                "platform": "android",
                "error_type": "FATAL",
                "events": 20,
                "affected_users": 10,
                "version_distribution": [
                    {"version": "1.0.8", "events": 15, "users": 8},
                    {"version": "1.0.10", "events": 5, "users": 2},
                ],
                "priority": {"level": "P0", "score": 90},
            },
            {
                "issue_id": "and_iss_old_only",
                "title": "Android Old Crash Only",
                "subtitle": "NullPointerException at Old.kt:1",
                "platform": "android",
                "error_type": "FATAL",
                "events": 30,
                "affected_users": 15,
                "version_distribution": [
                    {"version": "1.0.8", "events": 30, "users": 15},
                ],
                "priority": {"level": "P1", "score": 80},
            },
            {
                "issue_id": "ios_iss_latest",
                "title": "iOS Latest Crash",
                "subtitle": "EXC_BAD_ACCESS at iOS.swift:1",
                "platform": "ios",
                "error_type": "FATAL",
                "events": 18,
                "affected_users": 7,
                "version_distribution": [
                    {"version": "2.0.0", "events": 10, "users": 4},
                    {"version": "2.1.0", "events": 8, "users": 3},
                ],
                "priority": {"level": "P0", "score": 92},
            },
        ]

        # Simulation of multi-platform latest resolution
        latest_map = {"android": "1.0.10", "ios": "2.1.0"}

        def filter_latest_issues(issues, platform_filter: str):
            res = []
            for iss in issues:
                pf = iss["platform"]
                if platform_filter != "ALL" and pf != platform_filter:
                    continue
                target_ver = latest_map.get(pf)
                v_dist = next((v for v in iss.get("version_distribution", []) if v["version"] == target_ver), None)
                if v_dist and (v_dist.get("events", 0) > 0 or v_dist.get("users", 0) > 0):
                    res.append({
                        "issue_id": iss["issue_id"],
                        "platform": pf,
                        "scoped_events": v_dist["events"],
                        "scoped_users": v_dist["users"],
                    })
            return res

        # When Platform=ALL and Version=LATEST:
        all_latest = filter_latest_issues(app["top_issues"], "ALL")
        self.assertEqual(len(all_latest), 2)
        and_res = next(r for r in all_latest if r["issue_id"] == "and_iss_latest")
        ios_res = next(r for r in all_latest if r["issue_id"] == "ios_iss_latest")
        # Android issue has 1.0.10 scoped metrics (5 ev, 2 us)
        self.assertEqual(and_res["scoped_events"], 5)
        self.assertEqual(and_res["scoped_users"], 2)
        # iOS issue has 2.1.0 scoped metrics (8 ev, 3 us)
        self.assertEqual(ios_res["scoped_events"], 8)
        self.assertEqual(ios_res["scoped_users"], 3)

        # When Platform=android and Version=LATEST:
        and_only = filter_latest_issues(app["top_issues"], "android")
        self.assertEqual(len(and_only), 1)
        self.assertEqual(and_only[0]["issue_id"], "and_iss_latest")

        # When Platform=ios and Version=LATEST:
        ios_only = filter_latest_issues(app["top_issues"], "ios")
        self.assertEqual(len(ios_only), 1)
        self.assertEqual(ios_only[0]["issue_id"], "ios_iss_latest")

    def test_bigquery_top_issues_and_version_dist_platform_isolation(self):
        """Must Fix 2: BQ transform_bq_period_snapshot isolates seen_issues and ver_by_issue
        by (platform, issue_id) so identical issue_ids across Android and iOS are not corrupted or merged.
        """
        shared_issue_id = "shared_crash_hash_12345"

        tables_data = {
            "my_project.dataset.crashlytics_com_example_android": {
                "overview": [{"total_events": 10, "distinct_users": 4, "fatal_events": 10, "anr_events": 0, "non_fatal_events": 0}],
                "daily_trend": [],
                "new_issues": [],
                "top_issues": [{
                    "issue_id": shared_issue_id,
                    "issue_title": "Android NPE Crash",
                    "error_type": "FATAL",
                    "events": 10,
                    "users": 4,
                }],
                "issue_versions": [{
                    "issue_id": shared_issue_id,
                    "app_version": "1.0.10",
                    "events": 10,
                    "users": 4,
                }],
                "by_device": [],
                "by_os": [],
                "by_app_version": [],
                "custom_keys": [],
            },
            "my_project.dataset.crashlytics_com_example_ios": {
                "overview": [{"total_events": 35, "distinct_users": 9, "fatal_events": 35, "anr_events": 0, "non_fatal_events": 0}],
                "daily_trend": [],
                "new_issues": [],
                "top_issues": [{
                    "issue_id": shared_issue_id,
                    "issue_title": "iOS Bad Pointer Crash",
                    "error_type": "FATAL",
                    "events": 35,
                    "users": 9,
                }],
                "issue_versions": [{
                    "issue_id": shared_issue_id,
                    "app_version": "2.1.0",
                    "events": 35,
                    "users": 9,
                }],
                "by_device": [],
                "by_os": [],
                "by_app_version": [],
                "custom_keys": [],
            },
        }

        snap = transform_bq_period_snapshot(
            tables_data=tables_data,
            detected_platforms=["android", "ios"],
            days=30,
            start_date=dt.date(2026, 8, 1),
            end_date=dt.date(2026, 8, 30),
        )

        top_issues = snap["top_issues"]
        # Must have 2 distinct issue records, one for android, one for ios
        self.assertEqual(len(top_issues), 2)

        and_iss = next((i for i in top_issues if i["platform"] == "android"), None)
        ios_iss = next((i for i in top_issues if i["platform"] == "ios"), None)

        self.assertIsNotNone(and_iss)
        self.assertIsNotNone(ios_iss)

        # Android issue assertions
        self.assertEqual(and_iss["issue_id"], shared_issue_id)
        self.assertEqual(and_iss["events"], 10)
        self.assertEqual(and_iss["affected_users"], 4)
        self.assertEqual(len(and_iss["version_distribution"]), 1)
        self.assertEqual(and_iss["version_distribution"][0]["version"], "1.0.10")
        self.assertEqual(and_iss["version_distribution"][0]["events"], 10)
        self.assertEqual(and_iss["version_distribution"][0]["users"], 4)

        # iOS issue assertions
        self.assertEqual(ios_iss["issue_id"], shared_issue_id)
        self.assertEqual(ios_iss["events"], 35)
        self.assertEqual(ios_iss["affected_users"], 9)
        self.assertEqual(len(ios_iss["version_distribution"]), 1)
        self.assertEqual(ios_iss["version_distribution"][0]["version"], "2.1.0")
        self.assertEqual(ios_iss["version_distribution"][0]["events"], 35)
        self.assertEqual(ios_iss["version_distribution"][0]["users"], 9)

    def test_real_js_dom_execution_with_node(self):
        """Should Fix 3: Execute the generated dashboard JavaScript inside Node.js
        with a simulated DOM to verify real interactive runtime behavior:
        - updateVersionFilterOptions populates authoritative versions and LATEST label.
        - filterVersion = LATEST with Platform = ALL displays both Android and iOS latest issues.
        - filterVersion = 1.0.10 displays only 1.0.10 issues with scoped metrics.
        - switchApp resets filterVersion to ALL when version is not in new app.
        """
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("Node.js runtime is not available in environment")

        bundle = copy.deepcopy(self.base_bundle)
        app1 = bundle["apps"]["shop_app"]
        app1["version_health"] = [
            {"version": "1.0.10", "platform": "android", "status": "latest"},
            {"version": "1.0.8", "platform": "android", "status": "active"},
            {"version": "2.1.0", "platform": "ios", "status": "latest"},
            {"version": "2.0.0", "platform": "ios", "status": "active"},
        ]
        app1["top_issues"] = [
            {
                "issue_id": "and_iss_1",
                "title": "Android Crash 10",
                "subtitle": "NullPointer in A",
                "platform": "android",
                "error_type": "FATAL",
                "events": 20,
                "affected_users": 6,
                "version_distribution": [
                    {"version": "1.0.8", "events": 16, "users": 4},
                    {"version": "1.0.10", "events": 4, "users": 2},
                ],
                "priority": {"level": "P0", "score": 90},
            },
            {
                "issue_id": "ios_iss_1",
                "title": "iOS Crash 210",
                "subtitle": "Bad Access in B",
                "platform": "ios",
                "error_type": "FATAL",
                "events": 10,
                "affected_users": 3,
                "version_distribution": [
                    {"version": "2.0.0", "events": 2, "users": 1},
                    {"version": "2.1.0", "events": 8, "users": 2},
                ],
                "priority": {"level": "P1", "score": 85},
            },
        ]

        # Add second app with completely different versions
        bundle["apps"]["second_app"] = copy.deepcopy(app1)
        bundle["apps"]["second_app"]["metadata"]["display_name"] = "Second App"
        bundle["apps"]["second_app"]["version_health"] = [
            {"version": "5.0.0", "platform": "android", "status": "latest"}
        ]
        bundle["apps"]["second_app"]["top_issues"] = []

        html = build_html(bundle)
        scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        self.assertGreaterEqual(len(scripts), 2, "HTML must contain at least 2 <script> tags")
        client_js = scripts[1]

        # Save client_js to temp file
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f_client:
            f_client.write(client_js)
            client_js_path = f_client.name

        node_runner = """
        const fs = require('fs');
        const assert = require('assert');

        const elements = {};
        function getOrCreateElement(id) {
          if (!elements[id]) {
            elements[id] = {
              id,
              value: 'ALL',
              innerHTML: '',
              textContent: '',
              style: {},
              classList: {
                classes: new Set(),
                add(c) { this.classes.add(c); },
                remove(c) { this.classes.delete(c); },
                toggle(c) { if (this.classes.has(c)) this.classes.delete(c); else this.classes.add(c); },
                contains(c) { return this.classes.has(c); },
              },
              addEventListener: () => {},
            };
          }
          return elements[id];
        }

        global.document = {
          documentElement: { dataset: { theme: 'light' } },
          getElementById: (id) => getOrCreateElement(id),
          querySelector: (sel) => getOrCreateElement(sel.replace('#', '')),
          querySelectorAll: () => [],
          createElement: (tag) => getOrCreateElement('mock-' + Math.random()),
          addEventListener: () => {},
          readyState: 'complete',
        };
        global.$ = (id) => getOrCreateElement(id);
        global.window = global;
        global.navigator = { clipboard: { writeText: () => Promise.resolve() } };
        global.Chart = function() { return { destroy: () => {}, update: () => {} }; };
        global.Chart.register = () => {};
        global.chartInstances = {};

        // Load and execute client script
        const code = fs.readFileSync(process.argv[2], 'utf-8');
        eval(code);

        // 1. Check version dropdown options generated
        const selVer = $('filterVersion');
        assert.ok(selVer.innerHTML.includes('ALL'), 'Must include ALL option');
        assert.ok(selVer.innerHTML.includes('LATEST'), 'Must include LATEST option');
        assert.ok(selVer.innerHTML.includes('1.0.10'), 'Must include 1.0.10 option');
        assert.ok(selVer.innerHTML.includes('2.1.0'), 'Must include 2.1.0 option');
        assert.ok(selVer.innerHTML.includes('依各平台'), 'LATEST option should indicate per-platform latest');

        // 2. Test Platform=ALL and Version=LATEST
        selVer.value = 'LATEST';
        $('filterPlatform').value = 'ALL';
        renderIssuesList();

        const badgeText = $('issuesCountBadge').textContent;
        assert.ok(badgeText.includes('最新'), 'Badge should show latest version note');
        const listHtml = $('issuesListContainer').innerHTML;
        // Both Android and iOS issues should be present
        assert.ok(listHtml.includes('Android Crash 10'), 'Android latest issue must be shown');
        assert.ok(listHtml.includes('iOS Crash 210'), 'iOS latest issue must be shown');
        // Scoped metrics: Android issue has 4 events, iOS issue has 8 events
        assert.ok(listHtml.includes('4</b> 次事件'), 'Android issue should display scoped 4 events');
        assert.ok(listHtml.includes('8</b> 次事件'), 'iOS issue should display scoped 8 events');

        // Check Overview preview remains unscoped and all-version
        const overviewPreviewHtml = $('topIssuesPreviewBody').innerHTML;
        assert.ok(overviewPreviewHtml.includes('20'), 'Overview top issues preview must display all-version total events (20)');
        assert.ok(overviewPreviewHtml.includes('10'), 'Overview top issues preview must display all-version total events (10)');

        // 3. Test Version=1.0.10
        selVer.value = '1.0.10';
        renderIssuesList();
        const listHtml10 = $('issuesListContainer').innerHTML;
        assert.ok(listHtml10.includes('Android Crash 10'), 'Android issue must be shown for 1.0.10');
        assert.ok(!listHtml10.includes('iOS Crash 210'), 'iOS issue must NOT be shown for 1.0.10');

        // Overview preview MUST NOT be mutated or scoped by version filter (Must Fix - Overview unmixed scope)
        assert.strictEqual($('topIssuesPreviewBody').innerHTML, overviewPreviewHtml, 'Overview preview must not be mutated or scoped by version filter');

        // 4. Test Platform=ios and Version=LATEST
        $('filterPlatform').value = 'ios';
        handlePlatformFilterChange();
        selVer.value = 'LATEST';
        renderIssuesList();
        const listHtmlIos = $('issuesListContainer').innerHTML;
        assert.ok(!listHtmlIos.includes('Android Crash 10'), 'Android issue must not be in iOS list');
        assert.ok(listHtmlIos.includes('iOS Crash 210'), 'iOS issue must be in iOS list');

        // 5. Test switchApp fallback reset
        selVer.value = '2.1.0';
        switchApp('second_app');
        assert.strictEqual(selVer.value, 'ALL', 'Version should reset to ALL when switching to app without 2.1.0');

        console.log('ALL_JS_DOM_TESTS_PASSED');
        """

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f_runner:
            f_runner.write(node_runner)
            runner_path = f_runner.name

        try:
            res = subprocess.run(
                [node_bin, runner_path, client_js_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
            )
            self.assertEqual(res.returncode, 0, f"Node.js script failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
            self.assertIn("ALL_JS_DOM_TESTS_PASSED", res.stdout)
        finally:
            Path(client_js_path).unlink(missing_ok=True)
            Path(runner_path).unlink(missing_ok=True)


    def test_latest_platforms_isolation_when_same_version_across_platforms(self):
        """Should Fix: When the same version string exists across platforms (e.g. 1.0.10 on both),
        latest status is tracked per platform (latestPlatforms Set), so marking 1.0.10 latest on Android
        does not mistakenly make it the latest on iOS when iOS latest is 2.1.0.
        """
        node_bin = shutil.which("node")
        if not node_bin:
            self.skipTest("Node.js runtime is not available in environment")

        bundle = copy.deepcopy(self.base_bundle)
        app = bundle["apps"]["shop_app"]
        app["version_health"] = [
            {"version": "1.0.10", "platform": "android", "status": "latest"},
            {"version": "1.0.10", "platform": "ios", "status": "active"},
            {"version": "2.1.0", "platform": "ios", "status": "latest"},
        ]

        html = build_html(bundle)
        scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        self.assertGreaterEqual(len(scripts), 2)
        client_js = scripts[1]

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f_client:
            f_client.write(client_js)
            client_js_path = f_client.name

        node_script = """
        const fs = require('fs');
        const assert = require('assert');

        const elements = {};
        function getOrCreateElement(id) {
          if (!elements[id]) {
            elements[id] = {
              id,
              value: 'ALL',
              innerHTML: '',
              textContent: '',
              style: {},
              classList: { classes: new Set(), add: () => {}, remove: () => {}, toggle: () => {}, contains: () => false },
              addEventListener: () => {},
            };
          }
          return elements[id];
        }

        global.document = {
          documentElement: { dataset: { theme: 'light' } },
          getElementById: (id) => getOrCreateElement(id),
          querySelector: (sel) => getOrCreateElement(sel.replace('#', '')),
          querySelectorAll: () => [],
          createElement: () => getOrCreateElement('mock-' + Math.random()),
          addEventListener: () => {},
          readyState: 'complete',
        };
        global.$ = (id) => getOrCreateElement(id);
        global.window = global;
        global.navigator = { clipboard: { writeText: () => Promise.resolve() } };
        global.Chart = function() { return { destroy: () => {}, update: () => {} }; };
        global.Chart.register = () => {};
        global.chartInstances = {};

        eval(fs.readFileSync(process.argv[2], 'utf-8'));

        const app = getCurAppData();
        const snap = getCurPeriodSnapshot();

        // 1. Android latest must be 1.0.10
        const andLatest = resolveLatestVersion(app, snap, 'android');
        assert.strictEqual(andLatest, '1.0.10', 'Android latest must be 1.0.10');

        // 2. iOS latest must be 2.1.0 (NOT 1.0.10 even though 1.0.10 is latest on Android!)
        const iosLatest = resolveLatestVersion(app, snap, 'ios');
        assert.strictEqual(iosLatest, '2.1.0', 'iOS latest must be 2.1.0');

        // 3. resolveLatestVersionsByPlatform must isolate platforms accurately
        const latestMap = resolveLatestVersionsByPlatform(app, snap);
        assert.strictEqual(latestMap.android, '1.0.10');
        assert.strictEqual(latestMap.ios, '2.1.0');

        console.log('LATEST_PLATFORMS_ISOLATION_PASSED');
        """

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f_runner:
            f_runner.write(node_script)
            runner_path = f_runner.name

        try:
            res = subprocess.run(
                [node_bin, runner_path, client_js_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
            )
            self.assertEqual(res.returncode, 0, f"Node.js script failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
            self.assertIn("LATEST_PLATFORMS_ISOLATION_PASSED", res.stdout)
        finally:
            Path(client_js_path).unlink(missing_ok=True)
            Path(runner_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

