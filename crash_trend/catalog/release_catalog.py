"""Release Catalog assembly and recent health enrichment (Issue #29, #47, #53).

Provides:
- get_latest_app_version: Resolves the authoritative latest app version per platform.
- calculate_version_status: Evaluates latest, active, or legacy (>90d) status.
- build_release_catalog: Constructs decoupled persistent release catalog items.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal, cast

from crash_trend.catalog.comparison import compute_previous_release_comparison
from crash_trend.catalog.issue_lifecycle import is_version_sample_sufficient
from crash_trend.gate.policy import load_gate_policy
from crash_trend.schema_v2 import (
    PreviousReleaseComparison,
    ReleaseCatalogItem,
    ReleaseGateSummary,
    ReleaseIssueLifecycle,
    ReleaseRecentHealth,
)
from crash_trend.versions import max_version, version_key


def get_latest_app_version(
    app_data: dict[str, Any] | None,
    platform: str | None = None,
    catalog: Any | None = None,
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


def calculate_version_status(
    app_versions: dict[str, dict[str, Any]],
    version: str,
    platform: str,
    latest_version: str | None = None,
    reference_time: dt.datetime | None = None,
    known_versions: list[str] | None = None,
) -> Literal["latest", "active", "legacy"]:
    """Evaluates whether a version is latest, active, or legacy (>90d inactive)."""
    pf = "ios" if platform == "ios" else "android"
    latest_v = latest_version
    if not latest_v and known_versions:
        latest_v = max_version(known_versions)
    if latest_v and version == latest_v:
        return "latest"

    v_info = app_versions.get(pf, {}).get(version, {})
    explicit_status = str(v_info.get("status", "")).lower()
    if explicit_status in ("legacy", "deprecated"):
        return "legacy"

    ref_dt = reference_time or dt.datetime.now(dt.UTC)
    last_seen_str = v_info.get("last_seen")
    if last_seen_str:
        try:
            clean = last_seen_str.replace("Z", "+00:00")
            last_dt = dt.datetime.fromisoformat(clean)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=dt.UTC)
            else:
                last_dt = last_dt.astimezone(dt.UTC)
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
    catalog: Any,
    app_data: dict | None = None,
    platform: str | None = None,
    reference_date: Any | None = None,
    gate_policy: Any | None = None,
) -> list[ReleaseCatalogItem]:
    """Constructs the decoupled persistent release catalog conforming to ReleaseCatalogItem."""
    if gate_policy is not None:
        eff_policy = gate_policy
    else:
        effective_app_id = (
            getattr(catalog, "app_id", None)
            or (app_data.get("metadata", {}).get("app_id") if isinstance(app_data, dict) else None)
        )
        app_cfg = None
        if effective_app_id:
            try:
                from crash_trend.config import load_config
                cfg = load_config()
                app_cfg = (cfg.get("apps") or {}).get(effective_app_id)
            except Exception:
                app_cfg = None
        eff_policy = load_gate_policy(app_cfg)

    ref_dt = dt.datetime.now(dt.UTC)
    if reference_date is not None:
        if isinstance(reference_date, dt.datetime):
            ref_dt = reference_date if reference_date.tzinfo else reference_date.replace(tzinfo=dt.UTC)
        elif isinstance(reference_date, dt.date):
            ref_dt = dt.datetime(reference_date.year, reference_date.month, reference_date.day, tzinfo=dt.UTC)
        elif isinstance(reference_date, str):
            try:
                parsed = dt.datetime.fromisoformat(reference_date.replace("Z", "+00:00"))
                ref_dt = parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
            except Exception:
                pass

    target_platforms = [platform] if platform in ("android", "ios") else ["android", "ios"]
    catalog_items: list[ReleaseCatalogItem] = []

    for pf in target_platforms:
        known_vers = list(catalog.get_known_app_versions(platform=pf))
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

        latest_v = get_latest_app_version(app_data, platform=pf, catalog=catalog) or sorted_vers[-1]
        pf_issues = [iss for iss in catalog.issues.values() if iss.get("platform") == pf]
        version_sufficiency_map: dict[str, bool] = {
            v_name: is_version_sample_sufficient(catalog.app_versions.get(pf, {}).get(v_name))
            for v_name in sorted_vers
        }

        for idx, ver in enumerate(sorted_vers):
            v_prev = sorted_vers[idx - 1] if idx > 0 else None
            v_info = catalog.app_versions.get(pf, {}).get(ver, {})

            status = calculate_version_status(
                catalog.app_versions,
                ver,
                pf,
                latest_version=latest_v,
                reference_time=ref_dt,
                known_versions=sorted_vers,
            )

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

            introduced_ids: list[str] = []
            persistent_ids: list[str] = []
            regressed_ids: list[str] = []
            resolved_ids: list[str] = []

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

            recent_health: dict[str, ReleaseRecentHealth] = {}
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
                    snap_issues = (snap.get("top_issues") or []) if isinstance(snap, dict) else []
                    snap_pf_issues = [
                        s_iss for s_iss in snap_issues
                        if isinstance(s_iss, dict) and s_iss.get("platform") in (pf, "all", None)
                    ]
                    for s_iss in snap_pf_issues:
                        for v_dist in s_iss.get("version_distribution") or []:
                            if v_dist.get("version") == ver:
                                if s_iss.get("error_type") == "FATAL":
                                    snap_fatal += int(v_dist.get("events") or 0)
                                elif s_iss.get("error_type") == "ANR":
                                    snap_anr += int(v_dist.get("events") or 0)

                    new_iss_cnt = len([i for i in snap_pf_issues if i.get("first_seen_version") == ver])

                    w_entry: ReleaseRecentHealth = {
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
                    c_typed = cast(ReleaseRecentHealth, c_copy)
                    recent_health[w_key] = c_typed
                    recent_health[f"{w_key}d"] = c_typed
                else:
                    empty_entry: ReleaseRecentHealth = {
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

            vs_previous: PreviousReleaseComparison | None = None
            if v_prev:
                prev_info = catalog.app_versions.get(pf, {}).get(v_prev, {})
                prev_introduced_cnt = len([i for i in pf_issues if i.get("first_seen_version") == v_prev])

                vs_previous = compute_previous_release_comparison(
                    v_curr_info=v_info,
                    v_prev_info=prev_info,
                    v_prev=v_prev,
                    recent_health=recent_health,
                    introduced_count=len(introduced_ids),
                    prev_introduced_count=prev_introduced_cnt,
                    target_window=None,
                    min_adoption_rate=eff_policy.min_adoption_rate,
                    min_sessions=eff_policy.min_sessions,
                    min_version_events=eff_policy.min_version_events,
                )

            stability_status = vs_previous.get("stability", "baseline") if vs_previous else "baseline"

            rel_item: ReleaseCatalogItem = {
                "version": ver,
                "platform": cast(Literal["ios", "android"], pf),
                "first_seen": first_seen,
                "last_seen": last_seen,
                "release_date": rel_date,
                "status": status,
                "lifetime_crashes": lt_crashes,
                "lifetime_issues": lt_issues,
                "lifetime_affected_users": lt_users,
                "lifetime_fatal": lt_fatal,
                "lifetime_anr": lt_anr,
                "stability_status": stability_status,
                "recent_health": recent_health,
                "issue_lifecycle": issue_lifecycle,
                "vs_previous": vs_previous,
            }
            if eff_policy.enabled:
                from crash_trend.gate.evaluator import evaluate_release

                gate_eval = evaluate_release(cast(dict[str, Any], rel_item), eff_policy)
                rel_item["release_gate"] = cast(
                    ReleaseGateSummary,
                    {
                        "status": gate_eval["gate_status"],
                        "should_alert": gate_eval["alert"]["should_alert"],
                        "alert_severity": gate_eval["alert"]["alert_severity"],
                        "alert_summary": gate_eval["alert"]["alert_summary"],
                        "rules_triggered": gate_eval["alert"]["trigger_rules"],
                        "sample_sufficient": gate_eval["sample_sufficient"],
                        "rule_results": gate_eval["rule_results"],
                        "comparison_window": gate_eval.get("comparison_window"),
                        "evaluated_at": gate_eval["evaluated_at"],
                    },
                )
            else:
                rel_item["release_gate"] = None

            # Attach historical gate evaluations if available (Issue #61)
            cat_app_id = getattr(catalog, "app_id", None)
            if cat_app_id:
                try:
                    from crash_trend.gate.history import get_gate_history_store
                    cat_path = getattr(catalog, "catalog_path", None)
                    hist_path = cat_path.parent if cat_path else None
                    hist_store = get_gate_history_store(cat_app_id, custom_path=hist_path / "release_gate_history.sqlite3" if hist_path else None)
                    snaps = hist_store.get_release_gate_history(cat_app_id, pf, ver)
                    if snaps:
                        rel_item["gate_history"] = [
                            {
                                "evaluated_at": s.evaluated_at,
                                "gate_status": s.gate_status,
                                "sample_sufficient": s.sample_sufficient,
                                "summary": s.summary,
                                "rules_triggered": list(s.triggered_reasons),
                                "transition": s.transition.to_dict() if s.transition else None,
                                "evaluation_key": s.evaluation_key,
                                "policy_version": s.policy_version,
                                "policy_identity": s.policy_identity,
                                "comparison_window": s.comparison_window,
                                "rule_results": list(s.rule_results),
                            }
                            for s in snaps
                        ]
                except Exception:
                    pass


            catalog_items.append(rel_item)


    final_items: list[ReleaseCatalogItem] = []
    for pf in target_platforms:
        pf_items = [i for i in catalog_items if i["platform"] == pf]
        latest_items = [i for i in pf_items if i["status"] == "latest"]
        other_items = sorted([i for i in pf_items if i["status"] != "latest"], key=lambda x: version_key(x["version"]), reverse=True)
        final_items.extend(latest_items + other_items)

    return final_items
