"""Issue Historical Catalog and Deterministic Lifecycle Engine (Issue #29).

Provides:
- IssueHistoricalCatalog: Cross-window persistence of true historical first_seen,
  last_seen, version distributions, and app_versions per issue with strict platform isolation.
- detect_issue_lifecycle: Deterministic evaluation of the 5 lifecycle states:
  new_in_latest, persistent, regressed, resolved, not_observed_latest.
- enrich_app_data_with_lifecycle: Enriches AppDashboardV2Data top_issues and period snapshots
  with per-platform isolation and catalog tracking.
- bootstrap_catalog_from_disk: Offline / local bootstrap from existing monthly report data and snapshots.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

try:
    from crash_trend.config import ROOT, get_app, load_config, out_dir
    from crash_trend.schema_v2 import (
        HistoricalCatalogData,
        IssueLifecycle,
        LifecycleStatus,
        PreviousReleaseComparison,
        ReleaseCatalogItem,
        ReleaseIssueLifecycle,
        ReleaseRecentHealth,
        ReleaseStatus,
    )
    from crash_trend.versions import max_version, min_version, version_key
except ImportError:
    from config import ROOT, get_app, load_config, out_dir  # type: ignore
    from schema_v2 import (  # type: ignore
        HistoricalCatalogData,
        IssueLifecycle,
        LifecycleStatus,
        PreviousReleaseComparison,
        ReleaseCatalogItem,
        ReleaseIssueLifecycle,
        ReleaseRecentHealth,
        ReleaseStatus,
    )
    from versions import max_version, min_version, version_key  # type: ignore


def get_latest_app_version(
    app_data: dict,
    platform: Optional[str] = None,
    catalog: Optional["IssueHistoricalCatalog"] = None,
) -> str | None:
    """Extracts the true latest app version from authoritative catalog or app data.
    Strictly filters by platform ('android' or 'ios') if specified to prevent cross-platform pollution.

    Priority:
    1. If catalog is provided, combine catalog.get_known_app_versions(platform) with any valid versions in app_data,
       and return max_version across the union. This guarantees latest release resolution is decoupled from
       window-limited Top-N crash ranking (Blocker #2).
    2. If catalog is not provided:
       a. Max semver version among version_health items (filtered by platform)
       b. Max semver version in distributions.app_versions (filtered by platform)
    3. None (do NOT infer from top_issues.last_seen_version to avoid false positives)
    """
    if not isinstance(app_data, dict):
        if catalog:
            cat_versions = catalog.get_known_app_versions(platform=platform)
            return max_version(cat_versions) if cat_versions else None
        return None

    vh = app_data.get("version_health") or []
    filtered_vh = []
    for v in vh:
        if not isinstance(v, dict) or not v.get("version"):
            continue
        v_pf = v.get("platform")
        if platform is None or v_pf is None or v_pf == platform or v_pf == "all":
            filtered_vh.append(v)

    vh_versions = [str(v.get("version")).strip() for v in filtered_vh if v.get("version")]

    dist_versions = app_data.get("distributions", {}).get("app_versions") or []
    dist_v_list = []
    for v in dist_versions:
        if not isinstance(v, dict) or not v.get("app_version"):
            continue
        v_pf = v.get("platform")
        if platform is None or v_pf is None or v_pf == platform or v_pf == "all":
            dist_v_list.append(str(v["app_version"]).strip())

    if catalog:
        cat_versions = catalog.get_known_app_versions(platform=platform)
        all_candidates = set(cat_versions) | set(vh_versions) | set(dist_v_list)
        if all_candidates:
            return max_version(list(all_candidates))

    if vh_versions:
        return max_version(vh_versions)

    if dist_v_list:
        return max_version(dist_v_list)

    return None


def is_version_sample_sufficient(
    version_info: Optional[dict],
    min_adoption_rate: float = 0.05,
    min_sessions: int = 1000,
    min_version_events: int = 20,
) -> bool:
    """Evaluates whether the given version has sufficient observation/adoption evidence.

    An issue with 0 events can only be labeled 'resolved' if the version itself
    has enough traffic/adoption. Otherwise, it is 'not_observed_latest'.
    Similarly, an intermediate version can only prove an absence gap for 'regressed'
    if the version had sufficient observation evidence.
    """
    if not isinstance(version_info, dict):
        return False

    if version_info.get("sample_sufficient") is True:
        return True

    adoption_rate = version_info.get("adoption_rate")
    if adoption_rate is not None and isinstance(adoption_rate, (int, float)):
        return float(adoption_rate) >= min_adoption_rate

    sessions_total = version_info.get("sessions_total")
    if sessions_total is not None and isinstance(sessions_total, (int, float)):
        return int(sessions_total) >= min_sessions

    crash_events = version_info.get("crash_events", 0)
    if isinstance(crash_events, (int, float)) and crash_events >= min_version_events:
        return True

    return False


def detect_issue_lifecycle(
    issue_id: str,
    historical_versions: Iterable[str],
    all_known_versions: Iterable[str],
    latest_version: str,
    sample_sufficient: bool = False,
    current_version_events: Optional[Dict[str, int]] = None,
    known_version_sufficiency: Optional[Dict[str, bool]] = None,
    version_health_map: Optional[Dict[str, dict]] = None,
) -> IssueLifecycle:
    """Deterministically calculates the lifecycle contract for an issue.

    States:
    - new_in_latest: First observed exclusively in latest_version.
    - persistent: Present in older version(s) and still occurring in latest_version without proven intermediate gaps.
    - regressed: Present in older version(s) -> absent in >= 1 intermediate valid version with sufficient sample -> reappeared.
    - resolved: Historically occurred, 0 events in latest_version with sufficient sample/adoption.
    - not_observed_latest: 0 events in latest_version, but sample/adoption is insufficient.
    """
    sorted_all_versions = sorted(list(set(v for v in all_known_versions if v)), key=version_key)
    seen_set = set(v for v in historical_versions if v)

    # Incorporate current_version_events if provided
    if current_version_events:
        for ver, ev_count in current_version_events.items():
            if ev_count > 0:
                seen_set.add(ver)

    sorted_seen = sorted(list(seen_set), key=version_key)
    if not sorted_seen:
        # Fallback if no version is recorded
        return {
            "status": "not_observed_latest",
            "latest_version": latest_version,
            "first_seen_version": latest_version,
            "last_seen_version": latest_version,
            "versions_seen": 0,
            "confidence": "low",
            "previously_absent_since": None,
            "reappeared_version": None,
            "reason": "尚無任何版本出現紀錄",
        }

    first_seen_ver = min_version(sorted_seen) or latest_version
    last_seen_ver = max_version(sorted_seen) or latest_version
    versions_seen_count = len(sorted_seen)

    # Check if this issue is observed in latest_version
    occurred_in_latest = latest_version in seen_set
    if current_version_events and latest_version in current_version_events:
        occurred_in_latest = current_version_events[latest_version] > 0

    if occurred_in_latest:
        if first_seen_ver == latest_version and versions_seen_count == 1:
            return {
                "status": "new_in_latest",
                "latest_version": latest_version,
                "first_seen_version": first_seen_ver,
                "last_seen_version": last_seen_ver,
                "versions_seen": versions_seen_count,
                "confidence": "high",
                "previously_absent_since": None,
                "reappeared_version": None,
                "reason": f"首次觀察即出現在最新版本 {latest_version}",
            }

        # Check for gaps in intermediate versions between first_seen_ver and latest_version
        intermediate = [
            v for v in sorted_all_versions
            if version_key(first_seen_ver) < version_key(v) < version_key(latest_version)
        ]
        absent_versions = [v for v in intermediate if v not in seen_set]

        # An intermediate version only counts as an absence gap if it had sufficient observation evidence!
        proven_absent_versions: List[str] = []
        for v in absent_versions:
            if known_version_sufficiency is not None:
                if known_version_sufficiency.get(v, False):
                    proven_absent_versions.append(v)
            elif version_health_map is not None:
                if is_version_sample_sufficient(version_health_map.get(v)):
                    proven_absent_versions.append(v)
            else:
                proven_absent_versions.append(v)

        if proven_absent_versions:
            return {
                "status": "regressed",
                "latest_version": latest_version,
                "first_seen_version": first_seen_ver,
                "last_seen_version": last_seen_ver,
                "versions_seen": versions_seen_count,
                "confidence": "high",
                "previously_absent_since": proven_absent_versions[0],
                "reappeared_version": latest_version,
                "reason": f"於版本 {proven_absent_versions[0]} 消失後在 {latest_version} 重新出現",
            }
        else:
            reason = f"自版本 {first_seen_ver} 持續存在至最新版本 {latest_version}"
            if absent_versions:
                reason += f" (中間版本 {', '.join(absent_versions)} 樣本不足以證明曾消失)"
            return {
                "status": "persistent",
                "latest_version": latest_version,
                "first_seen_version": first_seen_ver,
                "last_seen_version": last_seen_ver,
                "versions_seen": versions_seen_count,
                "confidence": "high",
                "previously_absent_since": None,
                "reappeared_version": None,
                "reason": reason,
            }
    else:
        # Issue not observed in latest_version
        if sample_sufficient:
            return {
                "status": "resolved",
                "latest_version": latest_version,
                "first_seen_version": first_seen_ver,
                "last_seen_version": last_seen_ver,
                "versions_seen": versions_seen_count,
                "confidence": "high",
                "previously_absent_since": latest_version,
                "reappeared_version": None,
                "reason": f"最新版本 {latest_version} 具備足夠樣本且未再觀察到",
            }
        else:
            return {
                "status": "not_observed_latest",
                "latest_version": latest_version,
                "first_seen_version": first_seen_ver,
                "last_seen_version": last_seen_ver,
                "versions_seen": versions_seen_count,
                "confidence": "medium",
                "previously_absent_since": None,
                "reappeared_version": None,
                "reason": f"最新版本 {latest_version} 尚未觀察到，但樣本或採用率不足",
            }


class IssueHistoricalCatalog:
    """Manages cross-window persistent version catalog per application with platform isolation."""

    def __init__(self, catalog_path: Optional[Path] = None, app_id: Optional[str] = None):
        self.catalog_path = catalog_path
        self.app_id = app_id
        # Keyed canonically by f"{platform}:{issue_id}"
        self.issues: Dict[str, Dict[str, Any]] = {}
        # Grouped by platform: self.app_versions[platform][version]
        self.app_versions: Dict[str, Dict[str, Dict[str, Any]]] = {"android": {}, "ios": {}}
        self.updated_at: Optional[str] = None
        self.watermark: Optional[str] = None
        # Track distinct installation UUIDs per platform and version for authoritative deduplication
        self._version_installations: Dict[str, Dict[str, Set[str]]] = {"android": {}, "ios": {}}

    def _canonical_key(self, platform: str, issue_id: str) -> str:
        pf = "ios" if platform == "ios" else "android"
        return f"{pf}:{issue_id}"

    def advance_watermark(self, candidate_ts: Optional[str]) -> None:
        """Advances catalog watermark if candidate_ts is newer than current watermark."""
        if not candidate_ts:
            return
        ts_str = str(candidate_ts).strip()
        if not ts_str:
            return
        if not self.watermark:
            self.watermark = ts_str
            return
        try:
            c_clean = ts_str.replace("Z", "+00:00")
            w_clean = self.watermark.replace("Z", "+00:00")
            dt_cand = dt.datetime.fromisoformat(c_clean)
            dt_curr = dt.datetime.fromisoformat(w_clean)
            if dt_cand.tzinfo is None:
                dt_cand = dt_cand.replace(tzinfo=dt.timezone.utc)
            if dt_curr.tzinfo is None:
                dt_curr = dt_curr.replace(tzinfo=dt.timezone.utc)
            if dt_cand > dt_curr:
                self.watermark = ts_str
        except Exception:
            if ts_str > self.watermark:
                self.watermark = ts_str

    @staticmethod
    def _is_ts_le(ts: Optional[str], watermark: Optional[str]) -> bool:
        """Returns True if ts <= watermark (comparing ISO datetimes with timezone awareness)."""
        if not ts or not watermark:
            return False
        try:
            t_clean = str(ts).strip().replace("Z", "+00:00")
            w_clean = str(watermark).strip().replace("Z", "+00:00")
            dt_t = dt.datetime.fromisoformat(t_clean)
            dt_w = dt.datetime.fromisoformat(w_clean)
            if dt_t.tzinfo is None:
                dt_t = dt_t.replace(tzinfo=dt.timezone.utc)
            if dt_w.tzinfo is None:
                dt_w = dt_w.replace(tzinfo=dt.timezone.utc)
            return dt_t <= dt_w
        except Exception:
            return str(ts).strip() <= str(watermark).strip()

    def load(self) -> None:
        """Loads existing catalog file from disk if present."""
        if self.catalog_path and self.catalog_path.is_file():
            try:
                data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
                loaded_issues = data.get("issues", {})
                for k, iss in loaded_issues.items():
                    if isinstance(iss, dict):
                        pf = "ios" if iss.get("platform") == "ios" else "android"
                        iid = iss.get("issue_id", k)
                        canonical = self._canonical_key(pf, iid)
                        self.issues[canonical] = iss

                loaded_vers = data.get("app_versions", {})
                if isinstance(loaded_vers, dict):
                    for pf_or_ver, val in loaded_vers.items():
                        if pf_or_ver in ("android", "ios") and isinstance(val, dict):
                            self.app_versions.setdefault(pf_or_ver, {}).update(val)
                            for v_name, v_info in val.items():
                                if isinstance(v_info, dict):
                                    ids = v_info.get("installation_ids") or v_info.get("user_ids")
                                    if ids:
                                        self._version_installations.setdefault(pf_or_ver, {}).setdefault(v_name, set()).update(str(x) for x in ids if x)
                        elif isinstance(val, dict):
                            # Backward compat: flat dict -> assign to android by default
                            pf = val.get("platform", "android")
                            self.app_versions.setdefault(pf, {})[pf_or_ver] = val
                            ids = val.get("installation_ids") or val.get("user_ids")
                            if ids:
                                self._version_installations.setdefault(pf, {}).setdefault(pf_or_ver, set()).update(str(x) for x in ids if x)

                self.updated_at = data.get("updated_at")
                self.watermark = data.get("watermark")
                if data.get("app_id") and not self.app_id:
                    self.app_id = data["app_id"]
            except Exception:
                pass

    def save(self) -> None:
        """Saves current catalog file to disk strictly conforming to Schema V2.3."""
        if not self.catalog_path:
            return
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        self.updated_at = now_iso
        self.watermark = self.watermark or now_iso

        # Sync tracked installation IDs into app_versions before serialization
        for pf, vers in self.app_versions.items():
            for v_name, v_info in vers.items():
                if isinstance(v_info, dict):
                    inst_set = self._version_installations.get(pf, {}).get(v_name)
                    if inst_set:
                        v_info["installation_ids"] = sorted(list(inst_set))

        payload: Dict[str, Any] = {
            "schema_version": "2.3.0",
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
        platform: Optional[str] = None,
        window: Optional[int | str] = None,
    ) -> None:
        """Records version-level metrics, lifetime counts, and windowed recent health into catalog per platform."""
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
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
            if last_seen:
                self.advance_watermark(last_seen)

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
                fat_int = int(fat_val)
                anr_int = int(anr_val)
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
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
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
                    self.advance_watermark(ts_last)

    def update_from_catalog_rows(self, rows: Iterable[dict], is_incremental: bool = False) -> None:
        """Ingests broad catalog query rows (issue_id, app_version, first_seen_ts, last_seen_ts, events, users, fatal, anr)."""
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        initial_watermark = self.watermark

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

            # Watermark Boundary Idempotency: in incremental mode, if ts_last <= initial_watermark,
            # this batch/row was already included in the catalog up to watermark and must not be double-added.
            is_already_processed = bool(is_incremental and initial_watermark and ts_last and self._is_ts_le(ts_last, initial_watermark))

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

            # Deduplication for affected users
            inst_set = self._version_installations.setdefault(pf, {}).setdefault(ver, set())
            if not inst_set and v_obj.get("installation_ids"):
                inst_set.update(str(x) for x in v_obj["installation_ids"] if x)
            elif not inst_set and v_obj.get("user_ids"):
                inst_set.update(str(x) for x in v_obj["user_ids"] if x)

            raw_insts = row.get("installation_ids") or row.get("user_ids") or row.get("installations")
            if raw_insts:
                if isinstance(raw_insts, (list, set, tuple)):
                    inst_set.update(str(x) for x in raw_insts if x)
                else:
                    inst_set.add(str(raw_insts))
            if row.get("installation_uuid"):
                inst_set.add(str(row["installation_uuid"]))

            usr_count = row.get("affected_users") if row.get("affected_users") is not None else (
                row.get("lifetime_affected_users") if row.get("lifetime_affected_users") is not None else (
                    row.get("lifetime_users") if row.get("lifetime_users") is not None else row.get("users")
                )
            )

            if inst_set:
                v_obj["lifetime_affected_users"] = len(inst_set)
                v_obj["installation_ids"] = sorted(list(inst_set))
                v_obj["affected_users"] = len(inst_set)
            elif usr_count is not None:
                if is_incremental:
                    if not is_already_processed:
                        v_obj["lifetime_affected_users"] = max(int(v_obj.get("lifetime_affected_users") or 0), int(usr_count))
                else:
                    v_obj["lifetime_affected_users"] = max(int(v_obj.get("lifetime_affected_users") or 0), int(usr_count))
                v_obj["affected_users"] = v_obj["lifetime_affected_users"]

            # Deduplication for issues count
            iss_count = row.get("issues_count") if row.get("issues_count") is not None else row.get("lifetime_issues")
            if iid:
                canonical_key = self._canonical_key(pf, iid)
                existing = self.issues.get(canonical_key)
                if existing:
                    all_vers = set(existing.get("versions_seen", [])) | {ver}
                    sorted_vers = sorted(list(all_vers), key=version_key)
                    existing["versions_seen"] = sorted_vers
                    existing["first_seen_version"] = min_version([existing.get("first_seen_version"), ver]) or ver
                    existing["last_seen_version"] = max_version([existing.get("last_seen_version"), ver]) or ver
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

            if ts_last:
                self.advance_watermark(ts_last)

    def calculate_version_status(
        self,
        version: str,
        platform: str,
        latest_version: Optional[str] = None,
        reference_time: Optional[dt.datetime] = None,
    ) -> Literal["latest", "active", "legacy"]:
        """Evaluates whether a version is latest, active, or legacy (>90d inactive)."""
        pf = "ios" if platform == "ios" else "android"
        latest_v = latest_version or max_version(self.get_known_app_versions(platform=pf))
        if latest_v and version == latest_v:
            return "latest"

        v_info = self.app_versions.get(pf, {}).get(version, {})
        explicit_status = str(v_info.get("status", "")).lower()
        if explicit_status in ("legacy", "deprecated"):
            return "legacy"

        ref_dt = reference_time or dt.datetime.now(dt.timezone.utc)
        last_seen_str = v_info.get("last_seen")
        if last_seen_str:
            try:
                clean = last_seen_str.replace("Z", "+00:00")
                last_dt = dt.datetime.fromisoformat(clean)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=dt.timezone.utc)
                else:
                    last_dt = last_dt.astimezone(dt.timezone.utc)
                delta_days = (ref_dt - last_dt).total_seconds() / 86400.0
                if delta_days > 90.0:
                    return "legacy"
                return "active"
            except Exception:
                pass

        recent = v_info.get("recent_health", {})
        has_recent = False
        if isinstance(recent, dict):
            for r in recent.values():
                if isinstance(r, dict) and ((r.get("crash_events") or 0) > 0 or (r.get("sessions_total") or 0) > 0):
                    has_recent = True
                    break

        if has_recent:
            return "active"

        return "legacy" if ((v_info.get("crash_events") or 0) == 0 and not recent) else "active"

    def build_release_catalog(
        self,
        app_data: Optional[dict] = None,
        platform: Optional[str] = None,
        reference_date: Optional[Any] = None,
    ) -> List[ReleaseCatalogItem]:
        """Constructs the decoupled persistent release catalog conforming to ReleaseCatalogItem."""
        ref_dt = dt.datetime.now(dt.timezone.utc)
        if reference_date is not None:
            if isinstance(reference_date, dt.datetime):
                ref_dt = reference_date if reference_date.tzinfo else reference_date.replace(tzinfo=dt.timezone.utc)
            elif isinstance(reference_date, dt.date):
                ref_dt = dt.datetime(reference_date.year, reference_date.month, reference_date.day, tzinfo=dt.timezone.utc)
            elif isinstance(reference_date, str):
                try:
                    parsed = dt.datetime.fromisoformat(reference_date.replace("Z", "+00:00"))
                    ref_dt = parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
                except Exception:
                    pass

        target_platforms = [platform] if platform in ("android", "ios") else ["android", "ios"]
        catalog_items: List[ReleaseCatalogItem] = []

        for pf in target_platforms:
            known_vers = list(self.get_known_app_versions(platform=pf))
            if isinstance(app_data, dict):
                for v in app_data.get("version_health") or []:
                    if isinstance(v, dict) and v.get("version"):
                        v_pf = v.get("platform")
                        if v_pf is None or v_pf in (pf, "all"):
                            known_vers.append(str(v["version"]).strip())
                for d in app_data.get("distributions", {}).get("app_versions") or []:
                    if isinstance(d, dict) and d.get("app_version"):
                        d_pf = d.get("platform")
                        if d_pf is None or d_pf in (pf, "all"):
                            known_vers.append(str(d["app_version"]).strip())

            sorted_vers = sorted(list(set(v for v in known_vers if v)), key=version_key)
            if not sorted_vers:
                continue

            latest_v = get_latest_app_version(app_data, platform=pf, catalog=self) or sorted_vers[-1]
            pf_issues = [iss for iss in self.issues.values() if iss.get("platform") == pf]
            version_sufficiency_map: Dict[str, bool] = {
                v_name: is_version_sample_sufficient(self.app_versions.get(pf, {}).get(v_name))
                for v_name in sorted_vers
            }

            for idx, ver in enumerate(sorted_vers):
                v_prev = sorted_vers[idx - 1] if idx > 0 else None
                v_info = self.app_versions.get(pf, {}).get(ver, {})

                status = self.calculate_version_status(ver, pf, latest_version=latest_v, reference_time=ref_dt)

                first_seen = v_info.get("first_seen")
                last_seen = v_info.get("last_seen")

                matching_issues = [iss for iss in pf_issues if ver in iss.get("versions_seen", [])]
                if not first_seen and matching_issues:
                    f_ts_list = [iss.get("first_seen_timestamp") for iss in matching_issues if iss.get("first_seen_timestamp")]
                    if f_ts_list:
                        first_seen = min(f_ts_list)
                if not last_seen and matching_issues:
                    l_ts_list = [iss.get("last_seen_timestamp") for iss in matching_issues if iss.get("last_seen_timestamp")]
                    if l_ts_list:
                        last_seen = max(l_ts_list)

                # Authoritative release date: NEVER fallback to first_seen
                rel_date = v_info.get("release_date")
                if rel_date and not isinstance(rel_date, str):
                    rel_date = None

                lt_crashes = max(int(v_info.get("lifetime_crashes") or 0), int(v_info.get("crash_events") or 0))
                lt_issues = max(int(v_info.get("lifetime_issues") or 0), len(matching_issues))
                lt_users = max(int(v_info.get("lifetime_affected_users") or 0), int(v_info.get("affected_users") or 0))
                lt_fatal = int(v_info.get("lifetime_fatal") or 0)
                lt_anr = int(v_info.get("lifetime_anr") or 0)

                introduced_ids: List[str] = []
                persistent_ids: List[str] = []
                regressed_ids: List[str] = []
                resolved_ids: List[str] = []

                ver_key_val = version_key(ver)

                for iss in pf_issues:
                    iid = iss.get("issue_id", "")
                    if not iid:
                        continue
                    iss_f_ver = iss.get("first_seen_version", "")
                    iss_vers = set(iss.get("versions_seen", []))
                    if iss_f_ver == ver:
                        introduced_ids.append(iid)
                    elif ver in iss_vers:
                        if v_prev and v_prev in iss_vers:
                            persistent_ids.append(iid)
                        elif iss_f_ver and version_key(iss_f_ver) < ver_key_val:
                            # Intermediate versions between first_seen_version and current version
                            intermediate = [
                                v for v in sorted_vers
                                if version_key(iss_f_ver) < version_key(v) < ver_key_val
                            ]
                            absent_intermediate = [v for v in intermediate if v not in iss_vers]
                            # Only proven absent if version sample was sufficient
                            proven_absent = [v for v in absent_intermediate if version_sufficiency_map.get(v, False)]
                            if proven_absent:
                                regressed_ids.append(iid)
                            else:
                                persistent_ids.append(iid)
                        else:
                            persistent_ids.append(iid)
                    else:
                        # ver not in iss_vers: only count as resolved if it was active in immediate previous version AND current ver has sufficient sample
                        if v_prev and v_prev in iss_vers and version_sufficiency_map.get(ver, False):
                            resolved_ids.append(iid)

                issue_lifecycle: ReleaseIssueLifecycle = {
                    "introduced_count": len(introduced_ids),
                    "persistent_count": len(persistent_ids),
                    "regressed_count": len(regressed_ids),
                    "resolved_count": len(resolved_ids),
                    "introduced": introduced_ids,
                    "persistent": persistent_ids,
                    "regressed": regressed_ids,
                    "resolved": resolved_ids,
                    "introduced_issues": introduced_ids,
                    "persistent_issues": persistent_ids,
                    "regressed_issues": regressed_ids,
                    "resolved_issues": resolved_ids,
                }

                recent_health: Dict[str, ReleaseRecentHealth] = {}
                periods_dict = app_data.get("periods") if isinstance(app_data, dict) else None

                for w_key in ("7", "30", "90"):
                    snap = periods_dict.get(w_key) if periods_dict else None
                    snap_vh = snap.get("version_health", []) if snap else []
                    match_item = next(
                        (item for item in snap_vh if str(item.get("version", "")).strip() == ver and item.get("platform") in (pf, "all")),
                        None,
                    )
                    cached_recent = v_info.get("recent_health", {}).get(w_key) or v_info.get("recent_health", {}).get(f"{w_key}d")

                    if match_item:
                        ev = int(match_item.get("crash_events") or 0)
                        usr = int(match_item.get("affected_users") or 0)
                        sess = match_item.get("sessions_total")
                        cfu = match_item.get("crash_free_users_rate")
                        cfs = match_item.get("crash_free_sessions_rate")
                        adopt = match_item.get("adoption_rate")
                        suff = is_version_sample_sufficient(match_item)
                        st = match_item.get("status") or status
                        tr = match_item.get("trend") or "stable"

                        snap_fatal = 0
                        snap_anr = 0
                        snap_pf_issues = [s_iss for s_iss in (snap.get("top_issues") or []) if s_iss.get("platform") in (pf, "all", None)]
                        for s_iss in snap_pf_issues:
                            for v_dist in s_iss.get("version_distribution") or []:
                                if v_dist.get("version") == ver:
                                    if s_iss.get("error_type") == "FATAL":
                                        snap_fatal += int(v_dist.get("events") or 0)
                                    elif s_iss.get("error_type") == "ANR":
                                        snap_anr += int(v_dist.get("events") or 0)

                        new_iss_cnt = len([i for i in snap_pf_issues if i.get("first_seen_version") == ver])

                        w_entry = {
                            "crash_events": ev,
                            "affected_users": usr,
                            "sessions_total": sess,
                            "crash_free_users_rate": cfu,
                            "crash_free_sessions_rate": cfs,
                            "adoption_rate": adopt,
                            "fatal_events": snap_fatal,
                            "fatal_count": snap_fatal,
                            "anr_events": snap_anr,
                            "anr_count": snap_anr,
                            "new_issues_count": new_iss_cnt,
                            "active_issues_count": new_iss_cnt,
                            "sample_sufficient": suff,
                            "status": st,
                            "trend": tr,
                        }
                        recent_health[w_key] = w_entry
                        recent_health[f"{w_key}d"] = w_entry
                    elif cached_recent:
                        c_copy = dict(cached_recent)
                        f_cnt = c_copy.get("fatal_count") if c_copy.get("fatal_count") is not None else c_copy.get("fatal_events", 0)
                        a_cnt = c_copy.get("anr_count") if c_copy.get("anr_count") is not None else c_copy.get("anr_events", 0)
                        i_cnt = c_copy.get("active_issues_count") if c_copy.get("active_issues_count") is not None else c_copy.get("new_issues_count", 0)
                        c_copy["fatal_count"] = f_cnt
                        c_copy["fatal_events"] = f_cnt
                        c_copy["anr_count"] = a_cnt
                        c_copy["anr_events"] = a_cnt
                        c_copy["active_issues_count"] = i_cnt
                        c_copy["new_issues_count"] = i_cnt
                        recent_health[w_key] = c_copy
                        recent_health[f"{w_key}d"] = c_copy
                    else:
                        empty_entry = {
                            "crash_events": 0,
                            "affected_users": 0,
                            "sessions_total": None,
                            "crash_free_users_rate": None,
                            "crash_free_sessions_rate": None,
                            "adoption_rate": None,
                            "fatal_events": 0,
                            "fatal_count": 0,
                            "anr_events": 0,
                            "anr_count": 0,
                            "new_issues_count": 0,
                            "active_issues_count": 0,
                            "sample_sufficient": False,
                            "status": "inactive" if status == "legacy" else status,
                            "trend": "stable",
                        }
                        recent_health[w_key] = empty_entry
                        recent_health[f"{w_key}d"] = empty_entry

                vs_previous: Optional[PreviousReleaseComparison] = None
                if v_prev:
                    prev_info = self.app_versions.get(pf, {}).get(v_prev, {})
                    prev_recent = prev_info.get("recent_health", {})

                    # Find matching window for normalized exposure comparison
                    crash_rate_diff: Optional[float] = None
                    fatal_rate_diff: Optional[float] = None
                    anr_rate_diff: Optional[float] = None
                    comp_w = None
                    for candidate_w in ("30", "90", "7"):
                        c_w = recent_health.get(candidate_w)
                        p_w = prev_recent.get(candidate_w)
                        if c_w and p_w and c_w.get("sessions_total") and p_w.get("sessions_total"):
                            comp_w = candidate_w
                            break

                    if comp_w:
                        c_w = recent_health[comp_w]
                        p_w = prev_recent[comp_w]
                        c_ev = int(c_w.get("crash_events") or 0)
                        c_se = int(c_w.get("sessions_total") or 0)
                        p_ev = int(p_w.get("crash_events") or 0)
                        p_se = int(p_w.get("sessions_total") or 0)
                        if c_se > 0 and p_se > 0:
                            rate_curr = c_ev / c_se
                            rate_prev = p_ev / p_se
                            crash_rate_diff = round((rate_curr - rate_prev) / rate_prev, 4) if rate_prev > 0 else 0.0

                            c_fat = c_w.get("fatal_events") if c_w.get("fatal_events") is not None else c_w.get("fatal_count")
                            p_fat = p_w.get("fatal_events") if p_w.get("fatal_events") is not None else p_w.get("fatal_count")
                            if c_fat is not None and p_fat is not None:
                                r_fat_curr = int(c_fat) / c_se
                                r_fat_prev = int(p_fat) / p_se
                                if r_fat_prev > 0:
                                    fatal_rate_diff = round((r_fat_curr - r_fat_prev) / r_fat_prev, 4)
                                elif r_fat_curr == 0 and r_fat_prev == 0:
                                    fatal_rate_diff = 0.0

                            c_anr = c_w.get("anr_events") if c_w.get("anr_events") is not None else c_w.get("anr_count")
                            p_anr = p_w.get("anr_events") if p_w.get("anr_events") is not None else p_w.get("anr_count")
                            if c_anr is not None and p_anr is not None:
                                r_anr_curr = int(c_anr) / c_se
                                r_anr_prev = int(p_anr) / p_se
                                if r_anr_prev > 0:
                                    anr_rate_diff = round((r_anr_curr - r_anr_prev) / r_anr_prev, 4)
                                elif r_anr_curr == 0 and r_anr_prev == 0:
                                    anr_rate_diff = 0.0
                    else:
                        c_sess = v_info.get("sessions_total")
                        p_sess = prev_info.get("sessions_total")
                        c_ev = v_info.get("crash_events")
                        p_ev = prev_info.get("crash_events")
                        if c_sess and p_sess and int(c_sess) > 0 and int(p_sess) > 0:
                            c_se = int(c_sess)
                            p_se = int(p_sess)
                            if c_ev is not None and p_ev is not None:
                                rate_curr = int(c_ev) / c_se
                                rate_prev = int(p_ev) / p_se
                                crash_rate_diff = round((rate_curr - rate_prev) / rate_prev, 4) if rate_prev > 0 else 0.0

                            # Fallback: strictly require window-scoped fatal_events / fatal_count matching sessions.
                            # NEVER fall back to lifetime_fatal or lifetime_anr!
                            c_fat = v_info.get("fatal_events") if v_info.get("fatal_events") is not None else v_info.get("fatal_count")
                            p_fat = prev_info.get("fatal_events") if prev_info.get("fatal_events") is not None else prev_info.get("fatal_count")
                            if c_fat is not None and p_fat is not None:
                                r_fat_curr = int(c_fat) / c_se
                                r_fat_prev = int(p_fat) / p_se
                                if r_fat_prev > 0:
                                    fatal_rate_diff = round((r_fat_curr - r_fat_prev) / r_fat_prev, 4)
                                elif r_fat_curr == 0 and r_fat_prev == 0:
                                    fatal_rate_diff = 0.0
                            else:
                                fatal_rate_diff = None

                            c_anr = v_info.get("anr_events") if v_info.get("anr_events") is not None else v_info.get("anr_count")
                            p_anr = prev_info.get("anr_events") if prev_info.get("anr_events") is not None else prev_info.get("anr_count")
                            if c_anr is not None and p_anr is not None:
                                r_anr_curr = int(c_anr) / c_se
                                r_anr_prev = int(p_anr) / p_se
                                if r_anr_prev > 0:
                                    anr_rate_diff = round((r_anr_curr - r_anr_prev) / r_anr_prev, 4)
                                elif r_anr_curr == 0 and r_anr_prev == 0:
                                    anr_rate_diff = 0.0
                            else:
                                anr_rate_diff = None

                    cfu_curr = v_info.get("crash_free_users_rate")
                    if cfu_curr is None:
                        for rh in recent_health.values():
                            if isinstance(rh, dict) and rh.get("crash_free_users_rate") is not None:
                                cfu_curr = rh["crash_free_users_rate"]
                                break
                    cfu_prev = prev_info.get("crash_free_users_rate")
                    if cfu_prev is None:
                        for rh in prev_recent.values():
                            if isinstance(rh, dict) and rh.get("crash_free_users_rate") is not None:
                                cfu_prev = rh["crash_free_users_rate"]
                                break

                    cfu_diff: Optional[float] = None
                    if cfu_curr is not None and cfu_prev is not None:
                        cfu_diff = round(cfu_curr - cfu_prev, 4)

                    prev_introduced_cnt = len([i for i in pf_issues if i.get("first_seen_version") == v_prev])
                    new_issues_diff = len(introduced_ids) - prev_introduced_cnt

                    stability: Literal["improving", "stable", "degrading", "baseline"] = "stable"
                    if crash_rate_diff is not None:
                        if crash_rate_diff <= -0.05:
                            stability = "improving"
                        elif crash_rate_diff >= 0.05:
                            stability = "degrading"
                        else:
                            stability = "stable"
                    elif cfu_diff is not None:
                        if cfu_diff >= 0.001:
                            stability = "improving"
                        elif cfu_diff <= -0.001:
                            stability = "degrading"
                        else:
                            stability = "stable"

                    vs_previous = {
                        "previous_version": v_prev,
                        "crash_rate_change_pct": crash_rate_diff,
                        "crash_free_users_diff": cfu_diff,
                        "fatal_change_pct": fatal_rate_diff,
                        "fatal_rate_change_pct": fatal_rate_diff,
                        "anr_change_pct": anr_rate_diff,
                        "anr_rate_change_pct": anr_rate_diff,
                        "new_issues_diff": new_issues_diff,
                        "new_issues_count": new_issues_diff,
                        "stability": stability,
                        "stability_status": "improved" if stability == "improving" else ("regressed" if stability == "degrading" else stability),
                    }

                catalog_items.append({
                    "version": ver,
                    "platform": pf,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                    "release_date": rel_date,
                    "status": status,
                    "lifetime_crashes": lt_crashes,
                    "lifetime_issues": lt_issues,
                    "lifetime_affected_users": lt_users,
                    "lifetime_fatal": lt_fatal,
                    "lifetime_anr": lt_anr,
                    "stability_status": stability if vs_previous else "baseline",
                    "recent_health": recent_health,
                    "issue_lifecycle": issue_lifecycle,
                    "vs_previous": vs_previous,
                })

        final_items: List[ReleaseCatalogItem] = []
        for pf in target_platforms:
            pf_items = [i for i in catalog_items if i["platform"] == pf]
            latest_items = [i for i in pf_items if i["status"] == "latest"]
            other_items = sorted([i for i in pf_items if i["status"] != "latest"], key=lambda x: version_key(x["version"]), reverse=True)
            final_items.extend(latest_items + other_items)

        return final_items

    def get_known_app_versions(self, platform: Optional[str] = None) -> List[str]:
        """Returns sorted list of all known app versions recorded in catalog, optionally isolated by platform."""
        v_set = set()
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

    def get_version_info(self, version: str, platform: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if platform:
            return self.app_versions.get(platform, {}).get(version)
        return self.app_versions.get("android", {}).get(version) or self.app_versions.get("ios", {}).get(version)

    def get_issue_history(self, issue_id: str, platform: Optional[str] = None) -> Optional[Dict[str, Any]]:
        if platform:
            canonical = self._canonical_key(platform, issue_id)
            return self.issues.get(canonical)
        for pf in ("android", "ios"):
            ck = self._canonical_key(pf, issue_id)
            if ck in self.issues:
                return self.issues[ck]
        return self.issues.get(issue_id)


def bootstrap_catalog_from_disk(
    app_name: str,
    root_dir: Optional[Path] = None,
    catalog: Optional[IssueHistoricalCatalog] = None,
) -> IssueHistoricalCatalog:
    """Bootstraps an IssueHistoricalCatalog by scanning historical reports and archives on disk."""
    base = root_dir or ROOT
    cat = catalog
    if cat is None:
        cat_file = base / "out" / app_name / "historical_catalog.json"
        cat = IssueHistoricalCatalog(cat_file, app_id=app_name)
        cat.load()

    # 1. Scan reports/data/<app_name>/*.json (monthly historical reports)
    reports_dir = base / "reports" / "data" / app_name
    if reports_dir.is_dir():
        for r_file in sorted(reports_dir.glob("*.json")):
            try:
                r_data = json.loads(r_file.read_text(encoding="utf-8"))
                issues = r_data.get("issues") or r_data.get("top_issues") or []
                if issues:
                    cat.update_from_issues(issues)
                vh = r_data.get("version_health")
                if vh:
                    cat.update_app_versions(vh)
                app_vers = r_data.get("distributions", {}).get("app_versions") or []
                for av in app_vers:
                    if isinstance(av, dict) and av.get("app_version"):
                        cat.update_from_catalog_rows([{
                            "app_version": av["app_version"],
                            "events": av.get("events", 0),
                            "users": av.get("users", 0),
                            "platform": av.get("platform", "android"),
                        }])
            except Exception:
                pass

    # 2. Scan out/<app_name>/unified.json
    unified_file = base / "out" / app_name / "unified.json"
    if unified_file.is_file():
        try:
            u_data = json.loads(unified_file.read_text(encoding="utf-8"))
            u_issues = u_data.get("issues") or []
            if u_issues:
                cat.update_from_issues(u_issues)
            u_vh = u_data.get("version_health")
            if u_vh:
                cat.update_app_versions(u_vh)
        except Exception:
            pass

    # 3. Scan out/<app_name>/dashboard_v2.json
    v2_file = base / "out" / app_name / "dashboard_v2.json"
    if v2_file.is_file():
        try:
            v2_data = json.loads(v2_file.read_text(encoding="utf-8"))
            v2_issues = v2_data.get("top_issues") or []
            cat.update_from_issues(v2_issues)
            for p_k, snap in (v2_data.get("periods") or {}).items():
                if isinstance(snap, dict):
                    cat.update_from_issues(snap.get("top_issues") or [])
                    cat.update_app_versions(snap.get("version_health") or [], window=p_k)
            cat.update_app_versions(v2_data.get("version_health") or [])
        except Exception:
            pass

    cat.save()
    return cat


def enrich_app_data_with_lifecycle(
    app_data: dict,
    catalog: Optional[IssueHistoricalCatalog] = None,
    app_name: Optional[str] = None,
    out_dir: Optional[Path] = None,
    catalog_rows: Optional[Iterable[dict]] = None,
    version_catalog_rows: Optional[Iterable[dict]] = None,
    is_bootstrap: bool = False,
    is_incremental: Optional[bool] = None,
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

    if is_incremental is None:
        is_incremental = False

    if catalog_rows:
        # catalog_rows contains issue-level distribution (e.g. from 90-day lifecycle_catalog)
        # to update issue first/last seen and versions_seen; it is non-incremental for version totals.
        cat.update_from_catalog_rows(catalog_rows, is_incremental=False)

    if version_catalog_rows:
        cat.update_from_catalog_rows(version_catalog_rows, is_incremental=is_incremental)

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
    all_issues_to_index: List[dict] = []
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
            "last_sync_timestamp": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "error_message": None,
        }

    # 3. Determine per-platform version universe and latest version
    supported_platforms = ["android", "ios"]
    meta_pfs = app_data.get("metadata", {}).get("platforms")
    if meta_pfs and isinstance(meta_pfs, list):
        supported_platforms = [p for p in meta_pfs if p in ("android", "ios")] or ["android", "ios"]

    per_pf_latest: Dict[str, str] = {}
    per_pf_known: Dict[str, List[str]] = {}
    per_pf_sufficiency: Dict[str, Dict[str, bool]] = {}

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
            latest_v = max_version(list(known_set)) if known_set else "1.0.0"
        known_set.add(latest_v)
        sorted_pf_versions = sorted(list(known_set), key=version_key)

        # Build sample sufficiency for pf
        suff_map: Dict[str, bool] = {}
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

            if hist:
                if hist.get("first_seen_version"):
                    iss["first_seen_version"] = hist["first_seen_version"]
                if hist.get("last_seen_version"):
                    iss["last_seen_version"] = hist["last_seen_version"]
                if hist.get("first_seen_timestamp") and not iss.get("first_seen_timestamp"):
                    iss["first_seen_timestamp"] = hist["first_seen_timestamp"]
                if hist.get("last_seen_timestamp") and not iss.get("last_seen_timestamp"):
                    iss["last_seen_timestamp"] = hist["last_seen_timestamp"]

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
        for p_key, snap in periods.items():
            if not isinstance(snap, dict):
                continue

            snap_vh = snap.get("version_health") or vh
            snap_dist_v = snap.get("distributions", {}).get("app_versions") or []

            # Snapshot per-platform universes
            snap_pf_latest: Dict[str, str] = {}
            snap_pf_known: Dict[str, List[str]] = {}
            snap_pf_sufficiency: Dict[str, Dict[str, bool]] = {}

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

                snap_suff_map: Dict[str, bool] = {}
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

                if hist:
                    if hist.get("first_seen_version"):
                        iss["first_seen_version"] = hist["first_seen_version"]
                    if hist.get("last_seen_version"):
                        iss["last_seen_version"] = hist["last_seen_version"]
                    if hist.get("first_seen_timestamp") and not iss.get("first_seen_timestamp"):
                        iss["first_seen_timestamp"] = hist["first_seen_timestamp"]
                    if hist.get("last_seen_timestamp") and not iss.get("last_seen_timestamp"):
                        iss["last_seen_timestamp"] = hist["last_seen_timestamp"]

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
    release_catalog = cat.build_release_catalog(app_data)
    app_data["release_catalog"] = release_catalog
    if isinstance(periods, dict):
        for snap in periods.values():
            if isinstance(snap, dict):
                snap["release_catalog"] = release_catalog

    cat.save()
    return app_data


def main() -> None:
    """CLI entrypoint for standalone catalog bootstrap and lifecycle maintenance."""
    parser = argparse.ArgumentParser(description="Issue Historical Catalog & Lifecycle CLI")
    parser.add_argument("--app", required=True, help="Application ID defined in apps.yaml")
    parser.add_argument("--bootstrap", action="store_true", help="Bootstrap catalog from reports/data and out/ artifacts")
    args = parser.parse_args()

    cfg = load_config()
    app_cfg = get_app(args.app, cfg)
    print(f"=== Issue Historical Catalog: {args.app} ({app_cfg.get('display_name')}) ===")

    target_cat = out_dir(args.app) / "historical_catalog.json"
    cat = IssueHistoricalCatalog(target_cat, app_id=args.app)
    cat.load()

    if args.bootstrap:
        print("  ⏳ 正在執行歷史冷啟動 (Bootstrap from disk archives)...")
        cat = bootstrap_catalog_from_disk(args.app, root_dir=ROOT, catalog=cat)
        print(f"  ✓ 冷啟動完成！已保存 {len(cat.issues)} 筆歷史 Issue 與版本資訊至 {target_cat.relative_to(ROOT)}")
    else:
        print(f"  現有 Catalog 記錄: {len(cat.issues)} 筆 Issue, updated_at: {cat.updated_at}")


if __name__ == "__main__":
    main()
