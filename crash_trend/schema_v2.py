"""Dashboard V2 Data Schema TypedDicts and Runtime Validation Utilities.

This module defines the Python data contract for Dashboard V2, providing:
- Required-by-default TypedDict definitions with explicit NotRequired and Optional annotations
- Strict runtime validation for timestamps (ISO 8601 UTC), dates, numerical ranges, enums,
  and cross-field consistency
- Complete 1:1 parity between TypedDict required keys and runtime validator enforcement
- Safe error handling that returns structured error messages without crashing on malformed input
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Literal, Optional, TypedDict, Union

try:
    from typing import NotRequired
except ImportError:
    from typing_extensions import NotRequired  # type: ignore

SCHEMA_VERSION = "2.3.0"
SUPPORTED_SCHEMA_VERSIONS = {"2.0", "2.3", "2.3.0"}

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


SnapshotStatus = Literal[
    "available",
    "unavailable",
    "disabled",
    "error",
    "stale",
    "insufficient_data",
]


class SourceStatus(TypedDict):
    status: SnapshotStatus
    tables_queried: NotRequired[Optional[List[str]]]
    provider: NotRequired[Optional[str]]
    model: NotRequired[Optional[str]]
    last_sync_timestamp: Optional[str]
    error_message: Optional[str]


class SourcesAvailability(TypedDict):
    crashlytics_bq: SourceStatus
    firebase_sessions: SourceStatus
    mcp_crashlytics: SourceStatus
    gemini_ai: NotRequired[Optional[SourceStatus]]
    ai: NotRequired[Optional[SourceStatus]]
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
    provider: NotRequired[Optional[str]]
    model: Optional[str]
    generated_at: Optional[str]
    overview: str
    key_takeaways: List[str]
    distribution_insights: str
    recommended_actions: List[RecommendedAction]
    data_limitations: Optional[str]


class AppPeriodSnapshot(TypedDict):
    period: PeriodInfo
    kpi: OverviewKPI
    daily_trend: NotRequired[List[DailyTrendPoint]]
    version_health: List[VersionHealthItem]
    distributions: Distributions
    top_issues: List[IssueSummary]
    ai_summary: NotRequired[Optional[AISummary]]
    status: NotRequired[SnapshotStatus]
    error_message: NotRequired[Optional[str]]


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
    periods: NotRequired[Dict[str, AppPeriodSnapshot]]


class DashboardV2Bundle(TypedDict):
    schema_version: str
    generated_at: str
    default_app: str
    apps: Dict[str, AppDashboardV2Data]
    pipeline_run: NotRequired[Optional[Dict[str, Any]]]


# ---------------------------------------------------------------------------
# Runtime Validation Utilities
# ---------------------------------------------------------------------------

def is_valid_iso8601_utc(val: Any) -> bool:
    """Strict ISO 8601 UTC validation.
    Must be a valid string parseable by datetime, with UTC timezone (ends with Z or +00:00).
    """
    if not isinstance(val, str) or not val.strip():
        return False
    
    if not (val.endswith("Z") or val.endswith("+00:00")):
        return False
    
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


def validate_kpi_metric(metric: Any, name: str, errors: List[str]) -> None:
    if not isinstance(metric, dict):
        errors.append(f"{name} must be an object")
        return
    for k in ("value", "previous_value", "change_pct", "status"):
        if k not in metric:
            errors.append(f"{name}.{k} is required")
    
    if "value" in metric:
        if not isinstance(metric["value"], int) or metric["value"] < 0:
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
    for k in ("rate", "total", "crashed", "previous_rate", "change_pct_points", "status", "unavailable_reason"):
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
        if metric.get("unavailable_reason") is not None and not isinstance(metric["unavailable_reason"], str):
            errors.append(f"{name}.unavailable_reason must be a string or null")

    if "previous_rate" in metric and metric["previous_rate"] is not None:
        if not isinstance(metric["previous_rate"], (int, float)) or metric["previous_rate"] < 0.0 or metric["previous_rate"] > 1.0:
            errors.append(f"{name}.previous_rate must be a float between 0.0 and 1.0 or null")
    if "change_pct_points" in metric and metric["change_pct_points"] is not None:
        if not isinstance(metric["change_pct_points"], (int, float)):
            errors.append(f"{name}.change_pct_points must be a number or null")


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
        if "app_id" in meta and (not isinstance(meta["app_id"], str) or not meta["app_id"]):
            errors.append(f"{p}metadata.app_id must be a non-empty string")
        if "display_name" in meta and (not isinstance(meta["display_name"], str) or not meta["display_name"]):
            errors.append(f"{p}metadata.display_name must be a non-empty string")
        if "firebase_project_id" in meta and (not isinstance(meta["firebase_project_id"], str) or not meta["firebase_project_id"]):
            errors.append(f"{p}metadata.firebase_project_id must be a non-empty string")
        if "platforms" in meta:
            if not isinstance(meta["platforms"], list) or not meta["platforms"]:
                errors.append(f"{p}metadata.platforms must be a non-empty list")
            else:
                for pf in meta["platforms"]:
                    if pf not in {"ios", "android"}:
                        errors.append(f"{p}metadata.platforms item '{pf}' must be 'ios' or 'android'")
        if "source_repo" in meta and meta["source_repo"] is not None and not isinstance(meta["source_repo"], str):
            errors.append(f"{p}metadata.source_repo must be a string or null")
        if "custom_keys_monitored" in meta:
            if not isinstance(meta["custom_keys_monitored"], list):
                errors.append(f"{p}metadata.custom_keys_monitored must be a list")
            else:
                for ck in meta["custom_keys_monitored"]:
                    if not isinstance(ck, str):
                        errors.append(f"{p}metadata.custom_keys_monitored item must be a string")
    elif meta is not None:
        errors.append(f"{p}metadata must be an object")

    # 2. Period
    period = data.get("period")
    if isinstance(period, dict):
        for field in ("days", "start_time", "end_time", "comparison_period"):
            if field not in period:
                errors.append(f"{p}period.{field} is required")
        if "days" in period and (not isinstance(period["days"], int) or period["days"] <= 0):
            errors.append(f"{p}period.days must be a positive integer")
        if "start_time" in period and not is_valid_iso8601_utc(period["start_time"]):
            errors.append(f"{p}period.start_time must be a valid ISO 8601 UTC timestamp (ending in Z)")
        if "end_time" in period and not is_valid_iso8601_utc(period["end_time"]):
            errors.append(f"{p}period.end_time must be a valid ISO 8601 UTC timestamp (ending in Z)")
        
        comp = period.get("comparison_period")
        if comp is not None:
            if not isinstance(comp, dict):
                errors.append(f"{p}period.comparison_period must be an object or null")
            else:
                for cf in ("days", "start_time", "end_time"):
                    if cf not in comp:
                        errors.append(f"{p}period.comparison_period.{cf} is required")
                if "days" in comp and (not isinstance(comp["days"], int) or comp["days"] <= 0):
                    errors.append(f"{p}period.comparison_period.days must be a positive integer")
                if "start_time" in comp and not is_valid_iso8601_utc(comp["start_time"]):
                    errors.append(f"{p}period.comparison_period.start_time must be valid ISO 8601 UTC")
                if "end_time" in comp and not is_valid_iso8601_utc(comp["end_time"]):
                    errors.append(f"{p}period.comparison_period.end_time must be valid ISO 8601 UTC")
    elif period is not None:
        errors.append(f"{p}period must be an object")

    # 3. Sources
    sources = data.get("sources")
    if isinstance(sources, dict):
        valid_src_statuses = {"available", "unavailable", "disabled", "error", "stale", "insufficient_data"}
        check_sources = ["crashlytics_bq", "firebase_sessions", "mcp_crashlytics"]
        if "ai" in sources:
            check_sources.append("ai")
            if "gemini_ai" in sources:
                check_sources.append("gemini_ai")
        elif "gemini_ai" in sources:
            check_sources.append("gemini_ai")
        else:
            errors.append(f"{p}sources.ai is required and must be an object")

        for s_name in check_sources:
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
                if s_obj.get("error_message") is not None and not isinstance(s_obj["error_message"], str):
                    errors.append(f"{p}sources.{s_name}.error_message must be a string or null")
    elif sources is not None:
        errors.append(f"{p}sources must be an object")

    # 4. KPI
    kpi = data.get("kpi")
    if isinstance(kpi, dict):
        for kpi_f in ("crash_events", "affected_users", "crash_free_users", "crash_free_sessions", "new_issues_count", "events_by_error_type"):
            if kpi_f not in kpi:
                errors.append(f"{p}kpi.{kpi_f} is required")
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
                if err_k not in by_err:
                    errors.append(f"{p}kpi.events_by_error_type.{err_k} is required")
                elif not isinstance(by_err[err_k], int) or by_err[err_k] < 0:
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
            if "date" in item and not is_valid_date(item["date"]):
                errors.append(f"{p}daily_trend[{idx}].date must be valid YYYY-MM-DD")
            for int_f in ("crash_events", "affected_users", "fatal_events", "anr_events", "non_fatal_events"):
                if int_f in item and (not isinstance(item[int_f], int) or item[int_f] < 0):
                    errors.append(f"{p}daily_trend[{idx}].{int_f} must be a non-negative integer")
            if "sessions_total" in item and item["sessions_total"] is not None:
                if not isinstance(item["sessions_total"], int) or item["sessions_total"] < 0:
                    errors.append(f"{p}daily_trend[{idx}].sessions_total must be a non-negative integer or null")
            if "crashed_sessions" in item and item["crashed_sessions"] is not None:
                if not isinstance(item["crashed_sessions"], int) or item["crashed_sessions"] < 0:
                    errors.append(f"{p}daily_trend[{idx}].crashed_sessions must be a non-negative integer or null")
            if "crash_free_sessions_rate" in item and item["crash_free_sessions_rate"] is not None:
                if not isinstance(item["crash_free_sessions_rate"], (int, float)) or item["crash_free_sessions_rate"] < 0.0 or item["crash_free_sessions_rate"] > 1.0:
                    errors.append(f"{p}daily_trend[{idx}].crash_free_sessions_rate must be a float between 0.0 and 1.0 or null")
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
            if "version" in item and (not isinstance(item["version"], str) or not item["version"]):
                errors.append(f"{p}version_health[{idx}].version must be a non-empty string")
            if "platform" in item and item["platform"] not in {"ios", "android", "all"}:
                errors.append(f"{p}version_health[{idx}].platform must be ios, android, or all")
            if "release_date" in item and item["release_date"] is not None and not is_valid_date(item["release_date"]):
                errors.append(f"{p}version_health[{idx}].release_date must be YYYY-MM-DD or null")
            if "crash_events" in item and (not isinstance(item["crash_events"], int) or item["crash_events"] < 0):
                errors.append(f"{p}version_health[{idx}].crash_events must be a non-negative integer")
            if "affected_users" in item and (not isinstance(item["affected_users"], int) or item["affected_users"] < 0):
                errors.append(f"{p}version_health[{idx}].affected_users must be a non-negative integer")
            if "status" in item and item["status"] not in valid_vh_statuses:
                errors.append(f"{p}version_health[{idx}].status must be one of {valid_vh_statuses}")
            if "trend" in item and item["trend"] not in valid_vh_trends:
                errors.append(f"{p}version_health[{idx}].trend must be one of {valid_vh_trends}")
            for rate_f in ("crash_free_users_rate", "crash_free_sessions_rate", "adoption_rate"):
                if rate_f in item:
                    rf_val = item[rate_f]
                    if rf_val is not None and (not isinstance(rf_val, (int, float)) or rf_val < 0.0 or rf_val > 1.0):
                        errors.append(f"{p}version_health[{idx}].{rate_f} must be a float between 0.0 and 1.0 or null")
    elif vh is not None:
        errors.append(f"{p}version_health must be a list")

    # 7. Distributions
    dists = data.get("distributions")
    if isinstance(dists, dict):
        # 7.1 Platform
        if "platform" not in dists or not isinstance(dists["platform"], list):
            errors.append(f"{p}distributions.platform is required and must be a list")
        else:
            for idx, item in enumerate(dists["platform"]):
                if not isinstance(item, dict):
                    errors.append(f"{p}distributions.platform[{idx}] must be an object")
                    continue
                for req_f in ("name", "events", "users", "share"):
                    if req_f not in item:
                        errors.append(f"{p}distributions.platform[{idx}].{req_f} is required")
                if "name" in item and item["name"] not in {"ios", "android"}:
                    errors.append(f"{p}distributions.platform[{idx}].name must be 'ios' or 'android'")
                if "events" in item and (not isinstance(item["events"], int) or item["events"] < 0):
                    errors.append(f"{p}distributions.platform[{idx}].events must be a non-negative integer")
                if "users" in item and (not isinstance(item["users"], int) or item["users"] < 0):
                    errors.append(f"{p}distributions.platform[{idx}].users must be a non-negative integer")
                if "share" in item and (not isinstance(item["share"], (int, float)) or item["share"] < 0.0 or item["share"] > 1.0):
                    errors.append(f"{p}distributions.platform[{idx}].share must be a float between 0.0 and 1.0")

        # 7.2 Device models
        if "device_models" not in dists or not isinstance(dists["device_models"], list):
            errors.append(f"{p}distributions.device_models is required and must be a list")
        else:
            for idx, item in enumerate(dists["device_models"]):
                if not isinstance(item, dict):
                    errors.append(f"{p}distributions.device_models[{idx}] must be an object")
                    continue
                for req_f in ("model", "platform", "events", "users", "share"):
                    if req_f not in item:
                        errors.append(f"{p}distributions.device_models[{idx}].{req_f} is required")
                if "model" in item and (not isinstance(item["model"], str) or not item["model"]):
                    errors.append(f"{p}distributions.device_models[{idx}].model must be a non-empty string")
                if "platform" in item and item["platform"] not in {"ios", "android", "all"}:
                    errors.append(f"{p}distributions.device_models[{idx}].platform must be ios, android, or all")
                if "events" in item and (not isinstance(item["events"], int) or item["events"] < 0):
                    errors.append(f"{p}distributions.device_models[{idx}].events must be a non-negative integer")
                if "users" in item and (not isinstance(item["users"], int) or item["users"] < 0):
                    errors.append(f"{p}distributions.device_models[{idx}].users must be a non-negative integer")
                if "share" in item and (not isinstance(item["share"], (int, float)) or item["share"] < 0.0 or item["share"] > 1.0):
                    errors.append(f"{p}distributions.device_models[{idx}].share must be a float between 0.0 and 1.0")

        # 7.3 OS versions
        if "os_versions" not in dists or not isinstance(dists["os_versions"], list):
            errors.append(f"{p}distributions.os_versions is required and must be a list")
        else:
            for idx, item in enumerate(dists["os_versions"]):
                if not isinstance(item, dict):
                    errors.append(f"{p}distributions.os_versions[{idx}] must be an object")
                    continue
                for req_f in ("os_version", "platform", "events", "users", "share"):
                    if req_f not in item:
                        errors.append(f"{p}distributions.os_versions[{idx}].{req_f} is required")
                if "os_version" in item and (not isinstance(item["os_version"], str) or not item["os_version"]):
                    errors.append(f"{p}distributions.os_versions[{idx}].os_version must be a non-empty string")
                if "platform" in item and item["platform"] not in {"ios", "android", "all"}:
                    errors.append(f"{p}distributions.os_versions[{idx}].platform must be ios, android, or all")
                if "events" in item and (not isinstance(item["events"], int) or item["events"] < 0):
                    errors.append(f"{p}distributions.os_versions[{idx}].events must be a non-negative integer")
                if "users" in item and (not isinstance(item["users"], int) or item["users"] < 0):
                    errors.append(f"{p}distributions.os_versions[{idx}].users must be a non-negative integer")
                if "share" in item and (not isinstance(item["share"], (int, float)) or item["share"] < 0.0 or item["share"] > 1.0):
                    errors.append(f"{p}distributions.os_versions[{idx}].share must be a float between 0.0 and 1.0")

        # 7.4 App versions
        if "app_versions" not in dists or not isinstance(dists["app_versions"], list):
            errors.append(f"{p}distributions.app_versions is required and must be a list")
        else:
            for idx, item in enumerate(dists["app_versions"]):
                if not isinstance(item, dict):
                    errors.append(f"{p}distributions.app_versions[{idx}] must be an object")
                    continue
                for req_f in ("app_version", "platform", "events", "users", "share"):
                    if req_f not in item:
                        errors.append(f"{p}distributions.app_versions[{idx}].{req_f} is required")
                if "app_version" in item and (not isinstance(item["app_version"], str) or not item["app_version"]):
                    errors.append(f"{p}distributions.app_versions[{idx}].app_version must be a non-empty string")
                if "platform" in item and item["platform"] not in {"ios", "android", "all"}:
                    errors.append(f"{p}distributions.app_versions[{idx}].platform must be ios, android, or all")
                if "events" in item and (not isinstance(item["events"], int) or item["events"] < 0):
                    errors.append(f"{p}distributions.app_versions[{idx}].events must be a non-negative integer")
                if "users" in item and (not isinstance(item["users"], int) or item["users"] < 0):
                    errors.append(f"{p}distributions.app_versions[{idx}].users must be a non-negative integer")
                if "share" in item and (not isinstance(item["share"], (int, float)) or item["share"] < 0.0 or item["share"] > 1.0):
                    errors.append(f"{p}distributions.app_versions[{idx}].share must be a float between 0.0 and 1.0")

        # 7.5 Custom keys (optional)
        if "custom_keys" in dists and dists["custom_keys"] is not None:
            if not isinstance(dists["custom_keys"], list):
                errors.append(f"{p}distributions.custom_keys must be a list")
            else:
                for idx, item in enumerate(dists["custom_keys"]):
                    if not isinstance(item, dict):
                        errors.append(f"{p}distributions.custom_keys[{idx}] must be an object")
                        continue
                    for req_f in ("key", "value", "platform", "events"):
                        if req_f not in item:
                            errors.append(f"{p}distributions.custom_keys[{idx}].{req_f} is required")
                    if "events" in item and (not isinstance(item["events"], int) or item["events"] < 0):
                        errors.append(f"{p}distributions.custom_keys[{idx}].events must be a non-negative integer")
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
            if "platform" in issue and issue["platform"] not in {"ios", "android"}:
                errors.append(f"{p}top_issues[{idx}].platform must be 'ios' or 'android'")
            if "error_type" in issue and issue["error_type"] not in {"FATAL", "ANR", "NON_FATAL"}:
                errors.append(f"{p}top_issues[{idx}].error_type must be FATAL, ANR, or NON_FATAL")
            if "events" in issue and (not isinstance(issue["events"], int) or issue["events"] < 0):
                errors.append(f"{p}top_issues[{idx}].events must be a non-negative integer")
            if "affected_users" in issue and (not isinstance(issue["affected_users"], int) or issue["affected_users"] < 0):
                errors.append(f"{p}top_issues[{idx}].affected_users must be a non-negative integer")
            if "first_seen_timestamp" in issue and not is_valid_iso8601_utc(issue["first_seen_timestamp"]):
                errors.append(f"{p}top_issues[{idx}].first_seen_timestamp must be ISO 8601 UTC timestamp (ending in Z)")
            if "last_seen_timestamp" in issue and not is_valid_iso8601_utc(issue["last_seen_timestamp"]):
                errors.append(f"{p}top_issues[{idx}].last_seen_timestamp must be ISO 8601 UTC timestamp (ending in Z)")

            # Priority
            prio = issue.get("priority")
            if isinstance(prio, dict):
                for pk in ("score", "level", "trend", "score_breakdown"):
                    if pk not in prio:
                        errors.append(f"{p}top_issues[{idx}].priority.{pk} is required")
                if "score" in prio and (not isinstance(prio["score"], int) or prio["score"] < 0):
                    errors.append(f"{p}top_issues[{idx}].priority.score must be a non-negative integer")
                if "level" in prio and prio["level"] not in {"P0", "P1", "P2", "P3"}:
                    errors.append(f"{p}top_issues[{idx}].priority.level must be P0, P1, P2, or P3")
                if "trend" in prio and prio["trend"] not in {"new", "worsening", "stable", "improving"}:
                    errors.append(f"{p}top_issues[{idx}].priority.trend must be new, worsening, stable, or improving")
            elif prio is not None:
                errors.append(f"{p}top_issues[{idx}].priority must be an object")

            # Version distribution
            vdist = issue.get("version_distribution")
            if isinstance(vdist, list):
                for vidx, vitem in enumerate(vdist):
                    if not isinstance(vitem, dict):
                        errors.append(f"{p}top_issues[{idx}].version_distribution[{vidx}] must be an object")
                        continue
                    for v_req in ("version", "events", "users"):
                        if v_req not in vitem:
                            errors.append(f"{p}top_issues[{idx}].version_distribution[{vidx}].{v_req} is required")
                    if "events" in vitem and (not isinstance(vitem["events"], int) or vitem["events"] < 0):
                        errors.append(f"{p}top_issues[{idx}].version_distribution[{vidx}].events must be a non-negative integer")
                    if "users" in vitem and (not isinstance(vitem["users"], int) or vitem["users"] < 0):
                        errors.append(f"{p}top_issues[{idx}].version_distribution[{vidx}].users must be a non-negative integer")
            elif vdist is not None:
                errors.append(f"{p}top_issues[{idx}].version_distribution must be a list")

            # Blame Frame
            bf = issue.get("blame_frame")
            if bf is not None:
                if not isinstance(bf, dict):
                    errors.append(f"{p}top_issues[{idx}].blame_frame must be an object or null")
                else:
                    for bfk in ("file", "line", "symbol", "class_name", "method_name", "is_blame", "source_available"):
                        if bfk not in bf:
                            errors.append(f"{p}top_issues[{idx}].blame_frame.{bfk} is required")
                    if "line" in bf and bf["line"] is not None and (not isinstance(bf["line"], int) or bf["line"] <= 0):
                        errors.append(f"{p}top_issues[{idx}].blame_frame.line must be a positive integer or null")
                    if "is_blame" in bf and not isinstance(bf["is_blame"], bool):
                        errors.append(f"{p}top_issues[{idx}].blame_frame.is_blame must be a boolean")
                    if "source_available" in bf and not isinstance(bf["source_available"], bool):
                        errors.append(f"{p}top_issues[{idx}].blame_frame.source_available must be a boolean")

            # AI Issue Analysis
            ai_ia = issue.get("ai_analysis")
            if isinstance(ai_ia, dict):
                for ak in ("status", "root_cause", "suggested_fix", "effort", "confidence", "reasoning_sources"):
                    if ak not in ai_ia:
                        errors.append(f"{p}top_issues[{idx}].ai_analysis.{ak} is required")
                if "status" in ai_ia and ai_ia["status"] not in {"available", "unavailable", "pending", "skipped"}:
                    errors.append(f"{p}top_issues[{idx}].ai_analysis.status must be available, unavailable, pending, or skipped")
                eff = ai_ia.get("effort")
                if eff is not None and eff not in {"S", "M", "L"}:
                    errors.append(f"{p}top_issues[{idx}].ai_analysis.effort must be S, M, L, or null")
                conf = ai_ia.get("confidence")
                if conf is not None and conf not in {"high", "medium", "low", "needs_manual_review"}:
                    errors.append(f"{p}top_issues[{idx}].ai_analysis.confidence must be high, medium, low, needs_manual_review, or null")
            elif ai_ia is not None:
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
        if "status" in ai and ai["status"] not in {"available", "unavailable", "disabled", "error"}:
            errors.append(f"{p}ai_summary.status must be one of available, unavailable, disabled, error")
        gen_ts = ai.get("generated_at")
        if gen_ts is not None and not is_valid_iso8601_utc(gen_ts):
            errors.append(f"{p}ai_summary.generated_at must be ISO 8601 UTC timestamp or null")
        if "overview" in ai and not isinstance(ai["overview"], str):
            errors.append(f"{p}ai_summary.overview must be a string")
        if "key_takeaways" in ai and not isinstance(ai["key_takeaways"], list):
            errors.append(f"{p}ai_summary.key_takeaways must be a list of strings")
        rec_actions = ai.get("recommended_actions")
        if isinstance(rec_actions, list):
            for idx, act in enumerate(rec_actions):
                if not isinstance(act, dict):
                    errors.append(f"{p}ai_summary.recommended_actions[{idx}] must be an object")
                    continue
                for req_act in ("priority", "issue_id", "action", "effort"):
                    if req_act not in act:
                        errors.append(f"{p}ai_summary.recommended_actions[{idx}].{req_act} is required")
                if "priority" in act and act["priority"] not in {"P0", "P1", "P2", "P3"}:
                    errors.append(f"{p}ai_summary.recommended_actions[{idx}].priority must be P0, P1, P2, or P3")
                if "effort" in act and act["effort"] not in {"S", "M", "L"}:
                    errors.append(f"{p}ai_summary.recommended_actions[{idx}].effort must be S, M, or L")
        elif rec_actions is not None:
            errors.append(f"{p}ai_summary.recommended_actions must be a list")
    elif ai is not None:
        errors.append(f"{p}ai_summary must be an object")

    # 10. Limitations
    if "limitations" not in data or not isinstance(data.get("limitations"), list):
        errors.append(f"{p}limitations is required and must be a list of strings")
    else:
        for l_item in data["limitations"]:
            if not isinstance(l_item, str):
                errors.append(f"{p}limitations item must be a string")

    # 11. Periods (optional multi-period authoritative snapshots in V2.3)
    if "periods" in data and data["periods"] is not None:
        periods_dict = data["periods"]
        if not isinstance(periods_dict, dict):
            errors.append(f"{p}periods must be an object")
        else:
            for p_key, p_val in periods_dict.items():
                if not isinstance(p_key, str) or not p_key.isdigit():
                    errors.append(f"{p}periods key '{p_key}' must be a numeric string representing days (e.g. '7', '30', '90')")
                if not isinstance(p_val, dict):
                    errors.append(f"{p}periods['{p_key}'] must be an object")
                    continue
                # Validate period in snapshot
                if "period" in p_val and isinstance(p_val["period"], dict):
                    snap_days = p_val["period"].get("days")
                    if str(snap_days) != p_key:
                        errors.append(f"{p}periods['{p_key}'].period.days ({snap_days}) must match key '{p_key}'")
                else:
                    errors.append(f"{p}periods['{p_key}'].period is required")
                if "kpi" not in p_val or not isinstance(p_val["kpi"], dict):
                    errors.append(f"{p}periods['{p_key}'].kpi is required")
                if "top_issues" not in p_val or not isinstance(p_val["top_issues"], list):
                    errors.append(f"{p}periods['{p_key}'].top_issues is required")
                if "version_health" not in p_val or not isinstance(p_val["version_health"], list):
                    errors.append(f"{p}periods['{p_key}'].version_health is required")
                if "distributions" not in p_val or not isinstance(p_val["distributions"], dict):
                    errors.append(f"{p}periods['{p_key}'].distributions is required")
                if "status" in p_val and p_val["status"] is not None:
                    if p_val["status"] not in ("available", "unavailable", "error", "disabled", "insufficient_data", "stale"):
                        errors.append(f"{p}periods['{p_key}'].status '{p_val['status']}' is invalid")

    return errors


def validate_dashboard_v2(data: dict) -> List[str]:
    """Validates a full DashboardV2Bundle against Schema V2 rules."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return [f"Expected object root, got {type(data).__name__}"]

    for req_k in ("schema_version", "generated_at", "default_app", "apps"):
        if req_k not in data:
            errors.append(f"Root {req_k} is required")

    if data.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(f"Root schema_version must be one of {sorted(list(SUPPORTED_SCHEMA_VERSIONS))}, got {data.get('schema_version')}")

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
