import datetime as dt
import json
from pathlib import Path
import tempfile
from typing import Any, Dict

import pytest

from crash_trend.build_dashboard import build_html
from crash_trend.lifecycle import (
    IssueHistoricalCatalog,
    bootstrap_catalog_from_disk,
    enrich_app_data_with_lifecycle,
)
from crash_trend.schema_v2 import (
    AppDashboardV2Data,
    ReleaseCatalogItem,
    validate_app_dashboard_v2,
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


class TestReleaseCatalog:
    """Test suite for Issue #47 Persistent Release Catalog."""

    def test_release_catalog_persistence_and_reload(self):
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
            assert "android" in cat2.app_versions
            assert "1.0.0" in cat2.app_versions["android"]
            assert "2.0.0" in cat2.app_versions["android"]
            assert cat2.app_versions["android"]["1.0.0"]["release_date"] == "2026-01-01"
            assert cat2.app_versions["android"]["2.0.0"]["release_date"] is None
            assert cat2.app_versions["android"]["1.0.0"]["lifetime_crashes"] == 50

    def test_legacy_release_preservation_over_90d(self):
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

        assert status_090 == "legacy"
        assert status_100 == "active"
        assert status_110 == "latest"

        # Check full catalog item
        items = cat.build_release_catalog(platform="android", reference_date=ref_dt)
        item_090 = next(it for it in items if it["version"] == "0.9.0")
        assert item_090["status"] == "legacy"
        assert item_090["lifetime_crashes"] == 500
        assert item_090["lifetime_affected_users"] == 300

    def test_platform_strict_isolation(self):
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

        assert android_versions == {"1.0.0", "1.1.0"}
        assert ios_versions == {"5.0.0", "5.1.0"}

        # vs_previous must never cross platforms
        v110 = next(x for x in android_catalog if x["version"] == "1.1.0")
        assert v110["vs_previous"] is not None
        assert v110["vs_previous"]["previous_version"] == "1.0.0"

        v510 = next(x for x in ios_catalog if x["version"] == "5.1.0")
        assert v510["vs_previous"] is not None
        assert v510["vs_previous"]["previous_version"] == "5.0.0"

    def test_lifetime_unique_users_deduplication(self):
        """Test that lifetime unique users are deduplicated and NEVER summed across windows."""
        cat = IssueHistoricalCatalog("test_app")

        # Ingest period 7 (20 users), period 30 (50 users), period 90 (80 users)
        cat.update_app_versions([{"version": "2.0.0", "platform": "android", "affected_users": 20, "crash_events": 30}], window="7")
        cat.update_app_versions([{"version": "2.0.0", "platform": "android", "affected_users": 50, "crash_events": 90}], window="30")
        cat.update_app_versions([{"version": "2.0.0", "platform": "android", "affected_users": 80, "crash_events": 160}], window="90")

        # Verify app_versions stored lifetime metrics
        v_info = cat.app_versions["android"]["2.0.0"]
        assert v_info["lifetime_affected_users"] == 80
        assert v_info["lifetime_affected_users"] != (20 + 50 + 80)
        assert v_info["lifetime_crashes"] == 160

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
        assert v_info2["lifetime_affected_users"] == 85
        assert v_info2["lifetime_crashes"] == 170
        assert v_info2["lifetime_fatal"] == 20
        assert v_info2["lifetime_anr"] == 5

    def test_previous_release_normalized_comparison(self):
        """Test that comparison against the previous release uses normalized rates and delta calculations."""
        cat = IssueHistoricalCatalog("test_app")
        cat.update_app_versions([
            {
                "version": "1.0.0",
                "platform": "android",
                "lifetime_crashes": 100,
                "sessions_total": 10000,  # crash rate = 0.0100
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
                "sessions_total": 10000,  # crash rate = 0.0050 (-50%)
                "crash_free_users_rate": 0.990,  # +0.010 (+1.0%)
                "lifetime_fatal": 10,  # -50%
                "lifetime_anr": 5,  # -50%
                "first_seen": "2026-08-01T00:00:00Z",
                "last_seen": "2026-08-31T00:00:00Z",
            },
        ])

        catalog = cat.build_release_catalog(platform="android")
        v100 = next(x for x in catalog if x["version"] == "1.0.0")
        v110 = next(x for x in catalog if x["version"] == "1.1.0")

        # v1.0.0 has no predecessor
        assert v100["vs_previous"] is None
        assert v100["stability_status"] == "baseline"

        # v1.1.0 compares to v1.0.0
        vp = v110["vs_previous"]
        assert vp is not None
        assert vp["previous_version"] == "1.0.0"
        assert vp["crash_rate_change_pct"] == -0.5
        assert vp["crash_free_users_diff"] == 0.01
        assert vp["fatal_rate_change_pct"] == -0.5
        assert vp["anr_rate_change_pct"] == -0.5
        assert vp["stability"] == "improving"
        assert vp["stability_status"] == "improved"
        assert v110["stability_status"] == "improving"

    def test_issue_lifecycle_categorization_per_release(self):
        """Test 4 categories of issue lifecycle per release: introduced, persistent, regressed, resolved."""
        cat = IssueHistoricalCatalog("test_app")
        # Define 2 versions with sufficient samples
        cat.update_app_versions([
            {"version": "1.0.0", "platform": "android", "affected_users": 100, "crash_events": 200},
            {"version": "1.1.0", "platform": "android", "affected_users": 150, "crash_events": 300},
        ])

        # Issue 1: introduced in 1.0.0 and resolved in 1.1.0
        cat.issues["iss_resolved"] = {
            "issue_id": "iss_resolved",
            "title": "Old bug",
            "platform": "android",
            "first_seen_version": "1.0.0",
            "versions_seen": ["1.0.0"],
            "state": "RESOLVED",
        }
        # Issue 2: persistent across 1.0.0 and 1.1.0
        cat.issues["iss_persistent"] = {
            "issue_id": "iss_persistent",
            "title": "Persistent bug",
            "platform": "android",
            "first_seen_version": "1.0.0",
            "versions_seen": ["1.0.0", "1.1.0"],
            "state": "OPEN",
        }
        # Issue 3: introduced in 1.1.0
        cat.issues["iss_introduced"] = {
            "issue_id": "iss_introduced",
            "title": "Brand new bug",
            "platform": "android",
            "first_seen_version": "1.1.0",
            "versions_seen": ["1.1.0"],
            "state": "OPEN",
        }
        # Issue 4: regressed in 1.1.0
        cat.issues["iss_regressed"] = {
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
        lc = v110["issue_lifecycle"]

        assert "iss_introduced" in lc["introduced"]
        assert "iss_persistent" in lc["persistent"]
        assert "iss_regressed" in lc["regressed"]
        assert "iss_resolved" in lc["resolved"]
        assert lc["introduced_count"] == 1
        assert lc["persistent_count"] == 1
        assert lc["regressed_count"] == 1
        assert lc["resolved_count"] == 1

    def test_release_date_vs_first_seen_semantic_separation(self):
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

        # first_seen is derived from observation
        assert v300["first_seen"] == "2026-08-15T12:00:00Z"
        # release_date is None because no external authoritative date was configured
        assert v300["release_date"] is None

    def test_schema_validation_accepts_release_catalog(self):
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
        errors: list[str] = []
        validate_release_catalog([item], errors)
        assert not errors, f"Validation errors: {errors}"

        # test in full dashboard data structure
        app_data = _sample_app_data()
        enrich_app_data_with_lifecycle(app_data, app_name="test_app")
        assert "release_catalog" in app_data
        errors2: list[str] = []
        validate_release_catalog(app_data["release_catalog"], errors2)
        assert not errors2, f"Validation errors in app_data: {errors2}"

    def test_releases_table_renders_all_versions_regardless_of_window(self):
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
        assert 'id="view-releases"' in html
        assert 'id="filterReleasePlatform"' in html
        assert 'id="filterReleaseStatus"' in html
        assert 'id="searchReleaseVer"' in html
        assert 'id="releasesTableBody"' in html

        # Check modal structure
        assert 'id="releaseDetailModal"' in html
        assert 'id="releaseModalTitle"' in html
        assert 'id="releaseModalBody"' in html
        assert 'openReleaseDetail' in html
        assert 'switchReleaseRecentHealthTab' in html

        # Check data contains release_catalog
        assert 'release_catalog' in html
