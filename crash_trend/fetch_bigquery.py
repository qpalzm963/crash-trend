"""Crashlytics BigQuery export 查詢（google-cloud-bigquery，免裝 gcloud SDK）。

Dashboard V2 重構：
- 獨立 Overview 聚合查詢：COUNT(*) 全量事件、COUNT(DISTINCT installation_uuid) 全量去重用戶、COUNTIF 各錯誤類型
- 每日趨勢 (Daily Trend)：依 FORMAT_TIMESTAMP('%Y-%m-%d', event_timestamp) 聚合每日 events, users, fatal, anr, non_fatal
- Top Issues 聚合：依 Crashlytics 真實 schema（exceptions / error / threads）跨平台 COALESCE 提取 title / subtitle / error_type / ISO 8601 UTC 時間戳與 version_distribution
- App 表過濾：鎖定當前 App 對應的 Crashlytics 表名，找不到時回傳空陣列，防範 multi-app 專案資料污染
- 時間窗口對齊：全面以 calendar-day boundary (DATE_SUB(CURRENT_DATE(), INTERVAL {days - 1} DAY)) 對齊 SQL 與 Python buckets
- New Issues 計算：以 MIN(event_timestamp) 判定首次出現時間
- 維度分布 (Distributions)：platform, device_models, os_versions, app_versions, custom_keys
- 資料契約整合：提供 transform_bq_to_v2 產生符合 Dashboard V2 Data Schema 的標準資料
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from google.cloud import bigquery
except ImportError:
    bigquery = None  # type: ignore

try:
    from crash_trend.config import ROOT, app_argparser, get_app, load_config, out_dir, write_json
    from crash_trend.schema_v2 import (
        AppDashboardV2Data,
        AppMetadata,
        AppVersionDistItem,
        CustomKeyDistributionItem,
        DailyTrendPoint,
        DeviceDistItem,
        Distributions,
        EventsByErrorType,
        IssueSummary,
        KPIMetric,
        OSDistItem,
        OverviewKPI,
        PeriodInfo,
        PlatformDistItem,
        SourcesAvailability,
        SourceStatus,
        VersionDistCount,
        VersionHealthItem,
        is_valid_date,
        is_valid_iso8601_utc,
        validate_app_dashboard_v2,
    )
    from crash_trend.versions import max_version, min_version, version_key
except ImportError:
    from config import ROOT, app_argparser, get_app, load_config, out_dir, write_json
    from schema_v2 import (
        AppDashboardV2Data,
        AppMetadata,
        AppVersionDistItem,
        CustomKeyDistributionItem,
        DailyTrendPoint,
        DeviceDistItem,
        Distributions,
        EventsByErrorType,
        IssueSummary,
        KPIMetric,
        OSDistItem,
        OverviewKPI,
        PeriodInfo,
        PlatformDistItem,
        SourcesAvailability,
        SourceStatus,
        VersionDistCount,
        VersionHealthItem,
        is_valid_date,
        is_valid_iso8601_utc,
        validate_app_dashboard_v2,
    )
    from versions import max_version, min_version, version_key


# ---------------------------------------------------------------------------
# BigQuery SQL Query Templates (V2 - 對齊 Firebase Crashlytics 真實 Schema)
# ---------------------------------------------------------------------------

SQLS: Dict[str, str] = {
    # 1. 獨立 Overview 聚合查詢（期間內全量計數與全量去重用戶，對齊日曆日邊界）
    "overview": """
        SELECT
            COUNT(*) AS total_events,
            COUNT(DISTINCT installation_uuid) AS distinct_users,
            COUNTIF(UPPER(error_type) = 'FATAL') AS fatal_events,
            COUNTIF(UPPER(error_type) = 'ANR') AS anr_events,
            COUNTIF(UPPER(error_type) NOT IN ('FATAL', 'ANR') OR error_type IS NULL) AS non_fatal_events
        FROM `{table}`
        WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
          AND event_timestamp < TIMESTAMP_ADD(TIMESTAMP(CURRENT_DATE()), INTERVAL 1 DAY)""",
    # 2. 每日趨勢（依 YYYY-MM-DD 聚合每日 events, users, fatal, anr, non_fatal）
    "daily_trend": """
        SELECT
            FORMAT_TIMESTAMP('%Y-%m-%d', event_timestamp) AS date,
            COUNT(*) AS events,
            COUNT(DISTINCT installation_uuid) AS users,
            COUNTIF(UPPER(error_type) = 'FATAL') AS fatal_events,
            COUNTIF(UPPER(error_type) = 'ANR') AS anr_events,
            COUNTIF(UPPER(error_type) NOT IN ('FATAL', 'ANR') OR error_type IS NULL) AS non_fatal_events
        FROM `{table}`
        WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
          AND event_timestamp < TIMESTAMP_ADD(TIMESTAMP(CURRENT_DATE()), INTERVAL 1 DAY)
        GROUP BY 1
        ORDER BY date ASC""",
    # 3. Top Issues 聚合（跨平台 COALESCE exceptions / error / threads 提取 title/subtitle）
    "top_issues": """
        SELECT
            issue_id,
            COALESCE(
                exceptions[SAFE_OFFSET(0)].type,
                error[SAFE_OFFSET(0)].title,
                threads[SAFE_OFFSET(0)].title,
                'Unknown Error'
            ) AS issue_title,
            COALESCE(
                exceptions[SAFE_OFFSET(0)].frames[SAFE_OFFSET(0)].symbol,
                error[SAFE_OFFSET(0)].subtitle,
                threads[SAFE_OFFSET(0)].subtitle,
                ''
            ) AS issue_subtitle,
            error_type,
            COUNT(*) AS events,
            COUNT(DISTINCT installation_uuid) AS users,
            FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', MIN(event_timestamp)) AS first_seen_timestamp,
            FORMAT_TIMESTAMP('%Y-%m-%dT%H:%M:%SZ', MAX(event_timestamp)) AS last_seen_timestamp,
            MIN(application.display_version) AS first_seen_version,
            MAX(application.display_version) AS last_seen_version
        FROM `{table}`
        WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
          AND event_timestamp < TIMESTAMP_ADD(TIMESTAMP(CURRENT_DATE()), INTERVAL 1 DAY)
        GROUP BY 1, 2, 3, 4
        ORDER BY events DESC
        LIMIT 50""",
    # 4. 新增問題數 (New Issues)：以可用歷史中 MIN(event_timestamp) 落在當期者判定
    "new_issues": """
        WITH issue_first_seen AS (
            SELECT
                issue_id,
                MIN(event_timestamp) AS first_seen
            FROM `{table}`
            GROUP BY issue_id
        )
        SELECT
            COUNTIF(first_seen >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))) AS new_issues_count
        FROM issue_first_seen""",
    # 5. 逐 Issue 版本分布（建立 version_distribution 與 semver 範圍重算）
    "issue_versions": """
        SELECT
            issue_id,
            application.display_version AS app_version,
            COUNT(*) AS events,
            COUNT(DISTINCT installation_uuid) AS users
        FROM `{table}`
        WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
          AND event_timestamp < TIMESTAMP_ADD(TIMESTAMP(CURRENT_DATE()), INTERVAL 1 DAY)
        GROUP BY 1, 2
        ORDER BY events DESC
        LIMIT 500""",
    # 6. 維度分布：機型
    "by_device": """
        SELECT
            device.model AS device_model,
            COUNT(*) AS events,
            COUNT(DISTINCT installation_uuid) AS users
        FROM `{table}`
        WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
          AND event_timestamp < TIMESTAMP_ADD(TIMESTAMP(CURRENT_DATE()), INTERVAL 1 DAY)
        GROUP BY 1
        ORDER BY events DESC
        LIMIT 30""",
    # 7. 維度分布：作業系統
    "by_os": """
        SELECT
            operating_system.display_version AS os_version,
            COUNT(*) AS events,
            COUNT(DISTINCT installation_uuid) AS users
        FROM `{table}`
        WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
          AND event_timestamp < TIMESTAMP_ADD(TIMESTAMP(CURRENT_DATE()), INTERVAL 1 DAY)
        GROUP BY 1
        ORDER BY events DESC
        LIMIT 30""",
    # 8. 維度分布：App 版本
    "by_app_version": """
        SELECT
            application.display_version AS app_version,
            COUNT(*) AS events,
            COUNT(DISTINCT installation_uuid) AS users
        FROM `{table}`
        WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
          AND event_timestamp < TIMESTAMP_ADD(TIMESTAMP(CURRENT_DATE()), INTERVAL 1 DAY)
        GROUP BY 1
        ORDER BY events DESC
        LIMIT 30""",
}


def build_custom_keys_sql(table: str, days: int, keys: List[str]) -> Optional[str]:
    """動態組裝自訂 keys 分布查詢，防範非法字元注入。"""
    valid_keys = [k for k in keys if re.fullmatch(r"[\w-]+", k)]
    if not valid_keys:
        return None
    key_list = ", ".join(f"'{k}'" for k in valid_keys)
    return f"""
        SELECT
            key.key AS custom_key,
            key.value AS value,
            COUNT(*) AS events
        FROM `{table}`, UNNEST(custom_keys) AS key
        WHERE event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
          AND event_timestamp < TIMESTAMP_ADD(TIMESTAMP(CURRENT_DATE()), INTERVAL 1 DAY)
          AND key.key IN ({key_list})
        GROUP BY 1, 2
        ORDER BY events DESC
        LIMIT 60"""


# ---------------------------------------------------------------------------
# Timestamp & Data Formatting Helpers
# ---------------------------------------------------------------------------

def format_iso_utc(ts: Any, fallback: Optional[str] = None) -> str:
    """轉換任何輸入時間型態為嚴格 ISO 8601 UTC 字串（結尾為 Z）。"""
    if ts is None:
        if fallback and is_valid_iso8601_utc(fallback):
            return fallback
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if isinstance(ts, dt.datetime):
        if ts.tzinfo is None:
            ts_utc = ts.replace(tzinfo=dt.timezone.utc)
        else:
            ts_utc = ts.astimezone(dt.timezone.utc)
        return ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    if isinstance(ts, dt.date):
        return dt.datetime(ts.year, ts.month, ts.day, tzinfo=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if isinstance(ts, str):
        s = ts.strip()
        if is_valid_iso8601_utc(s):
            return s[:-6] + "Z" if s.endswith("+00:00") else s

        clean = s.replace(" UTC", "+00:00").replace(" ", "T")
        try:
            parsed = dt.datetime.fromisoformat(clean)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            else:
                parsed = parsed.astimezone(dt.timezone.utc)
            return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            pass

    if fallback and is_valid_iso8601_utc(fallback):
        return fallback
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_platform_from_table(table_name: str) -> str:
    """由 BigQuery 表名推斷平台（ios / android）。"""
    t = table_name.upper()
    if t.endswith("_IOS") or "_IOS_" in t or t.endswith("IOS"):
        return "ios"
    if t.endswith("_ANDROID") or "_ANDROID_" in t or t.endswith("ANDROID"):
        return "android"
    return "ios" if "ios" in table_name.lower() else "android"


def norm_error_type(raw: Any, fatal_hint: bool = False) -> str:
    """error_type 收斂三值 enum（FATAL / ANR / NON_FATAL）。"""
    et = (str(raw) if raw is not None else "").upper().replace("-", "_").strip()
    if et in ("FATAL", "ANR", "NON_FATAL"):
        return et
    return "FATAL" if fatal_hint else "NON_FATAL"


# ---------------------------------------------------------------------------
# BigQuery Client & Execution
# ---------------------------------------------------------------------------

def make_client(project: str) -> bigquery.Client:
    creds_cfg = (load_config().get("credentials") or {})
    sa_path = creds_cfg.get("bq_service_account")
    if sa_path:
        sa_file = Path(sa_path).expanduser()
        if not sa_file.exists():
            sys.exit(f"[錯誤] credentials.bq_service_account 指定的檔案不存在：{sa_file}")
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(str(sa_file))
        return bigquery.Client(project=project, credentials=creds)
    return bigquery.Client(project=project)  # ADC


def list_crash_tables(
    client: bigquery.Client,
    project: str,
    dataset: str,
    app_config: Optional[dict] = None,
) -> List[str]:
    """列出 Crashlytics 批次表，並根據 App 設定過濾，避免跨 App 混淆。找不到時回傳空陣列。"""
    tables = [t.table_id for t in client.list_tables(f"{project}.{dataset}")]
    batch_tables = [t for t in tables if not t.endswith("_REALTIME")]

    if not app_config:
        return batch_tables

    explicit_tables = app_config.get("tables")
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
        # 嚴格過濾：有設定 filter 但找不到 match 時回傳空陣列，絕不 silent fallback all
        return matched

    return batch_tables


def run_query(client: bigquery.Client, sql: str) -> List[dict]:
    rows = client.query(sql).result(max_results=500)
    out: List[dict] = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, (dt.datetime, dt.date)):
                d[k] = format_iso_utc(v)
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# BigQuery Raw Data -> Dashboard V2 Data Schema 轉換
# ---------------------------------------------------------------------------

def transform_bq_to_v2(
    bq_result: dict,
    app_config: dict,
    days: int = 30,
    end_time: Optional[dt.datetime] = None,
) -> AppDashboardV2Data:
    """把 BigQuery 查詢結果字典轉換為嚴格符合 Schema V2 的 AppDashboardV2Data。"""
    end_dt = end_time.astimezone(dt.timezone.utc) if end_time else dt.datetime.now(dt.timezone.utc)
    end_date = end_dt.date()
    start_date = end_date - dt.timedelta(days=days - 1)
    start_time_iso = f"{start_date.isoformat()}T00:00:00Z"
    end_time_iso = f"{end_date.isoformat()}T23:59:59Z"
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    app_id = str(app_config.get("app_id") or app_config.get("name") or "app")
    display_name = str(app_config.get("display_name") or app_id)
    firebase_project_id = str(app_config.get("firebase_project") or app_config.get("firebase_project_id") or "")
    source_repo = app_config.get("source_repo")
    custom_keys_monitored = list(app_config.get("custom_keys") or [])

    tables_data: Dict[str, dict] = (bq_result or {}).get("tables") or {}
    queried_tables = list(tables_data.keys())

    detected_platforms = sorted(list({extract_platform_from_table(t) for t in queried_tables}))
    if not detected_platforms:
        detected_platforms = [p for p in app_config.get("platforms", ["android"]) if p in ("ios", "android")]
    if not detected_platforms:
        detected_platforms = ["android"]

    metadata: AppMetadata = {
        "app_id": app_id,
        "display_name": display_name,
        "firebase_project_id": firebase_project_id,
        "platforms": detected_platforms,  # type: ignore
        "source_repo": str(source_repo) if source_repo else None,
        "custom_keys_monitored": custom_keys_monitored,
    }

    period: PeriodInfo = {
        "days": days,
        "start_time": start_time_iso,
        "end_time": end_time_iso,
        "comparison_period": None,
    }

    bq_status = "available" if queried_tables else ("error" if bq_result.get("errors") else "unavailable")
    sources: SourcesAvailability = {
        "crashlytics_bq": {
            "status": bq_status,  # type: ignore
            "tables_queried": queried_tables,
            "last_sync_timestamp": now_iso if bq_status == "available" else None,
            "error_message": str(bq_result.get("errors"))[:500] if bq_result.get("errors") else None,
        },
        "firebase_sessions": {
            "status": "unavailable",
            "last_sync_timestamp": None,
            "error_message": "Firebase Sessions export not configured",
        },
        "mcp_crashlytics": {
            "status": "unavailable",
            "last_sync_timestamp": None,
            "error_message": None,
        },
        "gemini_ai": {
            "status": "unavailable",
            "model": None,
            "last_sync_timestamp": None,
            "error_message": None,
        },
    }

    # 日期序列精確對齊日曆日（長度恰為 days）
    date_keys = [(start_date + dt.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

    daily_buckets: Dict[str, dict] = {
        d: {
            "crash_events": 0,
            "affected_users": 0,
            "fatal_events": 0,
            "anr_events": 0,
            "non_fatal_events": 0,
            "by_platform": {p: {"events": 0, "users": 0} for p in detected_platforms},
        }
        for d in date_keys
    }

    overview_total_events = 0
    overview_total_users = 0
    overview_fatal = 0
    overview_anr = 0
    overview_non_fatal = 0
    calculated_new_issues = 0
    has_new_issues_data = False

    platform_stats: Dict[str, Dict[str, int]] = {p: {"events": 0, "users": 0} for p in detected_platforms}
    raw_issues: List[dict] = []
    raw_devices: List[dict] = []
    raw_os: List[dict] = []
    raw_apps: List[dict] = []
    raw_custom_keys: List[dict] = []
    ver_by_issue: Dict[str, List[dict]] = {}

    for table_name, t_data in tables_data.items():
        platform = extract_platform_from_table(table_name)
        if platform not in platform_stats:
            platform_stats[platform] = {"events": 0, "users": 0}

        ov_rows = t_data.get("overview") or []
        t_events, t_users, t_fatal, t_anr, t_nonfatal = 0, 0, 0, 0, 0
        if ov_rows:
            ov = ov_rows[0]
            t_events = int(ov.get("total_events") or 0)
            t_users = int(ov.get("distinct_users") or 0)
            t_fatal = int(ov.get("fatal_events") or 0)
            t_anr = int(ov.get("anr_events") or 0)
            t_nonfatal = int(ov.get("non_fatal_events") or 0)
        else:
            for dr in t_data.get("daily_trend") or []:
                t_events += int(dr.get("events") or 0)
                t_users += int(dr.get("users") or 0)
                t_fatal += int(dr.get("fatal_events") or 0)
                t_anr += int(dr.get("anr_events") or 0)
                t_nonfatal += int(dr.get("non_fatal_events") or 0)

        overview_total_events += t_events
        overview_total_users += t_users
        overview_fatal += t_fatal
        overview_anr += t_anr
        overview_non_fatal += t_nonfatal

        platform_stats[platform]["events"] += t_events
        platform_stats[platform]["users"] += t_users

        new_iss_rows = t_data.get("new_issues") or []
        if new_iss_rows:
            calculated_new_issues += int(new_iss_rows[0].get("new_issues_count") or 0)
            has_new_issues_data = True

        for dr in t_data.get("daily_trend") or []:
            d_str = dr.get("date")
            if d_str in daily_buckets:
                ev = int(dr.get("events") or 0)
                us = int(dr.get("users") or 0)
                fat = int(dr.get("fatal_events") or 0)
                anr = int(dr.get("anr_events") or 0)
                nfat = int(dr.get("non_fatal_events") or 0)
                if fat + anr + nfat != ev:
                    nfat = max(0, ev - fat - anr)

                bucket = daily_buckets[d_str]
                bucket["crash_events"] += ev
                bucket["affected_users"] += us
                bucket["fatal_events"] += fat
                bucket["anr_events"] += anr
                bucket["non_fatal_events"] += nfat
                if platform in bucket["by_platform"]:
                    bucket["by_platform"][platform]["events"] += ev
                    bucket["by_platform"][platform]["users"] += us

        for iv in t_data.get("issue_versions") or []:
            iid = str(iv.get("issue_id") or "")
            if iid:
                ver_by_issue.setdefault(iid, []).append({
                    "version": str(iv.get("app_version") or "1.0.0"),
                    "events": int(iv.get("events") or 0),
                    "users": int(iv.get("users") or 0),
                })

        for it in t_data.get("top_issues") or []:
            raw_issues.append({**it, "_platform": platform})

        for dev in t_data.get("by_device") or []:
            raw_devices.append({**dev, "_platform": platform})

        for os_row in t_data.get("by_os") or []:
            raw_os.append({**os_row, "_platform": platform})

        for app_ver in t_data.get("by_app_version") or []:
            raw_apps.append({**app_ver, "_platform": platform})

        for ck in t_data.get("custom_keys") or []:
            raw_custom_keys.append({**ck, "_platform": platform})

    daily_trend: List[DailyTrendPoint] = []
    for d_str in date_keys:
        b = daily_buckets[d_str]
        ev = b["crash_events"]
        fat = b["fatal_events"]
        anr = b["anr_events"]
        nfat = b["non_fatal_events"]
        if fat + anr + nfat != ev:
            nfat = max(0, ev - fat - anr)

        daily_trend.append({
            "date": d_str,
            "crash_events": ev,
            "affected_users": b["affected_users"],
            "fatal_events": fat,
            "anr_events": anr,
            "non_fatal_events": nfat,
            "sessions_total": None,
            "crashed_sessions": None,
            "crash_free_sessions_rate": None,
            "by_platform": b["by_platform"],
        })

    daily_sum_events = sum(d["crash_events"] for d in daily_trend)
    daily_sum_fatal = sum(d["fatal_events"] for d in daily_trend)
    daily_sum_anr = sum(d["anr_events"] for d in daily_trend)
    daily_sum_non_fatal = sum(d["non_fatal_events"] for d in daily_trend)

    if overview_total_events == 0 and daily_sum_events > 0:
        overview_total_events = daily_sum_events
        overview_fatal = daily_sum_fatal
        overview_anr = daily_sum_anr
        overview_non_fatal = daily_sum_non_fatal
    elif daily_sum_events == overview_total_events:
        overview_fatal = daily_sum_fatal
        overview_anr = daily_sum_anr
        overview_non_fatal = daily_sum_non_fatal

    if overview_fatal + overview_anr + overview_non_fatal != overview_total_events:
        overview_non_fatal = max(0, overview_total_events - overview_fatal - overview_anr)

    new_issues_metric: KPIMetric
    if has_new_issues_data:
        new_issues_metric = {
            "value": calculated_new_issues,
            "previous_value": None,
            "change_pct": None,
            "status": "available",
        }
    else:
        new_issues_metric = {
            "value": 0,
            "previous_value": None,
            "change_pct": None,
            "status": "insufficient_data",
        }

    kpi: OverviewKPI = {
        "crash_events": {
            "value": overview_total_events,
            "previous_value": None,
            "change_pct": None,
            "status": "available",
        },
        "affected_users": {
            "value": overview_total_users,
            "previous_value": None,
            "change_pct": None,
            "status": "available",
        },
        "crash_free_users": {
            "rate": None,
            "total": None,
            "crashed": None,
            "previous_rate": None,
            "change_pct_points": None,
            "status": "unavailable",
            "unavailable_reason": "Firebase Sessions export 未開啟",
        },
        "crash_free_sessions": {
            "rate": None,
            "total": None,
            "crashed": None,
            "previous_rate": None,
            "change_pct_points": None,
            "status": "unavailable",
            "unavailable_reason": "Firebase Sessions export 未開啟",
        },
        "new_issues_count": new_issues_metric,
        "events_by_error_type": {
            "fatal": overview_fatal,
            "anr": overview_anr,
            "non_fatal": overview_non_fatal,
        },
    }

    seen_issues: Dict[str, IssueSummary] = {}

    for it in raw_issues:
        iid = str(it.get("issue_id") or "")
        if not iid:
            continue
        platform = it.get("_platform", "android")
        v_dist_raw = ver_by_issue.get(iid, [])
        v_dist: List[VersionDistCount] = sorted(
            v_dist_raw, key=lambda x: version_key(x["version"]), reverse=True
        )
        versions = [x["version"] for x in v_dist if x.get("version")]
        first_ver = min_version(versions) or str(it.get("first_seen_version") or "1.0.0")
        last_ver = max_version(versions) or str(it.get("last_seen_version") or "1.0.0")

        first_seen = format_iso_utc(it.get("first_seen_timestamp"), fallback=start_time_iso)
        last_seen = format_iso_utc(it.get("last_seen_timestamp"), fallback=end_time_iso)
        err_type = norm_error_type(
            it.get("error_type"),
            fatal_hint=(str(it.get("error_type") or "").upper() == "FATAL"),
        )

        title = str(it.get("issue_title") or it.get("title") or "Unknown Issue")
        subtitle = str(it.get("issue_subtitle") or it.get("subtitle") or "")

        iss_entry: IssueSummary = {
            "issue_id": iid,
            "platform": "ios" if platform == "ios" else "android",
            "title": title,
            "subtitle": subtitle,
            "error_type": err_type,  # type: ignore
            "priority": {
                "score": 0,
                "level": "P2",
                "trend": "stable",
                "score_breakdown": None,
            },
            "events": int(it.get("events") or 0),
            "affected_users": int(it.get("users") or 0),
            "first_seen_timestamp": first_seen,
            "last_seen_timestamp": last_seen,
            "first_seen_version": first_ver,
            "last_seen_version": last_ver,
            "version_distribution": v_dist,
            "blame_frame": None,
            "ai_analysis": {
                "status": "unavailable",
                "root_cause": None,
                "suggested_fix": None,
                "effort": None,
                "confidence": None,
                "reasoning_sources": None,
            },
            "detail": None,
        }
        if iid in seen_issues:
            existing = seen_issues[iid]
            existing["events"] += iss_entry["events"]
            existing["affected_users"] += iss_entry["affected_users"]
            existing["version_distribution"].extend(iss_entry["version_distribution"])
        else:
            seen_issues[iid] = iss_entry

    top_issues = sorted(seen_issues.values(), key=lambda x: (-x["events"], -x["affected_users"]))[:50]

    platform_dist: List[PlatformDistItem] = []
    for p_name in detected_platforms:
        p_stat = platform_stats.get(p_name, {"events": 0, "users": 0})
        p_ev = p_stat["events"]
        p_share = round(p_ev / overview_total_events, 4) if overview_total_events > 0 else 0.0
        platform_dist.append({
            "name": "ios" if p_name == "ios" else "android",
            "events": p_ev,
            "users": p_stat["users"],
            "share": min(1.0, max(0.0, p_share)),
        })

    device_models: List[DeviceDistItem] = []
    for dev in sorted(raw_devices, key=lambda x: -int(x.get("events") or 0))[:30]:
        dev_ev = int(dev.get("events") or 0)
        dev_share = round(dev_ev / overview_total_events, 4) if overview_total_events > 0 else 0.0
        pf = dev.get("_platform", "all")
        device_models.append({
            "model": str(dev.get("device_model") or "Unknown"),
            "platform": pf if pf in ("ios", "android") else "all",  # type: ignore
            "events": dev_ev,
            "users": int(dev.get("users") or 0),
            "share": min(1.0, max(0.0, dev_share)),
        })

    os_versions: List[OSDistItem] = []
    for os_r in sorted(raw_os, key=lambda x: -int(x.get("events") or 0))[:30]:
        os_ev = int(os_r.get("events") or 0)
        os_share = round(os_ev / overview_total_events, 4) if overview_total_events > 0 else 0.0
        pf = os_r.get("_platform", "all")
        os_versions.append({
            "os_version": str(os_r.get("os_version") or "Unknown"),
            "platform": pf if pf in ("ios", "android") else "all",  # type: ignore
            "events": os_ev,
            "users": int(os_r.get("users") or 0),
            "share": min(1.0, max(0.0, os_share)),
        })

    app_versions: List[AppVersionDistItem] = []
    for app_r in sorted(raw_apps, key=lambda x: -int(x.get("events") or 0))[:30]:
        app_ev = int(app_r.get("events") or 0)
        app_share = round(app_ev / overview_total_events, 4) if overview_total_events > 0 else 0.0
        pf = app_r.get("_platform", "all")
        app_versions.append({
            "app_version": str(app_r.get("app_version") or "Unknown"),
            "platform": pf if pf in ("ios", "android") else "all",  # type: ignore
            "events": app_ev,
            "users": int(app_r.get("users") or 0),
            "share": min(1.0, max(0.0, app_share)),
        })

    custom_keys: List[CustomKeyDistributionItem] = []
    for ck_r in sorted(raw_custom_keys, key=lambda x: -int(x.get("events") or 0))[:60]:
        custom_keys.append({
            "key": str(ck_r.get("custom_key") or ""),
            "value": str(ck_r.get("value") or ""),
            "platform": str(ck_r.get("_platform") or "all"),
            "events": int(ck_r.get("events") or 0),
        })

    distributions: Distributions = {
        "platform": platform_dist,
        "device_models": device_models,
        "os_versions": os_versions,
        "app_versions": app_versions,
        "custom_keys": custom_keys,
    }

    version_health: List[VersionHealthItem] = []
    sorted_app_vers = sorted(raw_apps, key=lambda x: version_key(str(x.get("app_version") or "")), reverse=True)
    for idx, v_item in enumerate(sorted_app_vers[:20]):
        v_name = str(v_item.get("app_version") or "1.0.0")
        pf = v_item.get("_platform", "all")
        version_health.append({
            "version": v_name,
            "platform": pf if pf in ("ios", "android") else "all",  # type: ignore
            "release_date": None,
            "crash_events": int(v_item.get("events") or 0),
            "affected_users": int(v_item.get("users") or 0),
            "crash_free_users_rate": None,
            "crash_free_sessions_rate": None,
            "adoption_rate": None,
            "status": "latest" if idx == 0 else "active",
            "trend": "stable",
        })

    ai_summary = {
        "status": "unavailable",
        "model": None,
        "generated_at": None,
        "overview": "",
        "key_takeaways": [],
        "distribution_insights": "",
        "recommended_actions": [],
        "data_limitations": None,
    }

    limitations = [
        "New Issues 計算以 BigQuery 現存資料之 MIN(event_timestamp) 判定",
    ]

    result_data: AppDashboardV2Data = {
        "metadata": metadata,
        "period": period,
        "sources": sources,
        "kpi": kpi,
        "daily_trend": daily_trend,
        "version_health": version_health,
        "distributions": distributions,
        "top_issues": top_issues,
        "ai_summary": ai_summary,  # type: ignore
        "limitations": limitations,
    }

    return result_data


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    args = app_argparser("查詢 Crashlytics BigQuery export").parse_args()
    app = get_app(args.app)
    project = app["firebase_project"]
    dataset = app.get("bq_dataset", "firebase_crashlytics")
    result: dict = {"project": project, "dataset": dataset, "tables": {}, "errors": {}}

    try:
        client = make_client(project)
        tables = list_crash_tables(client, project, dataset, app_config={**app, "app_id": args.app})
    except Exception as e:
        write_json(out_dir(args.app) / "crashlytics_bq.json", {**result, "errors": {"dataset": str(e)[:800]}})
        app_v2_data = transform_bq_to_v2(result, {**app, "app_id": args.app}, days=args.days)
        app_v2_data["sources"]["crashlytics_bq"] = {
            "status": "error",
            "last_sync_timestamp": None,
            "error_message": str(e)[:400],
        }
        write_json(out_dir(args.app) / "dashboard_v2.json", app_v2_data)
        sys.exit(
            f"[注意] 無法列出 {project}:{dataset} —— 尚未連結 BigQuery export、無資料，或憑證問題。\n"
            f"  憑證設定：apps.yaml 填 credentials.bq_service_account（SA json 路徑），"
            f"或本機跑一次 `gcloud auth application-default login`。\n"
            f"  {str(e)[:400]}"
        )

    if not tables:
        write_json(out_dir(args.app) / "crashlytics_bq.json", result)
        app_v2_data = transform_bq_to_v2(result, {**app, "app_id": args.app}, days=args.days)
        app_v2_data["sources"]["crashlytics_bq"] = {
            "status": "unavailable",
            "last_sync_timestamp": None,
            "error_message": f"未找到符合 App {args.app} 的 Crashlytics 批次表",
        }
        write_json(out_dir(args.app) / "dashboard_v2.json", app_v2_data)
        sys.exit(f"[注意] 沒有找到符合 App {args.app} 的 Crashlytics 批次表")

    sqls = dict(SQLS)
    keys = [k for k in app.get("custom_keys", []) if re.fullmatch(r"[\w-]+", k)]

    for table in tables:
        fq = f"{project}.{dataset}.{table}"
        result["tables"][table] = {}

        table_sqls = dict(sqls)
        if keys:
            ck_sql = build_custom_keys_sql(fq, args.days, keys)
            if ck_sql:
                table_sqls["custom_keys"] = ck_sql

        for name, sql in table_sqls.items():
            formatted_sql = sql.format(table=fq, days=args.days) if "{table}" in sql else sql
            try:
                result["tables"][table][name] = run_query(client, formatted_sql)
                print(f"  ✓ {table}.{name}: {len(result['tables'][table][name])} 列")
            except Exception as e:
                # 防禦性重試：官方標準 schema 為 Apple error (單數)；若個別專案之資料表欄位名為複數 errors，作為 secondary fallback 重試相容
                if "Unrecognized name: error" in str(e) and "error[" in formatted_sql:
                    try:
                        retry_sql = formatted_sql.replace("error[SAFE_OFFSET(0)]", "errors[SAFE_OFFSET(0)]")
                        result["tables"][table][name] = run_query(client, retry_sql)
                        print(f"  ✓ {table}.{name} (防禦性相容 errors 重試成功): {len(result['tables'][table][name])} 列")
                        continue
                    except Exception:
                        pass
                result["errors"][f"{table}.{name}"] = str(e)[:800]
                print(f"  ⚠ {table}.{name} 失敗：{str(e)[:200]}", file=sys.stderr)

    write_json(out_dir(args.app) / "crashlytics_bq.json", result)

    app_v2_data = transform_bq_to_v2(result, {**app, "app_id": args.app}, days=args.days)
    val_errors = validate_app_dashboard_v2(app_v2_data)
    if val_errors:
        print(f"  [警告] Schema V2 驗證出現 {len(val_errors)} 個錯誤：", file=sys.stderr)
        for ve in val_errors[:5]:
            print(f"    - {ve}", file=sys.stderr)
    else:
        print(f"  ✓ AppDashboardV2Data 轉換完成並通過 Schema V2 驗證")

    write_json(out_dir(args.app) / "dashboard_v2.json", app_v2_data)

    if result["errors"]:
        sys.exit("[注意] 部分查詢失敗（詳見輸出 errors 欄位）")


if __name__ == "__main__":
    main()
