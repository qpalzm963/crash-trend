"""Deterministic Release Regression Gate evaluator (Issue #57).

Evaluates release quality degradation against configurable threshold policies.
AI is strictly forbidden from determining PASS/FAIL status; evaluation is 100%
deterministic based on normalized metrics and lifecycle tracking.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from crash_trend.catalog.issue_lifecycle import is_version_sample_sufficient
from crash_trend.gate.artifact import (
    AlertHookPayload,
    AlertSeverity,
    GateStatus,
    PlatformGateResult,
    ReleaseGateArtifact,
    RuleEvaluationResult,
    RuleStatus,
)
from crash_trend.gate.policy import GatePolicy


def _is_sample_sufficient(
    item: dict[str, Any] | None,
    recent_health: dict[str, Any] | None,
    policy: GatePolicy,
) -> tuple[bool, int, str, str | None]:
    """Inspects recent health windows and release item using unified is_version_sample_sufficient."""
    min_adopt = policy.min_adoption_rate
    min_sess = policy.min_sessions
    min_ev = policy.min_version_events

    # 1. Inspect recent_health windows in order of relevance: 30d -> 90d -> 7d
    if isinstance(recent_health, dict) and recent_health:
        for candidate_w in ("30d", "30", "90d", "90", "7d", "7"):
            win_data = recent_health.get(candidate_w)
            if isinstance(win_data, dict):
                sess_int = int(win_data.get("sessions_total") or 0)
                if is_version_sample_sufficient(
                    win_data,
                    min_adoption_rate=min_adopt,
                    min_sessions=min_sess,
                    min_version_events=min_ev,
                ):
                    norm_w = f"{candidate_w.rstrip('d')}d"
                    if win_data.get("sample_sufficient") is True:
                        reason = f"{candidate_w} 視窗顯式標記為樣本充足"
                    elif (win_data.get("adoption_rate") or 0) >= min_adopt:
                        reason = f"{candidate_w} 視窗採用率充足 ({win_data.get('adoption_rate', 0):.1%} >= {min_adopt:.1%})"
                    elif sess_int >= min_sess:
                        reason = f"{candidate_w} 視窗工作階段充足 ({sess_int} sessions >= {min_sess})"
                    else:
                        ev = int(win_data.get("crash_events") or 0)
                        reason = f"{candidate_w} 視窗事件數充足 ({ev} events >= {min_ev})"
                    return True, sess_int, reason, norm_w

        # Check all other windows in recent_health
        for w_name, win_data in recent_health.items():
            if isinstance(win_data, dict):
                sess_int = int(win_data.get("sessions_total") or 0)
                if is_version_sample_sufficient(
                    win_data,
                    min_adoption_rate=min_adopt,
                    min_sessions=min_sess,
                    min_version_events=min_ev,
                ):
                    norm_w = f"{w_name.rstrip('d')}d" if w_name.rstrip('d').isdigit() else w_name
                    return True, sess_int, f"{w_name} 視窗樣本充足", norm_w

    # 2. Inspect top-level item if applicable
    if isinstance(item, dict):
        sess_int = int(item.get("sessions_total") or 0)
        if is_version_sample_sufficient(
            item,
            min_adoption_rate=min_adopt,
            min_sessions=min_sess,
            min_version_events=min_ev,
        ):
            if item.get("sample_sufficient") is True:
                reason = "版本顯式標記為樣本充足"
            elif (item.get("adoption_rate") or 0) >= min_adopt:
                reason = f"版本採用率充足 ({item.get('adoption_rate', 0):.1%} >= {min_adopt:.1%})"
            elif sess_int >= min_sess:
                reason = f"版本工作階段充足 ({sess_int} sessions >= {min_sess})"
            else:
                ev = int(item.get("lifetime_crashes") or item.get("crash_events") or 0)
                reason = f"版本事件數充足 ({ev} events >= {min_ev})"
            return True, sess_int, reason, None

    # Determine maximum observed sessions for informative diagnostics
    max_sess = 0
    if isinstance(recent_health, dict):
        for w in recent_health.values():
            if isinstance(w, dict):
                max_sess = max(max_sess, int(w.get("sessions_total") or 0))
    if isinstance(item, dict):
        max_sess = max(max_sess, int(item.get("sessions_total") or 0))

    return False, max_sess, f"數據未達充足門檻（未滿足採用率 {min_adopt:.1%}、工作階段 {min_sess} 或事件數 {min_ev} 標準）", None


def evaluate_release(
    item: dict[str, Any],
    policy: GatePolicy,
) -> PlatformGateResult:
    """Deterministically evaluates a single release item against a GatePolicy."""
    now_iso = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    ver = str(item.get("version", "unknown"))
    raw_pf = item.get("platform", "android")
    pf: Literal["ios", "android"] = "ios" if raw_pf == "ios" else "android"

    # Handle disabled policy semantics
    if not policy.enabled:
        dis_alert: AlertHookPayload = {
            "should_alert": False,
            "alert_severity": "none",
            "alert_summary": f"版本 {ver} ({pf}) 品質閘門未啟用 (enabled: false)",
            "trigger_rules": [],
        }
        return {
            "platform": pf,
            "target_version": ver,
            "previous_version": None,
            "gate_status": "pass",
            "sample_sufficient": True,
            "rule_results": [],
            "alert": dis_alert,
            "evaluated_at": now_iso,
            "comparison_window": None,
        }

    recent_health = item.get("recent_health") or {}
    sample_ok, total_sess, sess_reason, suff_w = _is_sample_sufficient(item, recent_health, policy)

    # 1. Sample Sufficiency Guard
    if not sample_ok:
        insuf_alert: AlertHookPayload = {
            "should_alert": False,
            "alert_severity": "none",
            "alert_summary": f"版本 {ver} ({pf}) 數據樣本不足 ({sess_reason})，暫停退化判定",
            "trigger_rules": [],
        }
        insuf_rule: RuleEvaluationResult = {
            "rule_name": "sample_sufficiency",
            "metric_name": "sessions_total",
            "current_value": total_sess,
            "previous_value": None,
            "warn_threshold": policy.min_sessions,
            "fail_threshold": policy.min_sessions,
            "status": "insufficient_data",
            "reason": f"樣本數不足：{sess_reason}，未達最小門檻 ({policy.min_sessions} sessions)",
        }
        return {
            "platform": pf,
            "target_version": ver,
            "previous_version": None,
            "gate_status": "insufficient_data",
            "sample_sufficient": False,
            "rule_results": [insuf_rule],
            "alert": insuf_alert,
            "evaluated_at": now_iso,
            "comparison_window": None,
        }

    # 2. Baseline Check (No previous version on platform)
    vs_previous = item.get("vs_previous")
    prev_ver = vs_previous.get("previous_version") if isinstance(vs_previous, dict) else None

    if not prev_ver:
        base_alert: AlertHookPayload = {
            "should_alert": False,
            "alert_severity": "none",
            "alert_summary": f"版本 {ver} ({pf}) 為該平台首個記錄版本，作為基準版",
            "trigger_rules": [],
        }
        base_rule: RuleEvaluationResult = {
            "rule_name": "baseline_version",
            "metric_name": "previous_version",
            "current_value": None,
            "previous_value": None,
            "warn_threshold": 0,
            "fail_threshold": 0,
            "status": "pass",
            "reason": "無前版基準資料，作為初始基準版本",
        }
        return {
            "platform": pf,
            "target_version": ver,
            "previous_version": None,
            "gate_status": "baseline",
            "sample_sufficient": True,
            "rule_results": [base_rule],
            "alert": base_alert,
            "evaluated_at": now_iso,
            "comparison_window": None,
        }

    # 3. Normalized Metric Rules Evaluation
    rules: list[RuleEvaluationResult] = []
    vs_p = vs_previous if isinstance(vs_previous, dict) else {}
    lc = item.get("issue_lifecycle") or {}

    # Rule 1: Crash rate change percentage
    cr_diff = vs_p.get("crash_rate_change_pct")
    if cr_diff is not None:
        cr_val = float(cr_diff)
        cr_st: RuleStatus
        if cr_val >= policy.crash_rate_change_pct.fail:
            cr_st = "fail"
            cr_msg = f"崩潰率上升 {cr_val * 100:+.2f}%，達到失敗門檻 (+{policy.crash_rate_change_pct.fail * 100:.1f}%)"
        elif cr_val >= policy.crash_rate_change_pct.warn:
            cr_st = "warn"
            cr_msg = f"崩潰率上升 {cr_val * 100:+.2f}%，達到警告門檻 (+{policy.crash_rate_change_pct.warn * 100:.1f}%)"
        else:
            cr_st = "pass"
            cr_msg = f"崩潰率變動 {cr_val * 100:+.2f}% 於正常範圍"
        rules.append({
            "rule_name": "crash_rate_regression",
            "metric_name": "crash_rate_change_pct",
            "current_value": round(cr_val, 4),
            "previous_value": 0.0,
            "warn_threshold": policy.crash_rate_change_pct.warn,
            "fail_threshold": policy.crash_rate_change_pct.fail,
            "status": cr_st,
            "reason": cr_msg,
        })
    else:
        rules.append({
            "rule_name": "crash_rate_regression",
            "metric_name": "crash_rate_change_pct",
            "current_value": None,
            "previous_value": None,
            "warn_threshold": policy.crash_rate_change_pct.warn,
            "fail_threshold": policy.crash_rate_change_pct.fail,
            "status": "skip",
            "reason": "未取得工作階段崩潰率數據",
        })

    # Rule 2: Crash-free users drop
    cfu_diff = vs_p.get("crash_free_users_diff")
    if cfu_diff is not None:
        # cfu_diff is (current - prev); a drop means cfu_diff < 0
        drop_val = -float(cfu_diff)
        cfu_st: RuleStatus
        if drop_val >= policy.crash_free_users_drop.fail:
            cfu_st = "fail"
            cfu_msg = f"無崩潰用戶率下降 {drop_val * 100:.2f}%，達到失敗門檻 (-{policy.crash_free_users_drop.fail * 100:.2f}%)"
        elif drop_val >= policy.crash_free_users_drop.warn:
            cfu_st = "warn"
            cfu_msg = f"無崩潰用戶率下降 {drop_val * 100:.2f}%，達到警告門檻 (-{policy.crash_free_users_drop.warn * 100:.2f}%)"
        else:
            cfu_st = "pass"
            cfu_msg = f"無崩潰用戶率變動 {float(cfu_diff) * 100:+.2f}% 於正常範圍"
        rules.append({
            "rule_name": "crash_free_users_drop",
            "metric_name": "crash_free_users_diff",
            "current_value": round(float(cfu_diff), 4),
            "previous_value": 0.0,
            "warn_threshold": -policy.crash_free_users_drop.warn,
            "fail_threshold": -policy.crash_free_users_drop.fail,
            "status": cfu_st,
            "reason": cfu_msg,
        })
    else:
        rules.append({
            "rule_name": "crash_free_users_drop",
            "metric_name": "crash_free_users_diff",
            "current_value": None,
            "previous_value": None,
            "warn_threshold": -policy.crash_free_users_drop.warn,
            "fail_threshold": -policy.crash_free_users_drop.fail,
            "status": "skip",
            "reason": "未取得無崩潰用戶率數據",
        })

    # Rule 3: Fatal rate change percentage
    fatal_diff = vs_p.get("fatal_rate_change_pct") if vs_p.get("fatal_rate_change_pct") is not None else vs_p.get("fatal_change_pct")
    if fatal_diff is not None:
        fat_val = float(fatal_diff)
        fat_st: RuleStatus
        if fat_val >= policy.fatal_rate_change_pct.fail:
            fat_st = "fail"
            fat_msg = f"Fatal 崩潰率上升 {fat_val * 100:+.2f}%，達到失敗門檻 (+{policy.fatal_rate_change_pct.fail * 100:.1f}%)"
        elif fat_val >= policy.fatal_rate_change_pct.warn:
            fat_st = "warn"
            fat_msg = f"Fatal 崩潰率上升 {fat_val * 100:+.2f}%，達到警告門檻 (+{policy.fatal_rate_change_pct.warn * 100:.1f}%)"
        else:
            fat_st = "pass"
            fat_msg = f"Fatal 崩潰率變動 {fat_val * 100:+.2f}% 於正常範圍"
        rules.append({
            "rule_name": "fatal_rate_regression",
            "metric_name": "fatal_rate_change_pct",
            "current_value": round(fat_val, 4),
            "previous_value": 0.0,
            "warn_threshold": policy.fatal_rate_change_pct.warn,
            "fail_threshold": policy.fatal_rate_change_pct.fail,
            "status": fat_st,
            "reason": fat_msg,
        })
    else:
        rules.append({
            "rule_name": "fatal_rate_regression",
            "metric_name": "fatal_rate_change_pct",
            "current_value": None,
            "previous_value": None,
            "warn_threshold": policy.fatal_rate_change_pct.warn,
            "fail_threshold": policy.fatal_rate_change_pct.fail,
            "status": "skip",
            "reason": "未取得 Fatal 崩潰率數據",
        })

    # Rule 4: ANR rate change percentage
    anr_diff = vs_p.get("anr_rate_change_pct") if vs_p.get("anr_rate_change_pct") is not None else vs_p.get("anr_change_pct")
    if anr_diff is not None:
        anr_val = float(anr_diff)
        anr_st: RuleStatus
        if anr_val >= policy.anr_rate_change_pct.fail:
            anr_st = "fail"
            anr_msg = f"ANR 率上升 {anr_val * 100:+.2f}%，達到失敗門檻 (+{policy.anr_rate_change_pct.fail * 100:.1f}%)"
        elif anr_val >= policy.anr_rate_change_pct.warn:
            anr_st = "warn"
            anr_msg = f"ANR 率上升 {anr_val * 100:+.2f}%，達到警告門檻 (+{policy.anr_rate_change_pct.warn * 100:.1f}%)"
        else:
            anr_st = "pass"
            anr_msg = f"ANR 率變動 {anr_val * 100:+.2f}% 於正常範圍"
        rules.append({
            "rule_name": "anr_rate_regression",
            "metric_name": "anr_rate_change_pct",
            "current_value": round(anr_val, 4),
            "previous_value": 0.0,
            "warn_threshold": policy.anr_rate_change_pct.warn,
            "fail_threshold": policy.anr_rate_change_pct.fail,
            "status": anr_st,
            "reason": anr_msg,
        })
    else:
        rules.append({
            "rule_name": "anr_rate_regression",
            "metric_name": "anr_rate_change_pct",
            "current_value": None,
            "previous_value": None,
            "warn_threshold": policy.anr_rate_change_pct.warn,
            "fail_threshold": policy.anr_rate_change_pct.fail,
            "status": "skip",
            "reason": "未取得 ANR 率數據",
        })

    # Rule 5: Regressed issues count
    regressed_cnt = int(lc.get("regressed_count", 0))
    reg_st: RuleStatus
    if regressed_cnt >= policy.regressed_issues_count.fail:
        reg_st = "fail"
        reg_msg = f"復發問題數 {regressed_cnt} 個，達到失敗門檻 ({policy.regressed_issues_count.fail} 個)"
    elif regressed_cnt >= policy.regressed_issues_count.warn:
        reg_st = "warn"
        reg_msg = f"復發問題數 {regressed_cnt} 個，達到警告門檻 ({policy.regressed_issues_count.warn} 個)"
    else:
        reg_st = "pass"
        reg_msg = f"復發問題數 {regressed_cnt} 個於正常範圍"
    rules.append({
        "rule_name": "regressed_issues_count",
        "metric_name": "regressed_issues_count",
        "current_value": regressed_cnt,
        "previous_value": 0,
        "warn_threshold": policy.regressed_issues_count.warn,
        "fail_threshold": policy.regressed_issues_count.fail,
        "status": reg_st,
        "reason": reg_msg,
    })

    # Rule 6: Introduced issues count
    introduced_cnt = int(lc.get("introduced_count", 0))
    intro_st: RuleStatus
    if introduced_cnt >= policy.introduced_issues_count.fail:
        intro_st = "fail"
        intro_msg = f"新引入問題數 {introduced_cnt} 個，達到失敗門檻 ({policy.introduced_issues_count.fail} 個)"
    elif introduced_cnt >= policy.introduced_issues_count.warn:
        intro_st = "warn"
        intro_msg = f"新引入問題數 {introduced_cnt} 個，達到警告門檻 ({policy.introduced_issues_count.warn} 個)"
    else:
        intro_st = "pass"
        intro_msg = f"新引入問題數 {introduced_cnt} 個於正常範圍"
    rules.append({
        "rule_name": "introduced_issues_count",
        "metric_name": "introduced_issues_count",
        "current_value": introduced_cnt,
        "previous_value": 0,
        "warn_threshold": policy.introduced_issues_count.warn,
        "fail_threshold": policy.introduced_issues_count.fail,
        "status": intro_st,
        "reason": intro_msg,
    })

    # 4. Overall Status Aggregation
    fail_rules = [r for r in rules if r["status"] == "fail"]
    warn_rules = [r for r in rules if r["status"] == "warn"]

    if fail_rules:
        overall: GateStatus = "fail"
        severity: AlertSeverity = "critical"
        should_alert = True
        summary = f"版本 {ver} ({pf}) 品質閘門判定失敗（{len(fail_rules)} 項超標）：" + "；".join(r["reason"] for r in fail_rules)
        trigger_names = [r["rule_name"] for r in fail_rules]
    elif warn_rules:
        overall = "warn"
        severity = "warning"
        should_alert = True
        summary = f"版本 {ver} ({pf}) 品質閘門觸發警告（{len(warn_rules)} 項預警）：" + "；".join(r["reason"] for r in warn_rules)
        trigger_names = [r["rule_name"] for r in warn_rules]
    else:
        overall = "pass"
        severity = "none"
        should_alert = False
        summary = f"版本 {ver} ({pf}) 品質閘門通過，所有正規化指標均在安全閾值內"
        trigger_names = []

    alert_payload: AlertHookPayload = {
        "should_alert": should_alert,
        "alert_severity": severity,
        "alert_summary": summary,
        "trigger_rules": trigger_names,
    }

    comp_win = (vs_p.get("comparison_window") if isinstance(vs_p, dict) else None) or suff_w

    return {
        "platform": pf,
        "target_version": ver,
        "previous_version": prev_ver,
        "gate_status": overall,
        "sample_sufficient": True,
        "rule_results": rules,
        "alert": alert_payload,
        "evaluated_at": now_iso,
        "comparison_window": comp_win,
    }


def evaluate_app_release_gate(
    app_id: str,
    catalog_items: list[dict[str, Any]],
    policy: GatePolicy,
    target_platforms: list[str] | None = None,
) -> ReleaseGateArtifact:
    """Evaluates release gate across target platforms for an application."""
    now_iso = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Handle disabled policy semantics
    if not policy.enabled:
        return {
            "schema_version": "1.0",
            "app_id": app_id,
            "generated_at": now_iso,
            "overall_status": "pass",
            "should_alert": False,
            "alert_severity": "none",
            "alert_summary": f"App [{app_id}] 品質閘門未啟用 (enabled: false)",
            "platforms": {},
            "policy_version": policy.policy_version,
            "policy": policy.to_dict(),
        }

    if target_platforms:
        pfs = [p.lower() for p in target_platforms if p.lower() in ("ios", "android")]
    else:
        found_pfs = {str(item.get("platform", "")).lower() for item in catalog_items if item.get("platform")}
        pfs = [p for p in ("android", "ios") if p in found_pfs]
        if not pfs:
            pfs = ["android", "ios"]

    platform_results: dict[str, PlatformGateResult] = {}

    for pf in pfs:
        pf_items = [item for item in catalog_items if item.get("platform") == pf]
        if not pf_items:
            continue

        # Target latest release for evaluation
        latest_item = next((item for item in pf_items if item.get("status") == "latest"), None)
        if not latest_item and pf_items:
            latest_item = pf_items[0]

        if latest_item:
            res = evaluate_release(latest_item, policy)
            platform_results[pf] = res

    # Aggregate overall application gate status
    # Priority: fail > warn > insufficient_data > pass > baseline
    statuses = [res["gate_status"] for res in platform_results.values()]
    if "fail" in statuses:
        overall_status: GateStatus = "fail"
        overall_severity: AlertSeverity = "critical"
        overall_should_alert = True
    elif "warn" in statuses:
        overall_status = "warn"
        overall_severity = "warning"
        overall_should_alert = True
    elif "insufficient_data" in statuses:
        overall_status = "insufficient_data"
        overall_severity = "none"
        overall_should_alert = False
    elif "pass" in statuses:
        overall_status = "pass"
        overall_severity = "none"
        overall_should_alert = False
    elif "baseline" in statuses:
        overall_status = "baseline"
        overall_severity = "none"
        overall_should_alert = False
    else:
        overall_status = "insufficient_data"
        overall_severity = "none"
        overall_should_alert = False

    alert_summaries = [res["alert"]["alert_summary"] for res in platform_results.values() if res["alert"]["should_alert"]]
    if alert_summaries:
        overall_summary = f"App [{app_id}] 品質閘門警示： " + "； ".join(alert_summaries)
    else:
        overall_summary = f"App [{app_id}] 品質閘門狀態：{overall_status.upper()}"

    return {
        "schema_version": "1.0",
        "app_id": app_id,
        "generated_at": now_iso,
        "overall_status": overall_status,
        "should_alert": overall_should_alert,
        "alert_severity": overall_severity,
        "alert_summary": overall_summary,
        "platforms": platform_results,
        "policy_version": policy.policy_version,
        "policy": policy.to_dict(),
    }
