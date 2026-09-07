"""Deterministic Issue Lifecycle Detection (Issue #29, #53).

Provides:
- is_version_sample_sufficient: Evaluates if a version has sufficient observation evidence.
- detect_issue_lifecycle: Deterministically evaluates the 5 canonical lifecycle states:
  new_in_latest, persistent, regressed, resolved, not_observed_latest.
"""

from __future__ import annotations

from collections.abc import Iterable

from crash_trend.schema_v2 import IssueLifecycle
from crash_trend.versions import max_version, min_version, version_key


def is_version_sample_sufficient(
    version_info: dict | None,
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
    current_version_events: dict[str, int] | None = None,
    known_version_sufficiency: dict[str, bool] | None = None,
    version_health_map: dict[str, dict] | None = None,
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
        proven_absent_versions: list[str] = []
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
