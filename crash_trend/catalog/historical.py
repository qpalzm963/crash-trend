"""Issue Historical Catalog and Persistence Orchestration (Issue #29, #47, #49, #53).

Provides:
- IssueHistoricalCatalog: Cross-window persistence of true historical first_seen,
  last_seen, version distributions, and app_versions per issue with strict platform isolation.
- enrich_app_data_with_lifecycle: Enriches AppDashboardV2Data top_issues and period snapshots
  with per-platform isolation and catalog tracking.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from crash_trend.authority_store import CatalogAuthorityStore
from crash_trend.catalog.issue_lifecycle import (
    detect_issue_lifecycle,
    is_version_sample_sufficient,
)
from crash_trend.catalog.release_catalog import (
    build_release_catalog,
    calculate_version_status,
    get_latest_app_version,
)
from crash_trend.catalog.watermark import advance_watermark, is_ts_le
from crash_trend.config import ROOT
from crash_trend.schema_v2 import ReleaseCatalogItem
from crash_trend.versions import max_version, min_version, version_key


class IssueHistoricalCatalog:
    """Manages cross-window persistent version catalog per application with platform isolation."""

    def __init__(
        self,
        catalog_path: str | Path | None = None,
        app_id: str | None = None,
        authority_store: CatalogAuthorityStore | None = None,
        authority_store_path: str | Path | None = None,
    ):
        # Support flexible argument passing: IssueHistoricalCatalog("app_name") or IssueHistoricalCatalog(Path(...))
        if catalog_path is not None and app_id is None:
            c_str = str(catalog_path)
            if not c_str.endswith(".json") and "/" not in c_str and "\\" not in c_str:
                app_id = c_str
                catalog_path = None

        self.catalog_path = Path(catalog_path) if catalog_path is not None else None
        self.app_id = app_id
        # Keyed canonically by f"{platform}:{issue_id}"
        self.issues: dict[str, dict[str, Any]] = {}
        # Grouped by platform: self.app_versions[platform][version]
        self.app_versions: dict[str, dict[str, dict[str, Any]]] = {"android": {}, "ios": {}}
        self.updated_at: str | None = None
        self.watermark: str | None = None
        self.bootstrap_complete: bool = False
        self.authority_state_version: int = 1
        self.authority_metadata: dict[str, Any] = {
            "backend": "sqlite",
            "state_version": 1,
            "bootstrap_complete": False,
        }

        # Initialize SQLite Authority Store
        if authority_store is not None:
            self.authority_store = authority_store
        elif authority_store_path is not None:
            self.authority_store = CatalogAuthorityStore(authority_store_path, app_id=app_id)
        elif self.catalog_path is not None:
            db_path = self.catalog_path.parent / "catalog_authority.sqlite3"
            self.authority_store = CatalogAuthorityStore(db_path, app_id=app_id)
        else:
            self.authority_store = CatalogAuthorityStore(":memory:", app_id=app_id)

        # In-memory transient tracker for backwards compatibility with raw callers
        self._version_installations: dict[str, dict[str, set[str]]] = {"android": {}, "ios": {}}

    def close(self) -> None:
        """Closes the underlying SQLite authority store."""
        if hasattr(self, "authority_store") and self.authority_store is not None:
            self.authority_store.close()

    def __enter__(self) -> IssueHistoricalCatalog:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _canonical_key(self, platform: str, issue_id: str) -> str:
        pf = "ios" if platform == "ios" else "android"
        return f"{pf}:{issue_id}"

    def advance_watermark(self, candidate_ts: str | None) -> None:
        """Advances catalog watermark if candidate_ts is newer than current watermark."""
        self.watermark = advance_watermark(self.watermark, candidate_ts)

    @staticmethod
    def _is_ts_le(ts: str | None, watermark: str | None) -> bool:
        """Returns True if ts <= watermark (comparing ISO datetimes with timezone awareness)."""
        return is_ts_le(ts, watermark)

    def load(self) -> None:
        """Loads existing catalog file from disk if present and migrates any legacy JSON installation IDs to SQLite."""
        if self.catalog_path and self.catalog_path.is_file():
            try:
                data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                return

            loaded_issues = data.get("issues", {})
            for k, iss in loaded_issues.items():
                if isinstance(iss, dict):
                    pf = "ios" if iss.get("platform") == "ios" else "android"
                    iid = iss.get("issue_id", k)
                    canonical = self._canonical_key(pf, iid)
                    self.issues[canonical] = iss

            loaded_vers = data.get("app_versions", {})
            legacy_ids_to_migrate: list[tuple[str, str, list[Any]]] = []

            if isinstance(loaded_vers, dict):
                for pf_or_ver, val in loaded_vers.items():
                    if pf_or_ver in ("android", "ios") and isinstance(val, dict):
                        self.app_versions.setdefault(pf_or_ver, {}).update(val)
                        for v_name, v_info in val.items():
                            if isinstance(v_info, dict):
                                ids = v_info.get("installation_ids") or v_info.get("user_ids")
                                if ids:
                                    legacy_ids_to_migrate.append((pf_or_ver, v_name, list(ids)))
                    elif isinstance(val, dict):
                        # Backward compat: flat dict -> assign to android by default
                        pf = val.get("platform", "android")
                        self.app_versions.setdefault(pf, {})[pf_or_ver] = val
                        ids = val.get("installation_ids") or val.get("user_ids")
                        if ids:
                            legacy_ids_to_migrate.append((pf, pf_or_ver, list(ids)))

            self.updated_at = data.get("updated_at")
            self.watermark = data.get("watermark")
            if data.get("app_id") and not self.app_id:
                self.app_id = data["app_id"]
                if hasattr(self.authority_store, "app_id") and not self.authority_store.app_id:
                    self.authority_store.app_id = self.app_id

            auth_data = data.get("authority")
            if isinstance(auth_data, dict):
                self.authority_metadata.update(auth_data)
                self.bootstrap_complete = bool(auth_data.get("bootstrap_complete", False))
            else:
                self.bootstrap_complete = bool(data.get("bootstrap_complete", False))

            self.authority_state_version = int(data.get("authority_state_version", 1))

            # Migrate legacy JSON installation_ids into SQLite authority store
            if legacy_ids_to_migrate:
                eff_app = self.app_id or "default"
                all_migrated_versions_complete = True
                for pf, v_name, ids in legacy_ids_to_migrate:
                    self.authority_store.add_installations(eff_app, pf, v_name, ids)
                    v_obj = self.app_versions.get(pf, {}).get(v_name)
                    if isinstance(v_obj, dict):
                        # Remove raw IDs from memory
                        v_obj.pop("installation_ids", None)
                        v_obj.pop("user_ids", None)
                        exact_count = self.authority_store.count_installations(eff_app, pf, v_name)
                        existing_users = max(
                            int(v_obj.get("lifetime_affected_users") or 0),
                            int(v_obj.get("affected_users") or 0),
                        )
                        if existing_users > 0 and exact_count < existing_users:
                            # Incomplete authority: IDs present but fewer than existing aggregate users
                            all_migrated_versions_complete = False
                            self.authority_store.mark_version_bootstrapped(eff_app, pf, v_name, False)
                            v_obj["lifetime_affected_users"] = existing_users
                            v_obj["affected_users"] = existing_users
                        else:
                            v_obj["lifetime_affected_users"] = exact_count
                            v_obj["affected_users"] = exact_count
                            self.authority_store.mark_version_bootstrapped(eff_app, pf, v_name, True)

                if all_migrated_versions_complete:
                    self.authority_store.mark_bootstrap_complete(eff_app, True)
                    self.bootstrap_complete = True
                    self.authority_metadata["bootstrap_complete"] = True
                else:
                    self.authority_store.mark_bootstrap_complete(eff_app, False)
                    self.bootstrap_complete = False
                    self.authority_metadata["bootstrap_complete"] = False

    def save(self) -> None:
        """Saves current catalog file to disk strictly conforming to Schema V2.3 without raw installation IDs."""
        if not self.catalog_path:
            return
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        now_iso = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
        self.updated_at = now_iso
        eff_app = self.app_id or "default"

        # Sync exact deduplicated counts from SQLite authority store into app_versions
        for pf, vers in self.app_versions.items():
            for v_name, v_info in vers.items():
                if isinstance(v_info, dict):
                    # Never serialize raw installation_ids or user_ids into JSON
                    v_info.pop("installation_ids", None)
                    v_info.pop("user_ids", None)

                    # Query exact deduplicated count from SQLite authority store
                    exact_count = self.authority_store.count_installations(eff_app, pf, v_name)
                    has_auth = self.authority_store.has_version_authority(eff_app, pf, v_name)
                    if has_auth:
                        final_users = exact_count
                    else:
                        existing_users = max(
                            int(v_info.get("lifetime_affected_users") or 0),
                            int(v_info.get("affected_users") or 0),
                        )
                        final_users = max(existing_users, exact_count)
                    v_info["lifetime_affected_users"] = final_users
                    v_info["affected_users"] = final_users

        payload: dict[str, Any] = {
            "schema_version": "2.3.0",
            "authority": {
                "backend": "sqlite",
                "state_version": self.authority_store.get_state_version(),
                "bootstrap_complete": getattr(self, "bootstrap_complete", False),
            },
            "authority_state_version": getattr(self, "authority_state_version", 1),
            "bootstrap_complete": getattr(self, "bootstrap_complete", False),
            "updated_at": now_iso,
            "watermark": self.watermark,
            "issues": self.issues,
            "app_versions": self.app_versions,
        }
        if self.app_id:
            payload["app_id"] = self.app_id

        self.catalog_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def update_app_versions(
        self,
        version_health: Iterable[dict],
        platform: str | None = None,
        window: int | str | None = None,
    ) -> None:
        """Records version-level metrics, lifetime counts, and windowed recent health into catalog per platform."""
        now_iso = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
        for v in version_health:
            if not isinstance(v, dict):
                continue
            ver = str(v.get("version", "")).strip()
            if not ver:
                continue

            pf = v.get("platform") or platform or "android"
            pf = "ios" if pf == "ios" else "android"

            self.app_versions.setdefault(pf, {})
            existing = self.app_versions[pf].get(ver, {})

            adoption = v.get("adoption_rate") if v.get("adoption_rate") is not None else existing.get("adoption_rate")
            sessions = v.get("sessions_total") if v.get("sessions_total") is not None else existing.get("sessions_total")
            events = v.get("crash_events", 0) if v.get("crash_events") is not None else existing.get("crash_events", 0)
            users = v.get("affected_users", 0) if v.get("affected_users") is not None else existing.get("affected_users", 0)
            status = v.get("status") or existing.get("status") or "active"
            cfu_rate = v.get("crash_free_users_rate") if v.get("crash_free_users_rate") is not None else existing.get("crash_free_users_rate")
            cfs_rate = v.get("crash_free_sessions_rate") if v.get("crash_free_sessions_rate") is not None else existing.get("crash_free_sessions_rate")

            # Authoritative release date: ONLY set if explicitly provided, NEVER fake first_seen as release_date
            rel_date = v.get("release_date") or existing.get("release_date")
            first_seen = v.get("first_seen") or existing.get("first_seen")
            last_seen = v.get("last_seen") or existing.get("last_seen")
            # NOTE: Watermark is strictly owned by genuine version_catalog ingestion;
            # window-scoped version_health snapshots MUST NOT advance the incremental checkpoint.

            is_suff = is_version_sample_sufficient({
                "adoption_rate": adoption,
                "sessions_total": sessions,
                "crash_events": events,
                "sample_sufficient": v.get("sample_sufficient") or existing.get("sample_sufficient"),
            })

            # Lifetime metrics: never sum across windows or days; use max / deduplicated
            lifetime_crashes = max(int(existing.get("lifetime_crashes") or 0), int(v.get("lifetime_crashes") or 0), int(events))
            lifetime_users = max(int(existing.get("lifetime_affected_users") or 0), int(v.get("lifetime_affected_users") or 0), int(users))
            lifetime_issues = max(int(existing.get("lifetime_issues") or 0), int(v.get("lifetime_issues") or 0))
            lifetime_fatal = max(int(existing.get("lifetime_fatal") or 0), int(v.get("lifetime_fatal") or 0))
            lifetime_anr = max(int(existing.get("lifetime_anr") or 0), int(v.get("lifetime_anr") or 0))

            # Update recent health dictionary per window (strictly window-scoped, NEVER leaking lifetime counts)
            recent_health = dict(existing.get("recent_health") or {})
            if window is not None:
                w_key = str(window)
                clean_w = w_key.rstrip("d")
                fat_val = v.get("fatal_events") if v.get("fatal_events") is not None else (v.get("fatal_count") if v.get("fatal_count") is not None else (v.get("lifetime_fatal") or 0))
                anr_val = v.get("anr_events") if v.get("anr_events") is not None else (v.get("anr_count") if v.get("anr_count") is not None else (v.get("lifetime_anr") or 0))
                fat_int = int(fat_val or 0)
                anr_int = int(anr_val or 0)
                w_data = {
                    "crash_events": int(events),
                    "affected_users": int(users),
                    "sessions_total": sessions,
                    "crash_free_users_rate": v.get("crash_free_users_rate"),
                    "crash_free_sessions_rate": v.get("crash_free_sessions_rate"),
                    "adoption_rate": adoption,
                    "fatal_events": fat_int,
                    "fatal_count": fat_int,
                    "anr_events": anr_int,
                    "anr_count": anr_int,
                    "new_issues_count": int(v.get("new_issues_count", 0)),
                    "active_issues_count": int(v.get("new_issues_count", 0)),
                    "sample_sufficient": is_suff,
                    "status": status,
                    "trend": v.get("trend") or "stable",
                }
                recent_health[clean_w] = w_data
                recent_health[f"{clean_w}d"] = w_data

            self.app_versions[pf][ver] = {
                "version": ver,
                "platform": pf,
                "status": status,
                "adoption_rate": adoption,
                "sessions_total": sessions,
                "crash_events": events,
                "crash_free_users_rate": cfu_rate,
                "crash_free_sessions_rate": cfs_rate,
                "sample_sufficient": is_suff,
                "release_date": rel_date,
                "first_seen": first_seen,
                "last_seen": last_seen,
                "lifetime_crashes": lifetime_crashes,
                "lifetime_issues": lifetime_issues,
                "lifetime_affected_users": lifetime_users,
                "lifetime_fatal": lifetime_fatal,
                "lifetime_anr": lifetime_anr,
                "recent_health": recent_health,
                "last_updated": now_iso,
            }

    def update_from_issues(self, issues: Iterable[dict]) -> None:
        """Merges a list of issues and their version distributions into the catalog with platform isolation."""
        now_iso = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
        for iss in issues:
            iid = iss.get("issue_id")
            if not iid:
                continue

            pf = "ios" if iss.get("platform") == "ios" else "android"
            canonical_key = self._canonical_key(pf, iid)

            v_dist = iss.get("version_distribution") or []
            dist_versions = [str(v["version"]).strip() for v in v_dist if isinstance(v, dict) and v.get("version")]
            iss_versions = set(dist_versions)
            if iss.get("first_seen_version"):
                iss_versions.add(str(iss["first_seen_version"]).strip())
            if iss.get("last_seen_version"):
                iss_versions.add(str(iss["last_seen_version"]).strip())

            ts_first = iss.get("first_seen_timestamp")
            ts_last = iss.get("last_seen_timestamp")
            err_type = iss.get("error_type", "NON_FATAL")

            existing = self.issues.get(canonical_key)
            if existing:
                all_vers = set(existing.get("versions_seen", [])) | iss_versions
                sorted_vers = sorted(list(all_vers), key=version_key)

                all_candidates_first = [existing.get("first_seen_version"), iss.get("first_seen_version")] + sorted_vers
                all_candidates_last = [existing.get("last_seen_version"), iss.get("last_seen_version")] + sorted_vers

                f_ver = min_version(all_candidates_first)
                l_ver = max_version(all_candidates_last)

                ts_first_list = [t for t in [existing.get("first_seen_timestamp"), ts_first] if t]
                ts_last_list = [t for t in [existing.get("last_seen_timestamp"), ts_last] if t]

                existing["versions_seen"] = sorted_vers
                existing["first_seen_version"] = f_ver or existing.get("first_seen_version")
                existing["last_seen_version"] = l_ver or existing.get("last_seen_version")
                if ts_first_list:
                    existing["first_seen_timestamp"] = min(ts_first_list)
                if ts_last_list:
                    existing["last_seen_timestamp"] = max(ts_last_list)
                existing["last_updated"] = now_iso
            else:
                sorted_vers = sorted(list(iss_versions), key=version_key)
                f_ver = min_version(sorted_vers) or iss.get("first_seen_version") or "1.0.0"
                l_ver = max_version(sorted_vers) or iss.get("last_seen_version") or f_ver

                self.issues[canonical_key] = {
                    "issue_id": iid,
                    "platform": pf,
                    "title": iss.get("title", ""),
                    "subtitle": iss.get("subtitle", ""),
                    "error_type": err_type,
                    "first_seen_version": f_ver,
                    "last_seen_version": l_ver,
                    "first_seen_timestamp": ts_first,
                    "last_seen_timestamp": ts_last,
                    "versions_seen": sorted_vers,
                    "last_updated": now_iso,
                }

            # Register/update each version entity in self.app_versions
            self.app_versions.setdefault(pf, {})
            for v_name in iss_versions:
                if not v_name:
                    continue
                v_obj = self.app_versions[pf].setdefault(v_name, {
                    "version": v_name,
                    "platform": pf,
                    "status": "active",
                    "adoption_rate": None,
                    "sessions_total": None,
                    "crash_events": 0,
                    "sample_sufficient": False,
                    "release_date": None,
                    "first_seen": None,
                    "last_seen": None,
                    "lifetime_crashes": 0,
                    "lifetime_issues": 0,
                    "lifetime_affected_users": 0,
                    "lifetime_fatal": 0,
                    "lifetime_anr": 0,
                    "recent_health": {},
                    "last_updated": now_iso,
                })
                if ts_first:
                    v_obj["first_seen"] = min(v_obj["first_seen"], ts_first) if v_obj.get("first_seen") else ts_first
                if ts_last:
                    v_obj["last_seen"] = max(v_obj["last_seen"], ts_last) if v_obj.get("last_seen") else ts_last

    def update_from_catalog_rows(
        self,
        rows: Iterable[dict],
        is_incremental: bool = False,
        advance_watermark: bool = True,
        checkpoint_watermark: str | None = None,
        is_bootstrap: bool | None = None,
    ) -> None:
        """Ingests broad catalog query rows (issue_id, app_version, first_seen_ts, last_seen_ts, events, users, fatal, anr)."""
        now_iso = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
        eval_watermark = checkpoint_watermark if checkpoint_watermark is not None else self.watermark
        eff_app = self.app_id or "default"
        effective_bootstrap = is_bootstrap if is_bootstrap is not None else (not is_incremental)
        if advance_watermark and not is_incremental:
            self.bootstrap_complete = True
            self.authority_store.mark_bootstrap_complete(eff_app, True)

        for row in rows:
            iid = row.get("issue_id")
            ver = str(row.get("app_version") or row.get("version") or "").strip()
            if not ver:
                continue

            pf = "ios" if (row.get("platform") or row.get("_platform")) == "ios" else "android"
            self.app_versions.setdefault(pf, {})
            v_obj = self.app_versions[pf].setdefault(ver, {
                "version": ver,
                "platform": pf,
                "status": "active",
                "adoption_rate": None,
                "sessions_total": None,
                "crash_events": 0,
                "sample_sufficient": False,
                "release_date": None,
                "first_seen": None,
                "last_seen": None,
                "lifetime_crashes": 0,
                "lifetime_issues": 0,
                "lifetime_affected_users": 0,
                "lifetime_fatal": 0,
                "lifetime_anr": 0,
                "recent_health": {},
                "last_updated": now_iso,
            })

            ts_first = row.get("first_seen_timestamp") or row.get("first_seen")
            ts_last = row.get("last_seen_timestamp") or row.get("last_seen") or row.get("event_timestamp")
            if ts_first:
                v_obj["first_seen"] = min(v_obj["first_seen"], ts_first) if v_obj.get("first_seen") else ts_first
            if ts_last:
                v_obj["last_seen"] = max(v_obj["last_seen"], ts_last) if v_obj.get("last_seen") else ts_last

            # Watermark Boundary Idempotency: in incremental mode, if ts_last <= eval_watermark,
            # this batch/row was already included in the catalog up to watermark and must not be double-added.
            is_already_processed = bool(is_incremental and eval_watermark and ts_last and self._is_ts_le(ts_last, eval_watermark))

            ev_count = row.get("crash_events") if row.get("crash_events") is not None else (
                row.get("lifetime_crashes") if row.get("lifetime_crashes") is not None else (None if iid else row.get("events"))
            )
            if ev_count is not None:
                if is_incremental:
                    if not is_already_processed:
                        v_obj["lifetime_crashes"] = int(v_obj.get("lifetime_crashes") or 0) + int(ev_count)
                else:
                    v_obj["lifetime_crashes"] = max(int(v_obj.get("lifetime_crashes") or 0), int(ev_count))
                v_obj["crash_events"] = v_obj["lifetime_crashes"]

            fat_count = row.get("fatal_events") if row.get("fatal_events") is not None else (
                row.get("fatal_count") if row.get("fatal_count") is not None else (None if iid else row.get("lifetime_fatal"))
            )
            if fat_count is not None:
                if is_incremental:
                    if not is_already_processed:
                        v_obj["lifetime_fatal"] = int(v_obj.get("lifetime_fatal") or 0) + int(fat_count)
                else:
                    v_obj["lifetime_fatal"] = max(int(v_obj.get("lifetime_fatal") or 0), int(fat_count))

            anr_count = row.get("anr_events") if row.get("anr_events") is not None else (
                row.get("anr_count") if row.get("anr_count") is not None else (None if iid else row.get("lifetime_anr"))
            )
            if anr_count is not None:
                if is_incremental:
                    if not is_already_processed:
                        v_obj["lifetime_anr"] = int(v_obj.get("lifetime_anr") or 0) + int(anr_count)
                else:
                    v_obj["lifetime_anr"] = max(int(v_obj.get("lifetime_anr") or 0), int(anr_count))

            # Deduplication for affected users using SQLite Authority Store
            inst_set = self._version_installations.setdefault(pf, {}).setdefault(ver, set())
            raw_insts = row.get("installation_ids") or row.get("user_ids") or row.get("installations")
            new_ids: list[str] = []
            if raw_insts:
                if isinstance(raw_insts, (list, set, tuple)):
                    new_ids.extend(str(x) for x in raw_insts if x)
                else:
                    new_ids.append(str(raw_insts))
            if row.get("installation_uuid"):
                new_ids.append(str(row["installation_uuid"]))

            if new_ids:
                inst_set.update(new_ids)
                self.authority_store.add_installations(
                    eff_app, pf, ver, new_ids, first_seen=ts_first, last_seen=ts_last
                )

            sqlite_count = self.authority_store.count_installations(eff_app, pf, ver)

            usr_count = row.get("affected_users") if row.get("affected_users") is not None else (
                row.get("lifetime_affected_users") if row.get("lifetime_affected_users") is not None else (
                    row.get("lifetime_users") if row.get("lifetime_users") is not None else row.get("users")
                )
            )

            existing_users = max(
                int(v_obj.get("lifetime_affected_users") or 0),
                int(v_obj.get("affected_users") or 0),
            )

            if effective_bootstrap:
                # Bootstrap run: exact count from authority store is authoritative for all scanned versions,
                # even if affected_users == 0 (e.g. all installation UUIDs are NULL).
                if sqlite_count > 0:
                    final_users = sqlite_count
                    self.authority_store.mark_version_bootstrapped(eff_app, pf, ver, True)
                elif int(usr_count or 0) == 0:
                    # Genuinely 0 users scanned during bootstrap (e.g. all UUIDs NULL)
                    final_users = 0
                    self.authority_store.mark_version_bootstrapped(eff_app, pf, ver, True)
                else:
                    # Caller passed aggregate users without SQLite installation IDs (e.g. mock test)
                    final_users = max(existing_users, int(usr_count or 0))
                    self.authority_store.mark_version_bootstrapped(eff_app, pf, ver, False)
            elif sqlite_count > 0:
                if sqlite_count >= existing_users:
                    # Complete exact deduplication: sqlite_count is fully established
                    final_users = sqlite_count
                    self.authority_store.mark_version_bootstrapped(eff_app, pf, ver, True)
                else:
                    # Partial / incomplete authority: preserve existing monotonic users without claiming exact authority
                    final_users = existing_users
                    if usr_count is not None and not is_already_processed:
                        final_users = max(final_users, int(usr_count))
                    self.authority_store.mark_version_bootstrapped(eff_app, pf, ver, False)
                    self.bootstrap_complete = False
                    self.authority_metadata["bootstrap_complete"] = False
                    self.authority_store.mark_bootstrap_complete(eff_app, False)
            else:
                # No SQLite installations ingested (e.g. caller passed aggregate users)
                final_users = existing_users
                if usr_count is not None and not is_already_processed:
                    final_users = max(final_users, int(usr_count))

            v_obj["lifetime_affected_users"] = final_users
            v_obj["affected_users"] = final_users
            # Purge raw installation_ids and user_ids from memory dictionary
            v_obj.pop("installation_ids", None)
            v_obj.pop("user_ids", None)

            # Deduplication for issues count
            iss_count = row.get("issues_count") if row.get("issues_count") is not None else row.get("lifetime_issues")
            if iid:
                canonical_key = self._canonical_key(pf, iid)
                existing = self.issues.get(canonical_key)
                if existing:
                    all_vers = set(existing.get("versions_seen", [])) | {ver}
                    sorted_vers = sorted(list(all_vers), key=version_key)
                    existing["versions_seen"] = sorted_vers
                    first_candidates = [v for v in (existing.get("first_seen_version"), ver) if v]
                    existing["first_seen_version"] = min_version(first_candidates) or ver
                    last_candidates = [v for v in (existing.get("last_seen_version"), ver) if v]
                    existing["last_seen_version"] = max_version(last_candidates) or ver
                    if ts_first and (not existing.get("first_seen_timestamp") or ts_first < existing["first_seen_timestamp"]):
                        existing["first_seen_timestamp"] = ts_first
                    if ts_last and (not existing.get("last_seen_timestamp") or ts_last > existing["last_seen_timestamp"]):
                        existing["last_seen_timestamp"] = ts_last
                    existing["last_updated"] = now_iso
                else:
                    self.issues[canonical_key] = {
                        "issue_id": iid,
                        "platform": pf,
                        "title": row.get("title", ""),
                        "subtitle": row.get("subtitle", ""),
                        "error_type": row.get("error_type", "NON_FATAL"),
                        "first_seen_version": ver,
                        "last_seen_version": ver,
                        "first_seen_timestamp": ts_first,
                        "last_seen_timestamp": ts_last,
                        "versions_seen": [ver],
                        "last_updated": now_iso,
                    }

            known_ver_issues = {
                iss_obj.get("issue_id")
                for iss_obj in self.issues.values()
                if iss_obj.get("platform") == pf and ver in iss_obj.get("versions_seen", []) and iss_obj.get("issue_id")
            }
            if known_ver_issues:
                v_obj["lifetime_issues"] = max(len(known_ver_issues), int(v_obj.get("lifetime_issues") or 0))
            elif iss_count is not None:
                if is_incremental:
                    if not is_already_processed:
                        v_obj["lifetime_issues"] = max(int(v_obj.get("lifetime_issues") or 0), int(iss_count))
                else:
                    v_obj["lifetime_issues"] = max(int(v_obj.get("lifetime_issues") or 0), int(iss_count))

            if advance_watermark and ts_last:
                self.advance_watermark(ts_last)

    def calculate_version_status(
        self,
        version: str,
        platform: str,
        latest_version: str | None = None,
        reference_time: dt.datetime | None = None,
    ) -> Literal["latest", "active", "legacy"]:
        """Evaluates whether a version is latest, active, or legacy (>90d inactive)."""
        return calculate_version_status(
            self.app_versions,
            version,
            platform,
            latest_version=latest_version,
            reference_time=reference_time,
            known_versions=self.get_known_app_versions(platform=platform),
        )

    def build_release_catalog(
        self,
        app_data: dict | None = None,
        platform: str | None = None,
        reference_date: Any | None = None,
        gate_policy: Any | None = None,
    ) -> list[ReleaseCatalogItem]:
        """Constructs the decoupled persistent release catalog conforming to ReleaseCatalogItem."""
        return build_release_catalog(
            catalog=self,
            app_data=app_data,
            platform=platform,
            reference_date=reference_date,
            gate_policy=gate_policy,
        )

    def get_known_app_versions(self, platform: str | None = None) -> list[str]:
        """Returns sorted list of all known app versions recorded in catalog, optionally isolated by platform."""
        v_set: set[str] = set()
        platforms_to_check = [platform] if platform in ("android", "ios") else ["android", "ios"]
        for p in platforms_to_check:
            v_set.update(self.app_versions.get(p, {}).keys())

        for iss in self.issues.values():
            iss_p = iss.get("platform", "android")
            if platform is None or iss_p == platform:
                for v in iss.get("versions_seen", []):
                    if v:
                        v_set.add(v)
        return sorted(list(v_set), key=version_key)

    def get_version_info(self, version: str, platform: str | None = None) -> dict[str, Any] | None:
        if platform:
            return self.app_versions.get(platform, {}).get(version)
        return self.app_versions.get("android", {}).get(version) or self.app_versions.get("ios", {}).get(version)

    def get_issue_history(self, issue_id: str, platform: str | None = None) -> dict[str, Any] | None:
        if platform:
            canonical = self._canonical_key(platform, issue_id)
            return self.issues.get(canonical)
        for pf in ("android", "ios"):
            ck = self._canonical_key(pf, issue_id)
            if ck in self.issues:
                return self.issues[ck]
        return self.issues.get(issue_id)


def _enrich_issue_with_history(iss: dict, hist: dict | None) -> None:
    """Enriches an issue dictionary with historical authority, ensuring historical first seen is preserved."""
    if hist:
        hist_f_ver = hist.get("first_seen_version")
        if hist_f_ver:
            if iss.get("first_seen_version"):
                earliest_v = min_version([iss["first_seen_version"], hist_f_ver])
                if earliest_v:
                    iss["first_seen_version"] = earliest_v
            else:
                iss["first_seen_version"] = hist_f_ver

        hist_l_ver = hist.get("last_seen_version")
        if hist_l_ver:
            if iss.get("last_seen_version"):
                latest_v = max_version([iss["last_seen_version"], hist_l_ver])
                if latest_v:
                    iss["last_seen_version"] = latest_v
            else:
                iss["last_seen_version"] = hist_l_ver

        hist_f_ts = hist.get("first_seen_timestamp")
        if hist_f_ts:
            if iss.get("first_seen_timestamp"):
                iss["first_seen_timestamp"] = min(iss["first_seen_timestamp"], hist_f_ts)
            else:
                iss["first_seen_timestamp"] = hist_f_ts

        hist_l_ts = hist.get("last_seen_timestamp")
        if hist_l_ts:
            if iss.get("last_seen_timestamp"):
                iss["last_seen_timestamp"] = max(iss["last_seen_timestamp"], hist_l_ts)
            else:
                iss["last_seen_timestamp"] = hist_l_ts

    timeline = iss.get("occurrence_timeline")
    if isinstance(timeline, dict):
        if iss.get("first_seen_timestamp"):
            timeline["first_seen_date"] = iss["first_seen_timestamp"][:10]
        if iss.get("last_seen_timestamp"):
            timeline["last_seen_date"] = iss["last_seen_timestamp"][:10]


def enrich_app_data_with_lifecycle(
    app_data: dict,
    catalog: IssueHistoricalCatalog | None = None,
    app_name: str | None = None,
    out_dir: Path | None = None,
    catalog_rows: Iterable[dict] | None = None,
    version_catalog_rows: Iterable[dict] | None = None,
    is_bootstrap: bool = False,
    is_incremental: bool | None = None,
    gate_policy: Any | None = None,
) -> dict:
    """Enriches app_data top_issues, all periods snapshots, and builds persistent release_catalog,
    strictly isolating Android and iOS version sequences and latest versions.
    """
    if not isinstance(app_data, dict):
        return app_data

    # 1. Catalog setup and load
    cat = catalog
    if cat is None:
        effective_app_id = app_name or app_data.get("metadata", {}).get("app_id")
        cat_path = None
        if effective_app_id:
            effective_out = out_dir or (ROOT / "out")
            cat_path = effective_out / effective_app_id / "historical_catalog.json"
        cat = IssueHistoricalCatalog(catalog_path=cat_path, app_id=effective_app_id)
        cat.load()

    if is_bootstrap:
        cat.bootstrap_complete = True

    if is_incremental is None:
        is_incremental = False

    # Freeze checkpoint before processing any catalog rows in this run
    initial_checkpoint = cat.watermark

    if catalog_rows:
        # catalog_rows contains issue-level distribution (e.g. from 90-day lifecycle_catalog)
        # to update issue first/last seen and versions_seen; it is non-incremental for version totals
        # and MUST NOT advance the global incremental watermark.
        cat.update_from_catalog_rows(catalog_rows, is_incremental=False, advance_watermark=False)

    if version_catalog_rows:
        # version_catalog_rows is authoritative for version totals and watermark progression.
        cat.update_from_catalog_rows(
            version_catalog_rows,
            is_incremental=is_incremental,
            advance_watermark=True,
            checkpoint_watermark=initial_checkpoint,
            is_bootstrap=is_bootstrap,
        )

    # Ingest app_versions from current version_health into catalog
    vh = app_data.get("version_health") or []
    cat.update_app_versions(vh)

    periods = app_data.get("periods") or {}
    if isinstance(periods, dict):
        for p_k, snap in periods.items():
            if isinstance(snap, dict):
                snap_vh = snap.get("version_health")
                if snap_vh:
                    cat.update_app_versions(snap_vh, window=p_k)

    # Collect all issues across top_issues and all period snapshots and update catalog
    all_issues_to_index: list[dict] = []
    if isinstance(app_data.get("top_issues"), list):
        all_issues_to_index.extend(app_data["top_issues"])

    if isinstance(periods, dict):
        for snap in periods.values():
            if isinstance(snap, dict) and isinstance(snap.get("top_issues"), list):
                all_issues_to_index.extend(snap["top_issues"])

    cat.update_from_issues(all_issues_to_index)
    cat.save()

    # 2. Record historical_catalog status in sources
    if "sources" in app_data and isinstance(app_data["sources"], dict):
        app_data["sources"]["historical_catalog"] = {
            "status": "available",
            "last_sync_timestamp": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
            "error_message": None,
        }

    # 3. Determine per-platform version universe and latest version
    per_pf_latest: dict[str, str] = {}
    per_pf_known: dict[str, list[str]] = {}
    per_pf_sufficiency: dict[str, dict[str, bool]] = {}

    for pf in ("android", "ios"):
        # Filter version health for pf
        pf_vh = [
            v for v in vh
            if isinstance(v, dict) and (v.get("platform") is None or v.get("platform") in (pf, "all"))
        ]
        pf_vh_map = {str(v.get("version")).strip(): v for v in pf_vh if v.get("version")}

        # Gather known versions for pf
        known_set = set(pf_vh_map.keys())
        for d in app_data.get("distributions", {}).get("app_versions") or []:
            if isinstance(d, dict) and d.get("app_version"):
                d_pf = d.get("platform")
                if d_pf is None or d_pf in (pf, "all"):
                    known_set.add(str(d["app_version"]).strip())

        for cv in cat.get_known_app_versions(platform=pf):
            known_set.add(cv)

        # Authoritative latest version for pf
        latest_v = get_latest_app_version(app_data, platform=pf, catalog=cat)
        if not latest_v:
            latest_v = (max_version(list(known_set)) if known_set else None) or "1.0.0"
        known_set.add(latest_v)
        sorted_pf_versions = sorted(list(known_set), key=version_key)

        # Build sample sufficiency for pf
        suff_map: dict[str, bool] = {}
        for v in sorted_pf_versions:
            v_info = pf_vh_map.get(v) or cat.get_version_info(v, platform=pf)
            suff_map[v] = is_version_sample_sufficient(v_info)

        per_pf_latest[pf] = latest_v
        per_pf_known[pf] = sorted_pf_versions
        per_pf_sufficiency[pf] = suff_map

    # 4. Enrich top-level top_issues with platform-isolated version universes
    if isinstance(app_data.get("top_issues"), list):
        for iss in app_data["top_issues"]:
            iid = iss.get("issue_id", "")
            iss_pf = "ios" if iss.get("platform") == "ios" else "android"

            hist = cat.get_issue_history(iid, platform=iss_pf)
            hist_versions = hist.get("versions_seen", []) if hist else []

            _enrich_issue_with_history(iss, hist)

            v_dist = iss.get("version_distribution") or []
            v_events = {v["version"]: v.get("events", 0) for v in v_dist if isinstance(v, dict) and v.get("version")}

            target_latest = per_pf_latest.get(iss_pf, "1.0.0")
            target_known = per_pf_known.get(iss_pf, [target_latest])
            target_suff = per_pf_sufficiency.get(iss_pf, {})
            latest_is_suff = target_suff.get(target_latest, False)

            lc = detect_issue_lifecycle(
                issue_id=iid,
                historical_versions=hist_versions,
                all_known_versions=target_known,
                latest_version=target_latest,
                sample_sufficient=latest_is_suff,
                current_version_events=v_events,
                known_version_sufficiency=target_suff,
            )
            iss["lifecycle"] = lc

    # 5. Enrich each period snapshot's top_issues with platform isolation
    if isinstance(periods, dict):
        for snap in periods.values():
            if not isinstance(snap, dict):
                continue

            snap_vh = snap.get("version_health") or vh
            snap_dist_v = snap.get("distributions", {}).get("app_versions") or []

            # Snapshot per-platform universes
            snap_pf_latest: dict[str, str] = {}
            snap_pf_known: dict[str, list[str]] = {}
            snap_pf_sufficiency: dict[str, dict[str, bool]] = {}

            for pf in ("android", "ios"):
                snap_pf_vh = [
                    v for v in snap_vh
                    if isinstance(v, dict) and (v.get("platform") is None or v.get("platform") in (pf, "all"))
                ]
                snap_pf_vh_map = {str(v.get("version")).strip(): v for v in snap_pf_vh if v.get("version")}

                snap_known_set = set(snap_pf_vh_map.keys())
                for d in snap_dist_v:
                    if isinstance(d, dict) and d.get("app_version"):
                        d_pf = d.get("platform")
                        if d_pf is None or d_pf in (pf, "all"):
                            snap_known_set.add(str(d["app_version"]).strip())

                for cv in cat.get_known_app_versions(platform=pf):
                    snap_known_set.add(cv)

                snap_latest_v = get_latest_app_version(snap, platform=pf, catalog=cat) or per_pf_latest.get(pf, "1.0.0")
                snap_known_set.add(snap_latest_v)
                sorted_snap_pf_versions = sorted(list(snap_known_set), key=version_key)

                snap_suff_map: dict[str, bool] = {}
                for v in sorted_snap_pf_versions:
                    v_info = snap_pf_vh_map.get(v) or cat.get_version_info(v, platform=pf)
                    snap_suff_map[v] = is_version_sample_sufficient(v_info)

                snap_pf_latest[pf] = snap_latest_v
                snap_pf_known[pf] = sorted_snap_pf_versions
                snap_pf_sufficiency[pf] = snap_suff_map

            snap_issues = snap.get("top_issues") or []
            for iss in snap_issues:
                iid = iss.get("issue_id", "")
                iss_pf = "ios" if iss.get("platform") == "ios" else "android"

                hist = cat.get_issue_history(iid, platform=iss_pf)
                hist_versions = hist.get("versions_seen", []) if hist else []

                _enrich_issue_with_history(iss, hist)

                v_dist = iss.get("version_distribution") or []
                v_events = {v["version"]: v.get("events", 0) for v in v_dist if isinstance(v, dict) and v.get("version")}

                target_snap_latest = snap_pf_latest.get(iss_pf, "1.0.0")
                target_snap_known = snap_pf_known.get(iss_pf, [target_snap_latest])
                target_snap_suff = snap_pf_sufficiency.get(iss_pf, {})
                snap_latest_is_suff = target_snap_suff.get(target_snap_latest, False)

                lc = detect_issue_lifecycle(
                    issue_id=iid,
                    historical_versions=hist_versions,
                    all_known_versions=target_snap_known,
                    latest_version=target_snap_latest,
                    sample_sufficient=snap_latest_is_suff,
                    current_version_events=v_events,
                    known_version_sufficiency=target_snap_suff,
                )
                iss["lifecycle"] = lc

    # 6. Build persistent Release Catalog and attach to app_data and period snapshots
    effective_policy = gate_policy
    if effective_policy is None:
        effective_app_id = app_name or (app_data.get("metadata", {}).get("app_id") if isinstance(app_data, dict) else None)
        app_cfg = None
        if effective_app_id:
            try:
                from crash_trend.config import load_config
                cfg = load_config()
                app_cfg = (cfg.get("apps") or {}).get(effective_app_id)
            except Exception:
                app_cfg = None
        try:
            from crash_trend.gate.policy import load_gate_policy
            effective_policy = load_gate_policy(app_cfg)
        except Exception:
            effective_policy = None

    release_catalog = cat.build_release_catalog(app_data, gate_policy=effective_policy)
    app_data["release_catalog"] = release_catalog
    if isinstance(periods, dict):
        for snap in periods.values():
            if isinstance(snap, dict):
                snap["release_catalog"] = release_catalog

    cat.save()
    if catalog is None:
        cat.close()
    return app_data
