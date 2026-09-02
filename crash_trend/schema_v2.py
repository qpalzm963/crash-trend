"""Dashboard V2 Data Schema TypedDicts and Validation Utilities.

This module defines the Python data contract for Dashboard V2, providing
static type hints for data pipelines (fetch_bigquery, sessions, issue_detail,
analyze_gemini, build_dashboard) and runtime schema validators.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Dict, List, Literal, Optional, TypedDict

SCHEMA_VERSION = "2.0"

# ---------------------------------------------------------------------------
# TypedDict Definitions
# ---------------------------------------------------------------------------

class AppMetadata(TypedDict, total=False):
    app_id: str
    display_name: str
    firebase_project_id: str
    platforms: List[str]
    source_repo: Optional[str]
    custom_keys_monitored: List[str]


class PeriodComparison(TypedDict, total=False):
    days: int
    start_time: str
    end_time: str


class PeriodInfo(TypedDict, total=False):
    days: int
    start_time: str
    end_time: str
    comparison_period: Optional[PeriodComparison]


class SourceStatus(TypedDict, total=False):
    status: Literal["available", "unavailable", "disabled", "error"]
    tables_queried: Optional[List[str]]
    model: Optional[str]
    last_sync_timestamp: Optional[str]
    error_message: Optional[str]


class SourcesAvailability(TypedDict, total=False):
    crashlytics_bq: SourceStatus
    firebase_sessions: SourceStatus
    mcp_crashlytics: SourceStatus
    gemini_ai: SourceStatus
    manual_console: Optional[SourceStatus]


class KPIMetric(TypedDict, total=False):
    value: int
    previous_value: Optional[int]
    change_pct: Optional[float]
    status: Literal["available", "insufficient_data", "error"]


class CrashFreeMetric(TypedDict, total=False):
    rate: Optional[float]  # 0.0 to 1.0 (e.g. 0.9985 for 99.85%)
    total: Optional[int]
    crashed: Optional[int]
    previous_rate: Optional[float]
    change_pct_points: Optional[float]
    status: Literal["available", "unavailable", "insufficient_data", "error"]
    unavailable_reason: Optional[str]


class EventsByErrorType(TypedDict, total=False):
    fatal: int
    anr: int
    non_fatal: int


class OverviewKPI(TypedDict, total=False):
    crash_events: KPIMetric
    affected_users: KPIMetric
    crash_free_users: CrashFreeMetric
    crash_free_sessions: CrashFreeMetric
    new_issues_count: KPIMetric
    events_by_error_type: EventsByErrorType


class DailyTrendPoint(TypedDict, total=False):
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


class VersionHealthItem(TypedDict, total=False):
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


class DistributionItem(TypedDict, total=False):
    events: int
    users: int
    share: float
    # Variant fields:
    name: Optional[str]        # for platform
    model: Optional[str]       # for device_models
    os_version: Optional[str]  # for os_versions
    app_version: Optional[str] # for app_versions
    platform: Optional[str]


class CustomKeyDistributionItem(TypedDict, total=False):
    key: str
    value: str
    platform: str
    events: int


class Distributions(TypedDict, total=False):
    platform: List[Dict[str, Any]]
    device_models: List[Dict[str, Any]]
    os_versions: List[Dict[str, Any]]
    app_versions: List[Dict[str, Any]]
    custom_keys: Optional[List[CustomKeyDistributionItem]]


class PriorityBreakdown(TypedDict, total=False):
    users_normalized: float
    events_normalized: float
    fatal_anr_boost: int
    worsening_boost: int
    latest_version_boost: int
    core_path_boost: int


class PriorityInfo(TypedDict, total=False):
    score: int
    level: Literal["P0", "P1", "P2", "P3"]
    trend: Literal["new", "worsening", "stable", "improving"]
    score_breakdown: Optional[PriorityBreakdown]


class BlameFrame(TypedDict, total=False):
    file: Optional[str]
    line: Optional[int]
    symbol: Optional[str]
    class_name: Optional[str]
    method_name: Optional[str]
    is_blame: bool
    source_available: bool


class AIIssueAnalysis(TypedDict, total=False):
    status: Literal["available", "unavailable", "pending", "skipped"]
    root_cause: Optional[str]
    suggested_fix: Optional[str]
    effort: Optional[Literal["S", "M", "L"]]
    confidence: Optional[Literal["high", "medium", "low", "needs_manual_review"]]
    reasoning_sources: Optional[List[str]]


class BreadcrumbItem(TypedDict, total=False):
    timestamp: str
    category: str
    message: str
    level: str
    data: Optional[Dict[str, Any]]


class LogItem(TypedDict, total=False):
    timestamp: str
    message: str


class IssueDetail(TypedDict, total=False):
    stack_trace: Optional[str]
    breadcrumbs: Optional[List[BreadcrumbItem]]
    logs: Optional[List[LogItem]]
    custom_keys: Optional[Dict[str, Any]]
    top_devices: Optional[List[Dict[str, Any]]]
    top_os: Optional[List[Dict[str, Any]]]


class VersionDistCount(TypedDict, total=False):
    version: str
    events: int
    users: int


class IssueSummary(TypedDict, total=False):
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


class RecommendedAction(TypedDict, total=False):
    priority: Literal["P0", "P1", "P2", "P3"]
    issue_id: str
    action: str
    effort: Literal["S", "M", "L"]


class AISummary(TypedDict, total=False):
    status: Literal["available", "unavailable", "disabled", "error"]
    model: Optional[str]
    generated_at: Optional[str]
    overview: str
    key_takeaways: List[str]
    distribution_insights: str
    recommended_actions: List[RecommendedAction]
    data_limitations: Optional[str]


class AppDashboardV2Data(TypedDict, total=False):
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


class DashboardV2Bundle(TypedDict, total=False):
    schema_version: str
    generated_at: str
    default_app: str
    apps: Dict[str, AppDashboardV2Data]


# ---------------------------------------------------------------------------
# Runtime Validation Utilities
# ---------------------------------------------------------------------------

_ISO8601_REGEX = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$"
)
_DATE_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_valid_iso8601(val: Any) -> bool:
    if not isinstance(val, str):
        return False
    return bool(_ISO8601_REGEX.match(val))


def is_valid_date(val: Any) -> bool:
    if not isinstance(val, str):
        return False
    return bool(_DATE_REGEX.match(val))


def validate_app_dashboard_v2(data: dict, prefix: str = "") -> List[str]:
    """Validates an AppDashboardV2Data dictionary against Schema V2 rules."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return [f"{prefix}Expected dict, got {type(data).__name__}"]

    p = f"{prefix}." if prefix else ""

    # 1. Metadata
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        errors.append(f"{p}metadata is required and must be an object")
    else:
        for field in ("app_id", "display_name", "firebase_project_id", "platforms"):
            if field not in meta:
                errors.append(f"{p}metadata.{field} is required")
        if not isinstance(meta.get("platforms"), list) or not meta.get("platforms"):
            errors.append(f"{p}metadata.platforms must be a non-empty list of strings")

    # 2. Period
    period = data.get("period")
    if not isinstance(period, dict):
        errors.append(f"{p}period is required and must be an object")
    else:
        if not isinstance(period.get("days"), int) or period["days"] <= 0:
            errors.append(f"{p}period.days must be a positive integer")
        if not is_valid_iso8601(period.get("start_time")):
            errors.append(f"{p}period.start_time must be a valid ISO 8601 timestamp string")
        if not is_valid_iso8601(period.get("end_time")):
            errors.append(f"{p}period.end_time must be a valid ISO 8601 timestamp string")

    # 3. Sources
    sources = data.get("sources")
    if not isinstance(sources, dict):
        errors.append(f"{p}sources is required and must be an object")
    else:
        valid_statuses = {"available", "unavailable", "disabled", "error"}
        for s_name in ("crashlytics_bq", "firebase_sessions", "gemini_ai"):
            s_obj = sources.get(s_name)
            if not isinstance(s_obj, dict):
                errors.append(f"{p}sources.{s_name} is required and must be an object")
            elif s_obj.get("status") not in valid_statuses:
                errors.append(f"{p}sources.{s_name}.status must be one of {valid_statuses}")

    # 4. KPI
    kpi = data.get("kpi")
    if not isinstance(kpi, dict):
        errors.append(f"{p}kpi is required and must be an object")
    else:
        # Check crash_events
        ev = kpi.get("crash_events")
        if not isinstance(ev, dict) or not isinstance(ev.get("value"), int):
            errors.append(f"{p}kpi.crash_events.value must be an integer")

        # Check affected_users
        us = kpi.get("affected_users")
        if not isinstance(us, dict) or not isinstance(us.get("value"), int):
            errors.append(f"{p}kpi.affected_users.value must be an integer")

        # Check crash_free_users & crash_free_sessions
        for cf_key in ("crash_free_users", "crash_free_sessions"):
            cf = kpi.get(cf_key)
            if not isinstance(cf, dict):
                errors.append(f"{p}kpi.{cf_key} is required and must be an object")
            else:
                cf_status = cf.get("status")
                if cf_status not in {"available", "unavailable", "insufficient_data", "error"}:
                    errors.append(f"{p}kpi.{cf_key}.status must be one of available/unavailable/insufficient_data/error")
                if cf_status == "available":
                    rate = cf.get("rate")
                    if not isinstance(rate, (int, float)) or rate < 0.0 or rate > 1.0:
                        errors.append(f"{p}kpi.{cf_key}.rate must be a float between 0.0 and 1.0 when available")
                elif cf_status == "unavailable" and cf.get("rate") is not None:
                    errors.append(f"{p}kpi.{cf_key}.rate must be null when status is unavailable")

        # Check events_by_error_type
        by_err = kpi.get("events_by_error_type")
        if not isinstance(by_err, dict):
            errors.append(f"{p}kpi.events_by_error_type is required")
        else:
            for k in ("fatal", "anr", "non_fatal"):
                if not isinstance(by_err.get(k), int) or by_err[k] < 0:
                    errors.append(f"{p}kpi.events_by_error_type.{k} must be a non-negative integer")

    # 5. Daily Trend
    daily = data.get("daily_trend")
    if not isinstance(daily, list):
        errors.append(f"{p}daily_trend must be a list")
    else:
        for idx, item in enumerate(daily):
            if not is_valid_date(item.get("date")):
                errors.append(f"{p}daily_trend[{idx}].date must be YYYY-MM-DD")
            if not isinstance(item.get("crash_events"), int):
                errors.append(f"{p}daily_trend[{idx}].crash_events must be an integer")

    # 6. Version Health
    vh = data.get("version_health")
    if not isinstance(vh, list):
        errors.append(f"{p}version_health must be a list")
    else:
        for idx, item in enumerate(vh):
            if not item.get("version"):
                errors.append(f"{p}version_health[{idx}].version is required")
            if not isinstance(item.get("crash_events"), int):
                errors.append(f"{p}version_health[{idx}].crash_events must be an integer")

    # 7. Distributions
    dists = data.get("distributions")
    if not isinstance(dists, dict):
        errors.append(f"{p}distributions is required and must be an object")
    else:
        for dk in ("platform", "device_models", "os_versions", "app_versions"):
            if not isinstance(dists.get(dk), list):
                errors.append(f"{p}distributions.{dk} must be a list")

    # 8. Top Issues
    issues = data.get("top_issues")
    if not isinstance(issues, list):
        errors.append(f"{p}top_issues must be a list")
    else:
        for idx, issue in enumerate(issues):
            if not issue.get("issue_id"):
                errors.append(f"{p}top_issues[{idx}].issue_id is required")
            if not issue.get("title"):
                errors.append(f"{p}top_issues[{idx}].title is required")
            if issue.get("error_type") not in {"FATAL", "ANR", "NON_FATAL"}:
                errors.append(f"{p}top_issues[{idx}].error_type must be FATAL, ANR, or NON_FATAL")
            if not is_valid_iso8601(issue.get("first_seen_timestamp")):
                errors.append(f"{p}top_issues[{idx}].first_seen_timestamp must be ISO 8601 timestamp")
            if not is_valid_iso8601(issue.get("last_seen_timestamp")):
                errors.append(f"{p}top_issues[{idx}].last_seen_timestamp must be ISO 8601 timestamp")

            prio = issue.get("priority")
            if not isinstance(prio, dict) or prio.get("level") not in {"P0", "P1", "P2", "P3"}:
                errors.append(f"{p}top_issues[{idx}].priority.level must be P0, P1, P2, or P3")

    # 9. AI Summary
    ai = data.get("ai_summary")
    if not isinstance(ai, dict):
        errors.append(f"{p}ai_summary is required and must be an object")
    else:
        if ai.get("status") not in {"available", "unavailable", "disabled", "error"}:
            errors.append(f"{p}ai_summary.status must be one of available/unavailable/disabled/error")
        if not isinstance(ai.get("overview"), str):
            errors.append(f"{p}ai_summary.overview must be a string")
        if not isinstance(ai.get("key_takeaways"), list):
            errors.append(f"{p}ai_summary.key_takeaways must be a list of strings")

    return errors


def validate_dashboard_v2(data: dict) -> List[str]:
    """Validates a full DashboardV2Bundle against Schema V2 rules."""
    errors: List[str] = []
    if not isinstance(data, dict):
        return [f"Expected dict root, got {type(data).__name__}"]

    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"Root schema_version must be '{SCHEMA_VERSION}', got {data.get('schema_version')}")

    if not is_valid_iso8601(data.get("generated_at")):
        errors.append("Root generated_at must be a valid ISO 8601 timestamp string")

    default_app = data.get("default_app")
    if not isinstance(default_app, str) or not default_app:
        errors.append("Root default_app must be a non-empty string")

    apps = data.get("apps")
    if not isinstance(apps, dict) or not apps:
        errors.append("Root apps must be a non-empty dictionary of app data")
    else:
        if default_app not in apps:
            errors.append(f"Root default_app '{default_app}' is not present in apps dict")
        for app_name, app_data in apps.items():
            errors.extend(validate_app_dashboard_v2(app_data, prefix=f"apps['{app_name}']"))

    return errors
