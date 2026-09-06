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
from crash_trend.fetch_bigquery import SQLS, build_version_catalog_sql, transform_bq_to_v2
from crash_trend.lifecycle import (
    IssueHistoricalCatalog,
    enrich_app_data_with_lifecycle,
    get_latest_app_version,
    should_trigger_catalog_bootstrap,
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

    def test_version_catalog_incremental_query_and_bootstrap_separation(self) -> None:
        """Test that version_catalog uses incremental query by default and bootstrap for cold starts (Review 5124070522)."""
        # Incremental template: uses bounded catalog_days filter, does NOT do full-scan, NO LIMIT 500
        inc_sql = SQLS["version_catalog"]
        self.assertIn("DATE_SUB(CURRENT_DATE(), INTERVAL {catalog_days} DAY)", inc_sql)
        self.assertNotIn("WHERE event_timestamp IS NOT NULL", inc_sql)
        self.assertNotIn("LIMIT 500", inc_sql)

        # Bootstrap template: full historical scan for initial bootstrap, NO LIMIT 500, includes installation_ids
        boot_sql = SQLS["version_catalog_bootstrap"]
        self.assertIn("WHERE event_timestamp IS NOT NULL", boot_sql)
        self.assertNotIn("DATE_SUB", boot_sql)
        self.assertNotIn("LIMIT 500", boot_sql)
        self.assertIn("ARRAY_AGG(DISTINCT installation_uuid IGNORE NULLS) AS installation_ids", boot_sql)

        # Base version_catalog template includes installation_ids
        self.assertIn("ARRAY_AGG(DISTINCT installation_uuid IGNORE NULLS) AS installation_ids", SQLS["version_catalog"])

        # Dynamic builder handles bootstrap, watermark, and catalog_days with strict > and installation_ids
        custom_inc = build_version_catalog_sql("my_table", watermark="2026-08-01T00:00:00Z")
        self.assertIn("event_timestamp > TIMESTAMP('2026-08-01T00:00:00Z')", custom_inc)
        self.assertNotIn("LIMIT 500", custom_inc)
        self.assertIn("ARRAY_AGG(DISTINCT installation_uuid IGNORE NULLS) AS installation_ids", custom_inc)

        custom_boot = build_version_catalog_sql("my_table", is_bootstrap=True)
        self.assertIn("WHERE event_timestamp IS NOT NULL", custom_boot)
        self.assertIn("ARRAY_AGG(DISTINCT installation_uuid IGNORE NULLS) AS installation_ids", custom_boot)

        # Watermark persistence in IssueHistoricalCatalog
        with tempfile.TemporaryDirectory() as tmpdir:
            c_file = Path(tmpdir) / "catalog.json"
            cat = IssueHistoricalCatalog(c_file, app_id="test_app")
            cat.watermark = "2026-08-15T00:00:00Z"
            cat.save()

            cat2 = IssueHistoricalCatalog(c_file, app_id="test_app")
            cat2.load()
            self.assertEqual(cat2.watermark, "2026-08-15T00:00:00Z")

    def test_bootstrap_then_incremental_lifetime_accumulation_and_watermark(self) -> None:
        """Test bootstrap -> incremental run #1 -> incremental run #2 verifying:
        - Lifetime crashes accumulate across incremental syncs
        - Duplicate installations across runs are deduplicated (never double-counted)
        - Watermark advances forward on each batch
        - Full persistence and reload preserve exact state (Review 5124094818).
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            cat_file = Path(tmpdir) / "historical_catalog.json"
            cat = IssueHistoricalCatalog(cat_file, app_id="test_app")

            # 1. Bootstrap run: cold-start with 1000 crashes, 50 fatal, 10 ANR, 500 installations
            inst_boot = [f"uuid_{i}" for i in range(500)]
            cat.update_from_catalog_rows([
                {
                    "app_version": "1.0.0",
                    "platform": "android",
                    "crash_events": 1000,
                    "fatal_events": 50,
                    "anr_events": 10,
                    "installation_ids": inst_boot,
                    "first_seen": "2026-08-01T00:00:00Z",
                    "last_seen": "2026-08-01T12:00:00Z",
                    "issues_count": 5,
                }
            ], is_incremental=False)
            cat.save()

            v_boot = cat.app_versions["android"]["1.0.0"]
            self.assertEqual(v_boot["lifetime_crashes"], 1000)
            self.assertEqual(v_boot["lifetime_fatal"], 50)
            self.assertEqual(v_boot["lifetime_anr"], 10)
            self.assertEqual(v_boot["lifetime_affected_users"], 500)
            self.assertEqual(cat.watermark, "2026-08-01T12:00:00Z")

            # 2. Incremental run #1: 10 new crashes, 2 fatal, 1 ANR
            # 3 installations: uuid_1 & uuid_2 are returning (duplicates), uuid_500 is new
            inst_inc1 = ["uuid_1", "uuid_2", "uuid_500"]
            cat.update_from_catalog_rows([
                {
                    "app_version": "1.0.0",
                    "platform": "android",
                    "crash_events": 10,
                    "fatal_events": 2,
                    "anr_events": 1,
                    "installation_ids": inst_inc1,
                    "last_seen": "2026-08-10T15:00:00Z",
                }
            ], is_incremental=True)
            cat.save()

            v_inc1 = cat.app_versions["android"]["1.0.0"]
            # Lifetime crashes accumulate: 1000 + 10 = 1010
            self.assertEqual(v_inc1["lifetime_crashes"], 1010)
            self.assertEqual(v_inc1["lifetime_fatal"], 52)
            self.assertEqual(v_inc1["lifetime_anr"], 11)
            # Unique users deduplicated: only uuid_500 is new -> 501 (not 500 + 3 = 503)
            self.assertEqual(v_inc1["lifetime_affected_users"], 501)
            # Watermark advanced
            self.assertEqual(cat.watermark, "2026-08-10T15:00:00Z")

            # 2.1. Replay run #1 (boundary idempotency check):
            # Same batch re-executed at watermark boundary (last_seen <= watermark).
            # Must NOT double-increment lifetime crashes, fatal, or anr!
            cat.update_from_catalog_rows([
                {
                    "app_version": "1.0.0",
                    "platform": "android",
                    "crash_events": 10,
                    "fatal_events": 2,
                    "anr_events": 1,
                    "installation_ids": inst_inc1,
                    "last_seen": "2026-08-10T15:00:00Z",
                }
            ], is_incremental=True)
            cat.save()

            v_replay = cat.app_versions["android"]["1.0.0"]
            self.assertEqual(v_replay["lifetime_crashes"], 1010, "Replayed batch must not double-count crashes")
            self.assertEqual(v_replay["lifetime_fatal"], 52, "Replayed batch must not double-count fatal")
            self.assertEqual(v_replay["lifetime_anr"], 11, "Replayed batch must not double-count anr")
            self.assertEqual(v_replay["lifetime_affected_users"], 501)
            self.assertEqual(cat.watermark, "2026-08-10T15:00:00Z")

            # 3. Incremental run #2: 5 new crashes, 1 fatal, 0 ANR
            # 2 installations: uuid_500 is returning from run #1, uuid_501 is new
            inst_inc2 = ["uuid_500", "uuid_501"]
            cat.update_from_catalog_rows([
                {
                    "app_version": "1.0.0",
                    "platform": "android",
                    "crash_events": 5,
                    "fatal_events": 1,
                    "anr_events": 0,
                    "installation_ids": inst_inc2,
                    "last_seen": "2026-08-20T18:00:00Z",
                }
            ], is_incremental=True)
            cat.save()

            v_inc2 = cat.app_versions["android"]["1.0.0"]
            # Lifetime crashes accumulate: 1010 + 5 = 1015
            self.assertEqual(v_inc2["lifetime_crashes"], 1015)
            self.assertEqual(v_inc2["lifetime_fatal"], 53)
            self.assertEqual(v_inc2["lifetime_anr"], 11)
            # Unique users deduplicated: only uuid_501 is new -> 502
            self.assertEqual(v_inc2["lifetime_affected_users"], 502)
            # Watermark advanced
            self.assertEqual(cat.watermark, "2026-08-20T18:00:00Z")

            # 4. Reload from disk into a brand new catalog instance
            cat_reloaded = IssueHistoricalCatalog(cat_file, app_id="test_app")
            cat_reloaded.load()
            self.assertEqual(cat_reloaded.watermark, "2026-08-20T18:00:00Z")
            v_reloaded = cat_reloaded.app_versions["android"]["1.0.0"]
            self.assertEqual(v_reloaded["lifetime_crashes"], 1015)
            self.assertEqual(v_reloaded["lifetime_fatal"], 53)
            self.assertEqual(v_reloaded["lifetime_anr"], 11)
            self.assertEqual(v_reloaded["lifetime_affected_users"], 502)

    def test_latest_version_decoupled_from_windowed_crash_ranking(self) -> None:
        """Test that persistent catalog 2.0.0 is marked as latest even if 30d Top-N only has 1.9.0 (Review 5124070522)."""
        cat = IssueHistoricalCatalog("test_app")
        cat.update_app_versions([
            {"version": "1.9.0", "platform": "android", "crash_events": 100, "lifetime_crashes": 100},
            {"version": "2.0.0", "platform": "android", "crash_events": 0, "lifetime_crashes": 0},
        ])

        # Current 30d window only has 1.9.0, falsely labeling it 'latest' because it has top crash count
        app_data = {
            "version_health": [
                {"version": "1.9.0", "platform": "android", "crash_events": 100, "status": "latest"},
            ],
            "distributions": {
                "app_versions": [
                    {"app_version": "1.9.0", "platform": "android", "events": 100},
                ]
            },
            "periods": {},
        }

        # get_latest_app_version must resolve 2.0.0 from authoritative catalog, NOT 1.9.0 from windowed crash ranking
        latest = get_latest_app_version(app_data, platform="android", catalog=cat)
        self.assertEqual(latest, "2.0.0")

        # build_release_catalog must also mark 2.0.0 as latest
        catalog = cat.build_release_catalog(app_data=app_data, platform="android")
        v200 = next(x for x in catalog if x["version"] == "2.0.0")
        v190 = next(x for x in catalog if x["version"] == "1.9.0")
        self.assertEqual(v200["status"], "latest")
        self.assertEqual(v190["status"], "active")

    def test_release_regression_requires_proven_absence_sample_sufficiency(self) -> None:
        """Test that intermediate release without sufficient sample does NOT falsely trigger regression (Review 5124070522)."""
        cat = IssueHistoricalCatalog("test_app")
        # 1.0.0: sufficient sample
        # 1.1.0: insufficient sample (only 2 crashes, no sessions)
        # 1.2.0: sufficient sample
        cat.update_app_versions([
            {"version": "1.0.0", "platform": "android", "crash_events": 100, "sessions_total": 5000, "sample_sufficient": True},
            {"version": "1.1.0", "platform": "android", "crash_events": 2, "sessions_total": 50, "sample_sufficient": False},
            {"version": "1.2.0", "platform": "android", "crash_events": 80, "sessions_total": 4000, "sample_sufficient": True},
        ])

        # Issue was seen in 1.0.0 and 1.2.0, absent in 1.1.0
        cat.issues["android:iss_test"] = {
            "issue_id": "iss_test",
            "platform": "android",
            "first_seen_version": "1.0.0",
            "versions_seen": ["1.0.0", "1.2.0"],
        }

        catalog = cat.build_release_catalog(platform="android")
        v120 = next(x for x in catalog if x["version"] == "1.2.0")
        lc_120 = v120["issue_lifecycle"]

        # Because 1.1.0 had insufficient sample, it could not prove absence; hence issue is persistent, NOT regressed!
        self.assertNotIn("iss_test", lc_120["regressed"])
        self.assertIn("iss_test", lc_120["persistent"])

        # Now simulate 1.1.0 having sufficient sample
        cat.app_versions["android"]["1.1.0"]["sample_sufficient"] = True
        cat.app_versions["android"]["1.1.0"]["crash_events"] = 500
        cat.app_versions["android"]["1.1.0"]["sessions_total"] = 20000

        catalog2 = cat.build_release_catalog(platform="android")
        v120_new = next(x for x in catalog2 if x["version"] == "1.2.0")
        lc_120_new = v120_new["issue_lifecycle"]

        # Now with 1.1.0 proven absent, 1.2.0 correctly triggers regression!
        self.assertIn("iss_test", lc_120_new["regressed"])
        self.assertNotIn("iss_test", lc_120_new["persistent"])

    def test_fatal_anr_rate_change_normalized_and_none_without_exposure(self) -> None:
        """Test that fatal/ANR comparison computes rate over sessions, and returns None without exposure (Review 5124070522)."""
        cat = IssueHistoricalCatalog("test_app")
        # Case A: with sessions exposure
        cat.update_app_versions([
            {
                "version": "1.0.0",
                "platform": "android",
                "sessions_total": 10000,
                "fatal_events": 20,
                "anr_events": 10,
                "crash_events": 100,
            },
            {
                "version": "1.1.0",
                "platform": "android",
                "sessions_total": 10000,
                "fatal_events": 10,
                "anr_events": 5,
                "crash_events": 50,
            },
        ], window="30")

        cat_items = cat.build_release_catalog(platform="android")
        v110 = next(x for x in cat_items if x["version"] == "1.1.0")
        vp = v110["vs_previous"]
        self.assertIsNotNone(vp)
        self.assertEqual(vp["fatal_rate_change_pct"], -0.5)
        self.assertEqual(vp["anr_rate_change_pct"], -0.5)

        # Case B: without sessions exposure (e.g. sessions_total is None or 0)
        cat_no_exp = IssueHistoricalCatalog("test_app2")
        cat_no_exp.update_app_versions([
            {
                "version": "1.0.0",
                "platform": "android",
                "sessions_total": None,
                "lifetime_fatal": 20,
                "lifetime_anr": 10,
                "fatal_events": 20,
                "anr_events": 10,
            },
            {
                "version": "1.1.0",
                "platform": "android",
                "sessions_total": None,
                "lifetime_fatal": 10,
                "lifetime_anr": 5,
                "fatal_events": 10,
                "anr_events": 5,
            },
        ])
        cat_items_no_exp = cat_no_exp.build_release_catalog(platform="android")
        v110_no_exp = next(x for x in cat_items_no_exp if x["version"] == "1.1.0")
        vp_no_exp = v110_no_exp["vs_previous"]
        self.assertIsNotNone(vp_no_exp)
        self.assertIsNone(vp_no_exp["fatal_rate_change_pct"])
        self.assertIsNone(vp_no_exp["anr_rate_change_pct"])
        self.assertIsNone(vp_no_exp["fatal_change_pct"])
        self.assertIsNone(vp_no_exp["anr_change_pct"])

    def test_fatal_anr_fallback_returns_none_when_matching_window_missing(self) -> None:
        """Test that when sessions_total exists but no matching recent_health window fatal/anr exists,
        fallback does NOT divide lifetime_fatal/lifetime_anr by sessions, returning None (Review 5124094818)."""
        cat = IssueHistoricalCatalog("test_app_fallback")
        # 1.0.0 and 1.1.0 have top-level sessions_total, but NO window recent_health fatal_events
        cat.update_app_versions([
            {
                "version": "1.0.0",
                "platform": "android",
                "sessions_total": 10000,
                "crash_events": 100,
                "lifetime_fatal": 20,
                "lifetime_anr": 10,
                # No fatal_events / fatal_count provided
            },
            {
                "version": "1.1.0",
                "platform": "android",
                "sessions_total": 12000,
                "crash_events": 80,
                "lifetime_fatal": 10,
                "lifetime_anr": 5,
                # No fatal_events / fatal_count provided
            },
        ])
        catalog = cat.build_release_catalog(platform="android")
        v110 = next(x for x in catalog if x["version"] == "1.1.0")
        vp = v110["vs_previous"]
        self.assertIsNotNone(vp)
        self.assertIsNotNone(vp["crash_rate_change_pct"])
        self.assertIsNone(vp["fatal_rate_change_pct"])
        self.assertIsNone(vp["anr_rate_change_pct"])
        self.assertIsNone(vp["fatal_change_pct"])
        self.assertIsNone(vp["anr_change_pct"])

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

    def test_transform_bq_to_v2_incremental_production_wiring_and_idempotency(self) -> None:
        """Test transform_bq_to_v2 integration with bootstrap -> incremental run #1 -> replay -> run #2:
        - BigQuery queries provide installation_ids for deduplication
        - Production wiring propagates is_incremental and advances watermark
        - Replay at watermark boundary is idempotent
        """
        app_cfg = {"app_id": "test_app", "platforms": ["android"]}

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # 1. Bootstrap run (cold-start)
            inst_boot = [f"uuid_{i}" for i in range(500)]
            bq_boot = {
                "tables": {
                    "android": {
                        "overview": [],
                        "top_issues": [],
                        "version_catalog": [
                            {
                                "app_version": "1.0.0",
                                "platform": "android",
                                "first_seen": "2026-08-01T00:00:00Z",
                                "last_seen": "2026-08-01T12:00:00Z",
                                "crash_events": 1000,
                                "affected_users": 500,
                                "installation_ids": inst_boot,
                                "fatal_events": 50,
                                "anr_events": 10,
                                "issues_count": 5,
                            }
                        ],
                    }
                }
            }

            v2_boot = transform_bq_to_v2(bq_boot, app_cfg, is_bootstrap=True, out_dir=tmppath)
            cat_boot_item = next(x for x in v2_boot["release_catalog"] if x["version"] == "1.0.0")
            self.assertEqual(cat_boot_item["lifetime_crashes"], 1000)
            self.assertEqual(cat_boot_item["lifetime_affected_users"], 500)
            self.assertEqual(cat_boot_item["lifetime_fatal"], 50)
            self.assertEqual(cat_boot_item["lifetime_anr"], 10)

            # Verify watermark on disk
            cat_file = tmppath / "test_app" / "historical_catalog.json"
            self.assertTrue(cat_file.is_file())
            cat_disk = json.loads(cat_file.read_text(encoding="utf-8"))
            self.assertEqual(cat_disk["watermark"], "2026-08-01T12:00:00Z")

            # 2. Incremental run #1: 10 new crashes, 2 fatal, 1 ANR
            # 3 installations: uuid_1 & uuid_2 are returning, uuid_500 is new
            inst_inc1 = ["uuid_1", "uuid_2", "uuid_500"]
            bq_inc1 = {
                "tables": {
                    "android": {
                        "overview": [],
                        "top_issues": [],
                        "version_catalog": [
                            {
                                "app_version": "1.0.0",
                                "platform": "android",
                                "first_seen": "2026-08-01T00:00:00Z",
                                "last_seen": "2026-08-10T15:00:00Z",
                                "crash_events": 10,
                                "affected_users": 3,
                                "installation_ids": inst_inc1,
                                "fatal_events": 2,
                                "anr_events": 1,
                                "issues_count": 5,
                            }
                        ],
                    }
                }
            }

            v2_inc1 = transform_bq_to_v2(bq_inc1, app_cfg, is_incremental=True, out_dir=tmppath)
            cat_inc1_item = next(x for x in v2_inc1["release_catalog"] if x["version"] == "1.0.0")
            self.assertEqual(cat_inc1_item["lifetime_crashes"], 1010)
            self.assertEqual(cat_inc1_item["lifetime_affected_users"], 501)
            self.assertEqual(cat_inc1_item["lifetime_fatal"], 52)
            self.assertEqual(cat_inc1_item["lifetime_anr"], 11)

            cat_disk = json.loads(cat_file.read_text(encoding="utf-8"))
            self.assertEqual(cat_disk["watermark"], "2026-08-10T15:00:00Z")

            # 3. Replay run #1 (idempotency check at watermark boundary)
            v2_replay = transform_bq_to_v2(bq_inc1, app_cfg, is_incremental=True, out_dir=tmppath)
            cat_replay_item = next(x for x in v2_replay["release_catalog"] if x["version"] == "1.0.0")
            self.assertEqual(cat_replay_item["lifetime_crashes"], 1010, "Idempotent replay must not double count crashes")
            self.assertEqual(cat_replay_item["lifetime_affected_users"], 501, "Idempotent replay must not double count users")
            self.assertEqual(cat_replay_item["lifetime_fatal"], 52)
            self.assertEqual(cat_replay_item["lifetime_anr"], 11)

            cat_disk = json.loads(cat_file.read_text(encoding="utf-8"))
            self.assertEqual(cat_disk["watermark"], "2026-08-10T15:00:00Z")

            # 4. Incremental run #2: 5 new crashes, 1 fatal, 0 ANR
            # 2 installations: uuid_500 is returning, uuid_501 is new
            inst_inc2 = ["uuid_500", "uuid_501"]
            bq_inc2 = {
                "tables": {
                    "android": {
                        "overview": [],
                        "top_issues": [],
                        "version_catalog": [
                            {
                                "app_version": "1.0.0",
                                "platform": "android",
                                "first_seen": "2026-08-01T00:00:00Z",
                                "last_seen": "2026-08-20T18:00:00Z",
                                "crash_events": 5,
                                "affected_users": 2,
                                "installation_ids": inst_inc2,
                                "fatal_events": 1,
                                "anr_events": 0,
                                "issues_count": 5,
                            }
                        ],
                    }
                }
            }

            v2_inc2 = transform_bq_to_v2(bq_inc2, app_cfg, is_incremental=True, out_dir=tmppath)
            cat_inc2_item = next(x for x in v2_inc2["release_catalog"] if x["version"] == "1.0.0")
            self.assertEqual(cat_inc2_item["lifetime_crashes"], 1015)
            self.assertEqual(cat_inc2_item["lifetime_affected_users"], 502)
            self.assertEqual(cat_inc2_item["lifetime_fatal"], 53)
            self.assertEqual(cat_inc2_item["lifetime_anr"], 11)

            cat_disk = json.loads(cat_file.read_text(encoding="utf-8"))
            self.assertEqual(cat_disk["watermark"], "2026-08-20T18:00:00Z")

    def test_transform_simultaneous_lifecycle_and_version_catalog_watermark_order(self) -> None:
        """Regression test for Review 5124155467 Blocker 1:
        When transform_bq_to_v2 receives BOTH lifecycle_catalog (90d issue rows)
        and version_catalog (incremental version rows) with old watermark = T0 and new data to T1:
        - lifecycle_catalog MUST NOT pre-advance watermark or cause version_catalog rows to be skipped
        - Lifetime crashes, fatal, anr, and users accumulate accurately from version_catalog
        - Watermark on disk advances to T1 after successful ingestion
        """
        app_cfg = {"app_id": "test_app", "platforms": ["android"]}

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            cat_file = tmppath / "test_app" / "historical_catalog.json"
            cat = IssueHistoricalCatalog(catalog_path=cat_file, app_id="test_app")

            # Setup baseline catalog with T0 watermark
            t0 = "2026-08-01T00:00:00Z"
            t1 = "2026-08-10T15:00:00Z"
            inst_boot = [f"uuid_{i}" for i in range(500)]
            cat.update_from_catalog_rows([
                {
                    "app_version": "1.0.0",
                    "platform": "android",
                    "first_seen": "2026-07-01T00:00:00Z",
                    "last_seen": t0,
                    "crash_events": 1000,
                    "fatal_events": 50,
                    "anr_events": 10,
                    "installation_ids": inst_boot,
                    "issues_count": 5,
                }
            ], is_incremental=False, advance_watermark=True)
            cat.save()

            self.assertEqual(cat.watermark, t0)
            self.assertEqual(cat.app_versions["android"]["1.0.0"]["lifetime_crashes"], 1000)

            # Prepare bq_result containing BOTH lifecycle_catalog (issue rows) and version_catalog (version rows)
            # Both have latest data at T1
            bq_result = {
                "tables": {
                    "android": {
                        "overview": [],
                        "top_issues": [],
                        "lifecycle_catalog": [
                            {
                                "issue_id": "iss_test_1",
                                "app_version": "1.0.0",
                                "first_seen_timestamp": "2026-07-15T00:00:00Z",
                                "last_seen_timestamp": t1,
                                "events": 5,
                                "users": 3,
                            }
                        ],
                        "version_catalog": [
                            {
                                "app_version": "1.0.0",
                                "platform": "android",
                                "first_seen": "2026-07-01T00:00:00Z",
                                "last_seen": t1,
                                "crash_events": 10,
                                "fatal_events": 2,
                                "anr_events": 1,
                                "affected_users": 2,
                                "installation_ids": ["uuid_1", "uuid_500"],  # uuid_1 dupe, uuid_500 new
                                "issues_count": 5,
                            }
                        ],
                    }
                }
            }

            # Ingest in incremental mode
            v2_data = transform_bq_to_v2(bq_result, app_cfg, is_incremental=True, out_dir=tmppath)
            cat_item = next(x for x in v2_data["release_catalog"] if x["version"] == "1.0.0")

            # Lifetime counts must accumulate (NOT be skipped due to lifecycle_catalog advancing watermark first!)
            self.assertEqual(cat_item["lifetime_crashes"], 1010, "Lifetime crashes must accumulate from 1000 to 1010")
            self.assertEqual(cat_item["lifetime_fatal"], 52, "Lifetime fatal must accumulate from 50 to 52")
            self.assertEqual(cat_item["lifetime_anr"], 11, "Lifetime anr must accumulate from 10 to 11")
            self.assertEqual(cat_item["lifetime_affected_users"], 501, "Unique users must deduplicate from 500 to 501")

            # Watermark on disk must advance to T1
            cat_disk = json.loads(cat_file.read_text(encoding="utf-8"))
            self.assertEqual(cat_disk["watermark"], t1, "Watermark must advance to T1 after successful version_catalog ingestion")

    def test_legacy_catalog_without_watermark_or_installation_ids_triggers_bootstrap(self) -> None:
        """Regression test for Review 5124155467 Blocker 2:
        Pre-watermark or pre-installation_ids historical_catalog.json must trigger
        full historical bootstrap (SQLS['version_catalog_bootstrap']), NOT 90-day rolling window query.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            cat_file = tmppath / "test_app" / "historical_catalog.json"
            cat_file.parent.mkdir(parents=True, exist_ok=True)

            # Case A: Legacy catalog on disk without watermark or installation_ids
            legacy_catalog_data = {
                "schema_version": "2.0.0",
                "updated_at": "2026-07-01T00:00:00Z",
                "app_versions": {
                    "android": {
                        "1.0.0": {
                            "version": "1.0.0",
                            "crash_events": 100,
                            "affected_users": 50,
                            "status": "active",
                            "sample_sufficient": True,
                            "last_updated": "2026-07-01T00:00:00Z",
                        }
                    }
                },
                "issues": {},
            }
            cat_file.write_text(json.dumps(legacy_catalog_data), encoding="utf-8")

            is_boot, wm = should_trigger_catalog_bootstrap(
                cat_data=legacy_catalog_data,
                cat_file_exists=True,
                explicit_bootstrap=False,
            )
            self.assertTrue(is_boot, "Legacy catalog without watermark must trigger bootstrap")
            self.assertIsNone(wm)

            # Case B: Catalog has a watermark, but lacks installation_ids authority state
            cat_with_wm_no_ids = {
                "schema_version": "2.3.0",
                "watermark": "2026-08-01T00:00:00Z",
                "app_versions": {
                    "android": {
                        "1.0.0": {
                            "version": "1.0.0",
                            "crash_events": 100,
                            "affected_users": 50,
                            # installation_ids is missing
                        }
                    }
                },
                "issues": {},
            }
            is_boot_b, wm_b = should_trigger_catalog_bootstrap(
                cat_data=cat_with_wm_no_ids,
                cat_file_exists=True,
                explicit_bootstrap=False,
            )
            self.assertTrue(is_boot_b, "Catalog with watermark but missing installation_ids must trigger bootstrap")
            self.assertIsNone(wm_b)

            # Case C: Modern catalog with both watermark and installation_ids
            modern_catalog = {
                "schema_version": "2.3.0",
                "watermark": "2026-08-01T00:00:00Z",
                "app_versions": {
                    "android": {
                        "1.0.0": {
                            "version": "1.0.0",
                            "crash_events": 100,
                            "affected_users": 2,
                            "installation_ids": ["uuid_1", "uuid_2"],
                        }
                    }
                },
                "issues": {},
            }
            is_boot_c, wm_c = should_trigger_catalog_bootstrap(
                cat_data=modern_catalog,
                cat_file_exists=True,
                explicit_bootstrap=False,
            )
            self.assertFalse(is_boot_c, "Modern catalog with watermark and installation_ids must NOT trigger bootstrap")
            self.assertEqual(wm_c, "2026-08-01T00:00:00Z")

            # Case D: Verify query template selection under bootstrap vs incremental
            # When is_bootstrap is True, version_catalog SQL must be version_catalog_bootstrap (WHERE event_timestamp IS NOT NULL)
            table_sqls = dict(SQLS)
            if is_boot:
                table_sqls["version_catalog"] = SQLS["version_catalog_bootstrap"]
            else:
                table_sqls["version_catalog"] = SQLS["version_catalog"]

            self.assertEqual(table_sqls["version_catalog"], SQLS["version_catalog_bootstrap"])
            self.assertIn("WHERE event_timestamp IS NOT NULL", table_sqls["version_catalog"])
            self.assertNotIn("DATE_SUB", table_sqls["version_catalog"])

            # Case E: Partial migration catalog with mixed versions (1.0.0 lacks IDs, 2.0.0 has IDs)
            mixed_catalog = {
                "schema_version": "2.3.0",
                "watermark": "2026-08-01T00:00:00Z",
                "app_versions": {
                    "android": {
                        "1.0.0": {
                            "version": "1.0.0",
                            "crash_events": 1000,
                            "lifetime_affected_users": 500,
                            # installation_ids missing
                        },
                        "2.0.0": {
                            "version": "2.0.0",
                            "crash_events": 10,
                            "lifetime_affected_users": 2,
                            "installation_ids": ["uuid_1", "uuid_2"],
                        }
                    }
                },
                "issues": {},
            }
            is_boot_e, wm_e = should_trigger_catalog_bootstrap(
                cat_data=mixed_catalog,
                cat_file_exists=True,
                explicit_bootstrap=False,
            )
            self.assertTrue(is_boot_e, "Mixed catalog where any version lacks installation_ids must trigger bootstrap")
            self.assertIsNone(wm_e)

    def test_partial_migration_catalog_triggers_bootstrap_and_preserves_monotonic_users(self) -> None:
        """Regression test for Review 5124192460:
        1. A catalog with >=2 versions where one has installation_ids and another lacks them
           must trigger bootstrap.
        2. If an incremental batch arrives for the version lacking authority state,
           lifetime_affected_users must NEVER decrease below the existing aggregate count.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            cat_file = tmppath / "test_app" / "historical_catalog.json"
            cat_file.parent.mkdir(parents=True, exist_ok=True)

            mixed_catalog_data = {
                "schema_version": "2.3.0",
                "watermark": "2026-08-01T00:00:00Z",
                "bootstrap_complete": False,
                "app_versions": {
                    "android": {
                        "1.0.0": {
                            "version": "1.0.0",
                            "platform": "android",
                            "status": "legacy",
                            "crash_events": 1000,
                            "affected_users": 500,
                            "lifetime_crashes": 1000,
                            "lifetime_affected_users": 500,
                            "last_updated": "2026-07-01T00:00:00Z",
                            # installation_ids missing: legacy aggregate-only version
                        },
                        "2.0.0": {
                            "version": "2.0.0",
                            "platform": "android",
                            "status": "active",
                            "crash_events": 10,
                            "affected_users": 2,
                            "lifetime_crashes": 10,
                            "lifetime_affected_users": 2,
                            "installation_ids": ["uuid_recent_1", "uuid_recent_2"],
                            "last_updated": "2026-08-01T00:00:00Z",
                        },
                    }
                },
                "issues": {},
            }
            cat_file.write_text(json.dumps(mixed_catalog_data), encoding="utf-8")

            # 1. Verify bootstrap is triggered
            is_boot, wm = should_trigger_catalog_bootstrap(
                cat_data=mixed_catalog_data,
                cat_file_exists=True,
                explicit_bootstrap=False,
            )
            self.assertTrue(is_boot, "Partial migration catalog with mixed authority state MUST trigger bootstrap")
            self.assertIsNone(wm)

            # 2. Simulate incremental ingestion on the legacy version lacking authority state
            cat = IssueHistoricalCatalog(catalog_path=cat_file, app_id="test_app")
            cat.load()

            self.assertEqual(cat.app_versions["android"]["1.0.0"]["lifetime_affected_users"], 500)

            incremental_rows = [
                {
                    "app_version": "1.0.0",
                    "platform": "android",
                    "crash_events": 5,
                    "affected_users": 2,
                    "installation_ids": ["uuid_inc_1", "uuid_inc_2"],
                    "last_seen_timestamp": "2026-08-05T00:00:00Z",
                }
            ]

            cat.update_from_catalog_rows(
                incremental_rows,
                is_incremental=True,
                advance_watermark=True,
                checkpoint_watermark="2026-08-01T00:00:00Z",
            )

            v1 = cat.app_versions["android"]["1.0.0"]
            # MUST NOT drop from 500 to 2!
            self.assertGreaterEqual(
                v1["lifetime_affected_users"],
                500,
                "Lifetime affected users must never decrease below existing aggregate when incremental batch has fewer UUIDs",
            )
            self.assertEqual(v1["lifetime_affected_users"], 500)
            self.assertEqual(v1["affected_users"], 500)
            # Lifetime crashes incremented: 1000 + 5 = 1005
            self.assertEqual(v1["lifetime_crashes"], 1005)
            # installation_ids purged from dict and tracked via authority store
            self.assertNotIn("installation_ids", v1)
            self.assertEqual(cat.authority_store.count_installations("test_app", "android", "1.0.0"), 2)

            # 3. Save catalog and verify serialization
            cat.save()
            # Verify installation_ids is not present in serialized JSON
            disk_data = json.loads(cat_file.read_text(encoding="utf-8"))
            self.assertNotIn("installation_ids", disk_data["app_versions"]["android"]["1.0.0"])
            self.assertIn("authority", disk_data)

            reloaded_cat = IssueHistoricalCatalog(catalog_path=cat_file, app_id="test_app")
            reloaded_cat.load()
            reloaded_v1 = reloaded_cat.app_versions["android"]["1.0.0"]
            self.assertEqual(reloaded_v1["lifetime_affected_users"], 500)
            self.assertNotIn("installation_ids", reloaded_v1)
            self.assertEqual(reloaded_cat.authority_store.count_installations("test_app", "android", "1.0.0"), 2)
            self.assertFalse(reloaded_cat.bootstrap_complete, "Incomplete authority must keep bootstrap_complete False")
            self.assertFalse(disk_data.get("bootstrap_complete", False))
            self.assertFalse(disk_data.get("authority", {}).get("bootstrap_complete", False))

            # Subsequent sync attempts must continue triggering full historical bootstrap until bootstrap finishes
            is_boot_after, _ = should_trigger_catalog_bootstrap(
                cat_data=disk_data,
                cat_file_exists=True,
                authority_store=reloaded_cat.authority_store,
                app_id="test_app",
            )
            self.assertTrue(is_boot_after, "Incomplete authority must continue to trigger bootstrap")


if __name__ == "__main__":
    unittest.main()
