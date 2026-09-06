"""Comprehensive regression tests for Catalog Authority Store (Issue #49).

Verifies:
1. Bootstrap exact deduplication & deterministic salted hashing.
2. Incremental sync with overlap deduplication.
3. Replay / retry idempotency (zero count delta on repeated batches).
4. Platform isolation (Android vs iOS same UUID tracks separately).
5. Multi-app isolation (Different apps with same UUID do not collide).
6. Legacy JSON catalog migration & JSON decoupling (omits installation_ids from JSON).
7. Missing/partial authority triggers full historical bootstrap.
8. Strict failure semantics: raises AuthorityStoreError on corruption / failure.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from crash_trend.authority_store import AuthorityStoreError, CatalogAuthorityStore
from crash_trend.fetch_bigquery import transform_bq_to_v2
from crash_trend.lifecycle import (
    IssueHistoricalCatalog,
    should_trigger_catalog_bootstrap,
)
from crash_trend.schema_v2 import validate_app_dashboard_v2, validate_historical_catalog


class TestCatalogAuthorityStore(unittest.TestCase):
    """Test suite for CatalogAuthorityStore and IssueHistoricalCatalog integration."""

    def test_bootstrap_exact_dedupe_and_hashing(self) -> None:
        """Verify duplicate installation UUIDs collapse to exact unique count and store salted SHA-256."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "catalog_authority.sqlite3"
            store = CatalogAuthorityStore(db_path, app_id="test_app")

            uuids = ["uuid_1", "uuid_2", "uuid_1", "uuid_3", "uuid_2"]  # 3 unique
            added = store.add_installations("test_app", "android", "1.0.0", uuids)
            self.assertEqual(added, 3)

            count = store.count_installations("test_app", "android", "1.0.0")
            self.assertEqual(count, 3)

            # Verify hashing: raw UUIDs must NOT be in the database
            conn = sqlite3.connect(str(db_path))
            try:
                cur = conn.cursor()
                cur.execute("SELECT installation_hash FROM release_installations")
                hashes = [r[0] for r in cur.fetchall()]
                self.assertEqual(len(hashes), 3)
                for raw in ["uuid_1", "uuid_2", "uuid_3"]:
                    self.assertNotIn(raw, hashes)
                    expected_hash = hashlib.sha256(f"test_app:{raw}".encode("utf-8")).hexdigest()
                    self.assertIn(expected_hash, hashes)
            finally:
                conn.close()

            store.close()

    def test_incremental_overlap(self) -> None:
        """Verify incremental sync deduplicates overlapping UUIDs and only counts new ones."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "catalog_authority.sqlite3"
            store = CatalogAuthorityStore(db_path, app_id="test_app")

            # Batch 1 (Bootstrap)
            store.add_installations("test_app", "android", "1.0.0", ["u1", "u2", "u3"])
            self.assertEqual(store.count_installations("test_app", "android", "1.0.0"), 3)

            # Batch 2 (Incremental): u2 and u3 overlap, u4 is new
            added = store.add_installations("test_app", "android", "1.0.0", ["u2", "u3", "u4"])
            self.assertEqual(added, 1)
            self.assertEqual(store.count_installations("test_app", "android", "1.0.0"), 4)

            store.close()

    def test_replay_idempotency(self) -> None:
        """Verify replaying the same batch twice yields 0 newly added installations and maintains exact counts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "catalog_authority.sqlite3"
            store = CatalogAuthorityStore(db_path, app_id="test_app")

            batch = [f"uuid_{i}" for i in range(100)]
            first_added = store.add_installations("test_app", "android", "1.0.0", batch)
            self.assertEqual(first_added, 100)
            self.assertEqual(store.count_installations("test_app", "android", "1.0.0"), 100)

            # Replay same batch
            second_added = store.add_installations("test_app", "android", "1.0.0", batch)
            self.assertEqual(second_added, 0)
            self.assertEqual(store.count_installations("test_app", "android", "1.0.0"), 100)

            store.close()

    def test_platform_isolation(self) -> None:
        """Verify identical installation UUIDs on Android and iOS track independently."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "catalog_authority.sqlite3"
            store = CatalogAuthorityStore(db_path, app_id="test_app")

            shared_uuid = ["device_common_123"]
            store.add_installations("test_app", "android", "2.0.0", shared_uuid)
            store.add_installations("test_app", "ios", "2.0.0", shared_uuid)

            self.assertEqual(store.count_installations("test_app", "android", "2.0.0"), 1)
            self.assertEqual(store.count_installations("test_app", "ios", "2.0.0"), 1)

            # Add device_common_456 only to iOS
            store.add_installations("test_app", "ios", "2.0.0", ["device_common_456"])
            self.assertEqual(store.count_installations("test_app", "android", "2.0.0"), 1)
            self.assertEqual(store.count_installations("test_app", "ios", "2.0.0"), 2)

            store.close()

    def test_multi_app_isolation(self) -> None:
        """Verify identical UUIDs across different apps do not collide due to app salting."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "catalog_authority.sqlite3"
            store = CatalogAuthorityStore(db_path)

            raw = "shared_uuid"
            store.add_installations("app_alpha", "android", "1.0.0", [raw])
            store.add_installations("app_beta", "android", "1.0.0", [raw])

            self.assertEqual(store.count_installations("app_alpha", "android", "1.0.0"), 1)
            self.assertEqual(store.count_installations("app_beta", "android", "1.0.0"), 1)

            # Verify hashes are different
            hash_a = store.hash_installation(raw, "app_alpha")
            hash_b = store.hash_installation(raw, "app_beta")
            self.assertNotEqual(hash_a, hash_b)

            store.close()

    def test_legacy_json_migration_and_json_decoupling(self) -> None:
        """Verify legacy catalog with installation_ids migrates to SQLite and JSON no longer contains IDs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            cat_file = tmppath / "historical_catalog.json"
            db_path = tmppath / "catalog_authority.sqlite3"

            # Create legacy catalog containing installation_ids
            legacy_data = {
                "schema_version": "2.3.0",
                "app_id": "migration_app",
                "updated_at": "2026-08-01T00:00:00Z",
                "watermark": "2026-08-01T12:00:00Z",
                "bootstrap_complete": True,
                "issues": {},
                "app_versions": {
                    "android": {
                        "1.0.0": {
                            "version": "1.0.0",
                            "platform": "android",
                            "first_seen": "2026-08-01T00:00:00Z",
                            "last_seen": "2026-08-01T12:00:00Z",
                            "lifetime_crashes": 100,
                            "lifetime_affected_users": 3,
                            "installation_ids": ["uuid_a", "uuid_b", "uuid_c"],
                            "issues_count": 2,
                        }
                    },
                    "ios": {},
                },
            }
            cat_file.write_text(json.dumps(legacy_data), encoding="utf-8")

            # Load via IssueHistoricalCatalog
            catalog = IssueHistoricalCatalog(cat_file, app_id="migration_app", authority_store_path=db_path)
            catalog.load()

            # 1. Verify exact counts migrated to SQLite
            sqlite_count = catalog.authority_store.count_installations("migration_app", "android", "1.0.0")
            self.assertEqual(sqlite_count, 3)

            # 2. Verify in-memory entity purged of installation_ids
            v_in_mem = catalog.app_versions["android"]["1.0.0"]
            self.assertNotIn("installation_ids", v_in_mem)
            self.assertNotIn("user_ids", v_in_mem)
            self.assertEqual(v_in_mem["lifetime_affected_users"], 3)

            # 3. Save catalog and verify JSON decoupling
            catalog.save()
            catalog.close()

            saved_json_text = cat_file.read_text(encoding="utf-8")
            saved_dict = json.loads(saved_json_text)

            # Assert JSON no longer contains installation_ids or user_ids
            self.assertNotIn("installation_ids", saved_json_text)
            self.assertNotIn("user_ids", saved_json_text)

            # Assert authority metadata section present
            self.assertIn("authority", saved_dict)
            self.assertEqual(saved_dict["authority"]["backend"], "sqlite")
            self.assertTrue(saved_dict["authority"]["bootstrap_complete"])

            # 4. Reload from saved JSON without installation_ids
            reloaded_cat = IssueHistoricalCatalog(cat_file, app_id="migration_app", authority_store_path=db_path)
            reloaded_cat.load()
            reloaded_v = reloaded_cat.app_versions["android"]["1.0.0"]
            self.assertEqual(reloaded_v["lifetime_affected_users"], 3)
            self.assertNotIn("installation_ids", reloaded_v)
            self.assertEqual(reloaded_cat.authority_store.count_installations("migration_app", "android", "1.0.0"), 3)
            reloaded_cat.close()

    def test_partial_legacy_catalog_triggers_bootstrap(self) -> None:
        """Verify catalog with versions missing installation authority triggers full historical bootstrap."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            cat_file = tmppath / "historical_catalog.json"
            db_path = tmppath / "catalog_authority.sqlite3"

            # Catalog where 1.0.0 has users > 0 but lacks installation_ids and has no SQLite records
            cat_data = {
                "schema_version": "2.3.0",
                "app_id": "test_app",
                "updated_at": "2026-08-01T00:00:00Z",
                "watermark": "2026-08-01T12:00:00Z",
                "bootstrap_complete": True,
                "issues": {},
                "app_versions": {
                    "android": {
                        "1.0.0": {
                            "version": "1.0.0",
                            "platform": "android",
                            "lifetime_crashes": 500,
                            "lifetime_affected_users": 300,
                            # installation_ids missing!
                        }
                    },
                    "ios": {},
                },
            }
            cat_file.write_text(json.dumps(cat_data), encoding="utf-8")

            is_boot, wm = should_trigger_catalog_bootstrap(
                cat_data=cat_data,
                cat_file_exists=True,
                authority_store_path=db_path,
                app_id="test_app",
                cat_file_path=cat_file,
            )
            self.assertTrue(is_boot, "Should trigger full bootstrap when versions lack installation authority")
            self.assertIsNone(wm)

    def test_sqlite_error_failure_semantics(self) -> None:
        """Verify corrupted or locked SQLite store raises AuthorityStoreError and marks source error."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            db_path = tmppath / "corrupted.sqlite3"
            # Write non-sqlite garbage to the file to simulate corruption
            db_path.write_bytes(b"NOT_A_SQLITE_DATABASE_CORRUPTED_DATA_HEADER_XYZ")

            with self.assertRaises(AuthorityStoreError):
                # Initializing on corrupted file must raise AuthorityStoreError
                CatalogAuthorityStore(db_path, app_id="test_app")

    def test_transform_bq_to_v2_with_authority_store(self) -> None:
        """Verify transform_bq_to_v2 end-to-end integration produces clean JSON and populates SQLite."""
        app_cfg = {"app_id": "e2e_app", "platforms": ["android"], "firebase_project": "test-project"}

        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            bq_data = {
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
                                "crash_events": 50,
                                "affected_users": 20,
                                "installation_ids": [f"user_{i}" for i in range(20)],
                                "fatal_events": 5,
                                "anr_events": 1,
                                "issues_count": 2,
                            }
                        ],
                    }
                }
            }

            v2_result = transform_bq_to_v2(bq_data, app_cfg, is_bootstrap=True, out_dir=tmppath)
            self.assertEqual(len(v2_result["release_catalog"]), 1)
            item = v2_result["release_catalog"][0]
            self.assertEqual(item["lifetime_affected_users"], 20)

            # Verify SQLite database was created and has exact records
            db_file = tmppath / "e2e_app" / "catalog_authority.sqlite3"
            self.assertTrue(db_file.is_file())

            store = CatalogAuthorityStore(db_file, app_id="e2e_app")
            self.assertEqual(store.count_installations("e2e_app", "android", "1.0.0"), 20)
            store.close()

            # Verify historical_catalog.json does NOT contain installation_ids
            cat_file = tmppath / "e2e_app" / "historical_catalog.json"
            self.assertTrue(cat_file.is_file())
            cat_text = cat_file.read_text(encoding="utf-8")
            self.assertNotIn("installation_ids", cat_text)
            self.assertNotIn("user_ids", cat_text)

            cat_json = json.loads(cat_text)
            self.assertIn("authority", cat_json)
            self.assertEqual(cat_json["authority"]["backend"], "sqlite")

            # Validate schema V2 and historical catalog validation
            self.assertEqual(validate_app_dashboard_v2(v2_result), [])
            self.assertEqual(validate_historical_catalog(cat_json), [])

    def test_partial_non_empty_ids_authority_incomplete_triggers_bootstrap(self) -> None:
        """Regression test for Review 5124265563 Blocker 1:

        Legacy catalog with non-empty installation_ids (2 IDs) but fewer than recorded
        lifetime_affected_users (500) must be treated as INCOMPLETE authority,
        keeping bootstrap_complete=False and triggering full historical bootstrap.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            cat_file = tmppath / "historical_catalog.json"
            db_path = tmppath / "catalog_authority.sqlite3"

            cat_data = {
                "schema_version": "2.3.0",
                "app_id": "partial_app",
                "updated_at": "2026-08-01T00:00:00Z",
                "watermark": "2026-08-01T12:00:00Z",
                "bootstrap_complete": True,
                "issues": {},
                "app_versions": {
                    "android": {
                        "1.0.0": {
                            "version": "1.0.0",
                            "platform": "android",
                            "lifetime_crashes": 1000,
                            "lifetime_affected_users": 500,
                            "installation_ids": ["uuid_1", "uuid_2"],  # Non-empty, but 2 < 500!
                        }
                    },
                    "ios": {},
                },
            }
            cat_file.write_text(json.dumps(cat_data), encoding="utf-8")

            # 1. Verification before load: should_trigger_catalog_bootstrap MUST detect incomplete authority
            is_boot, wm = should_trigger_catalog_bootstrap(
                cat_data=cat_data,
                cat_file_exists=True,
                authority_store_path=db_path,
                app_id="partial_app",
                cat_file_path=cat_file,
            )
            self.assertTrue(is_boot, "Non-empty but incomplete legacy IDs (2 < 500) MUST trigger full bootstrap")
            self.assertIsNone(wm)

            # 2. On load(), IDs are migrated but authority is flagged incomplete
            cat = IssueHistoricalCatalog(cat_file, app_id="partial_app", authority_store_path=db_path)
            cat.load()

            self.assertEqual(cat.authority_store.count_installations("partial_app", "android", "1.0.0"), 2)
            self.assertFalse(cat.authority_store.has_version_authority("partial_app", "android", "1.0.0"))
            self.assertFalse(cat.bootstrap_complete, "Catalog bootstrap_complete must be False after partial migration")
            self.assertEqual(cat.app_versions["android"]["1.0.0"]["lifetime_affected_users"], 500)

            # 3. On save(), serialized catalog records bootstrap_complete=False
            cat.save()
            cat.close()

            saved_disk = json.loads(cat_file.read_text(encoding="utf-8"))
            self.assertFalse(saved_disk.get("bootstrap_complete", False))
            self.assertFalse(saved_disk.get("authority", {}).get("bootstrap_complete", False))

            # 4. Incomplete authority continues to trigger full bootstrap
            is_boot_saved, _ = should_trigger_catalog_bootstrap(
                cat_data=saved_disk,
                cat_file_exists=True,
                authority_store_path=db_path,
                app_id="partial_app",
                cat_file_path=cat_file,
            )
            self.assertTrue(is_boot_saved, "Saved incomplete authority MUST still trigger bootstrap")

            # 5. Full bootstrap ingestion restores full authority and exact count
            boot_cat = IssueHistoricalCatalog(cat_file, app_id="partial_app", authority_store_path=db_path)
            boot_cat.load()
            all_500_ids = [f"uuid_{i}" for i in range(500)]
            boot_cat.update_from_catalog_rows([
                {
                    "app_version": "1.0.0",
                    "platform": "android",
                    "first_seen": "2026-08-01T00:00:00Z",
                    "last_seen": "2026-08-01T12:00:00Z",
                    "crash_events": 1000,
                    "affected_users": 500,
                    "installation_ids": all_500_ids,
                }
            ], is_bootstrap=True, advance_watermark=True)
            boot_cat.bootstrap_complete = True
            boot_cat.save()
            boot_cat.close()

            boot_saved = json.loads(cat_file.read_text(encoding="utf-8"))
            self.assertTrue(boot_saved["bootstrap_complete"])
            self.assertTrue(boot_saved["authority"]["bootstrap_complete"])
            is_boot_final, final_wm = should_trigger_catalog_bootstrap(
                cat_data=boot_saved,
                cat_file_exists=True,
                authority_store_path=db_path,
                app_id="partial_app",
                cat_file_path=cat_file,
            )
            self.assertFalse(is_boot_final, "Completed bootstrap must switch catalog to incremental mode")
            self.assertEqual(final_wm, "2026-08-01T12:00:00Z")

    def test_sqlite_error_propagation_not_silently_swallowed(self) -> None:
        """Regression test for Review 5124265563 Blocker 2:

        SQLite database errors must propagate as AuthorityStoreError and not be silently caught with 'pass'.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            corrupted_db = tmppath / "catalog_authority.sqlite3"
            corrupted_db.write_bytes(b"INVALID_HEADER_GARBAGE_BYTES")

            cat_file = tmppath / "historical_catalog.json"
            cat_file.write_text(json.dumps({
                "schema_version": "2.3.0",
                "app_id": "err_app",
                "watermark": "2026-08-01T00:00:00Z",
                "app_versions": {
                    "android": {
                        "1.0.0": {
                            "version": "1.0.0",
                            "lifetime_affected_users": 5,
                            "installation_ids": ["u1", "u2", "u3", "u4", "u5"],
                        }
                    }
                }
            }), encoding="utf-8")

            # 1. Opening corrupted authority store via IssueHistoricalCatalog must raise AuthorityStoreError
            with self.assertRaises(AuthorityStoreError):
                IssueHistoricalCatalog(cat_file, app_id="err_app", authority_store_path=corrupted_db)

            # 2. should_trigger_catalog_bootstrap with corrupted DB must raise AuthorityStoreError
            with self.assertRaises(AuthorityStoreError):
                should_trigger_catalog_bootstrap(
                    cat_data={
                        "watermark": "2026-08-01T00:00:00Z",
                        "app_versions": {"android": {"1.0.0": {"version": "1.0.0", "lifetime_affected_users": 5}}},
                    },
                    cat_file_exists=True,
                    authority_store_path=corrupted_db,
                    app_id="err_app",
                    cat_file_path=cat_file,
                )

    def test_cli_main_authority_store_error_handling(self) -> None:
        """Regression test for Review 5124265563 Blocker 3:

        AuthorityStoreError must be imported at module level in fetch_bigquery
        and handled without NameError in CLI main() exit path.
        """
        import crash_trend.fetch_bigquery as fbq
        from unittest.mock import patch

        # 1. Verify AuthorityStoreError is bound at module level
        self.assertTrue(hasattr(fbq, "AuthorityStoreError"))
        self.assertIs(fbq.AuthorityStoreError, AuthorityStoreError)

        # 2. Simulate transform_bq_to_v2 raising AuthorityStoreError inside main()
        with patch.object(fbq, "transform_bq_to_v2", side_effect=AuthorityStoreError("disk I/O error")):
            with patch.object(fbq, "list_crash_tables", return_value=["table_android"]):
                with patch.object(fbq, "make_client"):
                    with patch("sys.argv", ["fetch_bigquery.py", "--app", "demo", "--days", "7"]):
                        with patch.object(fbq, "get_app", return_value={"name": "demo", "firebase_project": "proj"}):
                            # main() must exit via sys.exit with message and NOT crash with NameError
                            with self.assertRaises(SystemExit) as ctx:
                                fbq.main()
                            self.assertIn("Catalog Authority Store", str(ctx.exception))

    def test_sqlite_partial_count_matches_users_without_bootstrap_status_triggers_bootstrap(self) -> None:
        """Regression test for Review 5124280881 Blocker 1:

        Even if sqlite_count == lifetime_affected_users, if per-version bootstrap_complete == 0,
        _has_verifiable_installation_authority and should_trigger_catalog_bootstrap must return True
        (triggering full historical bootstrap) rather than falsely trusting partial unverified authority.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            db_path = tmppath / "catalog_authority.sqlite3"
            cat_file = tmppath / "historical_catalog.json"

            store = CatalogAuthorityStore(db_path, app_id="test_app")
            # add_installations inserts 2 UUIDs, keeping bootstrap_complete=0 by default
            added = store.add_installations("test_app", "android", "1.0.0", ["u1", "u2"])
            self.assertEqual(added, 2)
            self.assertEqual(store.count_installations("test_app", "android", "1.0.0"), 2)
            self.assertFalse(store.has_version_authority("test_app", "android", "1.0.0"))

            cat_data = {
                "schema_version": "2.3.0",
                "app_id": "test_app",
                "watermark": "2026-08-01T00:00:00Z",
                "bootstrap_complete": True,
                "authority": {
                    "backend": "sqlite",
                    "state_version": 1,
                    "bootstrap_complete": True,
                },
                "app_versions": {
                    "android": {
                        "1.0.0": {
                            "version": "1.0.0",
                            "platform": "android",
                            "lifetime_crashes": 10,
                            "lifetime_affected_users": 2,
                            "affected_users": 2,
                        }
                    }
                },
            }
            cat_file.write_text(json.dumps(cat_data), encoding="utf-8")

            # 1. Authority incomplete (bootstrap_complete=0 in SQLite): MUST trigger full bootstrap
            is_boot, wm = should_trigger_catalog_bootstrap(
                cat_data=cat_data,
                cat_file_exists=True,
                authority_store=store,
                app_id="test_app",
                cat_file_path=cat_file,
            )
            self.assertTrue(
                is_boot,
                "Partial SQLite data without per-version bootstrap_complete=1 must trigger bootstrap even if count==users",
            )
            self.assertIsNone(wm)

            # 2. Once marked complete (e.g. after full bootstrap), should_trigger_catalog_bootstrap returns False
            store.mark_version_bootstrapped("test_app", "android", "1.0.0", True)
            self.assertTrue(store.has_version_authority("test_app", "android", "1.0.0"))

            is_boot2, wm2 = should_trigger_catalog_bootstrap(
                cat_data=cat_data,
                cat_file_exists=True,
                authority_store=store,
                app_id="test_app",
                cat_file_path=cat_file,
            )
            self.assertFalse(is_boot2)
            self.assertEqual(wm2, "2026-08-01T00:00:00Z")
            store.close()

    def test_bootstrap_zero_users_null_uuids_marks_complete_no_bootstrap_loop(self) -> None:
        """Regression test for Review 5124280881 Blocker 2:

        Full bootstrap on a version with crash events but all installation UUIDs NULL (affected_users=0)
        must mark per-version bootstrap_complete=1 in SQLite so subsequent runs do not enter an
        infinite bootstrap loop.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            db_path = tmppath / "catalog_authority.sqlite3"
            cat_file = tmppath / "historical_catalog.json"

            cat = IssueHistoricalCatalog(cat_file, app_id="test_app", authority_store_path=db_path)
            cat.update_from_catalog_rows(
                [
                    {
                        "app_version": "1.0.0",
                        "platform": "android",
                        "first_seen": "2026-08-01T00:00:00Z",
                        "last_seen": "2026-08-01T12:00:00Z",
                        "crash_events": 100,
                        "affected_users": 0,
                        "installation_ids": [],
                    }
                ],
                is_bootstrap=True,
                advance_watermark=True,
            )

            # Check that per-version authority is established as complete with 0 users
            self.assertEqual(cat.authority_store.count_installations("test_app", "android", "1.0.0"), 0)
            self.assertTrue(cat.authority_store.has_version_authority("test_app", "android", "1.0.0"))
            self.assertEqual(cat.app_versions["android"]["1.0.0"]["lifetime_affected_users"], 0)
            self.assertEqual(cat.app_versions["android"]["1.0.0"]["crash_events"], 100)

            cat.save()
            cat.close()

            # Inspect saved catalog JSON
            saved_json = json.loads(cat_file.read_text(encoding="utf-8"))
            self.assertTrue(saved_json["bootstrap_complete"])
            self.assertEqual(saved_json["app_versions"]["android"]["1.0.0"]["lifetime_affected_users"], 0)

            # should_trigger_catalog_bootstrap must recognize this version as complete and NOT loop back to bootstrap
            is_boot, wm = should_trigger_catalog_bootstrap(
                cat_data=saved_json,
                cat_file_exists=True,
                authority_store_path=db_path,
                app_id="test_app",
                cat_file_path=cat_file,
            )
            self.assertFalse(is_boot, "Zero-user bootstrapped version must NOT trigger infinite bootstrap loop")
            self.assertEqual(wm, "2026-08-01T12:00:00Z")


if __name__ == "__main__":
    unittest.main()
