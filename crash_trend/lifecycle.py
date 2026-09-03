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
    from crash_trend.schema_v2 import HistoricalCatalogData, IssueLifecycle, LifecycleStatus
    from crash_trend.versions import max_version, min_version, version_key
except ImportError:
    from config import ROOT, get_app, load_config, out_dir  # type: ignore
    from schema_v2 import HistoricalCatalogData, IssueLifecycle, LifecycleStatus  # type: ignore
    from versions import max_version, min_version, version_key  # type: ignore


def get_latest_app_version(
    app_data: dict,
    platform: Optional[str] = None,
    catalog: Optional["IssueHistoricalCatalog"] = None,
) -> str | None:
    """Extracts the true latest app version from version_health, distributions, or catalog.
    Strictly filters by platform ('android' or 'ios') if specified to prevent cross-platform pollution.

    Priority:
    1. version_health item where status == 'latest' (filtered by platform)
    2. Max semver version among version_health items (filtered by platform)
    3. Max semver version in distributions.app_versions (filtered by platform)
    4. Max semver version in catalog.app_versions for this platform (if catalog supplied)
    5. None (do NOT infer from top_issues.last_seen_version to avoid false positives)
    """
    if not isinstance(app_data, dict):
        return None

    vh = app_data.get("version_health") or []
    filtered_vh = []
    for v in vh:
        if not isinstance(v, dict) or not v.get("version"):
            continue
        v_pf = v.get("platform")
        if platform is None or v_pf is None or v_pf == platform or v_pf == "all":
            filtered_vh.append(v)

    for v in filtered_vh:
        if v.get("status") == "latest" and v.get("version"):
            return str(v["version"]).strip()

    vh_versions = [str(v.get("version")).strip() for v in filtered_vh if v.get("version")]
    if vh_versions:
        return max_version(vh_versions)

    dist_versions = app_data.get("distributions", {}).get("app_versions") or []
    dist_v_list = []
    for v in dist_versions:
        if not isinstance(v, dict) or not v.get("app_version"):
            continue
        v_pf = v.get("platform")
        if platform is None or v_pf is None or v_pf == platform or v_pf == "all":
            dist_v_list.append(str(v["app_version"]).strip())

    if dist_v_list:
        return max_version(dist_v_list)

    if catalog:
        cat_versions = catalog.get_known_app_versions(platform=platform)
        if cat_versions:
            return max_version(cat_versions)

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

    def _canonical_key(self, platform: str, issue_id: str) -> str:
        pf = "ios" if platform == "ios" else "android"
        return f"{pf}:{issue_id}"

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
                        elif isinstance(val, dict):
                            # Backward compat: flat dict -> assign to android by default
                            pf = val.get("platform", "android")
                            self.app_versions.setdefault(pf, {})[pf_or_ver] = val

                self.updated_at = data.get("updated_at")
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
        payload: Dict[str, Any] = {
            "schema_version": "2.3.0",
            "updated_at": now_iso,
            "issues": self.issues,
            "app_versions": self.app_versions,
        }
        if self.app_id:
            payload["app_id"] = self.app_id

        self.catalog_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def update_app_versions(self, version_health: Iterable[dict], platform: Optional[str] = None) -> None:
        """Records version-level metrics and sample sufficiency into catalog per platform."""
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
            events = v.get("crash_events", 0) or existing.get("crash_events", 0)
            status = v.get("status") or existing.get("status") or "active"

            is_suff = is_version_sample_sufficient({
                "adoption_rate": adoption,
                "sessions_total": sessions,
                "crash_events": events,
                "sample_sufficient": v.get("sample_sufficient") or existing.get("sample_sufficient"),
            })

            self.app_versions[pf][ver] = {
                "version": ver,
                "platform": pf,
                "status": status,
                "adoption_rate": adoption,
                "sessions_total": sessions,
                "crash_events": events,
                "sample_sufficient": is_suff,
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
            dist_versions = [v["version"] for v in v_dist if isinstance(v, dict) and v.get("version")]
            iss_versions = set(dist_versions)
            if iss.get("first_seen_version"):
                iss_versions.add(str(iss["first_seen_version"]))
            if iss.get("last_seen_version"):
                iss_versions.add(str(iss["last_seen_version"]))

            existing = self.issues.get(canonical_key)
            if existing:
                all_vers = set(existing.get("versions_seen", [])) | iss_versions
                sorted_vers = sorted(list(all_vers), key=version_key)

                all_candidates_first = [existing.get("first_seen_version"), iss.get("first_seen_version")] + sorted_vers
                all_candidates_last = [existing.get("last_seen_version"), iss.get("last_seen_version")] + sorted_vers

                f_ver = min_version(all_candidates_first)
                l_ver = max_version(all_candidates_last)

                ts_first_list = [t for t in [existing.get("first_seen_timestamp"), iss.get("first_seen_timestamp")] if t]
                ts_last_list = [t for t in [existing.get("last_seen_timestamp"), iss.get("last_seen_timestamp")] if t]

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
                    "error_type": iss.get("error_type", "NON_FATAL"),
                    "first_seen_version": f_ver,
                    "last_seen_version": l_ver,
                    "first_seen_timestamp": iss.get("first_seen_timestamp"),
                    "last_seen_timestamp": iss.get("last_seen_timestamp"),
                    "versions_seen": sorted_vers,
                    "last_updated": now_iso,
                }

    def update_from_catalog_rows(self, rows: Iterable[dict]) -> None:
        """Ingests broad catalog query rows (issue_id, app_version, first_seen_ts, last_seen_ts, events, users)."""
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        for row in rows:
            iid = row.get("issue_id")
            ver = str(row.get("app_version", "")).strip()
            if not iid or not ver:
                continue

            pf = "ios" if (row.get("platform") or row.get("_platform")) == "ios" else "android"
            canonical_key = self._canonical_key(pf, iid)

            ts_first = row.get("first_seen_timestamp")
            ts_last = row.get("last_seen_timestamp")

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
            if canonical in self.issues:
                return self.issues[canonical]
        # Fallback lookups
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
        except Exception:
            pass

    # 3. Scan out/<app_name>/dashboard_v2.json
    v2_file = base / "out" / app_name / "dashboard_v2.json"
    if v2_file.is_file():
        try:
            v2_data = json.loads(v2_file.read_text(encoding="utf-8"))
            v2_issues = v2_data.get("top_issues") or []
            cat.update_from_issues(v2_issues)
            for snap in (v2_data.get("periods") or {}).values():
                if isinstance(snap, dict):
                    cat.update_from_issues(snap.get("top_issues") or [])
                    cat.update_app_versions(snap.get("version_health") or [])
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
) -> dict:
    """Enriches app_data top_issues and all periods snapshots with deterministic lifecycle,
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

    if catalog_rows:
        cat.update_from_catalog_rows(catalog_rows)

    # Ingest app_versions from current version_health into catalog
    vh = app_data.get("version_health") or []
    cat.update_app_versions(vh)

    # Collect all issues across top_issues and all period snapshots and update catalog
    all_issues_to_index: List[dict] = []
    if isinstance(app_data.get("top_issues"), list):
        all_issues_to_index.extend(app_data["top_issues"])

    periods = app_data.get("periods") or {}
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
