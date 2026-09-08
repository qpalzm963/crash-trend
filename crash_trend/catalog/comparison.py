"""Previous-release normalized comparison and stability evaluation (Issue #29, #47, #53).

Provides:
- compute_previous_release_comparison: Evaluates normalized crash rate, fatal rate,
  ANR rate differences and stability status vs immediate previous release.
"""

from __future__ import annotations

from typing import Any, Literal

from crash_trend.catalog.issue_lifecycle import is_version_sample_sufficient
from crash_trend.schema_v2 import PreviousReleaseComparison


def compute_previous_release_comparison(
    v_curr_info: dict[str, Any],
    v_prev_info: dict[str, Any],
    v_prev: str,
    recent_health: dict[str, Any],
    introduced_count: int,
    prev_introduced_count: int,
    target_window: str | None = None,
    min_adoption_rate: float = 0.05,
    min_sessions: int = 1000,
    min_version_events: int = 20,
) -> PreviousReleaseComparison:
    """Computes comparison metrics between current release and previous release.

    Prefers matching recent_health windows (30d -> 90d -> 7d) that satisfy sample
    sufficiency with valid sessions_total for normalized exposure comparison.
    Falls back to any matching window with sessions, then lifetime sessions/events.
    """
    prev_recent = v_prev_info.get("recent_health", {})

    # Find matching window for normalized exposure comparison
    crash_rate_diff: float | None = None
    fatal_rate_diff: float | None = None
    anr_rate_diff: float | None = None
    comp_w: str | None = None
    matched_c_w: dict[str, Any] | None = None
    matched_p_w: dict[str, Any] | None = None

    # 1. Caller specified target_window (e.g. from sample-sufficiency check)
    if target_window:
        tw_clean = target_window.rstrip("d")
        for key in (tw_clean, f"{tw_clean}d"):
            c_cand = recent_health.get(key)
            p_cand = prev_recent.get(key) if isinstance(prev_recent, dict) else None
            if c_cand and p_cand and c_cand.get("sessions_total") and p_cand.get("sessions_total"):
                comp_w = f"{tw_clean}d"
                matched_c_w = c_cand
                matched_p_w = p_cand
                break

    # 2. Auto-select: Pass 1 - prefer window satisfying sample sufficiency
    if not comp_w:
        for candidate_w in ("30", "90", "7"):
            for key in (candidate_w, f"{candidate_w}d"):
                c_cand = recent_health.get(key)
                p_cand = prev_recent.get(key) if isinstance(prev_recent, dict) else None
                if c_cand and p_cand and c_cand.get("sessions_total") and p_cand.get("sessions_total"):
                    if is_version_sample_sufficient(
                        c_cand,
                        min_adoption_rate=min_adoption_rate,
                        min_sessions=min_sessions,
                        min_version_events=min_version_events,
                    ):
                        comp_w = f"{candidate_w}d"
                        matched_c_w = c_cand
                        matched_p_w = p_cand
                        break
            if comp_w:
                break

    # 3. Auto-select: Pass 2 - fall back to any matching window with sessions
    if not comp_w:
        for candidate_w in ("30", "90", "7"):
            for key in (candidate_w, f"{candidate_w}d"):
                c_cand = recent_health.get(key)
                p_cand = prev_recent.get(key) if isinstance(prev_recent, dict) else None
                if c_cand and p_cand and c_cand.get("sessions_total") and p_cand.get("sessions_total"):
                    comp_w = f"{candidate_w}d"
                    matched_c_w = c_cand
                    matched_p_w = p_cand
                    break
            if comp_w:
                break

    if matched_c_w and matched_p_w:
        c_w = matched_c_w
        p_w = matched_p_w
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
        c_sess = v_curr_info.get("sessions_total")
        p_sess = v_prev_info.get("sessions_total")
        raw_c_ev = v_curr_info.get("crash_events")
        raw_p_ev = v_prev_info.get("crash_events")
        if c_sess and p_sess and int(c_sess) > 0 and int(p_sess) > 0:
            c_se = int(c_sess)
            p_se = int(p_sess)
            if raw_c_ev is not None and raw_p_ev is not None:
                rate_curr = int(raw_c_ev) / c_se
                rate_prev = int(raw_p_ev) / p_se
                crash_rate_diff = round((rate_curr - rate_prev) / rate_prev, 4) if rate_prev > 0 else 0.0

            # Fallback: strictly require window-scoped fatal_events / fatal_count matching sessions.
            # NEVER fall back to lifetime_fatal or lifetime_anr!
            c_fat = v_curr_info.get("fatal_events") if v_curr_info.get("fatal_events") is not None else v_curr_info.get("fatal_count")
            p_fat = v_prev_info.get("fatal_events") if v_prev_info.get("fatal_events") is not None else v_prev_info.get("fatal_count")
            if c_fat is not None and p_fat is not None:
                r_fat_curr = int(c_fat) / c_se
                r_fat_prev = int(p_fat) / p_se
                if r_fat_prev > 0:
                    fatal_rate_diff = round((r_fat_curr - r_fat_prev) / r_fat_prev, 4)
                elif r_fat_curr == 0 and r_fat_prev == 0:
                    fatal_rate_diff = 0.0
            else:
                fatal_rate_diff = None

            c_anr = v_curr_info.get("anr_events") if v_curr_info.get("anr_events") is not None else v_curr_info.get("anr_count")
            p_anr = v_prev_info.get("anr_events") if v_prev_info.get("anr_events") is not None else v_prev_info.get("anr_count")
            if c_anr is not None and p_anr is not None:
                r_anr_curr = int(c_anr) / c_se
                r_anr_prev = int(p_anr) / p_se
                if r_anr_prev > 0:
                    anr_rate_diff = round((r_anr_curr - r_anr_prev) / r_anr_prev, 4)
                elif r_anr_curr == 0 and r_anr_prev == 0:
                    anr_rate_diff = 0.0
            else:
                anr_rate_diff = None

    cfu_curr = v_curr_info.get("crash_free_users_rate")
    if cfu_curr is None:
        for rh in recent_health.values():
            if isinstance(rh, dict) and rh.get("crash_free_users_rate") is not None:
                cfu_curr = rh["crash_free_users_rate"]
                break
    cfu_prev = v_prev_info.get("crash_free_users_rate")
    if cfu_prev is None and isinstance(prev_recent, dict):
        for rh in prev_recent.values():
            if isinstance(rh, dict) and rh.get("crash_free_users_rate") is not None:
                cfu_prev = rh["crash_free_users_rate"]
                break

    cfu_diff: float | None = None
    if cfu_curr is not None and cfu_prev is not None:
        cfu_diff = round(cfu_curr - cfu_prev, 4)

    new_issues_diff = introduced_count - prev_introduced_count

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

    stability_status = "improved" if stability == "improving" else ("regressed" if stability == "degrading" else stability)

    return {
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
        "stability_status": stability_status,
        "comparison_window": comp_w,
    }
