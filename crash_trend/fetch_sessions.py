"""Firebase Sessions BigQuery Export Fetcher & Metrics Calculator for Dashboard V2.

This module provides:
1. BigQuery client connection & table discovery for Firebase Sessions export
   (e.g., `{project}.firebase_sessions.events_*` or `{project}.firebase_sessions.{table}`).
2. Crash-free Users & Crash-free Sessions calculations:
   - Joined query between Firebase Sessions and Crashlytics tables on `session_id = firebase_session_id` (FATAL crashes)
   - Real Firebase Sessions export schema: `instance_id`, `session_id`, `event_timestamp`, `application.display_version`
   - `total_sessions`, `crashed_sessions`, `crash_free_sessions_rate`
   - `total_users`, `crashed_users`, `crash_free_users_rate` (derived in instance_id domain via session-instance mapping)
   - Accurate independent previous period comparison: `[now - 2*days, now - days)`
   - Per-version crash-free rates & adoption rates for Version Health
   - Daily crash-free sessions trend
3. App Table Filtering:
   - Isolates session tables matching app package/bundle/id, returning empty array on mismatch to prevent multi-app cross-contamination.
4. Graceful degradation:
   - When Sessions export is not enabled, dataset/tables are missing, or queries fail,
     marks status as "unavailable" with explicit `unavailable_reason`.
   - Never displays fake 0% or 0 values when data is unavailable (rate/total/crashed are null).
5. Strict conformance to `docs/dashboard_v2_schema.md` and `crash_trend/schema_v2.py`.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from crash_trend.config import ROOT, app_argparser, get_app, is_sessions_enabled, load_config, out_dir, write_json
    from crash_trend.fetch_bigquery import list_crash_tables
    from crash_trend.schema_v2 import CrashFreeMetric, SourceStatus
except ImportError:
    try:
        from config import ROOT, app_argparser, get_app, is_sessions_enabled, load_config, out_dir, write_json
        from fetch_bigquery import list_crash_tables
        from schema_v2 import CrashFreeMetric, SourceStatus
    except ImportError:
        from crash_trend.config import ROOT, app_argparser, get_app, is_sessions_enabled, load_config, out_dir, write_json
        from crash_trend.schema_v2 import CrashFreeMetric, SourceStatus
        list_crash_tables = None  # type: ignore

DEFAULT_SESSIONS_DATASET = "firebase_sessions"
DEFAULT_CRASH_DATASET = "firebase_crashlytics"
DEFAULT_UNAVAILABLE_REASON = "Firebase Sessions export table not found in dataset"


# ---------------------------------------------------------------------------
# SQL Query Templates (對齊 Firebase Sessions 官方 Schema 與 Crashlytics Join)
# ---------------------------------------------------------------------------

SQLS = {
    # 1. Join query between firebase_sessions (instance_id, session_id, event_timestamp)
    # and firebase_crashlytics (firebase_session_id, FATAL crashes)
    "kpi_joined": """
        WITH sessions AS (
            SELECT
                session_id,
                instance_id
            FROM `{sessions_table}`
            WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
              AND event_timestamp < TIMESTAMP_ADD(TIMESTAMP(CURRENT_DATE()), INTERVAL 1 DAY)
        ),
        crashes AS (
            SELECT DISTINCT
                firebase_session_id AS session_id
            FROM `{crash_table}`
            WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
              AND event_timestamp < TIMESTAMP_ADD(TIMESTAMP(CURRENT_DATE()), INTERVAL 1 DAY)
              AND UPPER(error_type) = 'FATAL'
              AND firebase_session_id IS NOT NULL
        )
        SELECT
            COUNT(DISTINCT s.session_id) AS total_sessions,
            COUNT(DISTINCT s.instance_id) AS total_users,
            COUNT(DISTINCT IF(c.session_id IS NOT NULL, s.session_id, NULL)) AS crashed_sessions,
            COUNT(DISTINCT IF(c.session_id IS NOT NULL, s.instance_id, NULL)) AS crashed_users
        FROM sessions s
        LEFT JOIN crashes c ON s.session_id = c.session_id
    """,

    # 2. Previous period KPI comparison (獨立前一日曆區間: [CURRENT_DATE - 2*days + 1, CURRENT_DATE - days + 1))
    "kpi_previous_joined": """
        WITH sessions_prev AS (
            SELECT
                session_id,
                instance_id
            FROM `{sessions_table}`
            WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} * 2 - 1 DAY))
              AND event_timestamp < TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
        ),
        crashes_prev AS (
            SELECT DISTINCT
                firebase_session_id AS session_id
            FROM `{crash_table}`
            WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} * 2 - 1 DAY))
              AND event_timestamp < TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
              AND UPPER(error_type) = 'FATAL'
              AND firebase_session_id IS NOT NULL
        )
        SELECT
            COUNT(DISTINCT s.session_id) AS prev_total_sessions,
            COUNT(DISTINCT s.instance_id) AS prev_total_users,
            COUNT(DISTINCT IF(c.session_id IS NOT NULL, s.session_id, NULL)) AS prev_crashed_sessions,
            COUNT(DISTINCT IF(c.session_id IS NOT NULL, s.instance_id, NULL)) AS prev_crashed_users
        FROM sessions_prev s
        LEFT JOIN crashes_prev c ON s.session_id = c.session_id
    """,

    # 3. Daily trend sessions query (joined with crashlytics)
    "daily_joined": """
        WITH sessions AS (
            SELECT
                session_id,
                FORMAT_TIMESTAMP('%Y-%m-%d', event_timestamp) AS session_date
            FROM `{sessions_table}`
            WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
              AND event_timestamp < TIMESTAMP_ADD(TIMESTAMP(CURRENT_DATE()), INTERVAL 1 DAY)
        ),
        crashes AS (
            SELECT DISTINCT
                firebase_session_id AS session_id
            FROM `{crash_table}`
            WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
              AND event_timestamp < TIMESTAMP_ADD(TIMESTAMP(CURRENT_DATE()), INTERVAL 1 DAY)
              AND UPPER(error_type) = 'FATAL'
              AND firebase_session_id IS NOT NULL
        )
        SELECT
            s.session_date AS date,
            COUNT(DISTINCT s.session_id) AS sessions_total,
            COUNT(DISTINCT IF(c.session_id IS NOT NULL, s.session_id, NULL)) AS crashed_sessions
        FROM sessions s
        LEFT JOIN crashes c ON s.session_id = c.session_id
        GROUP BY 1
        ORDER BY 1
    """,

    # 4. Version health sessions query (joined with crashlytics)
    "versions_joined": """
        WITH sessions AS (
            SELECT
                session_id,
                instance_id,
                COALESCE(application.display_version, 'unknown') AS version
            FROM `{sessions_table}`
            WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
              AND event_timestamp < TIMESTAMP_ADD(TIMESTAMP(CURRENT_DATE()), INTERVAL 1 DAY)
        ),
        crashes AS (
            SELECT DISTINCT
                firebase_session_id AS session_id
            FROM `{crash_table}`
            WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
              AND event_timestamp < TIMESTAMP_ADD(TIMESTAMP(CURRENT_DATE()), INTERVAL 1 DAY)
              AND UPPER(error_type) = 'FATAL'
              AND firebase_session_id IS NOT NULL
        )
        SELECT
            s.version,
            COUNT(DISTINCT s.session_id) AS sessions_total,
            COUNT(DISTINCT s.instance_id) AS users_total,
            COUNT(DISTINCT IF(c.session_id IS NOT NULL, s.session_id, NULL)) AS crashed_sessions,
            COUNT(DISTINCT IF(c.session_id IS NOT NULL, s.instance_id, NULL)) AS crashed_users
        FROM sessions s
        LEFT JOIN crashes c ON s.session_id = c.session_id
        GROUP BY 1
        ORDER BY sessions_total DESC
    """,
}


# ---------------------------------------------------------------------------
# BigQuery Client & Table Discovery
# ---------------------------------------------------------------------------

def make_sessions_client(project: str) -> Any:
    """Creates a BigQuery client using service account or Application Default Credentials."""
    from google.cloud import bigquery

    creds_cfg = (load_config().get("credentials") or {})
    sa_path = creds_cfg.get("bq_service_account")
    if sa_path:
        sa_file = Path(sa_path).expanduser()
        if not sa_file.exists():
            raise FileNotFoundError(f"credentials.bq_service_account not found: {sa_file}")
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(str(sa_file))
        return bigquery.Client(project=project, credentials=creds)
    return bigquery.Client(project=project)


def list_session_tables(
    client: Any,
    project: str,
    dataset: str = DEFAULT_SESSIONS_DATASET,
    app_config: Optional[dict] = None,
) -> List[str]:
    """Lists tables in the sessions dataset, with app package/bundle filtering."""
    try:
        tables = [t.table_id for t in client.list_tables(f"{project}.{dataset}")]
        batch_tables = [t for t in tables if not t.endswith("_REALTIME")]

        if not app_config:
            return batch_tables

        explicit_tables = app_config.get("sessions_tables") or app_config.get("tables")
        if explicit_tables is not None:
            return [t for t in batch_tables if t in explicit_tables]

        package_filters = []
        if app_config.get("package_name"):
            package_filters.append(str(app_config["package_name"]).replace(".", "_"))
        if app_config.get("bundle_id"):
            package_filters.append(str(app_config["bundle_id"]).replace(".", "_"))
        if app_config.get("app_id"):
            package_filters.append(str(app_config["app_id"]).replace(".", "_").replace("-", "_"))

        if package_filters:
            matched = [
                t for t in batch_tables
                if any(t.lower().startswith(pf.lower()) or pf.lower() in t.lower() for pf in package_filters)
            ]
            return matched

        return batch_tables
    except Exception as exc:
        print(f"  [Sessions] Table listing failed for {project}.{dataset}: {exc}", file=sys.stderr)
        return []


def run_sessions_query(client: Any, sql: str) -> List[Dict[str, Any]]:
    """Runs a BigQuery query and returns rows as dictionaries."""
    rows = client.query(sql).result(max_results=5000)
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Metric Calculation Utilities (Pure Functions)
# ---------------------------------------------------------------------------

def calculate_crash_free_rate(total: Optional[int], crashed: Optional[int], precision: int = 4) -> Optional[float]:
    """Calculates crash-free rate: (total - crashed) / total."""
    if total is None or crashed is None or total <= 0:
        return None
    crashed_bounded = max(0, min(crashed, total))
    rate = (total - crashed_bounded) / float(total)
    return round(max(0.0, min(1.0, rate)), precision)


def calculate_change_pct_points(
    current_rate: Optional[float], previous_rate: Optional[float], precision: int = 2
) -> Optional[float]:
    """Calculates percentage point change between two rates: (current - previous) * 100."""
    if current_rate is None or previous_rate is None:
        return None
    return round((current_rate - previous_rate) * 100.0, precision)


def calculate_adoption_rate(
    version_count: Optional[int], total_count: Optional[int], precision: int = 4
) -> Optional[float]:
    """Calculates adoption rate: version_count / total_count."""
    if version_count is None or total_count is None or total_count <= 0:
        return None
    return round(max(0.0, min(1.0, version_count / float(total_count))), precision)


def build_crash_free_metric(
    total: Optional[int],
    crashed: Optional[int],
    previous_rate: Optional[float] = None,
    status: str = "available",
    unavailable_reason: Optional[str] = None,
) -> CrashFreeMetric:
    """Builds a CrashFreeMetric dictionary conforming to Schema V2."""
    if status == "unavailable":
        return {
            "rate": None,
            "total": None,
            "crashed": None,
            "previous_rate": None,
            "change_pct_points": None,
            "status": "unavailable",
            "unavailable_reason": unavailable_reason or DEFAULT_UNAVAILABLE_REASON,
        }

    if status == "error":
        return {
            "rate": None,
            "total": None,
            "crashed": None,
            "previous_rate": previous_rate,
            "change_pct_points": None,
            "status": "error",
            "unavailable_reason": unavailable_reason,
        }

    if status == "insufficient_data" or total is None or total <= 0:
        return {
            "rate": None,
            "total": total if (total is not None and total >= 0) else None,
            "crashed": crashed if (crashed is not None and crashed >= 0) else None,
            "previous_rate": previous_rate,
            "change_pct_points": None,
            "status": "insufficient_data",
            "unavailable_reason": unavailable_reason or "Insufficient session data",
        }

    rate = calculate_crash_free_rate(total, crashed)
    change = calculate_change_pct_points(rate, previous_rate)
    return {
        "rate": rate,
        "total": total,
        "crashed": crashed,
        "previous_rate": previous_rate,
        "change_pct_points": change,
        "status": "available",
        "unavailable_reason": None,
    }


def build_unavailable_sessions_result(
    reason: str = DEFAULT_UNAVAILABLE_REASON,
    periods: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Generates an explicit unavailable response conforming to Schema V2 graceful degradation."""
    p_list = periods or [7, 30, 90]
    base_res = {
        "sources": {
            "status": "unavailable",
            "last_sync_timestamp": None,
            "error_message": reason,
            "tables_queried": None,
        },
        "kpi": {
            "crash_free_users": build_crash_free_metric(None, None, status="unavailable", unavailable_reason=reason),
            "crash_free_sessions": build_crash_free_metric(None, None, status="unavailable", unavailable_reason=reason),
        },
        "daily_trend": {},
        "version_health": {},
    }
    periods_dict = {}
    for p in p_list:
        periods_dict[str(p)] = {
            "sources": dict(base_res["sources"]),
            "kpi": {
                "crash_free_users": build_crash_free_metric(None, None, status="unavailable", unavailable_reason=reason),
                "crash_free_sessions": build_crash_free_metric(None, None, status="unavailable", unavailable_reason=reason),
            },
            "daily_trend": {},
            "version_health": {},
        }
    base_res["periods"] = periods_dict
    return base_res


def compute_daily_sessions(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Aggregates raw daily query rows into date-indexed daily sessions data."""
    result: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        d_str = str(r.get("date") or "")
        if not d_str:
            continue
        tot = int(r.get("sessions_total") or 0)
        cra = int(r.get("crashed_sessions") or 0)
        rate = calculate_crash_free_rate(tot, cra)
        if d_str in result:
            prev = result[d_str]
            new_tot = (prev["sessions_total"] or 0) + tot
            new_cra = (prev["crashed_sessions"] or 0) + cra
            result[d_str] = {
                "sessions_total": new_tot,
                "crashed_sessions": new_cra,
                "crash_free_sessions_rate": calculate_crash_free_rate(new_tot, new_cra),
            }
        else:
            result[d_str] = {
                "sessions_total": tot,
                "crashed_sessions": cra,
                "crash_free_sessions_rate": rate,
            }
    return result


def compute_version_sessions(
    rows: List[Dict[str, Any]], overall_total_sessions: Optional[int] = None
) -> Dict[str, Dict[str, Any]]:
    """Aggregates version query rows into version-indexed health metrics."""
    result: Dict[str, Dict[str, Any]] = {}
    sum_sessions = overall_total_sessions or sum(int(r.get("sessions_total") or 0) for r in rows)

    for r in rows:
        ver = str(r.get("version") or "unknown").strip()
        if not ver:
            continue
        tot_sess = int(r.get("sessions_total") or 0)
        cra_sess = int(r.get("crashed_sessions") or 0)
        tot_usr = int(r.get("users_total") or 0)
        cra_usr = int(r.get("crashed_users") or 0)

        if ver in result:
            prev = result[ver]
            tot_sess += prev["sessions_total"]
            cra_sess += prev["crashed_sessions"]
            tot_usr += prev["users_total"]
            cra_usr += prev["crashed_users"]

        cf_sess_rate = calculate_crash_free_rate(tot_sess, cra_sess)
        cf_usr_rate = calculate_crash_free_rate(tot_usr, cra_usr)
        adoption = calculate_adoption_rate(tot_sess, sum_sessions)

        result[ver] = {
            "version": ver,
            "sessions_total": tot_sess,
            "crashed_sessions": cra_sess,
            "crash_free_sessions_rate": cf_sess_rate,
            "users_total": tot_usr,
            "crashed_users": cra_usr,
            "crash_free_users_rate": cf_usr_rate,
            "adoption_rate": adoption,
        }
    return result


# ---------------------------------------------------------------------------
# High-Level Fetch Operations
# ---------------------------------------------------------------------------

def fetch_sessions_data(
    project: str,
    dataset: str = DEFAULT_SESSIONS_DATASET,
    tables: Optional[List[str]] = None,
    days: int = 30,
    comparison_days: Optional[int] = None,
    client: Optional[Any] = None,
    crash_dataset: str = DEFAULT_CRASH_DATASET,
    crash_tables: Optional[List[str]] = None,
    app_config: Optional[dict] = None,
    periods: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Fetches Firebase Sessions data from BigQuery with graceful degradation."""
    if client is None:
        try:
            client = make_sessions_client(project)
        except Exception as exc:
            reason = f"BigQuery client initialization failed: {exc}"
            print(f"  [Sessions] {reason}", file=sys.stderr)
            return build_unavailable_sessions_result(reason, periods=periods)

    if tables is None:
        tables = list_session_tables(client, project, dataset, app_config=app_config)

    if not tables:
        reason = f"Firebase Sessions export table not found in dataset {project}.{dataset}"
        return build_unavailable_sessions_result(reason, periods=periods)

    now_utc_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    total_sessions_all = 0
    total_users_all = 0
    crashed_sessions_all = 0
    crashed_users_all = 0

    prev_total_sessions = 0
    prev_crashed_sessions = 0
    prev_total_users = 0
    prev_crashed_users = 0

    all_daily_rows: List[Dict[str, Any]] = []
    all_version_rows: List[Dict[str, Any]] = []
    queried_tables: List[str] = []

    try:
        for table in tables:
            sessions_fq = f"{project}.{dataset}.{table}"
            matching_crash_table = (
                f"{project}.{crash_dataset}.{table}"
                if (crash_tables and table in crash_tables)
                else (f"{project}.{crash_dataset}.{crash_tables[0]}" if (crash_tables and len(crash_tables) > 0) else f"{project}.{crash_dataset}.{table}")
            )

            # 1. KPI Joined Query
            kpi_sql = SQLS["kpi_joined"].format(
                sessions_table=sessions_fq, crash_table=matching_crash_table, days=days
            )
            kpi_rows = run_sessions_query(client, kpi_sql)

            if kpi_rows:
                r = kpi_rows[0]
                total_sessions_all += int(r.get("total_sessions") or 0)
                total_users_all += int(r.get("total_users") or 0)
                crashed_sessions_all += int(r.get("crashed_sessions") or 0)
                crashed_users_all += int(r.get("crashed_users") or 0)

            # 2. Daily Trend Query
            daily_sql = SQLS["daily_joined"].format(
                sessions_table=sessions_fq, crash_table=matching_crash_table, days=days
            )
            daily_rows = run_sessions_query(client, daily_sql)
            all_daily_rows.extend(daily_rows)

            # 3. Version Health Query
            ver_sql = SQLS["versions_joined"].format(
                sessions_table=sessions_fq, crash_table=matching_crash_table, days=days
            )
            ver_rows = run_sessions_query(client, ver_sql)
            all_version_rows.extend(ver_rows)

            # 4. Previous Period Comparison (獨立前一期間: [now - 2*days, now - days))
            if comparison_days:
                try:
                    prev_sql = SQLS["kpi_previous_joined"].format(
                        sessions_table=sessions_fq, crash_table=matching_crash_table, days=comparison_days
                    )
                    prev_rows = run_sessions_query(client, prev_sql)
                    if prev_rows:
                        pr = prev_rows[0]
                        prev_total_sessions += int(pr.get("prev_total_sessions") or 0)
                        prev_crashed_sessions += int(pr.get("prev_crashed_sessions") or 0)
                        prev_total_users += int(pr.get("prev_total_users") or 0)
                        prev_crashed_users += int(pr.get("prev_crashed_users") or 0)
                except Exception:
                    pass

            queried_tables.append(table)

    except Exception as exc:
        err_msg = f"Sessions query execution failed: {str(exc)[:500]}"
        print(f"  [Sessions] {err_msg}", file=sys.stderr)
        err_res = {
            "sources": {
                "status": "error",
                "last_sync_timestamp": None,
                "error_message": err_msg,
                "tables_queried": queried_tables or None,
            },
            "kpi": {
                "crash_free_users": build_crash_free_metric(None, None, status="error", unavailable_reason=err_msg),
                "crash_free_sessions": build_crash_free_metric(None, None, status="error", unavailable_reason=err_msg),
            },
            "daily_trend": {},
            "version_health": {},
        }
        if periods:
            err_res["periods"] = {str(p): dict(err_res) for p in periods}
        return err_res

    prev_user_rate = calculate_crash_free_rate(prev_total_users, prev_crashed_users) if prev_total_users > 0 else None
    prev_sess_rate = calculate_crash_free_rate(prev_total_sessions, prev_crashed_sessions) if prev_total_sessions > 0 else None

    daily_sessions = compute_daily_sessions(all_daily_rows)
    version_sessions = compute_version_sessions(all_version_rows, overall_total_sessions=total_sessions_all)

    primary_res = {
        "sources": {
            "status": "available",
            "last_sync_timestamp": now_utc_iso,
            "error_message": None,
            "tables_queried": queried_tables,
        },
        "kpi": {
            "crash_free_users": build_crash_free_metric(
                total_users_all, crashed_users_all, previous_rate=prev_user_rate, status="available"
            ),
            "crash_free_sessions": build_crash_free_metric(
                total_sessions_all, crashed_sessions_all, previous_rate=prev_sess_rate, status="available"
            ),
        },
        "daily_trend": daily_sessions,
        "version_health": version_sessions,
    }

    if periods:
        periods_dict = {}
        for p in periods:
            if p == days:
                periods_dict[str(p)] = {
                    "sources": primary_res["sources"],
                    "kpi": primary_res["kpi"],
                    "daily_trend": primary_res["daily_trend"],
                    "version_health": primary_res["version_health"],
                }
            else:
                p_res = fetch_sessions_data(
                    project=project,
                    dataset=dataset,
                    tables=tables,
                    days=p,
                    comparison_days=comparison_days,
                    client=client,
                    crash_dataset=crash_dataset,
                    crash_tables=crash_tables,
                    app_config=app_config,
                    periods=None,
                )
                periods_dict[str(p)] = p_res
        primary_res["periods"] = periods_dict

    return primary_res


def fetch_sessions_for_app(
    app_name: str,
    days: int = 30,
    comparison_days: Optional[int] = None,
    client: Optional[Any] = None,
) -> Dict[str, Any]:
    """Fetches Sessions data for a given app defined in apps.yaml."""
    app_cfg = get_app(app_name)
    if not is_sessions_enabled(app_cfg):
        return build_unavailable_sessions_result("Sessions 匯出已停用 (disabled in config)")

    project = app_cfg.get("firebase_project")
    if not project:
        return build_unavailable_sessions_result(f"firebase_project not configured for app '{app_name}'")

    dataset = app_cfg.get("sessions_dataset", DEFAULT_SESSIONS_DATASET)
    crash_dataset = app_cfg.get("bq_dataset", DEFAULT_CRASH_DATASET)

    crash_tables: Optional[List[str]] = None
    if client and list_crash_tables is not None:
        try:
            crash_tables = list_crash_tables(client, project, crash_dataset, app_config=app_cfg)
        except Exception:
            crash_tables = None

    supported_periods = sorted(list({7, 30, 90, days}))
    result = fetch_sessions_data(
        project=project,
        dataset=dataset,
        days=days,
        comparison_days=comparison_days,
        client=client,
        crash_dataset=crash_dataset,
        crash_tables=crash_tables,
        app_config=app_cfg,
        periods=supported_periods,
    )
    return result


# ---------------------------------------------------------------------------
# Schema V2 Dashboard Enrichment Helpers
# ---------------------------------------------------------------------------

def enrich_app_dashboard_with_sessions(app_data: Dict[str, Any], sessions_result: Dict[str, Any]) -> Dict[str, Any]:
    """Merges Sessions results into an existing AppDashboardV2Data dict.
    Strictly preserves Schema V2 contract compliance and graceful degradation.
    """
    src_sessions = sessions_result.get("sources", {})
    kpi_sessions = sessions_result.get("kpi", {})
    daily_sessions = sessions_result.get("daily_trend", {})
    version_sessions = sessions_result.get("version_health", {})

    status = src_sessions.get("status", "unavailable")
    is_available = (status == "available")

    # 1. Update SourcesAvailability
    if "sources" in app_data and isinstance(app_data["sources"], dict):
        app_data["sources"]["firebase_sessions"] = {
            "status": status,
            "last_sync_timestamp": src_sessions.get("last_sync_timestamp"),
            "error_message": src_sessions.get("error_message"),
        }

    # 2. Update Top-level KPI
    if "kpi" in app_data and isinstance(app_data["kpi"], dict):
        app_data["kpi"]["crash_free_users"] = kpi_sessions.get(
            "crash_free_users",
            build_crash_free_metric(None, None, status="unavailable", unavailable_reason=src_sessions.get("error_message")),
        )
        app_data["kpi"]["crash_free_sessions"] = kpi_sessions.get(
            "crash_free_sessions",
            build_crash_free_metric(None, None, status="unavailable", unavailable_reason=src_sessions.get("error_message")),
        )

    # 3. Update Top-level Daily Trend Sessions Points
    if "daily_trend" in app_data and isinstance(app_data["daily_trend"], list):
        for point in app_data["daily_trend"]:
            if not isinstance(point, dict):
                continue
            date_key = point.get("date")
            if is_available and date_key in daily_sessions:
                sess_info = daily_sessions[date_key]
                point["sessions_total"] = sess_info.get("sessions_total")
                point["crashed_sessions"] = sess_info.get("crashed_sessions")
                point["crash_free_sessions_rate"] = sess_info.get("crash_free_sessions_rate")
            else:
                point["sessions_total"] = None
                point["crashed_sessions"] = None
                point["crash_free_sessions_rate"] = None

    if "version_health" in app_data and isinstance(app_data["version_health"], list):
        for item in app_data["version_health"]:
            if not isinstance(item, dict):
                continue
            ver_key = item.get("version")
            if is_available and ver_key in version_sessions:
                v_info = version_sessions[ver_key]
                item["crash_free_users_rate"] = v_info.get("crash_free_users_rate")
                item["crash_free_sessions_rate"] = v_info.get("crash_free_sessions_rate")
                item["adoption_rate"] = v_info.get("adoption_rate")
            else:
                item["crash_free_users_rate"] = None
                item["crash_free_sessions_rate"] = None
                item["adoption_rate"] = None

    # 4. Multi-period Snapshot Enrichment
    if "periods" in app_data and isinstance(app_data["periods"], dict):
        sess_periods = sessions_result.get("periods") or {}
        for p_key, snap in app_data["periods"].items():
            if not isinstance(snap, dict):
                continue
            snap_sess = sess_periods.get(p_key)
            if snap_sess and snap_sess.get("sources", {}).get("status") == "available":
                snap_kpi_sess = snap_sess.get("kpi") or {}
                snap_daily_sess = snap_sess.get("daily_trend") or {}
                snap_ver_sess = snap_sess.get("version_health") or {}
                snap_avail = True
            elif str(p_key) == str(app_data.get("period", {}).get("days")) and is_available:
                snap_kpi_sess = kpi_sessions
                snap_daily_sess = daily_sessions
                snap_ver_sess = version_sessions
                snap_avail = True
            else:
                snap_kpi_sess = {}
                snap_daily_sess = {}
                snap_ver_sess = {}
                snap_avail = False

            if "kpi" in snap and isinstance(snap["kpi"], dict):
                snap["kpi"]["crash_free_users"] = snap_kpi_sess.get(
                    "crash_free_users",
                    build_crash_free_metric(None, None, status="unavailable", unavailable_reason=src_sessions.get("error_message") or "該時間範圍無獨立 Sessions 資料"),
                )
                snap["kpi"]["crash_free_sessions"] = snap_kpi_sess.get(
                    "crash_free_sessions",
                    build_crash_free_metric(None, None, status="unavailable", unavailable_reason=src_sessions.get("error_message") or "該時間範圍無獨立 Sessions 資料"),
                )

            if "daily_trend" in snap and isinstance(snap["daily_trend"], list):
                for point in snap["daily_trend"]:
                    if not isinstance(point, dict):
                        continue
                    date_key = point.get("date")
                    if snap_avail and date_key in snap_daily_sess:
                        sess_info = snap_daily_sess[date_key]
                        point["sessions_total"] = sess_info.get("sessions_total")
                        point["crashed_sessions"] = sess_info.get("crashed_sessions")
                        point["crash_free_sessions_rate"] = sess_info.get("crash_free_sessions_rate")
                    else:
                        point["sessions_total"] = None
                        point["crashed_sessions"] = None
                        point["crash_free_sessions_rate"] = None

            if "version_health" in snap and isinstance(snap["version_health"], list):
                for item in snap["version_health"]:
                    if not isinstance(item, dict):
                        continue
                    ver_key = item.get("version")
                    if snap_avail and ver_key in snap_ver_sess:
                        v_info = snap_ver_sess[ver_key]
                        item["crash_free_users_rate"] = v_info.get("crash_free_users_rate")
                        item["crash_free_sessions_rate"] = v_info.get("crash_free_sessions_rate")
                        item["adoption_rate"] = v_info.get("adoption_rate")
                    else:
                        item["crash_free_users_rate"] = None
                        item["crash_free_sessions_rate"] = None
                        item["adoption_rate"] = None

    return app_data


# ---------------------------------------------------------------------------
# CLI Main Function
# ---------------------------------------------------------------------------

def main() -> None:
    parser = app_argparser("查詢 Firebase Sessions BigQuery export")
    parser.add_argument("--sessions-dataset", default=DEFAULT_SESSIONS_DATASET, help="Firebase Sessions dataset 名稱")
    args = parser.parse_args()

    app_cfg = get_app(args.app)
    if not is_sessions_enabled(app_cfg):
        print(f"  （App「{args.app}」設定為停用 Sessions，略過 BigQuery 查詢）")
        result = build_unavailable_sessions_result("Sessions 匯出已停用 (disabled in config)")
        target_path = out_dir(args.app) / "sessions.json"
        write_json(target_path, result)

        v2_path = out_dir(args.app) / "dashboard_v2.json"
        if v2_path.exists():
            try:
                app_data = json.loads(v2_path.read_text(encoding="utf-8"))
                enriched = enrich_app_dashboard_with_sessions(app_data, result)
                write_json(v2_path, enriched)
                print(f"  ✓ 已更新 {v2_path.relative_to(ROOT)} Sessions 指標（標記為未開啟）")
            except Exception as e:
                print(f"  ⚠ 更新 dashboard_v2.json Sessions 指標失敗：{e}", file=sys.stderr)
        return

    project = app_cfg.get("firebase_project")
    if not project:
        sys.exit(f"[錯誤] apps.yaml 裡的「{args.app}」未設定 firebase_project")

    print(f"=== 正在查詢 Firebase Sessions ({project}.{args.sessions_dataset}) ===")
    supported_periods = sorted(list({7, 30, 90, args.days}))
    result = fetch_sessions_data(project=project, dataset=args.sessions_dataset, days=args.days, app_config=app_cfg, periods=supported_periods)

    target_path = out_dir(args.app) / "sessions.json"
    write_json(target_path, result)

    # 若已有 dashboard_v2.json，自動注入 Sessions 指標
    v2_path = out_dir(args.app) / "dashboard_v2.json"
    if v2_path.exists():
        try:
            app_data = json.loads(v2_path.read_text(encoding="utf-8"))
            enriched = enrich_app_dashboard_with_sessions(app_data, result)
            write_json(v2_path, enriched)
            print(f"  ✓ 已更新 {v2_path.relative_to(ROOT)} Sessions 指標")
        except Exception as e:
            print(f"  ⚠ 更新 dashboard_v2.json Sessions 指標失敗：{e}", file=sys.stderr)

    status = result["sources"]["status"]
    if status == "available":
        cf_users = result["kpi"]["crash_free_users"]
        cf_sessions = result["kpi"]["crash_free_sessions"]
        print(f"  ✓ Sessions 查詢成功 (Status: {status})")
        print(f"    Crash-free Users: {cf_users.get('rate') * 100:.2f}% (Total: {cf_users.get('total')}, Crashed: {cf_users.get('crashed')})")
        print(f"    Crash-free Sessions: {cf_sessions.get('rate') * 100:.2f}% (Total: {cf_sessions.get('total')}, Crashed: {cf_sessions.get('crashed')})")
    else:
        print(f"  ⚠ Sessions 狀態: {status} (Reason: {result['sources'].get('error_message')})")


if __name__ == "__main__":
    main()
