"""Deterministic Issue Lifecycle Detection (Issue #29, #53).

Provides:
- is_version_sample_sufficient: Evaluates if a version has sufficient observation evidence.
- detect_issue_lifecycle: Deterministically evaluates the 5 canonical lifecycle states:
  new_in_latest, persistent, regressed, resolved, not_observed_latest.
- detect_issue_fix_status: Issue-centric view of whether a problem is still occurring.
"""

from __future__ import annotations

from collections.abc import Iterable

from crash_trend.schema_v2 import IssueFixStatus, IssueLifecycle
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

    if version_info.get("sample_sufficient") is False:
        return False

    if version_info.get("sample_sufficient") is True:
        return True

    adoption_rate = version_info.get("adoption_rate")
    if adoption_rate is not None and isinstance(adoption_rate, (int, float)):
        if float(adoption_rate) >= min_adoption_rate:
            return True

    sessions_total = version_info.get("sessions_total")
    if sessions_total is not None and isinstance(sessions_total, (int, float)):
        if int(sessions_total) >= min_sessions:
            return True

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


_CONFIDENCE_ORDER = ("high", "medium", "low")


def _downgrade(confidence: str) -> str:
    """把信心度降一級（high -> medium -> low），已是 low 則維持。"""
    try:
        return _CONFIDENCE_ORDER[min(_CONFIDENCE_ORDER.index(confidence) + 1, len(_CONFIDENCE_ORDER) - 1)]
    except ValueError:
        return "low"


def detect_issue_fix_status(
    versions_seen: Iterable[str],
    all_known_versions: Iterable[str],
    latest_version: str,
    version_health_map: dict[str, dict] | None = None,
    min_evidence_events: int = 20,
    min_evidence_sessions: int = 1000,
    issue_version_events: dict[str, int] | None = None,
    min_expected_occurrences: float = 3.0,
    data_age_days: float | None = None,
    data_as_of: str | None = None,
    stale_after_days: float = 7.0,
    unusable_after_days: float = 30.0,
) -> IssueFixStatus:
    """判定「這個 issue 目前還在不在」。

    與 :func:`detect_issue_lifecycle` 互補。後者以版本為主體，`resolved` 要求
    issue 在緊鄰的前一版仍然活躍（才算得上是 latest_version 的修復功勞）；因此
    一個在更早版本就消失的 issue 永遠不會被標為 resolved，即使它事實上已經很久
    沒再出現。本函式改以 issue 為主體，回答使用者實際會問的問題。

    「沒再出現」要成為「已修復」的證據，必須同時通過三道檢查：

    1. **資料夠新**：判定只能描述資料截止當下的狀態。一份兩週前的 catalog 說
       「已修復」，說的是兩週前的事；期間的復發它看不到。超過
       ``unusable_after_days`` 直接不給 likely_fixed，超過 ``stale_after_days``
       降一級信心度。
    2. **曝光量足以期望看到它**：以 issue 自身在最後出現版本的發生率，推算它在
       後續版本「本應」出現幾次。低於 ``min_expected_occurrences`` 代表就算 bug
       還在、這點資料量也照不出來，判 unproven。缺少分版本事件數時退回
       ``min_evidence_events`` 絕對門檻——那是粗略代理（版本層級總當機數可能
       全部來自其他 issue），因此信心度不會是 high。
    3. **後續版本確實沒再觀察到**。

    證據來源優先 sessions_total（曝光量的直接證據），無 Sessions 匯出時退回
    crash_events。

    狀態：
    - still_present: latest_version 仍觀察到此 issue。
    - likely_fixed: 後續版本具備足夠曝光量且未再觀察到，且資料夠新。
    - unproven: 曝光量不足或資料過舊，無法區分「已修復」與「還沒觀察到」。
    """
    seen_set = {v for v in versions_seen if v}
    sorted_all = sorted({v for v in all_known_versions if v}, key=version_key)

    def _result(status, last_seen, since, events, sessions, confidence, reason, expected=None):
        return {
            "status": status,
            "last_seen_version": last_seen,
            "versions_since": since,
            "evidence_events": events,
            "evidence_sessions": sessions,
            "confidence": confidence,
            "reason": reason,
            "expected_occurrences": expected,
            "data_age_days": data_age_days,
            "data_as_of": data_as_of,
        }

    if not seen_set:
        return _result("unproven", "", [], 0, None, "low", "尚無任何版本出現紀錄")

    last_seen = max_version(sorted(seen_set, key=version_key)) or latest_version

    if latest_version in seen_set:
        return _result("still_present", last_seen, [], 0, None, "high", f"最新版本 {latest_version} 仍觀察到")

    versions_since = [v for v in sorted_all if version_key(v) > version_key(last_seen)]
    if not versions_since:
        return _result("unproven", last_seen, [], 0, None, "low", f"{last_seen} 之後尚無其他版本可供佐證")

    health = version_health_map or {}
    evidence_events = 0
    evidence_sessions: int | None = None
    for v in versions_since:
        v_info = health.get(v) or {}
        ev = v_info.get("crash_events")
        if isinstance(ev, (int, float)):
            evidence_events += int(ev)
        sess = v_info.get("sessions_total")
        if isinstance(sess, (int, float)):
            evidence_sessions = (evidence_sessions or 0) + int(sess)

    since_desc = "、".join(versions_since)

    # 檢查 1：資料過舊時，「沒再出現」只代表資料截止前沒看到，不能當成已修復。
    if data_age_days is not None and data_age_days > unusable_after_days:
        return _result(
            "unproven", last_seen, versions_since, evidence_events, evidence_sessions, "low",
            f"最後出現於 {last_seen}；但判定所依據的資料已是 {data_age_days:.0f} 天前，期間的復發無從得知",
        )

    # 檢查 2：以 issue 自身發生率推算後續版本「本應」出現幾次。
    # 分母要跟證據同一種單位：有 sessions 時用「每 session 發生率 × 後續 sessions」；
    # 只有當機數時才退回「佔當機比例 × 後續當機數」。若在有 sessions 時仍用當機佔比，
    # 新版當機大幅減少（正是修好的樣子）反而會讓本應出現次數趨近 0，把最強的
    # sessions 證據擋掉——多給資料卻得出更弱的結論。
    expected: float | None = None
    basis = ""
    rate = 0.0
    own_at_last = (issue_version_events or {}).get(last_seen)
    last_info = health.get(last_seen) or {}
    total_at_last = last_info.get("crash_events")
    sessions_at_last = last_info.get("sessions_total")
    if isinstance(own_at_last, (int, float)) and own_at_last > 0:
        if evidence_sessions is not None and isinstance(sessions_at_last, (int, float)) and sessions_at_last > 0:
            rate = min(1.0, float(own_at_last) / float(sessions_at_last))
            expected = rate * float(evidence_sessions)
            basis = "sessions"
        elif evidence_sessions is not None and evidence_sessions >= min_evidence_sessions:
            # 後續版本有足量 sessions、但缺最後出現版本的 sessions 無法換算發生率：
            # 直接的 sessions 證據已足夠，不以當機佔比這個較弱的代理去否決它。
            expected = None
        elif isinstance(total_at_last, (int, float)) and total_at_last > 0:
            # 分版本事件數與版本總量可能來自不同視窗，佔比會超過 100%；夾在 1.0 以免高估本應出現次數
            rate = min(1.0, float(own_at_last) / float(total_at_last))
            expected = rate * float(evidence_events)
            basis = "events"

    if expected is not None and expected < min_expected_occurrences:
        if basis == "sessions":
            detail = (f"每萬 sessions 約 {rate * 10000:.2f} 次）；其後 {since_desc} 合計 {evidence_sessions} sessions，")
        else:
            detail = f"該版佔比 {rate:.0%}）；其後 {since_desc} 僅 {evidence_events} 次事件，"
        return _result(
            "unproven", last_seen, versions_since, evidence_events, evidence_sessions, "low",
            f"最後出現於 {last_seen}（{detail}本應出現約 {expected:.1f} 次，不足以證明消失",
            expected,
        )

    if evidence_sessions is not None and evidence_sessions >= min_evidence_sessions:
        conf = "high"
        reason = f"最後出現於 {last_seen}；其後 {since_desc} 合計 {evidence_sessions} sessions 未再觀察到"
        if expected is not None:
            reason += f"，依自身發生率本應出現約 {expected:.1f} 次"
    elif evidence_events >= min_evidence_events:
        # crash_events 是版本層級總量，可能全部來自其他 issue，只是粗略的曝光量代理
        conf = "medium"
        reason = f"最後出現於 {last_seen}；其後 {since_desc} 合計 {evidence_events} 次當機事件未再觀察到（無 Sessions 佐證）"
        if expected is not None:
            reason += f"，依自身發生率本應出現約 {expected:.1f} 次"
    else:
        return _result(
            "unproven", last_seen, versions_since, evidence_events, evidence_sessions, "low",
            f"最後出現於 {last_seen}；其後 {since_desc} 曝光量不足，無法證明已修復", expected,
        )

    # 檢查 3：資料偏舊但仍可用時降一級信心度，並在理由中標明截止範圍。
    if data_age_days is not None and data_age_days > stale_after_days:
        conf = _downgrade(conf)
        reason += f"（資料截至 {data_age_days:.0f} 天前）"

    return _result("likely_fixed", last_seen, versions_since, evidence_events, evidence_sessions, conf, reason, expected)
