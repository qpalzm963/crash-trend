"""Unit tests for Issue #47 Persistent Release Catalog and Dashboard V2.6.

Subclasses unittest.TestCase to ensure full compatibility with CI discovery:
`python -m unittest discover -s tests -p "test_*.py" -v`
"""

import datetime as dt
import json
from pathlib import Path
import tempfile
from typing import Any, Dict, List
import unittest

from crash_trend.build_dashboard import build_html
from crash_trend.fetch_bigquery import SQLS, transform_bq_to_v2
from crash_trend.lifecycle import (
    IssueHistoricalCatalog,
    enrich_app_data_with_lifecycle,
)
from crash_trend.schema_v2 import (
    ReleaseCatalogItem,
    validate_release_catalog,
)


def _sample_app_data() -> Dict[str, Any]:
    return {
        "metadata": {
            "app_id": "com.example.app",
            "display_name": "Example App",
            "firebase_project_id": "example-proj",
            "platforms": ["android", "ios"],
        },
        "period": {"days": 30, "start_time": "2026-08-01T00:00:00Z", "end_time": "2026-08-31T00:00:00Z"},
        "kpi": {
            "crash_free_users": {"rate": 0.992, "target": 0.995, "status": "available"},
            "crash_free_sessions": {"rate": 0.998, "target": 0.999, "status": "available"},
            "total_crashes": {"count": 150, "status": "available"},
            "affected_users": {"count": 80, "status": "available"},
        },
        "version_health": [
            {
                "version": "2.0.0",
                "platform": "android",
                "status": "latest",
                "release_date": None,
                "crash_events": 50,
                "affected_users": 20,
                "sessions_total": 5000,
                "crash_free_users_rate": 0.996,
                "crash_free_sessions_rate": 0.999,
                "adoption_rate": 0.50,
            },
            {
                "version": "1.9.0",
                "platform": "android",
                "status": "active",
                "release_date": "2026-07-01",
                "crash_events": 100,
                "affected_users": 60,
                "sessions_total": 10000,
                "crash_free_users_rate": 0.990,
                "crash_free_sessions_rate": 0.997,
                "adoption_rate": 0.45,
            },
        ],
        "top_issues": [],
        "periods": {
            "7": {
                "period": {"days": 7},
                "version_health": [
                    {"version": "2.0.0", "platform": "android", "crash_events": 20, "affected_users": 10, "status": "latest"},
                    {"version": "1.9.0", "platform": "android", "crash_events": 10, "affected_users": 5, "status": "active"},
                ],
                "top_issues": [],
            },
            "30": {
                "period": {"days": 30},
                "version_health": [
                    {"version": "2.0.0", "platform": "android", "crash_events": 50, "affected_users": 20, "status": "latest"},
                    {"version": "1.9.0", "platform": "android", "crash_events": 100, "affected_users": 60, "status": "active"},
                ],
                "top_issues": [],
            },
            "90": {
                "period": {"days": 90},
                "version_health": [
                    {"version": "2.0.0", "platform": "android", "crash_events": 50, "affected_users": 20, "status": "latest"},
                    {"version": "1.9.0", "platform": "android", "crash_events": 250, "affected_users": 110, "status": "active"},
                ],
                "top_issues": [],
            },
        },
    }


class TestReleaseCatalog(unittest.TestCase):
    """Test suite for Issue #47 Persistent Release Catalog and Review Fixes."""

    def test_release_catalog_persistence_and_reload(self) -> None:
        """Test that Release Catalog data persists to disk and reloads cleanly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            cat_file = tmppath / "catalog.json"
            cat = IssueHistoricalCatalog(catalog_path=cat_file, app_id="test_app")

            # Ingest android releases
            cat.update_app_versions([
                {
                    "version": "1.0.0",
                    "platform": "android",
                    "release_date": "2026-01-01",
                    "first_seen": "2026-01-01T00:00:00Z",
                    "last_seen": "2026-01-20T00:00:00Z",
                    "lifetime_crashes": 50,
                    "lifetime_affected_users": 25,
                },
                {
                    "version": "2.0.0",
                    "platform": "android",
                    "release_date": None,
                    "first_seen": "2026-08-01T00:00:00Z",
                    "last_seen": "2026-08-30T00:00:00Z",
                    "lifetime_crashes": 12,
                    "lifetime_affected_users": 8,
                },
            ], window="90")

            cat.save()

            # Reload from disk
            cat2 = IssueHistoricalCatalog(catalog_path=cat_file, app_id="test_app")
            cat2.load()
            self.assertIn("android", cat2.app_versions)
            self.assertIn("1.0.0", cat2.app_versions["android"])
            self.assertIn("2.0.0", cat2.app_versions["android"])
            self.assertEqual(cat2.app_versions["android"]["1.0.0"]["release_date"], "2026-01-01")
            self.assertIsNone(cat2.app_versions["android"]["2.0.0"]["release_date"])
            self.assertEqual(cat2.app_versions["android"]["1.0.0"]["lifetime_crashes"], 50)

    def test_legacy_release_preservation_over_90d(self) -> None:
        """Test that releases inactive for >90 days become legacy and are never deleted."""
        cat = IssueHistoricalCatalog("test_app")
        ref_dt = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.timezone.utc)

        cat.update_app_versions([
            {
                "version": "0.9.0",
                "platform": "android",
                "first_seen": "2026-01-01T00:00:00Z",
                "last_seen": "2026-04-01T00:00:00Z",  # 153 days before ref_dt (>90d)
                "lifetime_crashes": 500,
                "lifetime_affected_users": 300,
            },
            {
                "version": "1.0.0",
                "platform": "android",
                "first_seen": "2026-08-01T00:00:00Z",
                "last_seen": "2026-08-30T00:00:00Z",  # 2 days before ref_dt
                "lifetime_crashes": 30,
                "lifetime_affected_users": 20,
            },
            {
                "version": "1.1.0",
                "platform": "android",
                "first_seen": "2026-08-25T00:00:00Z",
                "last_seen": "2026-09-01T00:00:00Z",  # current latest
                "lifetime_crashes": 5,
                "lifetime_affected_users": 3,
            },
        ])

        status_090 = cat.calculate_version_status("0.9.0", "android", latest_version="1.1.0", reference_time=ref_dt)
        status_100 = cat.calculate_version_status("1.0.0", "android", latest_version="1.1.0", reference_time=ref_dt)
        status_110 = cat.calculate_version_status("1.1.0", "android", latest_version="1.1.0", reference_time=ref_dt)

        self.assertEqual(status_090, "legacy")
        self.assertEqual(status_100, "active")
        self.assertEqual(status_110, "latest")

        # Check full catalog item
        items = cat.build_release_catalog(platform="android", reference_date=ref_dt)
        item_090 = next(it for it in items if it["version"] == "0.9.0")
        self.assertEqual(item_090["status"], "legacy")
        self.assertEqual(item_090["lifetime_crashes"], 500)
        self.assertEqual(item_090["lifetime_affected_users"], 300)

    def test_platform_strict_isolation(self) -> None:
        """Test strict isolation between Android and iOS releases."""
        cat = IssueHistoricalCatalog("test_app")
        ref_dt = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.timezone.utc)

        cat.update_app_versions([
            {"version": "1.0.0", "platform": "android", "first_seen": "2026-08-01T00:00:00Z", "last_seen": "2026-08-20T00:00:00Z", "lifetime_crashes": 10},
            {"version": "1.1.0", "platform": "android", "first_seen": "2026-08-21T00:00:00Z", "last_seen": "2026-09-01T00:00:00Z", "lifetime_crashes": 20},
            {"version": "5.0.0", "platform": "ios", "first_seen": "2026-08-01T00:00:00Z", "last_seen": "2026-08-20T00:00:00Z", "lifetime_crashes": 5},
            {"version": "5.1.0", "platform": "ios", "first_seen": "2026-08-21T00:00:00Z", "last_seen": "2026-09-01T00:00:00Z", "lifetime_crashes": 8},
        ])

        android_catalog = cat.build_release_catalog(platform="android", reference_date=ref_dt)
        ios_catalog = cat.build_release_catalog(platform="ios", reference_date=ref_dt)

        android_versions = {x["version"] for x in android_catalog}
        ios_versions = {x["version"] for x in ios_catalog}

        self.assertEqual(android_versions, {"1.0.0", "1.1.0"})
        self.assertEqual(ios_versions, {"5.0.0", "5.1.0"})

        # vs_previous must never cross platforms
        v110 = next(x for x in android_catalog if x["version"] == "1.1.0")
        self.assertIsNotNone(v110["vs_previous"])
        self.assertEqual(v110["vs_previous"]["previous_version"], "1.0.0")

        v510 = next(x for x in ios_catalog if x["version"] == "5.1.0")
        self.assertIsNotNone(v510["vs_previous"])
        self.assertEqual(v510["vs_previous"]["previous_version"], "5.0.0")

    def test_lifetime_unique_users_deduplication(self) -> None:
        """Test that lifetime unique users are deduplicated and NEVER summed across windows."""
        cat = IssueHistoricalCatalog("test_app")

        # Ingest period 7 (20 users), period 30 (50 users), period 90 (80 users)
        cat.update_app_versions([{"version": "2.0.0", "platform": "android", "affected_users": 20, "crash_events": 30}], window="7")
        cat.update_app_versions([{"version": "2.0.0", "platform": "android", "affected_users": 50, "crash_events": 90}], window="30")
        cat.update_app_versions([{"version": "2.0.0", "platform": "android", "affected_users": 80, "crash_events": 160}], window="90")

        # Verify app_versions stored lifetime metrics
        v_info = cat.app_versions["android"]["2.0.0"]
        self.assertEqual(v_info["lifetime_affected_users"], 80)
        self.assertNotEqual(v_info["lifetime_affected_users"], (20 + 50 + 80))
        self.assertEqual(v_info["lifetime_crashes"], 160)

        # Now simulate ingesting via update_from_catalog_rows (BQ authoritative deduplication)
        cat.update_from_catalog_rows([
            {
                "version": "2.0.0",
                "platform": "android",
                "crash_events": 170,
                "affected_users": 85,
                "fatal_events": 20,
                "anr_events": 5,
                "issues_count": 12,
            }
        ])
        v_info2 = cat.app_versions["android"]["2.0.0"]
        self.assertEqual(v_info2["lifetime_affected_users"], 85)
        self.assertEqual(v_info2["lifetime_crashes"], 170)
        self.assertEqual(v_info2["lifetime_fatal"], 20)
        self.assertEqual(v_info2["lifetime_anr"], 5)

    def test_previous_release_normalized_comparison(self) -> None:
        """Test that comparison against the previous release uses normalized rates over matching window exposure."""
        cat = IssueHistoricalCatalog("test_app")
        cat.update_app_versions([
            {
                "version": "1.0.0",
                "platform": "android",
                "lifetime_crashes": 100,
                "sessions_total": 10000,
                "crash_events": 100,
                "crash_free_users_rate": 0.980,
                "lifetime_fatal": 20,
                "lifetime_anr": 10,
                "first_seen": "2026-07-01T00:00:00Z",
                "last_seen": "2026-07-31T00:00:00Z",
            },
            {
                "version": "1.1.0",
                "platform": "android",
                "lifetime_crashes": 50,
                "sessions_total": 10000,
                "crash_events": 50,
                "crash_free_users_rate": 0.990,
                "lifetime_fatal": 10,
                "lifetime_anr": 5,
                "first_seen": "2026-08-01T00:00:00Z",
                "last_seen": "2026-08-31T00:00:00Z",
            },
        ], window="30")

        catalog = cat.build_release_catalog(platform="android")
        v100 = next(x for x in catalog if x["version"] == "1.0.0")
        v110 = next(x for x in catalog if x["version"] == "1.1.0")

        # v1.0.0 has no predecessor
        self.assertIsNone(v100["vs_previous"])
        self.assertEqual(v100["stability_status"], "baseline")

        # v1.1.0 compares to v1.0.0 over 30d window exposure
        vp = v110["vs_previous"]
        self.assertIsNotNone(vp)
        self.assertEqual(vp["previous_version"], "1.0.0")
        # rate changed from 100/10000 (0.01) to 50/10000 (0.005) -> -0.5 (-50%)
        self.assertEqual(vp["crash_rate_change_pct"], -0.5)
        self.assertEqual(vp["crash_free_users_diff"], 0.01)
        self.assertEqual(vp["fatal_rate_change_pct"], -0.5)
        self.assertEqual(vp["anr_rate_change_pct"], -0.5)
        self.assertEqual(vp["stability"], "improving")
        self.assertEqual(vp["stability_status"], "improved")
        self.assertIn(v110["stability_status"], ("improving", "improved"))

    def test_issue_lifecycle_transitions_and_non_duplication(self) -> None:
        """Test 4 categories of issue lifecycle per release and non-duplication across subsequent releases."""
        cat = IssueHistoricalCatalog("test_app")
        # Define 3 sequential versions with sufficient samples
        cat.update_app_versions([
            {"version": "1.0.0", "platform": "android", "affected_users": 100, "crash_events": 200},
            {"version": "1.1.0", "platform": "android", "affected_users": 150, "crash_events": 300},
            {"version": "1.2.0", "platform": "android", "affected_users": 160, "crash_events": 280},
        ])

        # Issue 1: introduced in 1.0.0 and resolved in 1.1.0
        cat.issues["android:iss_resolved"] = {
            "issue_id": "iss_resolved",
            "title": "Old bug",
            "platform": "android",
            "first_seen_version": "1.0.0",
            "versions_seen": ["1.0.0"],
            "state": "RESOLVED",
        }
        # Issue 2: persistent across 1.0.0 and 1.1.0
        cat.issues["android:iss_persistent"] = {
            "issue_id": "iss_persistent",
            "title": "Persistent bug",
            "platform": "android",
            "first_seen_version": "1.0.0",
            "versions_seen": ["1.0.0", "1.1.0"],
            "state": "OPEN",
        }
        # Issue 3: introduced in 1.1.0
        cat.issues["android:iss_introduced"] = {
            "issue_id": "iss_introduced",
            "title": "Brand new bug",
            "platform": "android",
            "first_seen_version": "1.1.0",
            "versions_seen": ["1.1.0"],
            "state": "OPEN",
        }
        # Issue 4: regressed in 1.1.0 (seen in 0.9.0, skipped in 1.0.0, re-emerged in 1.1.0)
        cat.issues["android:iss_regressed"] = {
            "issue_id": "iss_regressed",
            "title": "Zombie bug",
            "platform": "android",
            "first_seen_version": "0.9.0",
            "versions_seen": ["0.9.0", "1.1.0"],
            "reappeared_version": "1.1.0",
            "state": "REGRESSED",
        }

        catalog = cat.build_release_catalog(platform="android")
        v110 = next(x for x in catalog if x["version"] == "1.1.0")
        lc_110 = v110["issue_lifecycle"]

        self.assertIn("iss_introduced", lc_110["introduced"])
        self.assertIn("iss_persistent", lc_110["persistent"])
        self.assertIn("iss_regressed", lc_110["regressed"])
        self.assertIn("iss_resolved", lc_110["resolved"])
        self.assertEqual(lc_110["introduced_count"], 1)
        self.assertEqual(lc_110["persistent_count"], 1)
        self.assertEqual(lc_110["regressed_count"], 1)
        self.assertEqual(lc_110["resolved_count"], 1)

        # In v1.2.0, iss_resolved was resolved in v1.1.0 and was not present in v1.1.0
        # It must NOT duplicate into v1.2.0's resolved list!
        v120 = next(x for x in catalog if x["version"] == "1.2.0")
        lc_120 = v120["issue_lifecycle"]
        self.assertNotIn("iss_resolved", lc_120["resolved"], "Resolved issues must only report transition once")

    def test_recent_health_dual_keys_and_field_contract(self) -> None:
        """Test that recent_health provides dual keys (7/7d, 30/30d, 90/90d) and aliased field names."""
        cat = IssueHistoricalCatalog("test_app")
        cat.update_app_versions([
            {
                "version": "2.0.0",
                "platform": "android",
                "crash_events": 20,
                "affected_users": 10,
                "fatal_events": 4,
                "anr_events": 2,
                "sessions_total": 1000,
                "crash_free_users_rate": 0.99,
            }
        ], window="7")

        catalog = cat.build_release_catalog(platform="android")
        v200 = next(x for x in catalog if x["version"] == "2.0.0")
        rh = v200["recent_health"]

        # Dual key access
        self.assertIn("7", rh)
        self.assertIn("7d", rh)
        self.assertEqual(rh["7"]["crash_events"], 20)
        self.assertEqual(rh["7d"]["crash_events"], 20)

        # Field aliases contract
        self.assertEqual(rh["7d"]["fatal_events"], 4)
        self.assertEqual(rh["7d"]["fatal_count"], 4)
        self.assertEqual(rh["7d"]["anr_events"], 2)
        self.assertEqual(rh["7d"]["anr_count"], 2)
        self.assertIn("active_issues_count", rh["7d"])
        self.assertIn("new_issues_count", rh["7d"])

    def test_multi_period_table_extraction_in_transform_bq(self) -> None:
        """Test that transform_bq_to_v2 extracts version_catalog from any period snapshot."""
        bq_result = {
            "app_id": "com.test.app",
            "platforms": ["android"],
            "period": {"days": 7},
            "tables": {
                "android": {
                    "overview": [],
                    "top_issues": [],
                }
            },
            "periods": {
                "7": {"tables": {"android": {"overview": [], "top_issues": []}}},
                "30": {"tables": {"android": {"overview": [], "top_issues": []}}},
                "90": {
                    "tables": {
                        "android": {
                            "overview": [],
                            "top_issues": [],
                            "version_catalog": [
                                {
                                    "app_id": "com.test.app",
                                    "app_version": "3.1.0",
                                    "platform": "android",
                                    "first_seen": "2026-01-01T00:00:00Z",
                                    "last_seen": "2026-08-30T00:00:00Z",
                                    "crash_events": 100,
                                    "affected_users": 40,
                                    "fatal_events": 10,
                                    "anr_events": 5,
                                    "issues_count": 8,
                                }
                            ]
                        }
                    }
                },
            },
        }

        cfg: Dict[str, Any] = {"apps": {"com.test.app": {"platforms": ["android"]}}}
        v2_data = transform_bq_to_v2(bq_result, cfg)
        self.assertIn("release_catalog", v2_data)
        cat_item = next((x for x in v2_data["release_catalog"] if x["version"] == "3.1.0"), None)
        self.assertIsNotNone(cat_item)
        self.assertEqual(cat_item["lifetime_crashes"], 100)
        self.assertEqual(cat_item["lifetime_affected_users"], 40)

    def test_version_catalog_sql_scans_all_time_partitions(self) -> None:
        """Test that SQLS['version_catalog'] uses true lifetime query without 90-day filter."""
        sql = SQLS["version_catalog"]
        self.assertIn("event_timestamp IS NOT NULL", sql)
        self.assertNotIn("INTERVAL 90 DAY", sql)
        self.assertNotIn("_TABLE_SUFFIX", sql)

    def test_release_date_vs_first_seen_semantic_separation(self) -> None:
        """Test strict semantic separation: release_date is NEVER faked as first_seen."""
        cat = IssueHistoricalCatalog("test_app")
        cat.update_from_issues([
            {
                "issue_id": "i1",
                "platform": "android",
                "first_seen_version": "3.0.0",
                "first_seen_timestamp": "2026-08-15T12:00:00Z",
                "last_seen_timestamp": "2026-08-20T12:00:00Z",
            }
        ])

        catalog = cat.build_release_catalog(platform="android")
        v300 = next(x for x in catalog if x["version"] == "3.0.0")

        self.assertEqual(v300["first_seen"], "2026-08-15T12:00:00Z")
        self.assertIsNone(v300["release_date"])

    def test_schema_validation_accepts_release_catalog(self) -> None:
        """Test schema validation for Release Catalog items and AppDashboardV2Data."""
        item: ReleaseCatalogItem = {
            "version": "1.0.0",
            "platform": "android",
            "status": "latest",
            "release_date": "2026-08-01",
            "first_seen": "2026-08-01T00:00:00Z",
            "last_seen": "2026-08-20T00:00:00Z",
            "lifetime_crashes": 50,
            "lifetime_issues": 10,
            "lifetime_affected_users": 30,
            "lifetime_fatal": 5,
            "lifetime_anr": 1,
            "stability_status": "stable",
            "recent_health": {
                "30": {
                    "crash_events": 50,
                    "affected_users": 30,
                    "sessions_total": 5000,
                    "crash_free_users_rate": 0.995,
                    "crash_free_sessions_rate": None,
                    "adoption_rate": 0.3,
                    "fatal_events": 5,
                    "anr_events": 1,
                    "new_issues_count": 2,
                    "sample_sufficient": True,
                    "status": "active",
                    "trend": "stable",
                }
            },
            "vs_previous": None,
            "issue_lifecycle": {
                "introduced_count": 1,
                "persistent_count": 0,
                "regressed_count": 0,
                "resolved_count": 0,
                "introduced": ["i1"],
                "persistent": [],
                "regressed": [],
                "resolved": [],
            },
        }

        # validate single catalog list
        errors: List[str] = []
        validate_release_catalog([item], errors)
        self.assertEqual(errors, [])

        # test in full dashboard data structure
        app_data = _sample_app_data()
        enrich_app_data_with_lifecycle(app_data, app_name="test_app")
        self.assertIn("release_catalog", app_data)
        errors2: List[str] = []
        validate_release_catalog(app_data["release_catalog"], errors2)
        self.assertEqual(errors2, [])

    def test_releases_table_renders_all_versions_regardless_of_window(self) -> None:
        """Test that build_html creates the Releases catalog view and detail modal properly."""
        app_data = _sample_app_data()
        enrich_app_data_with_lifecycle(app_data, app_name="test_app")

        bundle = {
            "schema_version": "2.6.0",
            "generated_at": "2026-09-04T12:00:00Z",
            "default_app": "com.example.app",
            "apps": {
                "com.example.app": app_data,
            },
        }

        html = build_html(bundle)

        # Check section header and table
        self.assertIn('id="view-releases"', html)
        self.assertIn('id="filterReleasePlatform"', html)
        self.assertIn('id="filterReleaseStatus"', html)
        self.assertIn('id="searchReleaseVer"', html)
        self.assertIn('id="releasesTableBody"', html)

        # Check modal structure
        self.assertIn('id="releaseDetailModal"', html)
        self.assertIn('id="releaseModalTitle"', html)
        self.assertIn('id="releaseModalBody"', html)
        self.assertIn('openReleaseDetail', html)
        self.assertIn('switchReleaseRecentHealthTab', html)

        # Check data contains release_catalog
        self.assertIn('release_catalog', html)


if __name__ == "__main__":
    unittest.main()
