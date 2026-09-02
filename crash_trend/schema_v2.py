"""Dashboard V2 Data Schema TypedDicts and Runtime Validation Utilities.

This module defines the Python data contract for Dashboard V2, providing:
- Required-by-default TypedDict definitions with explicit NotRequired and Optional annotations
- Strict runtime validation for timestamps (ISO 8601 UTC), dates, numerical ranges, enums,
  and cross-field consistency
- Safe error handling that returns structured error messages without crashing on malformed input
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, List, Literal, NotRequired, Optional, TypedDict, Union

SCHEMA_VERSION = "2.0"

# ---------------------------------------------------------------------------
# TypedDict Definitions (Required by default)
# ---------------------------------------------------------------------------

class AppMetadata(TypedDict):
    app_id: str
    display_name: str
    firebase_project_id: str
    platforms: List[Literal["ios", "android"]]
    source_repo: Optional[str]
    custom_keys_monitored: List[str]


class PeriodComparison(TypedDict):
    days: int
    start_time: str
    end_time: str


class PeriodInfo(TypedDict):
    days: int
    start_time: str
    end_time: str
    comparison_period: Optional[PeriodComparison]


class SourceStatus(TypedDict):
    status: Literal["available", "unavailable", "disabled", "error"]
    tables_queried: NotRequired[Optional[List[str]]]
    model: NotRequired[Optional[str]]
    last_sync_timestamp: Optional[str]
    error_message: Optional[str]


class SourcesAvailability(TypedDict):
    crashlytics_bq: SourceStatus
    firebase_sessions: SourceStatus
    mcp_crashlytics: SourceStatus
    gemini_ai: SourceStatus
    manual_console: NotRequired[Optional[SourceStatus]]


class KPIMetric(TypedDict):
    value: int
    previous_value: Optional[int]
    change_pct: Optional[float]
    status: Literal["available", "insufficient_data", "error"]


class CrashFreeMetric(TypedDict):
    rate: Optional[float]  # 0.0 to 1.0 (e.g. 0.9985 for 99.85%)
    total: Optional[int]
    crashed: Optional[int]
    previous_rate: Optional[float]
    change_pct_points: Optional[float]
    status: Literal["available", "unavailable", "insufficient_data", "error"]
    unavailable_reason: Optional[str]


class EventsByErrorType(TypedDict):
    fatal: int
    anr: int
    non_fatal: int


class OverviewKPI(TypedDict):
    crash_events: KPIMetric
    affected_users: KPIMetric
    crash_free_users: CrashFreeMetric
    crash_free_sessions: CrashFreeMetric
    new_issues_count: KPIMetric
    events_by_error_type: EventsByErrorType


class DailyTrendPoint(TypedDict):
    date: str  # YYYY-MM-DD
    crash_events: int
    affected_users: int
    fatal_events: int
    anr_events: int
    non_fatal_events: int
    sessions_total: Optional[int]
    crashed_sessions: Optional[int]
    crash_free_sessions_rate: Optional[float]
    by_platform: Optional[Dict[str, Dict[str, int]]]


class VersionHealthItem(TypedDict):
    version: str
    platform: Literal["ios", "android", "all"]
    release_date: Optional[str]
    crash_events: int
    affected_users: int
    crash_free_users_rate: Optional[float]
    crash_free_sessions_rate: Optional[float]
    adoption_rate: Optional[float]
    status: Literal["latest", "active", "maintenance", "deprecated"]
    trend: Literal["improving", "degrading", "stable", "new"]


class PlatformDistItem(TypedDict):
    name: Literal["ios", "android"]
    events: int
    users: int
    share: float


class DeviceDistItem(TypedDict):
    model: str
    platform: Literal["ios", "android", "all"]
    events: int
    users: int
    share: float


class OSDistItem(TypedDict):
    os_version: str
    platform: Literal["ios", "android", "all"]
    events: int
    users: int
    share: float


class AppVersionDistItem(TypedDict):
    app_version: str
    platform: Literal["ios", "android", "all"]
    events: int
    users: int
    share: float


class CustomKeyDistributionItem(TypedDict):
    key: str
    value: str
    platform: str
    events: int


class Distributions(TypedDict):
    platform: List[PlatformDistItem]
    device_models: List[DeviceDistItem]
    os_versions: List[OSDistItem]
    app_versions: List[AppVersionDistItem]
    custom_keys: NotRequired[List[CustomKeyDistributionItem]]


class PriorityBreakdown(TypedDict):
    users_normalized: float
    events_normalized: float
    fatal_anr_boost: int
    worsening_boost: int
    latest_version_boost: int
    core_path_boost: int


class PriorityInfo(TypedDict):
    score: int
    level: Literal["P0", "P1", "P2", "P3"]
    trend: Literal["new", "worsening", "stable", "improving"]
    score_breakdown: Optional[PriorityBreakdown]


class BlameFrame(TypedDict):
    file: Optional[str]
    line: Optional[int]
    symbol: Optional[str]
    class_name: Optional[str]
    method_name: Optional[str]
    is_blame: bool
    source_available: bool


class AIIssueAnalysis(TypedDict):
    status: Literal["available", "unavailable", "pending", "skipped"]
    root_cause: Optional[str]
    suggested_fix: Optional[str]
    effort: Optional[Literal["S", "M", "L"]]
    confidence: Optional[Literal["high", "medium", "low", "needs_manual_review"]]
    reasoning_sources: Optional[List[str]]


class BreadcrumbItem(TypedDict):
    timestamp: str
    category: str
    message: str
    level: str
    data: NotRequired[Optional[Dict[str, Any]]]


class LogItem(TypedDict):
    timestamp: str
    message: str


class TopDeviceCount(TypedDict):
    model: str
    events: int


class TopOSCount(TypedDict):
    os_version: str
    events: int


class IssueDetail(TypedDict):
    stack_trace: Optional[str]
    breadcrumbs: Optional[List[BreadcrumbItem]]
    logs: Optional[List[LogItem]]
    custom_keys: Optional[Dict[str, Any]]
    top_devices: Optional[List[TopDeviceCount]]
    top_os: Optional[List[TopOSCount]]


class VersionDistCount(TypedDict):
    version: str
    events: int
    users: int


class IssueSummary(TypedDict):
    issue_id: str
    platform: Literal["ios", "android"]
    title: str
    subtitle: str
    error_type: Literal["FATAL", "ANR", "NON_FATAL"]
    priority: PriorityInfo
    events: int
    affected_users: int
    first_seen_timestamp: str
    last_seen_timestamp: str
    first_seen_version: str
    last_seen_version: str
    version_distribution: List[VersionDistCount]
    blame_frame: Optional[BlameFrame]
    ai_analysis: AIIssueAnalysis
    detail: Optional[IssueDetail]


class RecommendedAction(TypedDict):
    priority: Literal["P0", "P1", "P2", "P3"]
    issue_id: str
    action: str
    effort: Literal["S", "M", "L"]


class AISummary(TypedDict):
    status: Literal["available", "unavailable", "disabled", "error"]
    model: Optional[str]
    generated_at: Optional[str]
    overview: str
    key_takeaways: List[str]
    distribution_insights: str
    recommended_actions: List[RecommendedAction]
    data_limitations: Optional[str]


class AppDashboardV2Data(TypedDict):
    metadata: AppMetadata
    period: PeriodInfo
    sources: SourcesAvailability
    kpi: OverviewKPI
    daily_trend: List[DailyTrendPoint]
    version_health: List[VersionHealthItem]
    distributions: Distributions
    top_issues: List[IssueSummary]
    ai_summary: AISummary
    limitations: List[str]


class DashboardV2Bundle(TypedDict):
    schema_version: str
    generated_at: str
    default_app: str
    apps: Dict[str, AppDashboardV2Data]


# ---------------------------------------------------------------------------
# Runtime Validation Utilities
# ---------------------------------------------------------------------------

def is_valid_iso8601_utc(val: Any) -> bool:
    """Strict ISO 8601 UTC validation.
    Must be a valid string parseable by datetime, with UTC timezone (ends with Z or +00:00).
    """
    if not isinstance(val, str) or not val.strip():
        return False
    
    # Must end with Z or +00:00
    if not (val.endswith("Z") or val.endswith("+00:00")):
        return False
    
    # Parse with datetime to enforce calendar validity (e.g. leap years, month ranges)
    norm = val[:-1] + "+00:00" if val.endswith("Z") else val
    try:
        parsed = dt.datetime.fromisoformat(norm)
        return parsed.tzinfo is not None
    except (ValueError, TypeError):
        return False


def is_valid_date(val: Any) -> bool:
    """Validates YYYY-MM-DD format with real calendar date checking."""
    if not isinstance(val, str) or len(val) != 10:
        return False
    try:
        dt.datetime.strptime(val, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def _check_type(val: Any, expected: type, name: str, errors: List[str], nullable: bool = False) -> bool:
    if val is None:
        if not nullable:
            errors.append(f"{name} cannot be null")
            return False
        return True
    if not isinstance(val, expected):
        errors.append(f"{name} must be {expected.__name__}, got {type(val).__name__}")
        return False
    return True


def validate_kpi_metric(metric: Any, name: str, errors: List[str]) -> None:
    if not isinstance(metric, dict):
        errors.append(f"{name} must be an object")
        return
    for k in ("value", "status"):
        if k not in metric:
            errors.append(f"{name}.{k} is required")
    if not isinstance(metric.get("value"), int) or metric["value"] < 0:
        errors.append(f"{name}.value must be a non-negative integer")
    status = metric.get("status")
    if status not in {"available", "insufficient_data", "error"}:
        errors.append(f"{name}.status must be one of available, insufficient_data, error")
    if "previous_value" in metric and metric["previous_value"] is not None:
        if not isinstance(metric["previous_value"], int) or metric["previous_value"] < 0:
            errors.append(f"{name}.previous_value must be a non-negative integer or null")
    if "change_pct" in metric and metric["change_pct"] is not None:
        if not isinstance(metric["change_pct"], (int, float)):
            errors.append(f"{name}.change_pct must be a number or null")


def validate_crash_free_metric(metric: Any, name: str, errors: List[str]) -> None:
    if not isinstance(metric, dict):
        errors.append(f"{name} must be an object")
        return
    for k in ("rate", "total", "crashed", "status"):
        if k not in metric:
            errors.append(f"{name}.{k} is required")
    
    status = metric.get("status")
    if status not in {"available", "unavailable", "insufficient_data", "error"}:
        errors.append(f"{name}.status must be one of available, unavailable, insufficient_data, error")
    
    rate = metric.get("rate")
    if status == "available":
        if not isinstance(rate, (int, float)) or rate < 0.0 or rate > 1.0:
            errors.append(f"{name}.rate must be a float between 0.0 and 1.0 when status is 'available'")
        if not isinstance(metric.get("total"), int) or metric["total"] < 0:
            errors.append(f"{name}.total must be a non-negative integer when status is 'available'")
        if not isinstance(metric.get("crashed"), int) or metric["crashed"] < 0:
            errors.append(f"{name}.crashed must be a non-negative integer when status is 'available'")
    elif status == "unavailable":
        if rate is not None:
            errors.append(f"{name}.rate must be null when status is 'unavailable'")
        if metric.get("total") is not None:
            errors.append(f"{name}.total must be null when status is 'unavailable'")
        if metric.get("crashed") is not None:
            errors.append(f"{name}.crashed must be null when status is 'unavailable'")


def validate_app_dashboard_v2(data: dict, prefix: str = "") -> List[str]:
    """Validates an AppDashboardV2Data dictionary strictly against Schema V2 rules."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return [f"{prefix}Expected object, got {type(data).__name__}"]

    p = f"{prefix}." if prefix else ""

    # Required top-level keys in AppDashboardV2Data
    req_top_keys = (
        "metadata", "period", "sources", "kpi", "daily_trend",
        "version_health", "distributions", "top_issues", "ai_summary", "limitations"
    )
    for k in req_top_keys:
        if k not in data:
            errors.append(f"{p}{k} is required")

    # 1. Metadata
    meta = data.get("metadata")
    if isinstance(meta, dict):
        for field in ("app_id", "display_name", "firebase_project_id", "platforms", "source_repo", "custom_keys_monitored"):
            if field not in meta:
                errors.append(f"{p}metadata.{field} is required")
        if not isinstance(meta.get("platforms"), list) or not meta.get("platforms"):
            errors.append(f"{p}metadata.platforms must be a non-empty list")
        else:
            for pf in meta.get("platforms", []):
                if pf not in {"ios", "android"}:
                    errors.append(f"{p}metadata.platforms item '{pf}' must be 'ios' or 'android'")
        if not isinstance(meta.get("custom_keys_monitored"), list):
            errors.append(f"{p}metadata.custom_keys_monitored must be a list")
    elif meta is not None:
        errors.append(f"{p}metadata must be an object")

    # 2. Period
    period = data.get("period")
    if isinstance(period, dict):
        for field in ("days", "start_time", "end_time", "comparison_period"):
            if field not in period:
                errors.append(f"{p}period.{field} is required")
        if not isinstance(period.get("days"), int) or period["days"] <= 0:
            errors.append(f"{p}period.days must be a positive integer")
        if not is_valid_iso8601_utc(period.get("start_time")):
            errors.append(f"{p}period.start_time must be a valid ISO 8601 UTC timestamp (ending in Z)")
        if not is_valid_iso8601_utc(period.get("end_time")):
            errors.append(f"{p}period.end_time must be a valid ISO 8601 UTC timestamp (ending in Z)")
        comp = period.get("comparison_period")
        if comp is not None:
            if not isinstance(comp, dict):
                errors.append(f"{p}period.comparison_period must be an object or null")
            else:
                if not isinstance(comp.get("days"), int) or comp["days"] <= 0:
                    errors.append(f"{p}period.comparison_period.days must be a positive integer")
                if not is_valid_iso8601_utc(comp.get("start_time")):
                    errors.append(f"{p}period.comparison_period.start_time must be valid ISO 8601 UTC")
                if not is_valid_iso8601_utc(comp.get("end_time")):
                    errors.append(f"{p}period.comparison_period.end_time must be valid ISO 8601 UTC")
    elif period is not None:
        errors.append(f"{p}period must be an object")

    # 3. Sources
    sources = data.get("sources")
    if isinstance(sources, dict):
        valid_src_statuses = {"available", "unavailable", "disabled", "error"}
        for s_name in ("crashlytics_bq", "firebase_sessions", "mcp_crashlytics", "gemini_ai"):
            s_obj = sources.get(s_name)
            if not isinstance(s_obj, dict):
                errors.append(f"{p}sources.{s_name} is required and must be an object")
            else:
                for req_f in ("status", "last_sync_timestamp", "error_message"):
                    if req_f not in s_obj:
                        errors.append(f"{p}sources.{s_name}.{req_f} is required")
                if s_obj.get("status") not in valid_src_statuses:
                    errors.append(f"{p}sources.{s_name}.status must be one of {valid_src_statuses}")
                sync_ts = s_obj.get("last_sync_timestamp")
                if sync_ts is not None and not is_valid_iso8601_utc(sync_ts):
                    errors.append(f"{p}sources.{s_name}.last_sync_timestamp must be ISO 8601 UTC or null")
    elif sources is not None:
        errors.append(f"{p}sources must be an object")

    # 4. KPI
    kpi = data.get("kpi")
    if isinstance(kpi, dict):
        validate_kpi_metric(kpi.get("crash_events"), f"{p}kpi.crash_events", errors)
        validate_kpi_metric(kpi.get("affected_users"), f"{p}kpi.affected_users", errors)
        validate_kpi_metric(kpi.get("new_issues_count"), f"{p}kpi.new_issues_count", errors)
        validate_crash_free_metric(kpi.get("crash_free_users"), f"{p}kpi.crash_free_users", errors)
        validate_crash_free_metric(kpi.get("crash_free_sessions"), f"{p}kpi.crash_free_sessions", errors)

        by_err = kpi.get("events_by_error_type")
        if not isinstance(by_err, dict):
            errors.append(f"{p}kpi.events_by_error_type is required and must be an object")
        else:
            for err_k in ("fatal", "anr", "non_fatal"):
                if not isinstance(by_err.get(err_k), int) or by_err[err_k] < 0:
                    errors.append(f"{p}kpi.events_by_error_type.{err_k} must be a non-negative integer")
    elif kpi is not None:
        errors.append(f"{p}kpi must be an object")

    # 5. Daily Trend
    daily = data.get("daily_trend")
    if isinstance(daily, list):
        for idx, item in enumerate(daily):
            if not isinstance(item, dict):
                errors.append(f"{p}daily_trend[{idx}] must be an object")
                continue
            for req_k in ("date", "crash_events", "affected_users", "fatal_events", "anr_events", "non_fatal_events", "sessions_total", "crashed_sessions", "crash_free_sessions_rate", "by_platform"):
                if req_k not in item:
                    errors.append(f"{p}daily_trend[{idx}].{req_k} is required")
            if not is_valid_date(item.get("date")):
                errors.append(f"{p}daily_trend[{idx}].date must be valid YYYY-MM-DD")
            for int_f in ("crash_events", "affected_users", "fatal_events", "anr_events", "non_fatal_events"):
                if not isinstance(item.get(int_f), int) or item[int_f] < 0:
                    errors.append(f"{p}daily_trend[{idx}].{int_f} must be a non-negative integer")
            # Cross-field error type check per daily point
            if isinstance(item.get("crash_events"), int) and isinstance(item.get("fatal_events"), int) and isinstance(item.get("anr_events"), int) and isinstance(item.get("non_fatal_events"), int):
                if item["fatal_events"] + item["anr_events"] + item["non_fatal_events"] != item["crash_events"]:
                    errors.append(f"{p}daily_trend[{idx}] sum of (fatal + anr + non_fatal) must equal crash_events")
    elif daily is not None:
        errors.append(f"{p}daily_trend must be a list")

    # 6. Version Health
    vh = data.get("version_health")
    if isinstance(vh, list):
        valid_vh_statuses = {"latest", "active", "maintenance", "deprecated"}
        valid_vh_trends = {"improving", "degrading", "stable", "new"}
        for idx, item in enumerate(vh):
            if not isinstance(item, dict):
                errors.append(f"{p}version_health[{idx}] must be an object")
                continue
            for req_k in ("version", "platform", "release_date", "crash_events", "affected_users", "crash_free_users_rate", "crash_free_sessions_rate", "adoption_rate", "status", "trend"):
                if req_k not in item:
                    errors.append(f"{p}version_health[{idx}].{req_k} is required")
            if not item.get("version"):
                errors.append(f"{p}version_health[{idx}].version must be a non-empty string")
            if item.get("platform") not in {"ios", "android", "all"}:
                errors.append(f"{p}version_health[{idx}].platform must be ios, android, or all")
            if item.get("release_date") is not None and not is_valid_date(item["release_date"]):
                errors.append(f"{p}version_health[{idx}].release_date must be YYYY-MM-DD or null")
            if item.get("status") not in valid_vh_statuses:
                errors.append(f"{p}version_health[{idx}].status must be one of {valid_vh_statuses}")
            if item.get("trend") not in valid_vh_trends:
                errors.append(f"{p}version_health[{idx}].trend must be one of {valid_vh_trends}")
            for rate_f in ("crash_free_users_rate", "crash_free_sessions_rate", "adoption_rate"):
                rf_val = item.get(rate_f)
                if rf_val is not None and (not isinstance(rf_val, (int, float)) or rf_val < 0.0 or rf_val > 1.0):
                    errors.append(f"{p}version_health[{idx}].{rate_f} must be a float between 0.0 and 1.0 or null")
    elif vh is not None:
        errors.append(f"{p}version_health must be a list")

    # 7. Distributions
    dists = data.get("distributions")
    if isinstance(dists, dict):
        for dk in ("platform", "device_models", "os_versions", "app_versions"):
            d_list = dists.get(dk)
            if not isinstance(d_list, list):
                errors.append(f"{p}distributions.{dk} is required and must be a list")
            else:
                for idx, item in enumerate(d_list):
                    if not isinstance(item, dict):
                        errors.append(f"{p}distributions.{dk}[{idx}] must be an object")
                        continue
                    if not isinstance(item.get("events"), int) or item["events"] < 0:
                        errors.append(f"{p}distributions.{dk}[{idx}].events must be a non-negative integer")
                    if not isinstance(item.get("users"), int) or item["users"] < 0:
                        errors.append(f"{p}distributions.{dk}[{idx}].users must be a non-negative integer")
                    if not isinstance(item.get("share"), (int, float)) or item["share"] < 0.0 or item["share"] > 1.0:
                        errors.append(f"{p}distributions.{dk}[{idx}].share must be a float between 0.0 and 1.0")
    elif dists is not None:
        errors.append(f"{p}distributions must be an object")

    # 8. Top Issues
    issues = data.get("top_issues")
    if isinstance(issues, list):
        for idx, issue in enumerate(issues):
            if not isinstance(issue, dict):
                errors.append(f"{p}top_issues[{idx}] must be an object")
                continue
            for req_k in (
                "issue_id", "platform", "title", "subtitle", "error_type", "priority",
                "events", "affected_users", "first_seen_timestamp", "last_seen_timestamp",
                "first_seen_version", "last_seen_version", "version_distribution",
                "blame_frame", "ai_analysis", "detail"
            ):
                if req_k not in issue:
                    errors.append(f"{p}top_issues[{idx}].{req_k} is required")
            if issue.get("platform") not in {"ios", "android"}:
                errors.append(f"{p}top_issues[{idx}].platform must be 'ios' or 'android'")
            if issue.get("error_type") not in {"FATAL", "ANR", "NON_FATAL"}:
                errors.append(f"{p}top_issues[{idx}].error_type must be FATAL, ANR, or NON_FATAL")
            if not is_valid_iso8601_utc(issue.get("first_seen_timestamp")):
                errors.append(f"{p}top_issues[{idx}].first_seen_timestamp must be ISO 8601 UTC timestamp")
            if not is_valid_iso8601_utc(issue.get("last_seen_timestamp")):
                errors.append(f"{p}top_issues[{idx}].last_seen_timestamp must be ISO 8601 UTC timestamp")

            prio = issue.get("priority")
            if isinstance(prio, dict):
                for pk in ("score", "level", "trend", "score_breakdown"):
                    if pk not in prio:
                        errors.append(f"{p}top_issues[{idx}].priority.{pk} is required")
                if prio.get("level") not in {"P0", "P1", "P2", "P3"}:
                    errors.append(f"{p}top_issues[{idx}].priority.level must be P0, P1, P2, or P3")
                if prio.get("trend") not in {"new", "worsening", "stable", "improving"}:
                    errors.append(f"{p}top_issues[{idx}].priority.trend must be new, worsening, stable, or improving")
            else:
                errors.append(f"{p}top_issues[{idx}].priority must be an object")

            # Blame Frame
            bf = issue.get("blame_frame")
            if bf is not None:
                if not isinstance(bf, dict):
                    errors.append(f"{p}top_issues[{idx}].blame_frame must be an object or null")
                else:
                    for bfk in ("file", "line", "symbol", "class_name", "method_name", "is_blame", "source_available"):
                        if bfk not in bf:
                            errors.append(f"{p}top_issues[{idx}].blame_frame.{bfk} is required")

            # AI Issue Analysis
            ai_ia = issue.get("ai_analysis")
            if isinstance(ai_ia, dict):
                for ak in ("status", "root_cause", "suggested_fix", "effort", "confidence", "reasoning_sources"):
                    if ak not in ai_ia:
                        errors.append(f"{p}top_issues[{idx}].ai_analysis.{ak} is required")
                if ai_ia.get("status") not in {"available", "unavailable", "pending", "skipped"}:
                    errors.append(f"{p}top_issues[{idx}].ai_analysis.status must be available, unavailable, pending, or skipped")
                eff = ai_ia.get("effort")
                if eff is not None and eff not in {"S", "M", "L"}:
                    errors.append(f"{p}top_issues[{idx}].ai_analysis.effort must be S, M, L, or null")
            else:
                errors.append(f"{p}top_issues[{idx}].ai_analysis must be an object")

            # Detail
            det = issue.get("detail")
            if det is not None:
                if not isinstance(det, dict):
                    errors.append(f"{p}top_issues[{idx}].detail must be an object or null")
                else:
                    for det_k in ("stack_trace", "breadcrumbs", "logs", "custom_keys", "top_devices", "top_os"):
                        if det_k not in det:
                            errors.append(f"{p}top_issues[{idx}].detail.{det_k} is required")
    elif issues is not None:
        errors.append(f"{p}top_issues must be a list")

    # 9. AI Summary
    ai = data.get("ai_summary")
    if isinstance(ai, dict):
        for req_k in ("status", "model", "generated_at", "overview", "key_takeaways", "distribution_insights", "recommended_actions", "data_limitations"):
            if req_k not in ai:
                errors.append(f"{p}ai_summary.{req_k} is required")
        if ai.get("status") not in {"available", "unavailable", "disabled", "error"}:
            errors.append(f"{p}ai_summary.status must be one of available, unavailable, disabled, error")
        gen_ts = ai.get("generated_at")
        if gen_ts is not None and not is_valid_iso8601_utc(gen_ts):
            errors.append(f"{p}ai_summary.generated_at must be ISO 8601 UTC timestamp or null")
        if not isinstance(ai.get("overview"), str):
            errors.append(f"{p}ai_summary.overview must be a string")
        if not isinstance(ai.get("key_takeaways"), list):
            errors.append(f"{p}ai_summary.key_takeaways must be a list of strings")
        rec_actions = ai.get("recommended_actions")
        if isinstance(rec_actions, list):
            for idx, act in enumerate(rec_actions):
                if not isinstance(act, dict):
                    errors.append(f"{p}ai_summary.recommended_actions[{idx}] must be an object")
                    continue
                if act.get("priority") not in {"P0", "P1", "P2", "P3"}:
                    errors.append(f"{p}ai_summary.recommended_actions[{idx}].priority must be P0, P1, P2, or P3")
                if act.get("effort") not in {"S", "M", "L"}:
                    errors.append(f"{p}ai_summary.recommended_actions[{idx}].effort must be S, M, or L")
        elif rec_actions is not None:
            errors.append(f"{p}ai_summary.recommended_actions must be a list")
    elif ai is not None:
        errors.append(f"{p}ai_summary must be an object")

    # 10. Limitations
    if not isinstance(data.get("limitations"), list):
        errors.append(f"{p}limitations must be a list of strings")

    return errors


def validate_dashboard_v2(data: dict) -> List[str]:
    """Validates a full DashboardV2Bundle against Schema V2 rules."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return [f"Expected object root, got {type(data).__name__}"]

    for req_k in ("schema_version", "generated_at", "default_app", "apps"):
        if req_k not in data:
            errors.append(f"Root {req_k} is required")

    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"Root schema_version must be '{SCHEMA_VERSION}', got {data.get('schema_version')}")

    if not is_valid_iso8601_utc(data.get("generated_at")):
        errors.append("Root generated_at must be a valid ISO 8601 UTC timestamp string (ending in Z)")

    default_app = data.get("default_app")
    if not isinstance(default_app, str) or not default_app:
        errors.append("Root default_app must be a non-empty string")

    apps = data.get("apps")
    if not isinstance(apps, dict) or not apps:
        errors.append("Root apps must be a non-empty dictionary of app data")
    else:
        if default_app and default_app not in apps:
            errors.append(f"Root default_app '{default_app}' is not present in apps dict")
        for app_name, app_data in apps.items():
            errors.extend(validate_app_dashboard_v2(app_data, prefix=f"apps['{app_name}']"))

    return errors
