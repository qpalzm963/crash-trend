"""Crashlytics Issue Detail Data Fetcher & Blame Frame Analyzer.

This module provides issue-level diagnostics for Dashboard V2:
- Top-level `blame_frame` parsing with multi-tiered priority:
  1. Official top-level `blame_frame`
  2. Frame with `blamed: true` / `is_blame: true` in exceptions / error / threads
  3. Non-system frame heuristic fallback
  4. Top frame fallback
- Exception, error (Apple), and thread stack trace formatting with line limits
- Breadcrumbs and logs collection supporting both BQ schema (name/params/timestamp) and MCP schema (category/message/timestamp)
- Supplemental merge between BigQuery export and MCP/cache (e.g. BQ provides stack trace, MCP provides breadcrumbs)
- Custom keys, top devices, and top OS distributions
- Graceful degradation: individual query failures degrade to detail: null or blame_frame: null
- Full conformance with docs/dashboard_v2_schema.md and crash_trend/schema_v2.py
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from google.cloud import bigquery
except ImportError:
    bigquery = None  # type: ignore

try:
    from crash_trend.config import (
        ROOT,
        app_argparser,
        get_app,
        get_mcp_config,
        is_mcp_cache_fresh,
        load_config,
        out_dir,
        write_json,
    )
    from crash_trend.schema_v2 import (
        BlameFrame,
        BreadcrumbItem,
        IssueDetail,
        LogItem,
        TopDeviceCount,
        TopOSCount,
        is_valid_iso8601_utc,
    )
except ImportError:
    from config import (
        ROOT,
        app_argparser,
        get_app,
        get_mcp_config,
        is_mcp_cache_fresh,
        load_config,
        out_dir,
        write_json,
    )
    from schema_v2 import (
        BlameFrame,
        BreadcrumbItem,
        IssueDetail,
        LogItem,
        TopDeviceCount,
        TopOSCount,
        is_valid_iso8601_utc,
    )

MAX_TRACE_LINES = 40
DEFAULT_BREADCRUMBS_LIMIT = 50
DEFAULT_LOGS_LIMIT = 50
DEFAULT_TOP_DIST_LIMIT = 5

# Common system package / framework prefixes to skip when identifying blame frame
SYSTEM_FRAME_PREFIXES = (
    "android.",
    "androidx.",
    "com.android.",
    "java.",
    "javax.",
    "kotlin.",
    "kotlinx.",
    "dalvik.",
    "libsystem",
    "libdispatch",
    "libobjc",
    "UIKit",
    "UIKitCore",
    "CoreFoundation",
    "Foundation",
    "SwiftUI",
    "dart:",
    "package:flutter/",
)


# ---------------------------------------------------------------------------
# Timestamp Normalization Utilities
# ---------------------------------------------------------------------------

def normalize_timestamp_utc(val: Any) -> str:
    """Converts a datetime, ISO string, timestamp number to strict ISO 8601 UTC (ending in Z)."""
    if val is None:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if isinstance(val, dt.datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=dt.timezone.utc)
        else:
            val = val.astimezone(dt.timezone.utc)
        return val.strftime("%Y-%m-%dT%H:%M:%SZ")

    if isinstance(val, (int, float)):
        if val > 1e11:
            val = val / 1000.0
        try:
            d = dt.datetime.fromtimestamp(val, tz=dt.timezone.utc)
            return d.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, OSError):
            return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if isinstance(val, str):
        s = val.strip()
        if is_valid_iso8601_utc(s):
            if s.endswith("+00:00"):
                return s[:-6] + "Z"
            return s
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = dt.datetime.strptime(s.split(".")[0].replace("Z", ""), fmt)
                return parsed.replace(tzinfo=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue

    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Symbol Resolution & Source Availability
# ---------------------------------------------------------------------------

def parse_symbol(symbol: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Extracts (class_name, method_name) from a symbol or function signature string."""
    if not symbol or not isinstance(symbol, str):
        return None, None

    sym = symbol.strip()
    if not sym:
        return None, None

    sym = re.sub(r"\s*\(.*?\)$", "", sym)
    sym = re.sub(r"\s+\+\s+\d+$", "", sym)

    objc_match = re.match(r"^[-+]\s*\[\s*([A-Za-z0-9_]+)\s+([A-Za-z0-9_:]+)\s*\]$", sym)
    if objc_match:
        cls, meth = objc_match.group(1), objc_match.group(2)
        clean_meth = meth.split(":")[0] if ":" in meth else meth
        return cls, clean_meth

    if "::" in sym:
        parts = sym.split("::")
        if len(parts) >= 2:
            return "::".join(parts[:-1]), parts[-1]

    sym = re.sub(r"^(?:specialized\s+|static\s+|@objc\s+)+", "", sym)

    swift_arg_idx = sym.find("(")
    if swift_arg_idx != -1:
        sym = sym[:swift_arg_idx]

    if "." in sym:
        parts = sym.split(".")
        if len(parts) >= 2:
            class_name = ".".join(parts[:-1])
            method_name = parts[-1]
            return class_name, method_name

    return None, sym


def check_source_available(file_path: Optional[str], source_repo: Optional[Union[str, Path]]) -> bool:
    """Checks whether the file exists in the specified source repository."""
    if not file_path or not source_repo:
        return False

    repo = Path(source_repo).expanduser()
    if not repo.is_dir():
        return False

    direct_target = repo / file_path
    if direct_target.is_file():
        return True

    basename = Path(file_path).name
    if not basename:
        return False

    try:
        hits = list(repo.rglob(basename))
        return len(hits) > 0
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Blame Frame Parsing & Extraction
# ---------------------------------------------------------------------------

def parse_blame_frame(
    frame_data: Any,
    default_file: Optional[str] = None,
    default_line: Optional[int] = None,
    source_repo: Optional[Union[str, Path]] = None,
    source_available: Optional[bool] = None,
) -> Optional[BlameFrame]:
    """Parses blame frame from a dict, string, or frame object into a valid BlameFrame TypedDict."""
    if frame_data is None and default_file is None:
        return None

    file_val: Optional[str] = None
    line_val: Optional[int] = None
    symbol_val: Optional[str] = None
    class_val: Optional[str] = None
    method_val: Optional[str] = None

    if isinstance(frame_data, dict):
        file_val = frame_data.get("file") or frame_data.get("filename") or default_file
        raw_line = frame_data.get("line") if frame_data.get("line") is not None else default_line
        symbol_val = frame_data.get("symbol")
        class_val = frame_data.get("class_name") or frame_data.get("className")
        method_val = frame_data.get("method_name") or frame_data.get("methodName")

        if raw_line is not None:
            try:
                parsed_line = int(raw_line)
                line_val = parsed_line if parsed_line > 0 else None
            except (ValueError, TypeError):
                line_val = None

        if source_available is None and "source_available" in frame_data:
            source_available = bool(frame_data["source_available"])

    elif isinstance(frame_data, str):
        raw_str = frame_data.strip()
        m = re.search(r"([\w/.-]+\.(?:kt|java|swift|dart|m|mm|cpp|c|h|cc|py))(?::|\s+line\s+)?(\d+)?", raw_str)
        if m:
            file_val = m.group(1)
            if m.group(2):
                try:
                    p_line = int(m.group(2))
                    line_val = p_line if p_line > 0 else None
                except ValueError:
                    line_val = None
        else:
            file_val = default_file
            line_val = default_line

        sym_match = re.search(r"(?:at\s+)?([A-Za-z0-9_.$:]+(?:\.[A-Za-z0-9_.$:]+)+)", raw_str)
        if sym_match:
            symbol_val = sym_match.group(1)

    else:
        file_val = default_file
        line_val = default_line

    if file_val is not None:
        file_val = str(file_val).strip()
        if not file_val or file_val.lower() in ("unknown", "(unknown source)", "(native method)", "?"):
            file_val = None

    if symbol_val is not None:
        symbol_val = str(symbol_val).strip()
        if not symbol_val or symbol_val == "?":
            symbol_val = None

    if symbol_val and (not class_val or not method_val):
        inferred_cls, inferred_meth = parse_symbol(symbol_val)
        class_val = class_val or inferred_cls
        method_val = method_val or inferred_meth

    if not any([file_val, line_val, symbol_val, class_val, method_val]):
        return None

    if source_available is None:
        source_available = check_source_available(file_val, source_repo)

    return {
        "file": file_val,
        "line": line_val,
        "symbol": symbol_val,
        "class_name": class_val,
        "method_name": method_val,
        "is_blame": True,
        "source_available": bool(source_available),
    }


def is_system_frame(symbol: Optional[str], file: Optional[str]) -> bool:
    """Checks whether a frame belongs to system / runtime frameworks rather than app code."""
    if symbol:
        for prefix in SYSTEM_FRAME_PREFIXES:
            if symbol.startswith(prefix):
                return True
    if file:
        if file.startswith("dart:") or "/flutter/" in file or file.startswith("androidx/"):
            return True
    return False


def extract_blame_frame_from_frames(
    frames: List[dict],
    source_repo: Optional[Union[str, Path]] = None,
) -> Optional[BlameFrame]:
    """Extracts the most relevant blame frame from a list of stack frames.
    Priority:
    1. Official Crashlytics `blamed == True` or `is_blame == True` or `importance > 0`
    2. First non-system application frame from top to bottom
    3. Top frame fallback
    """
    if not frames:
        return None

    # 1. Official blamed / is_blame field priority
    for f in frames:
        if f.get("blamed") is True or f.get("is_blame") is True or f.get("importance", 0) > 0:
            parsed = parse_blame_frame(f, source_repo=source_repo)
            if parsed:
                return parsed

    # 2. Non-system frame heuristic fallback
    for f in frames:
        sym = f.get("symbol")
        filename = f.get("file") or f.get("filename")
        if not is_system_frame(sym, filename):
            parsed = parse_blame_frame(f, source_repo=source_repo)
            if parsed:
                return parsed

    # 3. Fallback to top frame
    return parse_blame_frame(frames[0], source_repo=source_repo)


# ---------------------------------------------------------------------------
# Stack Trace Formatting & Truncation
# ---------------------------------------------------------------------------

def truncate_trace(text: str, max_lines: int = MAX_TRACE_LINES) -> str:
    """Truncates stack trace text cleanly to max_lines."""
    if not text:
        return ""
    lines = text.strip().splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines]) + f"\n…（截斷，原 {len(lines)} 行）"


def format_stack_trace(
    exceptions: Any = None,
    threads: Any = None,
    error: Any = None,
    raw_trace: Optional[str] = None,
    max_lines: int = MAX_TRACE_LINES,
) -> Optional[str]:
    """Formats structured exceptions, error (Apple), or threads into a standard stack trace string."""
    if raw_trace and isinstance(raw_trace, str) and raw_trace.strip():
        return truncate_trace(raw_trace, max_lines=max_lines)

    lines: List[str] = []

    # 1. Format Exceptions (Android / Generic)
    if isinstance(exceptions, list) and exceptions:
        for exc in exceptions:
            if not isinstance(exc, dict):
                continue
            exc_type = exc.get("type") or "Exception"
            exc_reason = exc.get("reason") or exc.get("exception_message")
            header = f"{exc_type}: {exc_reason}" if exc_reason else str(exc_type)
            lines.append(header)

            frames = exc.get("frames") or []
            for f in frames:
                if not isinstance(f, dict):
                    continue
                sym = f.get("symbol") or "unknown"
                fname = f.get("file") or f.get("filename")
                line_no = f.get("line")
                if fname and line_no:
                    lines.append(f"\tat {sym}({fname}:{line_no})")
                elif fname:
                    lines.append(f"\tat {sym}({fname})")
                else:
                    lines.append(f"\tat {sym}")

    # 2. Format Error (Apple domain)
    elif isinstance(error, list) and error:
        for err in error:
            if not isinstance(err, dict):
                continue
            err_type = err.get("type") or err.get("error_name") or err.get("title") or "Error"
            err_reason = err.get("reason") or err.get("subtitle")
            header = f"{err_type}: {err_reason}" if err_reason else str(err_type)
            lines.append(header)

            frames = err.get("frames") or []
            for f in frames:
                if not isinstance(f, dict):
                    continue
                sym = f.get("symbol") or "unknown"
                fname = f.get("file") or f.get("filename")
                line_no = f.get("line")
                if fname and line_no:
                    lines.append(f"\tat {sym}({fname}:{line_no})")
                elif fname:
                    lines.append(f"\tat {sym}({fname})")
                else:
                    lines.append(f"\tat {sym}")

    # 3. Format Threads
    elif isinstance(threads, list) and threads:
        crashed_threads = [t for t in threads if isinstance(t, dict) and t.get("crashed")]
        target_threads = crashed_threads if crashed_threads else [t for t in threads if isinstance(t, dict)]

        for t in target_threads[:3]:
            name = t.get("name") or "Thread"
            crashed_tag = " (crashed)" if t.get("crashed") else ""
            lines.append(f'"{name}"{crashed_tag}')

            frames = t.get("frames") or []
            for f in frames:
                if not isinstance(f, dict):
                    continue
                sym = f.get("symbol") or "unknown"
                fname = f.get("file") or f.get("filename")
                line_no = f.get("line")
                if fname and line_no:
                    lines.append(f"  at {sym}({fname}:{line_no})")
                elif fname:
                    lines.append(f"  at {sym}({fname})")
                else:
                    lines.append(f"  at {sym}")

    if not lines:
        return None

    return truncate_trace("\n".join(lines), max_lines=max_lines)


# ---------------------------------------------------------------------------
# Breadcrumbs, Logs, Custom Keys, Distributions Parsing
# ---------------------------------------------------------------------------

def parse_breadcrumbs(
    raw_breadcrumbs: Any,
    max_items: int = DEFAULT_BREADCRUMBS_LIMIT,
) -> Optional[List[BreadcrumbItem]]:
    """Parses breadcrumbs list supporting both BQ schema (name/params/timestamp) and MCP schema (category/message/timestamp)."""
    if raw_breadcrumbs is None:
        return None
    if not isinstance(raw_breadcrumbs, list):
        return []

    items: List[BreadcrumbItem] = []
    for b in raw_breadcrumbs:
        if not isinstance(b, dict):
            continue
        ts = normalize_timestamp_utc(b.get("timestamp"))

        # Category / Name mapping
        category = str(b.get("category") or b.get("name") or "general")

        # Params / Data mapping
        params = b.get("params")
        data = b.get("data")
        data_dict = None
        if isinstance(data, dict):
            data_dict = data
        elif isinstance(params, list):
            data_dict = {}
            for p in params:
                if isinstance(p, dict) and "key" in p:
                    data_dict[str(p["key"])] = p.get("value")
        elif isinstance(params, dict):
            data_dict = params

        message = str(b.get("message") or "")
        if not message and data_dict:
            message = ", ".join(f"{k}={v}" for k, v in data_dict.items())
        elif not message:
            message = category

        level = str(b.get("level") or "info")

        item: BreadcrumbItem = {
            "timestamp": ts,
            "category": category,
            "message": message,
            "level": level,
        }
        if data_dict is not None:
            item["data"] = data_dict
        items.append(item)

    if len(items) > max_items:
        items = items[-max_items:]

    return items


def parse_logs(
    raw_logs: Any,
    max_items: int = DEFAULT_LOGS_LIMIT,
) -> Optional[List[LogItem]]:
    """Parses logs list with ISO 8601 UTC timestamps and max items limit."""
    if raw_logs is None:
        return None
    if not isinstance(raw_logs, list):
        return []

    items: List[LogItem] = []
    for l in raw_logs:
        if not isinstance(l, dict):
            continue
        ts = normalize_timestamp_utc(l.get("timestamp"))
        msg = str(l.get("message") or "")
        items.append({"timestamp": ts, "message": msg})

    if len(items) > max_items:
        items = items[-max_items:]

    return items


def parse_custom_keys(raw_custom_keys: Any) -> Optional[Dict[str, Any]]:
    """Parses custom keys from repeated struct list or dictionary."""
    if raw_custom_keys is None:
        return None

    if isinstance(raw_custom_keys, dict):
        return dict(raw_custom_keys)

    if isinstance(raw_custom_keys, list):
        out: Dict[str, Any] = {}
        for item in raw_custom_keys:
            if isinstance(item, dict) and "key" in item:
                out[str(item["key"])] = item.get("value")
        return out

    return None


def parse_top_devices(
    devices_data: Any,
    top_n: int = DEFAULT_TOP_DIST_LIMIT,
) -> Optional[List[TopDeviceCount]]:
    """Parses top device counts."""
    if devices_data is None:
        return None
    if not isinstance(devices_data, list):
        return []

    out: List[TopDeviceCount] = []
    for item in devices_data:
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or item.get("device_model") or "").strip()
        if not model:
            continue
        events = int(item.get("events") or 0)
        out.append({"model": model, "events": max(0, events)})

    out.sort(key=lambda x: -x["events"])
    return out[:top_n]


def parse_top_os(
    os_data: Any,
    top_n: int = DEFAULT_TOP_DIST_LIMIT,
) -> Optional[List[TopOSCount]]:
    """Parses top OS counts."""
    if os_data is None:
        return None
    if not isinstance(os_data, list):
        return []

    out: List[TopOSCount] = []
    for item in os_data:
        if not isinstance(item, dict):
            continue
        os_ver = str(item.get("os_version") or item.get("display_version") or "").strip()
        if not os_ver:
            continue
        events = int(item.get("events") or 0)
        out.append({"os_version": os_ver, "events": max(0, events)})

    out.sort(key=lambda x: -x["events"])
    return out[:top_n]


# ---------------------------------------------------------------------------
# IssueDetail Builder
# ---------------------------------------------------------------------------

def build_issue_detail(
    stack_trace: Optional[str] = None,
    breadcrumbs: Optional[List[BreadcrumbItem]] = None,
    logs: Optional[List[LogItem]] = None,
    custom_keys: Optional[Dict[str, Any]] = None,
    top_devices: Optional[List[TopDeviceCount]] = None,
    top_os: Optional[List[TopOSCount]] = None,
) -> Optional[IssueDetail]:
    """Builds a schema-compliant IssueDetail dictionary with all 6 required fields."""
    if (
        stack_trace is None
        and breadcrumbs is None
        and logs is None
        and custom_keys is None
        and top_devices is None
        and top_os is None
    ):
        return None

    return {
        "stack_trace": stack_trace,
        "breadcrumbs": breadcrumbs if breadcrumbs is not None else [],
        "logs": logs if logs is not None else [],
        "custom_keys": custom_keys if custom_keys is not None else {},
        "top_devices": top_devices if top_devices is not None else [],
        "top_os": top_os if top_os is not None else [],
    }


# ---------------------------------------------------------------------------
# BigQuery Querying for Issue Details
# ---------------------------------------------------------------------------

def fetch_issue_details_from_bq(
    client: Any,
    project: str,
    dataset: str,
    tables: List[str],
    issue_ids: List[str],
    days: int = 30,
    source_repo: Optional[Union[str, Path]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Queries detailed crash events, stack frames, breadcrumbs, logs, and custom keys from BigQuery."""
    if not client or not tables or not issue_ids:
        return {}

    valid_ids = [i for i in issue_ids if re.fullmatch(r"[\w.-]+", i)]
    if not valid_ids:
        return {}

    id_list_str = ", ".join(f"'{i}'" for i in valid_ids)
    results: Dict[str, Dict[str, Any]] = {}

    for table in tables:
        fq_table = f"{project}.{dataset}.{table}"

        # 1. Query sample events for each issue including blame_frame, breadcrumbs, error
        sql_events = f"""
        WITH ranked_events AS (
            SELECT
                issue_id,
                event_timestamp,
                device.model AS device_model,
                operating_system.display_version AS os_version,
                exceptions,
                threads,
                error,
                blame_frame,
                breadcrumbs,
                custom_keys,
                logs,
                ROW_NUMBER() OVER(PARTITION BY issue_id ORDER BY event_timestamp DESC) AS rn
            FROM `{fq_table}`
            WHERE issue_id IN ({id_list_str})
              AND event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
        )
        SELECT * FROM ranked_events WHERE rn = 1
        """

        # 2. Query top devices per issue
        sql_devices = f"""
        SELECT
            issue_id,
            device.model AS model,
            COUNT(*) AS events
        FROM `{fq_table}`
        WHERE issue_id IN ({id_list_str})
          AND event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
        GROUP BY 1, 2
        ORDER BY issue_id, events DESC
        """

        # 3. Query top OS per issue
        sql_os = f"""
        SELECT
            issue_id,
            operating_system.display_version AS os_version,
            COUNT(*) AS events
        FROM `{fq_table}`
        WHERE issue_id IN ({id_list_str})
          AND event_timestamp >= TIMESTAMP(DATE_SUB(CURRENT_DATE(), INTERVAL {days} - 1 DAY))
        GROUP BY 1, 2
        ORDER BY issue_id, events DESC
        """

        try:
            event_rows = [dict(r) for r in client.query(sql_events).result(max_results=200)]
        except Exception as e:
            if "Unrecognized name: error" in str(e) and "error," in sql_events:
                try:
                    retry_sql = sql_events.replace("error,\n", "errors,\n")
                    event_rows = [dict(r) for r in client.query(retry_sql).result(max_results=200)]
                except Exception as retry_e:
                    print(f"  ⚠ BigQuery sample events query retry failed for {table}: {retry_e}")
                    event_rows = []
            else:
                print(f"  ⚠ BigQuery sample events query failed for {table}: {e}")
                event_rows = []

        devices_by_issue: Dict[str, List[dict]] = {}
        try:
            device_rows = [dict(r) for r in client.query(sql_devices).result(max_results=500)]
            for r in device_rows:
                iid = r.get("issue_id")
                if iid not in devices_by_issue:
                    devices_by_issue[iid] = []
                devices_by_issue[iid].append({"name": r.get("model") or "Unknown", "count": r.get("events", 0)})
        except Exception as e:
            print(f"  ⚠ BigQuery devices query failed for {table}: {e}")

        os_by_issue: Dict[str, List[dict]] = {}
        try:
            os_rows = [dict(r) for r in client.query(sql_os).result(max_results=500)]
            for r in os_rows:
                iid = r.get("issue_id")
                if iid not in os_by_issue:
                    os_by_issue[iid] = []
                os_by_issue[iid].append({"name": r.get("os_version") or "Unknown", "count": r.get("events", 0)})
        except Exception as e:
            print(f"  ⚠ BigQuery OS query failed for {table}: {e}")

        for ev in event_rows:
            iid = ev.get("issue_id")
            if not iid:
                continue

            exceptions = ev.get("exceptions") or []
            threads = ev.get("threads") or []
            error = ev.get("error") or ev.get("errors") or []
            raw_top_blame = ev.get("blame_frame")

            trace_str = format_stack_trace(exceptions=exceptions, threads=threads, error=error)

            # Blame resolution priority:
            # 1. Official top-level blame_frame
            # 2. exceptions / error / threads frames (blamed: true -> non-system -> top)
            blame = None
            if raw_top_blame and isinstance(raw_top_blame, dict) and any(raw_top_blame.values()):
                blame = parse_blame_frame(raw_top_blame, source_repo=source_repo)

            if blame is None:
                if exceptions:
                    for exc in exceptions:
                        frames = exc.get("frames") if isinstance(exc, dict) else []
                        if frames:
                            blame = extract_blame_frame_from_frames(frames, source_repo=source_repo)
                            if blame:
                                break
                elif error:
                    for err in error:
                        frames = err.get("frames") if isinstance(err, dict) else []
                        if frames:
                            blame = extract_blame_frame_from_frames(frames, source_repo=source_repo)
                            if blame:
                                break
                elif threads:
                    for t in threads:
                        frames = t.get("frames") if isinstance(t, dict) else []
                        if frames:
                            blame = extract_blame_frame_from_frames(frames, source_repo=source_repo)
                            if blame:
                                break

            logs_list = parse_logs(ev.get("logs"))
            breadcrumbs_list = parse_breadcrumbs(ev.get("breadcrumbs"))
            custom_keys_dict = parse_custom_keys(ev.get("custom_keys"))
            top_devs = parse_top_devices(devices_by_issue.get(iid, []))
            top_os_list = parse_top_os(os_by_issue.get(iid, []))

            detail = build_issue_detail(
                stack_trace=trace_str,
                breadcrumbs=breadcrumbs_list,
                logs=logs_list,
                custom_keys=custom_keys_dict,
                top_devices=top_devs,
                top_os=top_os_list,
            )

            results[iid] = {
                "blame_frame": blame,
                "detail": detail,
            }

    return results


# ---------------------------------------------------------------------------
# MCP & Cache Fallback
# ---------------------------------------------------------------------------

def load_issue_details_from_stacktraces_cache(
    stacktraces_path: Path,
    source_repo: Optional[Union[str, Path]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Loads cached stack traces and blame frames from stacktraces.json."""
    if not stacktraces_path.exists():
        return {}

    try:
        data = json.loads(stacktraces_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    issues = data.get("issues") or {}
    results: Dict[str, Dict[str, Any]] = {}

    for iid, item in issues.items():
        if not isinstance(item, dict):
            continue

        trace = item.get("stack_trace")
        raw_bf = item.get("blame_frame") or {}
        blame = parse_blame_frame(raw_bf, source_repo=source_repo)

        detail = build_issue_detail(
            stack_trace=trace,
            breadcrumbs=parse_breadcrumbs(item.get("breadcrumbs")),
            logs=parse_logs(item.get("logs")),
            custom_keys=parse_custom_keys(item.get("custom_keys")),
            top_devices=parse_top_devices(item.get("top_devices")),
            top_os=parse_top_os(item.get("top_os")),
        )

        results[iid] = {
            "blame_frame": blame,
            "detail": detail,
        }

    return results


# ---------------------------------------------------------------------------
# Unified Issue Detail Fetching & Enrichment (Supplemental Merge)
# ---------------------------------------------------------------------------

def safe_get_app(name: str) -> dict:
    """Safely retrieves app config, falling back to dummy configuration if not found."""
    try:
        return get_app(name)
    except (SystemExit, Exception):
        return {"firebase_project": name, "source_repo": None}


def fetch_issue_details(
    app_name: str,
    issue_ids: List[str],
    days: int = 30,
    bq_client: Any = None,
    source_repo: Optional[Union[str, Path]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Fetches IssueDetail and BlameFrame for specified issues with supplemental merge.

    Data sources:
    1. BigQuery export query (primary)
    2. stacktraces.json (from MCP fetch) as supplemental / fallback
    3. Heuristic / subtitle fallback
    """
    app = safe_get_app(app_name)
    repo = source_repo or app.get("source_repo")
    results: Dict[str, Dict[str, Any]] = {}

    # 1. Query BigQuery (if client available)
    if bq_client is not None and issue_ids:
        project = app.get("firebase_project")
        dataset = app.get("bq_dataset", "firebase_crashlytics")
        try:
            tables = [t.table_id for t in bq_client.list_tables(f"{project}.{dataset}")]
            batch_tables = [t for t in tables if not t.endswith("_REALTIME")]
            if batch_tables:
                bq_results = fetch_issue_details_from_bq(
                    bq_client, project, dataset, batch_tables, list(issue_ids), days=days, source_repo=repo
                )
                for iid, data in bq_results.items():
                    results[iid] = data
        except Exception as e:
            print(f"  [fetch_issue_details] BigQuery lookup bypassed/failed: {e}")

    # 2. Supplemental merge from MCP cache (out/<app>/stacktraces.json)
    mcp_cfg = get_mcp_config(app)
    if mcp_cfg["mode"] == "off":
        cached = {}
    else:
        st_path = ROOT / "out" / app_name / "stacktraces.json"
        cached = load_issue_details_from_stacktraces_cache(st_path, source_repo=repo)

    for iid in issue_ids:
        cached_data = cached.get(iid)
        if iid not in results or results[iid].get("detail") is None:
            if cached_data:
                results[iid] = cached_data
            else:
                results[iid] = {"blame_frame": None, "detail": None}
        elif cached_data:
            # Supplemental merge: supplement missing fields in BQ detail from MCP
            bq_detail = results[iid]["detail"]
            mcp_detail = cached_data.get("detail") or {}

            if bq_detail and mcp_detail:
                if not bq_detail.get("breadcrumbs") and mcp_detail.get("breadcrumbs"):
                    bq_detail["breadcrumbs"] = mcp_detail["breadcrumbs"]
                if not bq_detail.get("logs") and mcp_detail.get("logs"):
                    bq_detail["logs"] = mcp_detail["logs"]
                if not bq_detail.get("custom_keys") and mcp_detail.get("custom_keys"):
                    bq_detail["custom_keys"] = mcp_detail["custom_keys"]
                if not bq_detail.get("stack_trace") and mcp_detail.get("stack_trace"):
                    bq_detail["stack_trace"] = mcp_detail["stack_trace"]

            if not results[iid].get("blame_frame") and cached_data.get("blame_frame"):
                results[iid]["blame_frame"] = cached_data["blame_frame"]

    return results


def enrich_top_issues(
    issues: List[dict],
    app_name: str,
    days: int = 30,
    bq_client: Any = None,
    source_repo: Optional[Union[str, Path]] = None,
) -> List[dict]:
    """Enriches a list of IssueSummary dictionaries with blame_frame and detail."""
    if not issues:
        return []

    app = safe_get_app(app_name)
    repo = source_repo or app.get("source_repo")
    issue_ids = [i.get("issue_id") for i in issues if i.get("issue_id")]
    details_map = fetch_issue_details(
        app_name, issue_ids, days=days, bq_client=bq_client, source_repo=repo
    )

    enriched = []
    for issue in issues:
        item = dict(issue)
        iid = item.get("issue_id")
        fetched = details_map.get(iid) if iid else None

        blame = item.get("blame_frame")
        if blame is None and fetched:
            blame = fetched.get("blame_frame")
        if blame is None and item.get("subtitle"):
            blame = parse_blame_frame(item["subtitle"], source_repo=repo)

        detail = item.get("detail")
        if detail is None and fetched:
            detail = fetched.get("detail")

        item["blame_frame"] = blame
        item["detail"] = detail
        enriched.append(item)

    return enriched


def get_mcp_source_status(app_name: str) -> dict:
    """Returns SourceStatus dictionary for sources.mcp_crashlytics according to Schema V2."""
    app = safe_get_app(app_name)
    mcp_cfg = get_mcp_config(app)
    mode = mcp_cfg["mode"]
    max_age_days = mcp_cfg["max_age_days"]
    st_path = ROOT / "out" / app_name / "stacktraces.json"
    err_path = ROOT / "out" / app_name / "stacktraces_last_error.json"

    if mode == "off":
        return {
            "status": "disabled",
            "last_sync_timestamp": None,
            "error_message": "MCP 模式已停用 (disabled in config)",
        }

    last_err_msg = None
    if err_path.exists():
        try:
            err_data = json.loads(err_path.read_text(encoding="utf-8"))
            last_err_msg = err_data.get("error_message") or str(err_data.get("errors") or "")
        except Exception:
            pass

    if not st_path.exists():
        if last_err_msg:
            return {
                "status": "error",
                "last_sync_timestamp": None,
                "error_message": f"MCP 執行失敗：{last_err_msg}",
            }
        return {
            "status": "unavailable",
            "last_sync_timestamp": None,
            "error_message": "無 MCP 快取資料",
        }

    is_fresh, age_days, gen_at = is_mcp_cache_fresh(st_path, max_age_days=max_age_days)
    if is_fresh:
        return {
            "status": "available",
            "last_sync_timestamp": gen_at,
            "error_message": None,
        }
    else:
        age_disp = f"{age_days:.1f}" if age_days is not None else "未知"
        msg = f"MCP 快取過期（已快取 {age_disp} 天 > 上限 {max_age_days} 天）"
        if last_err_msg:
            msg += f"；最近一次刷新失敗：{last_err_msg}"
        return {
            "status": "available",
            "last_sync_timestamp": gen_at,
            "error_message": msg,
        }


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    p = app_argparser("Fetch detailed Crashlytics issue information (stack, blame frame, logs, breadcrumbs)")
    p.add_argument("--top", type=int, default=10, help="Number of top issues to fetch details for")
    p.add_argument("--issue-id", help="Single issue ID to fetch")
    args = p.parse_args()

    app = get_app(args.app)
    odir = out_dir(args.app)
    days = args.days

    v2_path = odir / "dashboard_v2.json"
    unified_path = odir / "unified.json"
    target_ids = []
    if args.issue_id:
        target_ids = [args.issue_id]
    elif v2_path.exists():
        try:
            v2_data = json.loads(v2_path.read_text(encoding="utf-8"))
            target_ids = [i["issue_id"] for i in v2_data.get("top_issues", [])[: args.top] if i.get("issue_id")]
        except Exception:
            target_ids = []
    if not target_ids and unified_path.exists():
        try:
            u = json.loads(unified_path.read_text(encoding="utf-8"))
            target_ids = [i["issue_id"] for i in u.get("issues", [])[: args.top] if i.get("issue_id")]
        except Exception:
            target_ids = []

    bq_client = None
    if bigquery is not None:
        try:
            try:
                from crash_trend.fetch_bigquery import make_client
            except ImportError:
                from fetch_bigquery import make_client
            bq_client = make_client(app["firebase_project"])
        except Exception as e:
            print(f"  [注意] 無法建立 BigQuery client：{e}")

    results = fetch_issue_details(
        args.app, target_ids, days=days, bq_client=bq_client, source_repo=app.get("source_repo")
    )
    write_json(odir / "issue_details.json", results)
    print(f"  ✓ Issue details fetched for {len(results)} issues")

    # 若已有 dashboard_v2.json，自動更新 Top Issues 的 blame_frame 與 detail，以及 sources.mcp_crashlytics
    if v2_path.exists():
        try:
            app_data = json.loads(v2_path.read_text(encoding="utf-8"))
            if "top_issues" in app_data and isinstance(app_data["top_issues"], list):
                app_data["top_issues"] = enrich_top_issues(
                    app_data["top_issues"],
                    app_name=args.app,
                    days=days,
                    bq_client=bq_client,
                    source_repo=app.get("source_repo"),
                )
            if "periods" in app_data and isinstance(app_data["periods"], dict):
                for snap in app_data["periods"].values():
                    if "top_issues" in snap and isinstance(snap["top_issues"], list):
                        snap["top_issues"] = enrich_top_issues(
                            snap["top_issues"],
                            app_name=args.app,
                            days=snap.get("period", {}).get("days", days),
                            bq_client=bq_client,
                            source_repo=app.get("source_repo"),
                        )
            if "sources" in app_data and isinstance(app_data["sources"], dict):
                app_data["sources"]["mcp_crashlytics"] = get_mcp_source_status(args.app)
            write_json(v2_path, app_data)
            print(f"  ✓ 已更新 {v2_path.relative_to(ROOT)} Top Issues 與 MCP 資料源狀態")
        except Exception as e:
            print(f"  ⚠ 更新 dashboard_v2.json Top Issues 失敗：{e}", file=sys.stderr)


if __name__ == "__main__":
    main()
