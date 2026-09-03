"""Issue Historical Catalog and Deterministic Lifecycle Engine (Issue #29).

Provides:
- IssueHistoricalCatalog: Cross-window persistence of true historical first_seen,
  last_seen, version distributions, and app_versions per issue across analysis runs.
- detect_issue_lifecycle: Deterministic evaluation of the 5 lifecycle states:
  new_in_latest, persistent, regressed, resolved, not_observed_latest.
- enrich_app_data_with_lifecycle: Enriches AppDashboardV2Data top_issues and period snapshots.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

try:
    from crash_trend.config import ROOT
    from crash_trend.schema_v2 import IssueLifecycle, LifecycleStatus
    from crash_trend.versions import max_version, min_version, version_key
except ImportError:
    from config import ROOT  # type: ignore
    from schema_v2 import IssueLifecycle, LifecycleStatus  # type: ignore
    from versions import max_version, min_version, version_key  # type: ignore


def get_latest_app_version(
    app_data: dict,
    catalog: Optional["IssueHistoricalCatalog"] = None,
) -> str | None:
    """Extracts the true latest app version from version_health, distributions, or catalog.

    Priority:
    1. version_health item where status == 'latest'
    2. Max semver version among version_health items
    3. Max semver version in distributions.app_versions
    4. Max semver version in catalog.app_versions (if catalog supplied)
    5. None (do NOT infer from top_issues.last_seen_version to avoid false positives)
    """
    if not isinstance(app_data, dict):
        return None

    vh = app_data.get("version_health") or []
    for v in vh:
        if isinstance(v, dict) and v.get("status") == "latest" and v.get("version"):
            return str(v["version"]).strip()

    vh_versions = [str(v.get("version")).strip() for v in vh if isinstance(v, dict) and v.get("version")]
    if vh_versions:
        return max_version(vh_versions)

    dist_versions = app_data.get("distributions", {}).get("app_versions") or []
    dist_v_list = [str(v.get("app_version")).strip() for v in dist_versions if isinstance(v, dict) and v.get("app_version")]
    if dist_v_list:
        return max_version(dist_v_list)

    if catalog:
        cat_versions = catalog.get_known_app_versions()
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

        # Must Fix 3: An intermediate version only counts as an absence gap
        # if that version had sufficient observation evidence!
        proven_absent_versions: List[str] = []
        for v in absent_versions:
            if known_version_sufficiency is not None:
                if known_version_sufficiency.get(v, False):
                    proven_absent_versions.append(v)
            elif version_health_map is not None:
                if is_version_sample_sufficient(version_health_map.get(v)):
                    proven_absent_versions.append(v)
            else:
                # If neither map is provided, default to treating absent versions as candidate gaps
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
    """Manages cross-window persistent version catalog per application."""

    def __init__(self, catalog_path: Optional[Path] = None):
        self.catalog_path = catalog_path
        self.issues: Dict[str, Dict[str, Any]] = {}
        self.app_versions: Dict[str, Dict[str, Any]] = {}
        self.updated_at: Optional[str] = None

    def load(self) -> None:
        """Loads existing catalog file from disk if present."""
        if self.catalog_path and self.catalog_path.is_file():
            try:
                data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
                self.issues = data.get("issues", {})
                self.app_versions = data.get("app_versions", {})
                self.updated_at = data.get("updated_at")
            except Exception:
                pass

    def save(self) -> None:
        """Saves current catalog file to disk."""
        if not self.catalog_path:
            return
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        self.updated_at = now_iso
        payload = {
            "version": "1.0",
            "updated_at": now_iso,
            "issues": self.issues,
            "app_versions": self.app_versions,
        }
        self.catalog_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def update_app_versions(self, version_health: Iterable[dict]) -> None:
        """Records version-level metrics and sample sufficiency into catalog."""
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        for v in version_health:
            if not isinstance(v, dict):
                continue
            ver = str(v.get("version", "")).strip()
            if not ver:
                continue
            existing = self.app_versions.get(ver, {})
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

            self.app_versions[ver] = {
                "version": ver,
                "status": status,
                "adoption_rate": adoption,
                "sessions_total": sessions,
                "crash_events": events,
                "sample_sufficient": is_suff,
                "last_updated": now_iso,
            }

    def update_from_issues(self, issues: Iterable[dict]) -> None:
        """Merges a list of issues and their version distributions into the catalog."""
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        for iss in issues:
            iid = iss.get("issue_id")
            if not iid:
                continue

            v_dist = iss.get("version_distribution") or []
            dist_versions = [v["version"] for v in v_dist if isinstance(v, dict) and v.get("version")]
            iss_versions = set(dist_versions)
            if iss.get("first_seen_version"):
                iss_versions.add(str(iss["first_seen_version"]))
            if iss.get("last_seen_version"):
                iss_versions.add(str(iss["last_seen_version"]))

            existing = self.issues.get(iid)
            if existing:
                all_vers = set(existing.get("versions_seen", [])) | iss_versions
                sorted_vers = sorted(list(all_vers), key=version_key)

                # Merge first_seen / last_seen with true min/max
                all_candidates_first = [existing.get("first_seen_version"), iss.get("first_seen_version")] + sorted_vers
                all_candidates_last = [existing.get("last_seen_version"), iss.get("last_seen_version")] + sorted_vers

                f_ver = min_version(all_candidates_first)
                l_ver = max_version(all_candidates_last)

                # Merge timestamps
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

                self.issues[iid] = {
                    "issue_id": iid,
                    "platform": iss.get("platform", "android"),
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
            ts_first = row.get("first_seen_timestamp")
            ts_last = row.get("last_seen_timestamp")

            existing = self.issues.get(iid)
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
                self.issues[iid] = {
                    "issue_id": iid,
                    "platform": row.get("platform", "android"),
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

    def get_known_app_versions(self) -> List[str]:
        """Returns sorted list of all known app versions recorded in catalog."""
        v_set = set(self.app_versions.keys())
        for iss in self.issues.values():
            for v in iss.get("versions_seen", []):
                if v:
                    v_set.add(v)
        return sorted(list(v_set), key=version_key)

    def get_version_info(self, version: str) -> Optional[Dict[str, Any]]:
        return self.app_versions.get(version)

    def get_issue_history(self, issue_id: str) -> Optional[Dict[str, Any]]:
        return self.issues.get(issue_id)


def enrich_app_data_with_lifecycle(
    app_data: dict,
    catalog: Optional[IssueHistoricalCatalog] = None,
    app_name: Optional[str] = None,
    out_dir: Optional[Path] = None,
    catalog_rows: Optional[Iterable[dict]] = None,
) -> dict:
    """Enriches app_data top_issues and all periods snapshots with deterministic lifecycle."""
    if not isinstance(app_data, dict):
        return app_data

    # 1. Catalog setup and load
    cat = catalog
    if cat is None:
        cat_path = None
        if app_name:
            effective_out = out_dir or (ROOT / "out")
            cat_path = effective_out / app_name / "historical_catalog.json"
        cat = IssueHistoricalCatalog(catalog_path=cat_path)
        cat.load()

    if catalog_rows:
        cat.update_from_catalog_rows(catalog_rows)

    # Ingest app_versions from current version_health into catalog
    vh = app_data.get("version_health") or []
    cat.update_app_versions(vh)

    # 2. Gather all known versions across version_health, distributions, and catalog
    all_known_set = set(str(v.get("version")).strip() for v in vh if isinstance(v, dict) and v.get("version"))
    dist_v = app_data.get("distributions", {}).get("app_versions") or []
    for d in dist_v:
        if isinstance(d, dict) and d.get("app_version"):
            all_known_set.add(str(d["app_version"]).strip())
    for cv in cat.get_known_app_versions():
        all_known_set.add(cv)

    # 3. Resolve authoritative latest_version
    latest_ver = get_latest_app_version(app_data, catalog=cat)
    if not latest_ver:
        latest_ver = max_version(list(all_known_set)) if all_known_set else "1.0.0"
    all_known_set.add(latest_ver)
    all_known_sorted = sorted(list(all_known_set), key=version_key)

    # 4. Build sample sufficiency mapping for all known versions
    vh_map = {str(v.get("version")).strip(): v for v in vh if isinstance(v, dict) and v.get("version")}
    known_version_sufficiency: Dict[str, bool] = {}
    for v in all_known_sorted:
        v_info = vh_map.get(v) or cat.get_version_info(v)
        known_version_sufficiency[v] = is_version_sample_sufficient(v_info)

    sample_sufficient = known_version_sufficiency.get(latest_ver, False)

    # 5. Collect all issues across top_issues and all period snapshots and update catalog
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

    # 6. Enrich top-level top_issues
    if isinstance(app_data.get("top_issues"), list):
        for iss in app_data["top_issues"]:
            iid = iss.get("issue_id", "")
            hist = cat.get_issue_history(iid)
            hist_versions = hist.get("versions_seen", []) if hist else []

            # Retain true historical first_seen_version
            if hist:
                if hist.get("first_seen_version"):
                    iss["first_seen_version"] = hist["first_seen_version"]
                if hist.get("last_seen_version"):
                    iss["last_seen_version"] = hist["last_seen_version"]
                if hist.get("first_seen_timestamp") and not iss.get("first_seen_timestamp"):
                    iss["first_seen_timestamp"] = hist["first_seen_timestamp"]
                if hist.get("last_seen_timestamp") and not iss.get("last_seen_timestamp"):
                    iss["last_seen_timestamp"] = hist["last_seen_timestamp"]

            # Current version events breakdown if available
            v_dist = iss.get("version_distribution") or []
            v_events = {v["version"]: v.get("events", 0) for v in v_dist if isinstance(v, dict) and v.get("version")}

            lc = detect_issue_lifecycle(
                issue_id=iid,
                historical_versions=hist_versions,
                all_known_versions=all_known_sorted,
                latest_version=latest_ver,
                sample_sufficient=sample_sufficient,
                current_version_events=v_events,
                known_version_sufficiency=known_version_sufficiency,
            )
            iss["lifecycle"] = lc

    # 7. Enrich each period snapshot's top_issues
    if isinstance(periods, dict):
        for p_key, snap in periods.items():
            if not isinstance(snap, dict):
                continue

            snap_vh = snap.get("version_health") or vh
            snap_latest_ver = get_latest_app_version(snap, catalog=cat) or latest_ver
            snap_vh_map = {str(v.get("version")).strip(): v for v in snap_vh if isinstance(v, dict) and v.get("version")}

            snap_sufficiency: Dict[str, bool] = {}
            for v in all_known_sorted:
                v_info = snap_vh_map.get(v) or cat.get_version_info(v)
                snap_sufficiency[v] = is_version_sample_sufficient(v_info)
            snap_sample_sufficient = snap_sufficiency.get(snap_latest_ver, False)

            snap_issues = snap.get("top_issues") or []
            for iss in snap_issues:
                iid = iss.get("issue_id", "")
                hist = cat.get_issue_history(iid)
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

                lc = detect_issue_lifecycle(
                    issue_id=iid,
                    historical_versions=hist_versions,
                    all_known_versions=all_known_sorted,
                    latest_version=snap_latest_ver,
                    sample_sufficient=snap_sample_sufficient,
                    current_version_events=v_events,
                    known_version_sufficiency=snap_sufficiency,
                )
                iss["lifecycle"] = lc

    return app_data
