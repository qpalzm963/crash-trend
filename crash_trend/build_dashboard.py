"""產生自包含靜態儀表板 dashboard.html (Dashboard V2)。

支援 Schema V2 資料契約 (DashboardV2Bundle)，採淺色 SaaS / B2B Analytics 現代風格。
內嵌 Chart.js (vendor/chart.umd.min.js)，無外部 CDN 依賴，file:// 本地離線可直接開啟。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Union

try:
    from crash_trend.schema_v2 import validate_dashboard_v2, SCHEMA_VERSION
except ImportError:
    try:
        from schema_v2 import validate_dashboard_v2, SCHEMA_VERSION
    except ImportError:
        from .schema_v2 import validate_dashboard_v2, SCHEMA_VERSION

# Root directory
ROOT = Path(__file__).resolve().parent.parent
VENDOR_JS = ROOT / "vendor" / "chart.umd.min.js"
DEFAULT_OUT_HTML = ROOT / "dashboard.html"


def get_vendor_chartjs() -> str:
    """Reads vendor Chart.js library or returns an empty fallback if missing."""
    if VENDOR_JS.is_file():
        return VENDOR_JS.read_text(encoding="utf-8")
    return "/* Chart.js vendor script not found */"


def assemble_bundle_from_apps(cfg: Optional[dict] = None) -> Optional[dict]:
    """Scans out/<app_id>/ for app-level V2 data and bundles them into a DashboardV2Bundle."""
    if cfg is None:
        try:
            import yaml
            apps_yaml = ROOT / "apps.yaml"
            if not apps_yaml.exists():
                apps_yaml = ROOT / "apps.example.yaml"
            if apps_yaml.exists():
                with open(apps_yaml, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            else:
                cfg = {}
        except Exception:
            cfg = {}

    apps_cfg = (cfg or {}).get("apps") or {}
    collected_apps: dict[str, dict] = {}

    for app_id in apps_cfg.keys():
        app_out_dir = ROOT / "out" / app_id
        for ac in [app_out_dir / "dashboard_v2.json", app_out_dir / "app_v2.json"]:
            if ac.is_file():
                try:
                    data = json.loads(ac.read_text(encoding="utf-8"))
                    if "apps" in data and app_id in data["apps"]:
                        collected_apps[app_id] = data["apps"][app_id]
                    elif "metadata" in data and "kpi" in data:
                        collected_apps[app_id] = data
                    break
                except Exception as e:
                    print(f"  [Warning] Failed to load {ac}: {e}")

    if not collected_apps:
        out_root = ROOT / "out"
        if out_root.is_dir():
            for p in out_root.iterdir():
                if p.is_dir() and p.name not in collected_apps:
                    for ac in [p / "dashboard_v2.json", p / "app_v2.json"]:
                        if ac.is_file():
                            try:
                                data = json.loads(ac.read_text(encoding="utf-8"))
                                if "apps" in data and p.name in data["apps"]:
                                    collected_apps[p.name] = data["apps"][p.name]
                                elif "metadata" in data and "kpi" in data:
                                    collected_apps[p.name] = data
                                break
                            except Exception:
                                pass

    if not collected_apps:
        return None

    default_app = list(collected_apps.keys())[0]
    now_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_utc,
        "default_app": default_app,
        "apps": collected_apps,
    }

    run_sum_env = os.environ.get("PIPELINE_RUN_SUMMARY")
    run_sum_path = Path(run_sum_env) if run_sum_env else ROOT / "out" / "pipeline_run.json"
    if run_sum_path.is_file():
        try:
            bundle["pipeline_run"] = json.loads(run_sum_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Ensure all collected apps have lifecycle enriched before strict Schema V2.3 validation
    for app_id, a_data in collected_apps.items():
        if isinstance(a_data, dict) and isinstance(a_data.get("top_issues"), list):
            has_missing_lc = any(isinstance(i, dict) and "lifecycle" not in i for i in a_data["top_issues"])
            if has_missing_lc:
                try:
                    from crash_trend.lifecycle import enrich_app_data_with_lifecycle
                    enrich_app_data_with_lifecycle(a_data, app_name=app_id, out_dir=ROOT / "out")
                except Exception:
                    pass

    # 驗證組裝之 bundle 是否符合 Schema V2，失敗時不寫入正式檔案
    val_errors = validate_dashboard_v2(bundle)
    if val_errors:
        print(f"  [警告] 組裝之 Dashboard V2 bundle 驗證失敗（{len(val_errors)} 項錯誤）：", file=sys.stderr)
        for ve in val_errors[:5]:
            print(f"    - {ve}", file=sys.stderr)
        return None

    # Save assembled bundle to out/ and reports/
    out_dir = ROOT / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dashboard_v2.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")

    reports_dir = ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "dashboard_v2.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")

    return bundle


def collect_data(data_path: Optional[Union[str, Path]] = None) -> dict:
    """Loads Dashboard V2 bundle data from specified path or standard locations."""
    if data_path:
        p = Path(data_path)
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
        raise FileNotFoundError(f"Specified data file not found: {data_path}")

    # 1. Try to assemble from multi-app out/<app>/ data
    assembled = assemble_bundle_from_apps()
    if assembled:
        return assembled

    # 2. Search production locations (嚴禁在正式環境偷偷 fallback 至測試 fixture)
    candidates = [
        ROOT / "reports" / "dashboard_v2.json",
        ROOT / "out" / "dashboard_v2.json",
    ]
    for c in candidates:
        if c.is_file():
            try:
                data = json.loads(c.read_text(encoding="utf-8"))
                if not validate_dashboard_v2(data):
                    return data
            except Exception:
                pass

    # Fallback to minimal bundle if nothing found
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": "2026-09-02T00:00:00Z",
        "default_app": "default_app",
        "apps": {
            "default_app": {
                "metadata": {
                    "app_id": "default_app",
                    "display_name": "My Application",
                    "firebase_project_id": "my-app-default",
                    "platforms": ["android", "ios"],
                    "source_repo": None,
                    "custom_keys_monitored": [],
                },
                "period": {
                    "days": 30,
                    "start_time": "2026-08-03T00:00:00Z",
                    "end_time": "2026-09-02T00:00:00Z",
                    "comparison_period": None,
                },
                "sources": {
                    "crashlytics_bq": {
                        "status": "available",
                        "last_sync_timestamp": "2026-09-02T00:00:00Z",
                        "error_message": None,
                    },
                    "firebase_sessions": {
                        "status": "unavailable",
                        "last_sync_timestamp": None,
                        "error_message": "Sessions export not configured",
                    },
                    "mcp_crashlytics": {
                        "status": "unavailable",
                        "last_sync_timestamp": None,
                        "error_message": None,
                    },
                    "gemini_ai": {
                        "status": "disabled",
                        "last_sync_timestamp": None,
                        "error_message": None,
                    },
                    "ai": {
                        "status": "disabled",
                        "provider": "gemini",
                        "model": None,
                        "last_sync_timestamp": None,
                        "error_message": None,
                    },
                },
                "kpi": {
                    "crash_events": {"value": 0, "previous_value": None, "change_pct": None, "status": "available"},
                    "affected_users": {"value": 0, "previous_value": None, "change_pct": None, "status": "available"},
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
                    "new_issues_count": {"value": 0, "previous_value": None, "change_pct": None, "status": "available"},
                    "events_by_error_type": {"fatal": 0, "anr": 0, "non_fatal": 0},
                },
                "daily_trend": [],
                "version_health": [],
                "distributions": {
                    "platform": [],
                    "device_models": [],
                    "os_versions": [],
                    "app_versions": [],
                },
                "top_issues": [],
                "ai_summary": {
                    "status": "unavailable",
                    "model": None,
                    "generated_at": None,
                    "overview": "尚無 AI 摘要分析。",
                    "key_takeaways": [],
                    "distribution_insights": "",
                    "recommended_actions": [],
                    "data_limitations": None,
                },
                "limitations": [],
            }
        },
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crashlytics Engineering Dashboard V2</title>
<style>
  :root {
    --bg-app: #f8fafc;
    --bg-surface: #ffffff;
    --bg-subtle: #f1f5f9;
    --bg-muted: #e2e8f0;
    --border: #e2e8f0;
    --border-subtle: #f1f5f9;
    --border-hover: #cbd5e1;
    --text-main: #0f172a;
    --text-muted: #64748b;
    --text-subtle: #94a3b8;
    --accent: #2563eb;
    --accent-light: #eff6ff;
    --accent-hover: #1d4ed8;
    --accent-text: #1d4ed8;
    --success: #10b981;
    --success-light: #ecfdf5;
    --success-text: #047857;
    --warning: #f59e0b;
    --warning-light: #fffbeb;
    --warning-text: #b45309;
    --danger: #ef4444;
    --danger-light: #fef2f2;
    --danger-text: #b91c1c;
    --p0: #dc2626;
    --p0-bg: #fee2e2;
    --p1: #d97706;
    --p1-bg: #fef3c7;
    --p2: #2563eb;
    --p2-bg: #dbeafe;
    --p3: #64748b;
    --p3-bg: #f1f5f9;
    --sidebar-w: 240px;
    --sidebar-collapsed-w: 72px;
    --header-h: 64px;
    --radius-sm: 6px;
    --radius-md: 8px;
    --radius-lg: 12px;
    --radius-full: 9999px;
    --shadow-sm: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
    --shadow-card: 0 1px 3px 0 rgba(0, 0, 0, 0.08), 0 1px 2px -1px rgba(0, 0, 0, 0.05);
    --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.08), 0 2px 4px -2px rgba(0, 0, 0, 0.05);
    --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.08), 0 4px 6px -4px rgba(0, 0, 0, 0.03);
    --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, "Noto Sans TC", "PingFang TC", sans-serif;
    --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  }

  [data-theme="dark"] {
    --bg-app: #0b1120;
    --bg-surface: #131d33;
    --bg-subtle: #1e293b;
    --bg-muted: #334155;
    --border: #24324d;
    --border-subtle: #1a263d;
    --border-hover: #475569;
    --text-main: #f8fafc;
    --text-muted: #94a3b8;
    --text-subtle: #64748b;
    --accent: #3b82f6;
    --accent-light: rgba(59, 130, 246, 0.15);
    --accent-hover: #60a5fa;
    --accent-text: #93c5fd;
    --shadow-card: 0 1px 3px 0 rgba(0, 0, 0, 0.4);
    --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.5);
    --danger-light: rgba(239, 68, 68, 0.2);
    --danger-text: #fca5a5;
    --warning-light: rgba(245, 158, 11, 0.2);
    --warning-text: #fcd34d;
    --success-light: rgba(16, 185, 129, 0.2);
    --success-text: #6ee7b7;
    --p0-bg: rgba(220, 38, 38, 0.25);
    --p1-bg: rgba(217, 119, 6, 0.25);
    --p2-bg: rgba(37, 99, 235, 0.25);
    --p3-bg: rgba(100, 116, 139, 0.25);
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { -webkit-font-smoothing: antialiased; }
  body {
    background: var(--bg-app);
    color: var(--text-main);
    font: 14px/1.5 var(--font-sans);
    min-height: 100vh;
    display: flex;
    overflow-x: hidden;
  }

  /* ── Sidebar (左側導覽列) ────────────────────────── */
  .sidebar {
    width: var(--sidebar-w);
    background: var(--bg-surface);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    z-index: 50;
    transition: width 0.2s ease, transform 0.2s ease;
  }
  .sidebar.collapsed {
    width: var(--sidebar-collapsed-w);
  }
  .sidebar-header {
    height: var(--header-h);
    display: flex;
    align-items: center;
    padding: 0 16px;
    border-bottom: 1px solid var(--border);
    gap: 10px;
  }
  .brand-logo {
    width: 32px;
    height: 32px;
    border-radius: var(--radius-md);
    background: linear-gradient(135deg, var(--accent), #4f46e5);
    display: flex;
    align-items: center;
    justify-content: center;
    color: #ffffff;
    flex-shrink: 0;
    box-shadow: 0 2px 4px rgba(37, 99, 235, 0.3);
  }
  .brand-info {
    display: flex;
    flex-direction: column;
    overflow: hidden;
    white-space: nowrap;
  }
  .brand-title {
    font-size: 15px;
    font-weight: 700;
    color: var(--text-main);
    letter-spacing: -0.01em;
  }
  .brand-sub {
    font-size: 11px;
    font-weight: 500;
    color: var(--text-muted);
  }
  .brand-badge {
    display: inline-block;
    font-size: 9px;
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 4px;
    background: var(--accent-light);
    color: var(--accent-text);
    margin-left: 6px;
  }
  .sidebar.collapsed .brand-info { display: none; }

  .nav-menu {
    flex: 1;
    padding: 14px 10px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    overflow-y: auto;
  }
  .nav-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 9px 12px;
    border-radius: var(--radius-md);
    color: var(--text-muted);
    font-size: 13.5px;
    font-weight: 500;
    cursor: pointer;
    text-decoration: none;
    transition: background 0.15s, color 0.15s;
    user-select: none;
    border: none;
    background: transparent;
    width: 100%;
    text-align: left;
  }
  .nav-item:hover {
    background: var(--bg-subtle);
    color: var(--text-main);
  }
  .nav-item.active {
    background: var(--accent-light);
    color: var(--accent-text);
    font-weight: 600;
  }
  .nav-icon {
    width: 20px;
    height: 20px;
    flex-shrink: 0;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .sidebar.collapsed .nav-label { display: none; }

  .sidebar-footer {
    padding: 12px;
    border-top: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .collapse-btn {
    padding: 6px 8px;
    border-radius: var(--radius-sm);
    border: 1px solid var(--border);
    background: var(--bg-surface);
    color: var(--text-muted);
    cursor: pointer;
    font-size: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.15s;
  }
  .collapse-btn:hover {
    color: var(--text-main);
    border-color: var(--border-hover);
  }

  /* ── Main Layout Wrapper ─────────────────────────── */
  .layout-wrapper {
    flex: 1;
    margin-left: var(--sidebar-w);
    display: flex;
    flex-direction: column;
    min-width: 0;
    transition: margin-left 0.2s ease;
  }
  .sidebar.collapsed + .layout-wrapper {
    margin-left: var(--sidebar-collapsed-w);
  }

  /* ── Header (頂部功能列) ─────────────────────────── */
  header.top-header {
    height: var(--header-h);
    background: var(--bg-surface);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 24px;
    position: sticky;
    top: 0;
    z-index: 40;
  }
  .header-left {
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .mobile-toggle {
    display: none;
    border: 1px solid var(--border);
    background: var(--bg-surface);
    padding: 6px 8px;
    border-radius: var(--radius-sm);
    cursor: pointer;
  }
  .app-select-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .app-label {
    font-size: 11px;
    font-weight: 700;
    color: var(--text-subtle);
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }
  .app-selector {
    font-size: 13.5px;
    font-weight: 600;
    color: var(--text-main);
    background: var(--bg-subtle);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 6px 12px;
    cursor: pointer;
    outline: none;
    transition: border-color 0.15s, box-shadow 0.15s;
  }
  .app-selector:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--accent-light);
  }

  /* Date / Period Filter */
  .period-filters {
    display: inline-flex;
    background: var(--bg-subtle);
    padding: 3px;
    border-radius: var(--radius-md);
    border: 1px solid var(--border);
  }
  .period-btn {
    border: none;
    background: transparent;
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 500;
    color: var(--text-muted);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all 0.15s;
  }
  .period-btn.active {
    background: var(--bg-surface);
    color: var(--text-main);
    font-weight: 600;
    box-shadow: var(--shadow-sm);
  }

  .header-right {
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .search-box {
    position: relative;
    display: flex;
    align-items: center;
  }
  .search-input {
    width: 220px;
    height: 34px;
    padding: 0 12px 0 32px;
    font-size: 13px;
    border-radius: var(--radius-md);
    border: 1px solid var(--border);
    background: var(--bg-subtle);
    color: var(--text-main);
    outline: none;
    transition: width 0.2s, border-color 0.15s;
  }
  .search-input:focus {
    width: 280px;
    border-color: var(--accent);
    background: var(--bg-surface);
  }
  .search-icon {
    position: absolute;
    left: 10px;
    width: 14px;
    height: 14px;
    color: var(--text-subtle);
    pointer-events: none;
  }

  /* Sources Availability Chips in Header */
  .source-badges {
    display: flex;
    gap: 6px;
    align-items: center;
  }
  .src-chip {
    font-size: 11px;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: var(--radius-full);
    display: inline-flex;
    align-items: center;
    gap: 5px;
    background: var(--bg-subtle);
    color: var(--text-muted);
    border: 1px solid var(--border);
  }
  .src-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
  }
  .src-dot.available { background: var(--success); box-shadow: 0 0 0 2px var(--success-light); }
  .src-dot.unavailable { background: var(--text-subtle); }
  .src-dot.disabled { background: var(--text-subtle); }
  .src-dot.stale { background: var(--warning); box-shadow: 0 0 0 2px var(--warning-light); }
  .src-dot.insufficient_data { background: var(--accent); box-shadow: 0 0 0 2px var(--accent-light); }
  .src-dot.error { background: var(--danger); box-shadow: 0 0 0 2px var(--danger-light); }

  .theme-toggle-btn {
    width: 34px;
    height: 34px;
    border-radius: var(--radius-md);
    border: 1px solid var(--border);
    background: var(--bg-surface);
    color: var(--text-muted);
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: all 0.15s;
  }
  .theme-toggle-btn:hover {
    color: var(--text-main);
    border-color: var(--border-hover);
  }

  /* ── Content Area & Views ────────────────────────── */
  main.content-area {
    padding: 24px;
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 24px;
  }
  .view-container {
    display: none;
    flex-direction: column;
    gap: 20px;
    animation: fadeIn 0.18s ease-in-out;
  }
  .view-container.active {
    display: flex;
  }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  /* ── Section Titles ──────────────────────────────── */
  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 2px;
  }
  .section-title {
    font-size: 17px;
    font-weight: 700;
    color: var(--text-main);
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .section-subtitle {
    font-size: 13px;
    color: var(--text-muted);
    font-weight: 400;
  }

  /* ── KPI Cards (首頁指標卡) ──────────────────────── */
  .kpi-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
  }
  @media (max-width: 1100px) {
    .kpi-grid { grid-template-columns: repeat(2, 1fr); }
  }
  @media (max-width: 640px) {
    .kpi-grid { grid-template-columns: 1fr; }
  }

  .kpi-card {
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: 20px;
    box-shadow: var(--shadow-card);
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    position: relative;
    overflow: hidden;
    transition: transform 0.15s, box-shadow 0.15s;
  }
  .kpi-card:hover {
    box-shadow: var(--shadow-md);
  }
  .kpi-top {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    margin-bottom: 12px;
  }
  .kpi-meta {
    display: flex;
    flex-direction: column;
  }
  .kpi-title {
    font-size: 12.5px;
    font-weight: 600;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .kpi-icon-wrap {
    width: 36px;
    height: 36px;
    border-radius: var(--radius-md);
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--bg-subtle);
    color: var(--accent);
  }
  .kpi-value-row {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin-bottom: 8px;
  }
  .kpi-value {
    font-size: 28px;
    font-weight: 700;
    font-family: var(--font-mono);
    color: var(--text-main);
    letter-spacing: -0.02em;
    line-height: 1.1;
  }
  .kpi-value.unavailable-text {
    font-size: 22px;
    color: var(--text-subtle);
    font-weight: 600;
  }
  .kpi-badge-unavailable {
    display: inline-block;
    font-size: 10px;
    font-weight: 600;
    padding: 2px 6px;
    border-radius: 4px;
    background: var(--bg-muted);
    color: var(--text-muted);
  }

  .kpi-bottom {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 12px;
    color: var(--text-muted);
    padding-top: 10px;
    border-top: 1px solid var(--border-subtle);
  }
  .delta-pill {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    font-size: 11.5px;
    font-weight: 600;
    font-family: var(--font-mono);
  }
  .delta-pill.good { color: var(--success-text); }
  .delta-pill.bad { color: var(--danger-text); }
  .delta-pill.neutral { color: var(--text-muted); }

  /* Circular progress indicator */
  .ring-wrap {
    position: relative;
    width: 44px;
    height: 44px;
    flex-shrink: 0;
  }
  .ring-svg {
    transform: rotate(-90deg);
    width: 44px;
    height: 44px;
  }
  .ring-bg {
    stroke: var(--bg-muted);
    stroke-width: 4;
    fill: none;
  }
  .ring-progress {
    stroke: var(--accent);
    stroke-width: 4;
    fill: none;
    stroke-linecap: round;
    transition: stroke-dashoffset 0.8s ease;
  }
  .ring-progress.good { stroke: var(--success); }
  .ring-progress.warn { stroke: var(--warning); }
  .ring-progress.danger { stroke: var(--danger); }

  /* ── AI Insights Banner ──────────────────────────── */
  .ai-summary-card {
    background: linear-gradient(135deg, rgba(37, 99, 235, 0.04), rgba(79, 70, 229, 0.06));
    border: 1px solid rgba(37, 99, 235, 0.2);
    border-radius: var(--radius-lg);
    padding: 20px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .ai-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .ai-title {
    font-size: 14px;
    font-weight: 700;
    color: var(--accent-text);
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .ai-model-tag {
    font-size: 11px;
    font-family: var(--font-mono);
    color: var(--text-muted);
    background: var(--bg-surface);
    padding: 2px 8px;
    border-radius: var(--radius-full);
    border: 1px solid var(--border);
  }
  .ai-overview-text {
    font-size: 13.5px;
    color: var(--text-main);
    line-height: 1.6;
  }
  .ai-takeaways-list {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 10px;
    margin-top: 4px;
  }
  .ai-takeaway-item {
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 10px 14px;
    font-size: 12.5px;
    color: var(--text-main);
    display: flex;
    align-items: flex-start;
    gap: 8px;
    box-shadow: var(--shadow-sm);
  }

  /* ── Charts Grid ─────────────────────────────────── */
  .charts-grid {
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    gap: 16px;
  }
  .col-12 { grid-column: span 12; }
  .col-8  { grid-column: span 8; }
  .col-6  { grid-column: span 6; }
  .col-4  { grid-column: span 4; }
  @media (max-width: 960px) {
    .col-8, .col-6, .col-4 { grid-column: span 12; }
  }

  .chart-card {
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: 18px 20px;
    box-shadow: var(--shadow-card);
    display: flex;
    flex-direction: column;
    min-height: 280px;
  }
  .chart-card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
  }
  .chart-title {
    font-size: 14px;
    font-weight: 600;
    color: var(--text-main);
  }
  .chart-subtitle {
    font-size: 11.5px;
    color: var(--text-muted);
  }
  .chart-container {
    position: relative;
    flex: 1;
    min-height: 220px;
    width: 100%;
  }

  /* ── Issues Table & Lists ────────────────────────── */
  .table-container {
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-card);
    overflow: hidden;
  }
  .table-toolbar {
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
  }
  .filters-group {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }
  .filter-select {
    font-size: 12px;
    font-weight: 500;
    color: var(--text-main);
    background: var(--bg-subtle);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 5px 10px;
    cursor: pointer;
    outline: none;
  }

  table.data-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    text-align: left;
  }
  table.data-table th {
    background: var(--bg-subtle);
    color: var(--text-muted);
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 10px 16px;
    border-bottom: 1px solid var(--border);
    user-select: none;
    white-space: nowrap;
  }
  table.data-table td {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border-subtle);
    vertical-align: middle;
    color: var(--text-main);
  }
  table.data-table tr:hover td {
    background: var(--bg-subtle);
  }
  table.data-table tr:last-child td {
    border-bottom: none;
  }
  td.mono-num {
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
  }

  /* Badges and Chips */
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 11px;
    font-weight: 600;
    padding: 2px 7px;
    border-radius: 4px;
    white-space: nowrap;
  }
  .badge-p0 { background: var(--p0-bg); color: var(--p0); }
  .badge-p1 { background: var(--p1-bg); color: var(--p1); }
  .badge-p2 { background: var(--p2-bg); color: var(--p2); }
  .badge-p3 { background: var(--p3-bg); color: var(--p3); }

  .badge-fatal { background: var(--danger-light); color: var(--danger-text); }
  .badge-anr { background: var(--warning-light); color: var(--warning-text); }
  .badge-nonfatal { background: var(--bg-subtle); color: var(--text-muted); }

  .badge-lifecycle-new { background: #fee2e2; color: #991b1b; }
  .badge-lifecycle-persistent { background: #fef3c7; color: #92400e; }
  .badge-lifecycle-regressed { background: #ede9fe; color: #5b21b6; }
  .badge-lifecycle-resolved { background: #dcfce7; color: #166534; }
  .badge-lifecycle-not-observed { background: var(--bg-muted); color: var(--text-muted); }

  .badge-status {
    padding: 2px 8px;
    border-radius: var(--radius-full);
    font-size: 10.5px;
    font-weight: 600;
  }
  .badge-latest { background: var(--accent-light); color: var(--accent-text); }
  .badge-active { background: var(--success-light); color: var(--success-text); }
  .badge-maintenance { background: var(--warning-light); color: var(--warning-text); }
  .badge-deprecated { background: var(--bg-muted); color: var(--text-muted); }
  .badge-info { background: var(--accent-light); color: var(--accent-text); }

  /* ── Data Sources Health Card ── */
  .data-sources-card {
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: 16px 20px;
    margin-bottom: 20px;
    box-shadow: var(--shadow-sm);
  }
  .data-sources-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 14px;
    flex-wrap: wrap;
    gap: 8px;
  }
  .data-sources-title {
    font-size: 14px;
    font-weight: 700;
    color: var(--text-main);
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .data-sources-run-info {
    font-size: 12px;
    color: var(--text-muted);
  }
  .data-sources-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
  }
  @media (max-width: 992px) {
    .data-sources-grid { grid-template-columns: repeat(2, 1fr); }
  }
  @media (max-width: 576px) {
    .data-sources-grid { grid-template-columns: 1fr; }
  }
  .data-source-item {
    background: var(--bg-subtle);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    transition: border-color 0.15s;
  }
  .data-source-item:hover {
    border-color: var(--border-hover);
  }
  .data-source-item-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .data-source-name {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-main);
  }
  .data-source-freshness {
    font-size: 12px;
    color: var(--text-muted);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .data-source-note {
    font-size: 11.5px;
    color: var(--text-subtle);
    word-break: break-word;
    margin-top: 2px;
    line-height: 1.4;
  }
  .data-source-note.warning {
    color: var(--warning-text);
  }
  .data-source-note.error {
    color: var(--danger-text);
  }

  /* Issue Accordion Item */
  .issue-accordion {
    border-bottom: 1px solid var(--border);
  }
  .issue-accordion:last-child {
    border-bottom: none;
  }
  .issue-summary-row {
    padding: 14px 18px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 14px;
    transition: background 0.15s;
    user-select: none;
  }
  .issue-summary-row:hover {
    background: var(--bg-subtle);
  }
  .issue-rank {
    font-family: var(--font-mono);
    font-size: 14px;
    font-weight: 700;
    color: var(--text-subtle);
    min-width: 24px;
  }
  .issue-title-group {
    flex: 1;
    min-width: 0;
  }
  .issue-main-title {
    font-size: 13.5px;
    font-weight: 600;
    color: var(--text-main);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .issue-sub-title {
    font-size: 11.5px;
    font-family: var(--font-mono);
    color: var(--text-muted);
    margin-top: 2px;
  }
  .issue-stats-group {
    display: flex;
    align-items: center;
    gap: 16px;
    font-size: 12.5px;
    font-family: var(--font-mono);
    color: var(--text-muted);
    white-space: nowrap;
  }

  .issue-detail-panel {
    display: none;
    padding: 16px 20px 20px;
    background: var(--bg-subtle);
    border-top: 1px dashed var(--border);
    flex-direction: column;
    gap: 14px;
  }
  .issue-detail-panel.open {
    display: flex;
  }
  .detail-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 14px;
  }
  .detail-box {
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 12px 14px;
  }
  .detail-box-title {
    font-size: 11px;
    font-weight: 700;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-bottom: 6px;
  }
  .detail-box-content {
    font-size: 12.5px;
    color: var(--text-main);
    line-height: 1.5;
  }
  pre.code-stack {
    font-family: var(--font-mono);
    font-size: 11px;
    background: var(--bg-surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-sm);
    padding: 10px;
    max-height: 200px;
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-all;
    color: var(--text-main);
  }

  .btn-copy-prompt {
    font-size: 11.5px;
    font-weight: 600;
    padding: 6px 12px;
    border-radius: var(--radius-md);
    background: var(--accent);
    color: #ffffff;
    border: none;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    transition: background 0.15s;
    align-self: flex-start;
  }
  .btn-copy-prompt:hover {
    background: var(--accent-hover);
  }

  /* Empty state */
  .empty-state {
    padding: 48px 24px;
    text-align: center;
    color: var(--text-muted);
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
  }
  .empty-state-title {
    font-size: 15px;
    font-weight: 600;
    color: var(--text-main);
  }

  /* Responsive styles */
  @media (max-width: 768px) {
    .sidebar {
      transform: translateX(-100%);
    }
    .sidebar.mobile-open {
      transform: translateX(0);
    }
    .layout-wrapper {
      margin-left: 0 !important;
    }
    .mobile-toggle {
      display: inline-flex;
    }
    .search-input {
      width: 140px;
    }
    .search-input:focus {
      width: 180px;
    }
  }

  /* Toast Notification */
  .toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: var(--text-main);
    color: var(--bg-surface);
    padding: 10px 18px;
    border-radius: var(--radius-md);
    font-size: 13px;
    font-weight: 500;
    box-shadow: var(--shadow-lg);
    z-index: 999;
    opacity: 0;
    transform: translateY(8px);
    transition: all 0.2s ease;
    pointer-events: none;
  }
  .toast.show {
    opacity: 1;
    transform: translateY(0);
  }
</style>
</head>
<body>

<!-- ── Sidebar ───────────────────────────────────── -->
<aside class="sidebar" id="sidebar">
  <div class="sidebar-header">
    <div class="brand-logo">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/>
      </svg>
    </div>
    <div class="brand-info">
      <div class="brand-title">Crashlytics <span class="brand-badge">V2</span></div>
      <div class="brand-sub">Telemetry Dashboard</div>
    </div>
  </div>

  <nav class="nav-menu">
    <button class="nav-item active" onclick="switchView('overview')" id="nav-overview">
      <span class="nav-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>
      </span>
      <span class="nav-label">總覽 (Overview)</span>
    </button>
    <button class="nav-item" onclick="switchView('issues')" id="nav-issues">
      <span class="nav-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
      </span>
      <span class="nav-label">問題列表 (Issues)</span>
    </button>
    <button class="nav-item" onclick="switchView('version_health')" id="nav-version_health">
      <span class="nav-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
      </span>
      <span class="nav-label">版本健康度 (Version Health)</span>
    </button>
    <button class="nav-item" onclick="switchView('devices')" id="nav-devices">
      <span class="nav-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>
      </span>
      <span class="nav-label">裝置分析 (Devices)</span>
    </button>
    <button class="nav-item" onclick="switchView('releases')" id="nav-releases">
      <span class="nav-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/></svg>
      </span>
      <span class="nav-label">發佈版本 (Releases)</span>
    </button>
    <button class="nav-item" onclick="switchView('notifications')" id="nav-notifications">
      <span class="nav-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>
      </span>
      <span class="nav-label">通知 (Notifications)</span>
    </button>
    <button class="nav-item" onclick="switchView('ai_insights')" id="nav-ai_insights">
      <span class="nav-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>
      </span>
      <span class="nav-label">AI 分析 (AI Insights)</span>
    </button>
    <button class="nav-item" onclick="switchView('settings')" id="nav-settings">
      <span class="nav-icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
      </span>
      <span class="nav-label">設定 (Settings)</span>
    </button>
  </nav>

  <div class="sidebar-footer">
    <button class="collapse-btn" onclick="toggleSidebar()" title="折疊導覽列">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
    </button>
  </div>
</aside>

<!-- ── Layout Wrapper ────────────────────────────── -->
<div class="layout-wrapper">

  <!-- ── Top Header ──────────────────────────────── -->
  <header class="top-header">
    <div class="header-left">
      <button class="mobile-toggle" onclick="toggleMobileMenu()">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
      </button>

      <div class="app-select-wrap">
        <span class="app-label">App:</span>
        <select id="appSelector" class="app-selector" onchange="switchApp(this.value)">
          <!-- Populated dynamically -->
        </select>
      </div>

      <div class="period-filters">
        <button class="period-btn" onclick="setPeriod(7)" id="p-7d">7d</button>
        <button class="period-btn active" onclick="setPeriod(30)" id="p-30d">30d</button>
        <button class="period-btn" onclick="setPeriod(90)" id="p-90d">90d</button>
      </div>
    </div>

    <div class="header-right">
      <div class="search-box">
        <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input type="text" class="search-input" id="globalSearch" placeholder="搜尋問題、檔案、ID..." oninput="handleSearch(this.value)">
      </div>

      <div class="source-badges" id="headerSourceBadges">
        <!-- Populated dynamically -->
      </div>

      <button class="theme-toggle-btn" onclick="toggleTheme()" title="切換深/淺色主題">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>
      </button>
    </div>
  </header>

  <!-- ── Main Content Area ────────────────────────── -->
  <main class="content-area">

    <!-- VIEW: OVERVIEW (總覽) -->
    <section class="view-container active" id="view-overview">
      <div class="section-header">
        <div>
          <h2 class="section-title">總覽 (Overview)</h2>
          <div class="section-subtitle" id="overviewPeriodSubtitle">載入中...</div>
        </div>
      </div>

      <!-- KPI Cards -->
      <div class="kpi-grid">
        <!-- KPI 1: Crash-free Users -->
        <div class="kpi-card" id="cardCrashFreeUsers">
          <div class="kpi-top">
            <div class="kpi-meta">
              <span class="kpi-title">無當機用戶率 <small style="font-weight: normal; opacity: 0.7;">(Crash-free Users)</small></span>
              <div class="kpi-value-row" id="cfUsersValueRow">
                <span class="kpi-value" id="kpiCFUsers">—</span>
              </div>
            </div>
            <div class="ring-wrap" id="cfUsersRing">
              <svg class="ring-svg" viewBox="0 0 44 44">
                <circle class="ring-bg" cx="22" cy="22" r="18"/>
                <circle class="ring-progress good" id="cfUsersProgress" cx="22" cy="22" r="18" stroke-dasharray="113.097" stroke-dashoffset="113.097"/>
              </svg>
            </div>
          </div>
          <div class="kpi-bottom">
            <span class="delta-pill" id="kpiCFUsersDelta">—</span>
            <span id="kpiCFUsersCounts">—</span>
          </div>
        </div>

        <!-- KPI 2: Crash Events -->
        <div class="kpi-card" id="cardCrashEvents">
          <div class="kpi-top">
            <div class="kpi-meta">
              <span class="kpi-title">當機事件總數 <small style="font-weight: normal; opacity: 0.7;">(Crash Events)</small></span>
              <div class="kpi-value-row">
                <span class="kpi-value" id="kpiEvents">0</span>
              </div>
            </div>
            <div class="kpi-icon-wrap">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
            </div>
          </div>
          <div class="kpi-bottom">
            <span class="delta-pill" id="kpiEventsDelta">—</span>
            <span id="kpiErrorBreakdown">Fatal: 0 · ANR: 0</span>
          </div>
        </div>

        <!-- KPI 3: Affected Users -->
        <div class="kpi-card" id="cardAffectedUsers">
          <div class="kpi-top">
            <div class="kpi-meta">
              <span class="kpi-title">受影響人數 <small style="font-weight: normal; opacity: 0.7;">(Affected Users)</small></span>
              <div class="kpi-value-row">
                <span class="kpi-value" id="kpiUsers">0</span>
              </div>
            </div>
            <div class="kpi-icon-wrap">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
            </div>
          </div>
          <div class="kpi-bottom">
            <span class="delta-pill" id="kpiUsersDelta">—</span>
            <span>去重受影響用戶</span>
          </div>
        </div>

        <!-- KPI 4: New Issues -->
        <div class="kpi-card" id="cardNewIssues">
          <div class="kpi-top">
            <div class="kpi-meta">
              <span class="kpi-title">新增問題數 <small style="font-weight: normal; opacity: 0.7;">(New Issues)</small></span>
              <div class="kpi-value-row">
                <span class="kpi-value" id="kpiNewIssues">0</span>
              </div>
            </div>
            <div class="kpi-icon-wrap">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>
            </div>
          </div>
          <div class="kpi-bottom">
            <span class="delta-pill" id="kpiNewIssuesDelta">—</span>
            <span>本期首見問題數</span>
          </div>
        </div>
      </div>

      <!-- Data Sources Health -->
      <div class="data-sources-card" id="overviewDataSourcesCard">
        <div class="data-sources-header">
          <div class="data-sources-title">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
            資料來源健康度 (Data Sources Health)
          </div>
          <div class="data-sources-run-info" id="overviewLatestRunInfo">載入中...</div>
        </div>
        <div class="data-sources-grid" id="overviewDataSourcesGrid">
          <!-- Populated dynamically -->
        </div>
      </div>

      <!-- AI Quick Insights -->
      <div class="ai-summary-card" id="aiQuickCard">
        <div class="ai-header">
          <div class="ai-title">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/></svg>
            AI 策略摘要 (AI Insights)
          </div>
          <span class="ai-model-tag" id="aiModelTag">Gemini Flash</span>
        </div>
        <p class="ai-overview-text" id="aiOverviewText">載入中...</p>
        <div class="ai-takeaways-list" id="aiTakeawaysList"></div>
      </div>

      <!-- Trend & Breakdown Charts -->
      <div class="charts-grid">
        <div class="chart-card col-8">
          <div class="chart-card-header">
            <div>
              <div class="chart-title">每日趨勢 (Daily Trend)</div>
              <div class="chart-subtitle" id="dailyTrendChartSubtitle">事件數與受影響用戶每日變化</div>
            </div>
          </div>
          <div class="chart-container">
            <canvas id="chartDailyTrend"></canvas>
          </div>
        </div>

        <div class="chart-card col-4">
          <div class="chart-card-header">
            <div>
              <div class="chart-title">錯誤類型佔比 (Error Types)</div>
              <div class="chart-subtitle">Fatal vs ANR vs Non-fatal</div>
            </div>
          </div>
          <div class="chart-container">
            <canvas id="chartErrorTypes"></canvas>
          </div>
        </div>

        <div class="chart-card col-6">
          <div class="chart-card-header">
            <div>
              <div class="chart-title">平台分布 (Platforms)</div>
              <div class="chart-subtitle">Android vs iOS 事件與用戶佔比</div>
            </div>
          </div>
          <div class="chart-container">
            <canvas id="chartPlatforms"></canvas>
          </div>
        </div>

        <div class="chart-card col-6">
          <div class="chart-card-header">
            <div>
              <div class="chart-title">Top App 版本分布</div>
              <div class="chart-subtitle">依崩潰事件量排序</div>
            </div>
          </div>
          <div class="chart-container">
            <canvas id="chartAppVersions"></canvas>
          </div>
        </div>
      </div>

      <!-- Top Issues Quick Preview -->
      <div class="table-container">
        <div class="table-toolbar">
          <div class="section-title" style="font-size:14px">優先關注問題 (Top Priority Issues)</div>
          <button class="filter-select" onclick="switchView('issues')">查看完整問題列表 →</button>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>等級</th>
              <th>標題與位置</th>
              <th>平台</th>
              <th>層級</th>
              <th>生命週期</th>
              <th>事件數</th>
              <th>受影響用戶</th>
              <th>最新版本</th>
            </tr>
          </thead>
          <tbody id="topIssuesPreviewBody"></tbody>
        </table>
      </div>
    </section>

    <!-- VIEW: ISSUES (問題列表) -->
    <section class="view-container" id="view-issues">
      <div class="section-header">
        <div>
          <h2 class="section-title">問題列表 (Issues)</h2>
          <div class="section-subtitle">點擊展開查看 AI 建議修法、元兇程式碼與 Stack Trace</div>
        </div>
      </div>

      <div class="table-container">
        <div class="table-toolbar">
          <div class="filters-group">
            <select class="filter-select" id="filterErrorType" onchange="renderIssuesList()">
              <option value="ALL">全部層級</option>
              <option value="FATAL">FATAL (致命閃退)</option>
              <option value="ANR">ANR (無回應)</option>
              <option value="NON_FATAL">NON_FATAL (非致命)</option>
            </select>
            <select class="filter-select" id="filterPlatform" onchange="renderIssuesList()">
              <option value="ALL">全部平台</option>
              <option value="android">Android</option>
              <option value="ios">iOS</option>
            </select>
            <select class="filter-select" id="filterPriority" onchange="renderIssuesList()">
              <option value="ALL">全部優先級</option>
              <option value="P0">P0</option>
              <option value="P1">P1</option>
              <option value="P2">P2</option>
              <option value="P3">P3</option>
            </select>
            <select class="filter-select" id="filterLifecycle" onchange="renderIssuesList()">
              <option value="ALL">全部生命週期</option>
              <option value="new_in_latest">🔴 新版引入</option>
              <option value="persistent">🟡 持續存在</option>
              <option value="regressed">🟣 回歸</option>
              <option value="resolved">🟢 已收斂</option>
              <option value="not_observed_latest">⚪ 最新版未見</option>
            </select>
            <select class="filter-select" id="sortIssuesSelect" onchange="setSort(this.value)">
              <option value="priority">依優先級 (P0 ~ P3)</option>
              <option value="events">依事件數 (Events)</option>
              <option value="users">依受影響用戶 (Users)</option>
              <option value="last_seen">依最近出現時間</option>
            </select>
          </div>
          <div id="issuesCountBadge" style="font-size:12px;color:var(--text-muted)">共 0 個問題</div>
        </div>

        <div id="issuesListContainer">
          <!-- Accordion list items -->
        </div>
      </div>
    </section>

    <!-- VIEW: VERSION HEALTH (版本健康度) -->
    <section class="view-container" id="view-version_health">
      <div class="section-header">
        <div>
          <h2 class="section-title">版本健康度 (Version Health)</h2>
          <div class="section-subtitle">各發佈版本之 Crash-free 指標、採納率與穩定趨勢</div>
        </div>
      </div>

      <div class="table-container">
        <table class="data-table">
          <thead>
            <tr>
              <th>版本號</th>
              <th>平台</th>
              <th>發佈日期</th>
              <th>狀態</th>
              <th>趨勢</th>
              <th>Crash-free (Users)</th>
              <th>Crash-free (Sessions)</th>
              <th>採納率</th>
              <th>崩潰事件數</th>
              <th>受影響用戶</th>
            </tr>
          </thead>
          <tbody id="versionHealthTableBody"></tbody>
        </table>
      </div>
    </section>

    <!-- VIEW: DEVICES (裝置分析) -->
    <section class="view-container" id="view-devices">
      <div class="section-header">
        <div>
          <h2 class="section-title">裝置與系統分析 (Devices & OS)</h2>
          <div class="section-subtitle">主要崩潰機型、作業系統版本分布與自訂 Key 交叉分析</div>
        </div>
      </div>

      <div class="charts-grid">
        <div class="chart-card col-6">
          <div class="chart-card-header">
            <div>
              <div class="chart-title">機型崩潰排行 (Device Models)</div>
              <div class="chart-subtitle">事件數最高之裝置型號</div>
            </div>
          </div>
          <div class="chart-container">
            <canvas id="chartDeviceModels"></canvas>
          </div>
        </div>

        <div class="chart-card col-6">
          <div class="chart-card-header">
            <div>
              <div class="chart-title">OS 版本排行 (OS Versions)</div>
              <div class="chart-subtitle">各作業系統版本分布</div>
            </div>
          </div>
          <div class="chart-container">
            <canvas id="chartOSVersions"></canvas>
          </div>
        </div>
      </div>

      <div class="table-container" style="margin-top:16px">
        <div class="table-toolbar">
          <div class="section-title" style="font-size:14px">機型詳細分布</div>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>機型名稱</th>
              <th>平台</th>
              <th>崩潰事件數</th>
              <th>受影響用戶</th>
              <th>佔比</th>
            </tr>
          </thead>
          <tbody id="deviceModelsTableBody"></tbody>
        </table>
      </div>
    </section>

    <!-- VIEW: RELEASES (發佈版本) -->
    <section class="view-container" id="view-releases">
      <div class="section-header">
        <div>
          <h2 class="section-title">發佈版本 (Releases)</h2>
          <div class="section-subtitle">版本發佈歷程與穩定度追蹤</div>
        </div>
      </div>
      <div class="table-container">
        <table class="data-table">
          <thead>
            <tr>
              <th>版本號</th>
              <th>發佈日期</th>
              <th>狀態</th>
              <th>事件數</th>
              <th>用戶數</th>
              <th>Crash-free (Users)</th>
              <th>採納率</th>
            </tr>
          </thead>
          <tbody id="releasesTableBody"></tbody>
        </table>
      </div>
    </section>

    <!-- VIEW: NOTIFICATIONS (通知與管道狀態) -->
    <section class="view-container" id="view-notifications">
      <div class="section-header">
        <div>
          <h2 class="section-title">數據管道與通知 (Data Pipelines)</h2>
          <div class="section-subtitle">數據來源連線狀態、最後同步時間與資料限制備註</div>
        </div>
      </div>

      <div class="charts-grid" id="pipelineCardsGrid">
        <!-- Filled dynamically -->
      </div>

      <div class="ai-summary-card" style="margin-top:16px" id="limitationsCard">
        <div class="ai-title" style="color:var(--text-main)">資料收集限制與注意事項 (Data Limitations)</div>
        <ul id="limitationsList" style="padding-left:20px;font-size:13px;color:var(--text-muted);display:flex;flex-direction:column;gap:6px"></ul>
      </div>
    </section>

    <!-- VIEW: AI INSIGHTS (AI 分析) -->
    <section class="view-container" id="view-ai_insights">
      <div class="section-header">
        <div>
          <h2 class="section-title">AI 分析與行動建議 (AI Insights)</h2>
          <div class="section-subtitle">Gemini 深入分析、分佈洞察與建議行動清單</div>
        </div>
      </div>

      <div class="ai-summary-card">
        <div class="ai-header">
          <div class="ai-title">AI 策略摘要</div>
          <span class="ai-model-tag" id="aiFullModelTag">Gemini Flash</span>
        </div>
        <p class="ai-overview-text" id="aiFullOverviewText"></p>
        <div class="ai-takeaways-list" id="aiFullTakeawaysList"></div>
        <div style="margin-top:12px;font-size:13px;color:var(--text-main);background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--radius-md);padding:12px 14px">
          <b>分佈洞察：</b> <span id="aiDistributionInsights"></span>
        </div>
      </div>

      <div class="table-container" style="margin-top:16px">
        <div class="table-toolbar">
          <div class="section-title" style="font-size:14px">推薦行動清單 (Recommended Actions)</div>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>優先級</th>
              <th>目標 Issue</th>
              <th>建議行動</th>
              <th>預估工作量</th>
            </tr>
          </thead>
          <tbody id="recommendedActionsTableBody"></tbody>
        </table>
      </div>
    </section>

    <!-- VIEW: SETTINGS (設定) -->
    <section class="view-container" id="view-settings">
      <div class="section-header">
        <div>
          <h2 class="section-title">應用程式設定 (Settings)</h2>
          <div class="section-subtitle">當前 App Metadata、監控自訂鍵值與系統資訊</div>
        </div>
      </div>

      <div class="table-container">
        <table class="data-table">
          <tbody id="settingsTableBody"></tbody>
        </table>
      </div>
    </section>

  </main>
</div>

<div class="toast" id="toast">已複製到剪貼簿</div>

<!-- Chart.js Embedded -->
<script>
__CHARTJS__
</script>

<!-- Embedded Dashboard V2 Bundle Data & Client Logic -->
<script>
const DATA = __DATA__;
let curAppId = DATA.default_app || Object.keys(DATA.apps || {})[0];
let curPeriodDays = 30;
let searchQuery = "";
let chartInstances = {};

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = n => (n != null && !isNaN(n)) ? Number(n).toLocaleString("zh-Hant") : "0";

// Theme support
try {
  const savedTheme = localStorage.getItem("ct_theme") || "light";
  document.documentElement.dataset.theme = savedTheme;
} catch (e) {}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("ct_theme", next); } catch (e) {}
  renderCharts();
}

function toggleSidebar() {
  $("sidebar").classList.toggle("collapsed");
}

function toggleMobileMenu() {
  $("sidebar").classList.toggle("mobile-open");
}

function showToast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2000);
}

// Navigation between views
function switchView(viewName) {
  document.querySelectorAll(".nav-item").forEach(el => el.classList.remove("active"));
  const btn = $("nav-" + viewName);
  if (btn) btn.classList.add("active");

  document.querySelectorAll(".view-container").forEach(el => el.classList.remove("active"));
  const v = $("view-" + viewName);
  if (v) v.classList.add("active");

  if (window.innerWidth <= 768) {
    $("sidebar").classList.remove("mobile-open");
  }
}

function isUsablePeriodSnapshot(snap) {
  if (!snap) return false;
  if (snap.status === "error" || snap.status === "insufficient_data") return false;
  const usStatus = snap.kpi?.affected_users?.status;
  if (usStatus === "error" || usStatus === "insufficient_data") return false;
  return true;
}

function switchApp(appId) {
  if (DATA.apps && DATA.apps[appId]) {
    curAppId = appId;
    $("appSelector").value = appId;
    const app = DATA.apps[appId];
    if (app.periods) {
      const curSnap = app.periods[String(curPeriodDays)];
      if (!isUsablePeriodSnapshot(curSnap)) {
        const avail = Object.keys(app.periods)
          .map(Number)
          .filter(d => isUsablePeriodSnapshot(app.periods[String(d)]))
          .sort((a,b) => a - b);
        curPeriodDays = avail.includes(30) ? 30 : (avail[0] || (Object.keys(app.periods).map(Number).sort((a,b) => a - b)[0] || 30));
      }
    }
    renderAll();
  }
}

let curSortField = "priority";
let curSortAsc = false;

function setPeriod(days) {
  const app = getCurAppData();
  if (!app) return;
  const availableDays = (app?.daily_trend || []).length || app?.period?.days || 30;
  const snap = app.periods ? app.periods[String(days)] : null;
  const hasPeriod = (snap != null) || (!app.periods && days <= availableDays);
  if (!hasPeriod) {
    showToast(`目前資料無 ${days} 天之獨立數據`);
    return;
  }
  if (snap && !isUsablePeriodSnapshot(snap)) {
    showToast(`${days} 天數據查詢異常：${snap.error_message || "權威彙總缺失"}`);
    return;
  }
  curPeriodDays = Number(days);
  renderAll();
}

function setSort(field) {
  if (curSortField === field) {
    curSortAsc = !curSortAsc;
  } else {
    curSortField = field;
    curSortAsc = false;
  }
  renderIssuesList();
}

function handleSearch(val) {
  searchQuery = (val || "").trim().toLowerCase();
  renderIssuesList();
}

function getCurAppData() {
  return (DATA.apps && DATA.apps[curAppId]) ? DATA.apps[curAppId] : null;
}

function getCurPeriodSnapshot() {
  const app = getCurAppData();
  if (!app) return null;
  if (app.periods) {
    const snap = app.periods[String(curPeriodDays)];
    if (snap && isUsablePeriodSnapshot(snap)) {
      return snap;
    }
    const avail = Object.keys(app.periods)
      .map(Number)
      .filter(d => isUsablePeriodSnapshot(app.periods[String(d)]))
      .sort((a,b) => a - b);
    if (avail.length > 0) {
      const fallbackDays = avail.includes(30) ? 30 : avail[0];
      return app.periods[String(fallbackDays)];
    }
    if (snap) return snap;
  }
  return {
    period: app.period || { days: curPeriodDays },
    kpi: app.kpi || {},
    daily_trend: app.daily_trend || [],
    version_health: app.version_health || [],
    distributions: app.distributions || {},
    top_issues: app.top_issues || [],
    ai_summary: app.ai_summary || {},
  };
}

// ── Data Freshness and Source Health Resolution (Issue #23) ──
function formatFreshness(isoStr, refIsoStr) {
  if (!isoStr) return "—";
  const d = new Date(isoStr);
  if (isNaN(d.getTime())) return isoStr;
  const now = refIsoStr ? new Date(refIsoStr) : new Date();
  const diffMs = Math.max(0, now.getTime() - d.getTime());
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHours = Math.floor(diffMin / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffMin < 2) return "本次同步";
  if (diffMin < 60) return `${diffMin} 分鐘前`;
  if (diffHours < 24) return `${diffHours} 小時前`;
  if (diffDays < 30) return `${diffDays} 天前`;
  const diffMonths = Math.floor(diffDays / 30);
  return `${diffMonths} 個月前`;
}

function resolveSourceHealth(sourceKey, srcObj, generatedAt) {
  if (!srcObj) {
    return {
      status: "unavailable",
      label: "未提供",
      badgeClass: "badge-deprecated",
      dotClass: "disabled",
      freshness: "—",
      timestamp: "—",
      note: "無來源設定",
      isSupplemental: false,
    };
  }

  const rawStatus = (srcObj.status || "").toLowerCase();
  const errMsg = srcObj.error_message || "";
  const ts = srcObj.last_sync_timestamp;
  const freshness = formatFreshness(ts, generatedAt);

  let status = rawStatus || "unavailable";
  let label = "正常";
  let badgeClass = "badge-active";
  let dotClass = "available";
  let note = errMsg || "";
  let isSupplemental = false;

  const isExplicitDisabled = status === "disabled" ||
    errMsg.includes("disabled") ||
    errMsg.includes("已停用") ||
    errMsg.includes("未開啟") ||
    errMsg.includes("not configured");

  const isStale = status === "stale" ||
    (status === "available" && (errMsg.includes("過期") || errMsg.includes("stale")));

  if (isExplicitDisabled) {
    status = "disabled";
    label = "— 未開啟";
    badgeClass = "badge-deprecated";
    dotClass = "disabled";
    if (!note) note = "來源未啟用";
  } else if (isStale) {
    status = "stale";
    label = "⚠ 過期";
    badgeClass = "badge-maintenance";
    dotClass = "stale";
    isSupplemental = true;
    if (!note) note = "快取已逾期（使用上次快取中）";
    else if (!note.includes("使用")) note = `${note}（使用上次快取中）`;
  } else if (status === "error" || (status !== "available" && errMsg && !ts)) {
    status = "error";
    label = "✕ 錯誤";
    badgeClass = "badge-fatal";
    dotClass = "error";
    if (!note) note = "資料來源查詢或連線失敗";
  } else if (status === "insufficient_data") {
    status = "insufficient_data";
    label = "ℹ 資料不足";
    badgeClass = "badge-info";
    dotClass = "insufficient_data";
    if (!note) note = "區間內無足夠資料";
  } else if (status === "available") {
    status = "available";
    label = "✓ 正常";
    badgeClass = "badge-active";
    dotClass = "available";
    if (!note) note = "資料來源正常運作";
  } else {
    status = "unavailable";
    label = "未提供";
    badgeClass = "badge-deprecated";
    dotClass = "disabled";
  }

  return {
    status,
    label,
    badgeClass,
    dotClass,
    freshness,
    timestamp: ts || "—",
    note,
    isSupplemental,
    raw: srcObj,
  };
}

// Render Header Elements
function renderHeader() {
  const sel = $("appSelector");
  const apps = DATA.apps || {};
  sel.innerHTML = Object.keys(apps).map(id => {
    const meta = apps[id].metadata || {};
    const name = meta.display_name || id;
    return `<option value="${esc(id)}" ${id === curAppId ? "selected" : ""}>${esc(name)} (${id})</option>`;
  }).join("");

  const app = getCurAppData();
  if (!app) return;

  // Period buttons availability verification
  const availableDays = (app.daily_trend || []).length || app.period?.days || 30;
  [7, 30, 90].forEach(d => {
    const btn = $("p-" + d + "d");
    if (btn) {
      const snap = app.periods ? app.periods[String(d)] : null;
      const hasPeriod = (snap != null) || (!app.periods && d <= availableDays);
      const isUsable = snap ? isUsablePeriodSnapshot(snap) : (!app.periods && d <= availableDays);
      if (!hasPeriod) {
        btn.disabled = true;
        btn.title = `目前資料無 ${d} 天之獨立數據`;
        btn.classList.add("disabled");
        btn.style.opacity = "0.45";
        btn.style.cursor = "not-allowed";
      } else if (!isUsable) {
        btn.disabled = true;
        btn.title = `${d} 天數據異常：${snap?.error_message || "權威彙總缺失"}`;
        btn.classList.add("disabled");
        btn.style.opacity = "0.45";
        btn.style.cursor = "not-allowed";
      } else {
        btn.disabled = false;
        btn.title = `切換為 ${d} 天數據`;
        btn.classList.remove("disabled");
        btn.style.opacity = "1";
        btn.style.cursor = "pointer";
      }
    }
  });
  if (app.periods) {
    const curSnap = app.periods[String(curPeriodDays)];
    if (!isUsablePeriodSnapshot(curSnap)) {
      const avail = Object.keys(app.periods)
        .map(Number)
        .filter(d => isUsablePeriodSnapshot(app.periods[String(d)]))
        .sort((a,b) => a - b);
      curPeriodDays = avail.includes(30) ? 30 : (avail[0] || (Object.keys(app.periods).map(Number).sort((a,b) => a - b)[0] || 30));
    }
  } else if (curPeriodDays > availableDays) {
    curPeriodDays = availableDays >= 30 ? 30 : (availableDays >= 7 ? 7 : availableDays);
  }
  document.querySelectorAll(".period-btn").forEach(b => b.classList.remove("active"));
  const activeBtn = $("p-" + (curPeriodDays || 30) + "d");
  if (activeBtn && !activeBtn.disabled) activeBtn.classList.add("active");

  const srcs = app.sources || {};
  const badgeWrap = $("headerSourceBadges");
  if (badgeWrap) {
    const aiSrc = srcs.ai || srcs.gemini_ai;
    const aiName = (aiSrc && aiSrc.provider === "openrouter") ? "OpenRouter AI" : "Gemini AI";
    const sKeys = [
      ["BigQuery", "crashlytics_bq", srcs.crashlytics_bq],
      ["Sessions", "firebase_sessions", srcs.firebase_sessions],
      ["MCP", "mcp_crashlytics", srcs.mcp_crashlytics],
      [aiName, (srcs.ai ? "ai" : "gemini_ai"), aiSrc]
    ];
    badgeWrap.innerHTML = sKeys.map(([name, key, obj]) => {
      const res = resolveSourceHealth(key, obj, DATA.generated_at);
      return `<span class="src-chip" title="${name}: ${res.label} (${res.freshness}) · ${esc(res.note)}"><span class="src-dot ${res.dotClass}"></span>${name}</span>`;
    }).join("");
  }
}

// Render Data Sources Health in Overview
function renderDataSourcesHealth() {
  const app = getCurAppData();
  if (!app) return;
  const grid = $("overviewDataSourcesGrid");
  if (!grid) return;

  const runInfo = $("overviewLatestRunInfo");
  if (runInfo) {
    if (DATA.pipeline_run) {
      const pr = DATA.pipeline_run;
      const appRun = pr.apps && pr.apps[curAppId];
      const runStatus = appRun ? appRun.status : pr.status;
      const icon = runStatus === "success" ? "✓" : (runStatus === "degraded" ? "⚠" : "✕");
      const statusText = runStatus === "success" ? "同步正常" : (runStatus === "degraded" ? "部分降級" : "同步失敗");
      const dur = pr.duration_sec != null ? ` · 耗時 ${pr.duration_sec}s` : "";
      runInfo.innerHTML = `<span class="badge badge-status ${runStatus === 'success' ? 'badge-active' : (runStatus === 'degraded' ? 'badge-maintenance' : 'badge-fatal')}">${icon} ${statusText}</span> 最近執行: ${esc(formatFreshness(pr.finished_at, DATA.generated_at))}${dur}`;
    } else {
      runInfo.textContent = `報表產生於 ${formatFreshness(DATA.generated_at, null)}`;
    }
  }

  const srcs = app.sources || {};
  const aiSrc = srcs.ai || srcs.gemini_ai;
  const aiName = (aiSrc && aiSrc.provider === "openrouter") ? "OpenRouter AI" : "Gemini AI";
  const sourcesList = [
    { key: "crashlytics_bq", name: "Crashlytics BigQuery", obj: srcs.crashlytics_bq },
    { key: "firebase_sessions", name: "Firebase Sessions", obj: srcs.firebase_sessions },
    { key: "mcp_crashlytics", name: "Crashlytics MCP", obj: srcs.mcp_crashlytics },
    { key: (srcs.ai ? "ai" : "gemini_ai"), name: aiName, obj: aiSrc },
  ];

  grid.innerHTML = sourcesList.map(s => {
    const res = resolveSourceHealth(s.key, s.obj, DATA.generated_at);
    return `
      <div class="data-source-item">
        <div class="data-source-item-top">
          <span class="data-source-name">${esc(s.name)}</span>
          <span class="badge badge-status ${res.badgeClass}">${esc(res.label)}</span>
        </div>
        <div class="data-source-freshness">
          <span>新鮮度</span>
          <b>${esc(res.freshness)}</b>
        </div>
        <div class="data-source-note ${res.status === 'error' ? 'error' : (res.status === 'stale' ? 'warning' : '')}">
          ${esc(res.note)}
        </div>
      </div>
    `;
  }).join("");
}

// Render KPI Cards
function renderKPIs() {
  const app = getCurAppData();
  if (!app) return;
  const snap = getCurPeriodSnapshot();
  if (!snap) return;

  const kpi = snap.kpi || {};
  const meta = app.metadata || {};
  const period = snap.period || {};
  const srcs = app.sources || {};

  const pDays = period.days || curPeriodDays || 30;
  const startStr = (period.start_time || "").slice(0, 10);
  const endStr = (period.end_time || "").slice(0, 10);
  $("overviewPeriodSubtitle").textContent = `${meta.display_name || curAppId} · 總覽指標 (${pDays} 天：${startStr} ~ ${endStr})`;

  // Authoritative metrics directly from Schema V2 (no client-side derivation)
  const totalEvents = kpi.crash_events?.value || 0;
  const totalUsers = kpi.affected_users?.value || 0;
  const fatalEvents = kpi.events_by_error_type?.fatal || 0;
  const anrEvents = kpi.events_by_error_type?.anr || 0;
  const nonFatalEvents = kpi.events_by_error_type?.non_fatal || 0;

  // 1. Crash-free Users
  const cfu = kpi.crash_free_users || {};
  const cfuVal = $("kpiCFUsers");
  const cfuProg = $("cfUsersProgress");
  const cfuDelta = $("kpiCFUsersDelta");
  const cfuCounts = $("kpiCFUsersCounts");

  if (cfu.status === "available" && cfu.rate != null) {
    const ratePct = (cfu.rate * 100).toFixed(2);
    cfuVal.innerHTML = `${ratePct}<span style="font-size:16px;font-weight:600;margin-left:2px">%</span>`;
    cfuVal.classList.remove("unavailable-text");

    const circumference = 2 * Math.PI * 18; // ~113.097
    const offset = circumference * (1 - cfu.rate);
    cfuProg.style.strokeDashoffset = offset;

    if (cfu.change_pct_points != null) {
      const isUp = cfu.change_pct_points >= 0;
      cfuDelta.className = `delta-pill ${isUp ? "good" : "bad"}`;
      cfuDelta.innerHTML = `${isUp ? "▲ +" : "▼ "}${cfu.change_pct_points.toFixed(2)}% vs 上期`;
    } else {
      cfuDelta.className = "delta-pill neutral";
      cfuDelta.textContent = "無基期";
    }
    cfuCounts.textContent = `${fmt(cfu.crashed)} 崩潰 / ${fmt(cfu.total)} 用戶`;
  } else {
    // Explicit Unavailable / Error / Insufficient Data semantics - strictly no 0%
    cfuProg.style.strokeDashoffset = 113.097;
    cfuVal.classList.add("unavailable-text");
    cfuDelta.className = "delta-pill neutral";

    const reason = cfu.unavailable_reason || (srcs.firebase_sessions ? srcs.firebase_sessions.error_message : "") || "";
    const isExplicitlyDisabled = reason.toLowerCase().includes("disabled") || reason.includes("已停用") || reason.includes("未開啟");

    if (cfu.status === "error") {
      cfuVal.innerHTML = `<span class="kpi-badge-unavailable" style="background:var(--danger-light);color:var(--danger-text)">錯誤</span>`;
      cfuDelta.textContent = "BigQuery 查詢失敗";
      cfuCounts.textContent = reason || "請檢查查詢或權限";
    } else if (cfu.status === "insufficient_data") {
      cfuVal.innerHTML = `<span class="kpi-badge-unavailable" style="background:var(--warning-light);color:var(--warning-text)">資料不足</span>`;
      cfuDelta.textContent = "連線樣本過少";
      cfuCounts.textContent = reason || "無法計算無當機率";
    } else if (isExplicitlyDisabled) {
      cfuVal.innerHTML = `<span class="kpi-badge-unavailable">未開啟</span>`;
      cfuDelta.textContent = "未開啟連線統計";
      cfuCounts.textContent = "缺少總上線人數";
    } else {
      cfuVal.innerHTML = `<span class="kpi-badge-unavailable">無資料</span>`;
      cfuDelta.textContent = "未找到 Sessions 資料";
      cfuCounts.textContent = reason || "未匯出連線資料";
    }
  }

  // 2. Crash Events
  const ev = kpi.crash_events || {};
  if (ev.status === "error") {
    $("kpiEvents").innerHTML = `<span style="font-size:16px;color:var(--danger-text)">查詢失敗</span>`;
    $("kpiEventsDelta").className = "delta-pill neutral";
    $("kpiEventsDelta").textContent = "BigQuery 錯誤";
  } else {
    $("kpiEvents").textContent = fmt(totalEvents);
    const evDelta = $("kpiEventsDelta");
    if (ev.change_pct != null) {
      const isDown = ev.change_pct <= 0;
      evDelta.className = `delta-pill ${isDown ? "good" : "bad"}`;
      evDelta.innerHTML = `${ev.change_pct > 0 ? "▲ +" : "▼ "}${ev.change_pct.toFixed(1)}% vs 上期`;
    } else {
      evDelta.className = "delta-pill neutral";
      evDelta.textContent = "無基期";
    }
  }
  $("kpiErrorBreakdown").textContent = `Fatal: ${fmt(fatalEvents)} · ANR: ${fmt(anrEvents)} · Non-fatal: ${fmt(nonFatalEvents)}`;

  // 3. Affected Users
  const us = kpi.affected_users || {};
  if (us.status === "error") {
    $("kpiUsers").innerHTML = `<span style="font-size:16px;color:var(--danger-text)">查詢失敗</span>`;
    $("kpiUsersDelta").className = "delta-pill neutral";
    $("kpiUsersDelta").textContent = "Overview 錯誤";
  } else if (us.status === "insufficient_data") {
    $("kpiUsers").innerHTML = `<span style="font-size:16px;color:var(--warning-text)">資料不足</span>`;
    $("kpiUsersDelta").className = "delta-pill neutral";
    $("kpiUsersDelta").textContent = "無去重指標";
  } else {
    $("kpiUsers").textContent = fmt(totalUsers);
    const usDelta = $("kpiUsersDelta");
    if (us.change_pct != null) {
      const isDown = us.change_pct <= 0;
      usDelta.className = `delta-pill ${isDown ? "good" : "bad"}`;
      usDelta.innerHTML = `${us.change_pct > 0 ? "▲ +" : "▼ "}${us.change_pct.toFixed(1)}% vs 上期`;
    } else {
      usDelta.className = "delta-pill neutral";
      usDelta.textContent = "無基期";
    }
  }

  // 4. New Issues
  const ni = kpi.new_issues_count || {};
  $("kpiNewIssues").textContent = fmt(ni.value);
  const niDelta = $("kpiNewIssuesDelta");
  if (ni.change_pct != null) {
    const isDown = ni.change_pct <= 0;
    niDelta.className = `delta-pill ${isDown ? "good" : "bad"}`;
    niDelta.innerHTML = `${ni.change_pct > 0 ? "▲ +" : "▼ "}${ni.change_pct.toFixed(1)}% vs 上期`;
  } else {
    niDelta.className = "delta-pill neutral";
    niDelta.textContent = "無基期";
  }
}

// Render AI Summary Cards
function renderAISummaries() {
  const app = getCurAppData();
  if (!app) return;
  const snap = getCurPeriodSnapshot();
  const ai = snap?.ai_summary || app.ai_summary || {};

  const modelText = ai.model || "Gemini Flash";
  $("aiModelTag").textContent = modelText;
  $("aiFullModelTag").textContent = modelText;

  const overview = ai.overview || "尚無 AI 策略分析。";
  $("aiOverviewText").textContent = overview;
  $("aiFullOverviewText").textContent = overview;

  const takeaways = ai.key_takeaways || [];
  const takeawaysHtml = takeaways.map(t =>
    `<div class="ai-takeaway-item">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2.5" style="flex-shrink:0;margin-top:2px"><polyline points="20 6 9 17 4 12"/></svg>
      <span>${esc(t)}</span>
    </div>`
  ).join("");

  $("aiTakeawaysList").innerHTML = takeawaysHtml;
  $("aiFullTakeawaysList").innerHTML = takeawaysHtml;
  $("aiDistributionInsights").textContent = ai.distribution_insights || "—";

  // Recommended actions
  const actions = ai.recommended_actions || [];
  const actBody = $("recommendedActionsTableBody");
  if (actions.length) {
    actBody.innerHTML = actions.map(act => `
      <tr>
        <td><span class="badge badge-${(act.priority || "p2").toLowerCase()}">${esc(act.priority)}</span></td>
        <td class="mono-num">${esc(act.issue_id)}</td>
        <td><b>${esc(act.action)}</b></td>
        <td><span class="badge" style="background:var(--bg-subtle)">工作量 ${esc(act.effort || "?")}</span></td>
      </tr>
    `).join("");
  } else {
    actBody.innerHTML = `<tr><td colspan="4" class="empty-state">尚無推薦行動</td></tr>`;
  }
}

// Chart Helpers
function getChartColors() {
  const isDark = document.documentElement.dataset.theme === "dark";
  return {
    text: isDark ? "#94a3b8" : "#64748b",
    grid: isDark ? "#1e293b" : "#f1f5f9",
    accent: isDark ? "#3b82f6" : "#2563eb",
    danger: isDark ? "#f87171" : "#ef4444",
    warning: isDark ? "#fbbf24" : "#f59e0b",
    success: isDark ? "#34d399" : "#10b981",
  };
}

function destroyChart(id) {
  if (chartInstances[id]) {
    chartInstances[id].destroy();
    delete chartInstances[id];
  }
}

function renderCharts() {
  if (typeof Chart === "undefined") return;
  const app = getCurAppData();
  if (!app) return;
  const snap = getCurPeriodSnapshot();
  const colors = getChartColors();
  const period = snap?.period || app.period || {};

  // 1. Daily Trend (sliced by curPeriodDays)
  destroyChart("chartDailyTrend");
  const daily = (snap && snap.daily_trend && snap.daily_trend.length) ? snap.daily_trend : (app.daily_trend || []);
  const activeDays = Math.min(curPeriodDays || period.days || 30, daily.length || 30);
  const activeDaily = daily.slice(-activeDays);

  const subEl = $("dailyTrendChartSubtitle");
  if (subEl) {
    if (curPeriodDays && curPeriodDays > daily.length) {
      subEl.textContent = `事件數與受影響用戶（實際僅有 ${daily.length} 天資料）`;
    } else {
      subEl.textContent = `事件數與受影響用戶每日變化（顯示最近 ${activeDaily.length} 天）`;
    }
  }

  const trendCtx = $("chartDailyTrend");
  if (trendCtx && activeDaily.length) {
    chartInstances["chartDailyTrend"] = new Chart(trendCtx, {
      type: "line",
      data: {
        labels: activeDaily.map(d => d.date.slice(5)),
        datasets: [
          {
            label: "事件數 (Events)",
            data: activeDaily.map(d => d.crash_events),
            borderColor: colors.danger,
            backgroundColor: "rgba(239, 68, 68, 0.08)",
            fill: true,
            tension: 0.3,
            borderWidth: 2,
            pointRadius: 2,
          },
          {
            label: "受影響用戶 (Users)",
            data: activeDaily.map(d => d.affected_users),
            borderColor: colors.accent,
            backgroundColor: "transparent",
            tension: 0.3,
            borderWidth: 2,
            pointRadius: 2,
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { position: "top", labels: { color: colors.text, boxWidth: 12 } }
        },
        scales: {
          x: { ticks: { color: colors.text }, grid: { color: colors.grid } },
          y: { ticks: { color: colors.text }, grid: { color: colors.grid } }
        }
      }
    });
  }

  // 2. Error Types
  destroyChart("chartErrorTypes");
  const errCtx = $("chartErrorTypes");
  const kpiErr = snap?.kpi?.events_by_error_type || app.kpi?.events_by_error_type || {};
  if (errCtx) {
    chartInstances["chartErrorTypes"] = new Chart(errCtx, {
      type: "doughnut",
      data: {
        labels: ["Fatal 閃退", "ANR 凍結", "Non-fatal 非致命"],
        datasets: [{
          data: [kpiErr.fatal || 0, kpiErr.anr || 0, kpiErr.non_fatal || 0],
          backgroundColor: [colors.danger, colors.warning, "#94a3b8"],
          borderWidth: 0,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "68%",
        plugins: {
          legend: { position: "bottom", labels: { color: colors.text, boxWidth: 10 } }
        }
      }
    });
  }

  // 3. Platform Breakdown
  destroyChart("chartPlatforms");
  const platCtx = $("chartPlatforms");
  const platData = snap?.distributions?.platform || app.distributions?.platform || [];
  if (platCtx && platData.length) {
    chartInstances["chartPlatforms"] = new Chart(platCtx, {
      type: "bar",
      data: {
        labels: platData.map(p => p.name.toUpperCase()),
        datasets: [
          {
            label: "事件數",
            data: platData.map(p => p.events),
            backgroundColor: colors.accent,
            borderRadius: 4,
          },
          {
            label: "用戶數",
            data: platData.map(p => p.users),
            backgroundColor: "#94a3b8",
            borderRadius: 4,
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: "top", labels: { color: colors.text, boxWidth: 12 } } },
        scales: {
          x: { ticks: { color: colors.text }, grid: { display: false } },
          y: { ticks: { color: colors.text }, grid: { color: colors.grid } }
        }
      }
    });
  }

  // 4. App Versions
  destroyChart("chartAppVersions");
  const verCtx = $("chartAppVersions");
  const verData = snap?.distributions?.app_versions || app.distributions?.app_versions || [];
  if (verCtx && verData.length) {
    chartInstances["chartAppVersions"] = new Chart(verCtx, {
      type: "bar",
      data: {
        labels: verData.map(v => v.app_version),
        datasets: [{
          label: "事件數",
          data: verData.map(v => v.events),
          backgroundColor: colors.danger,
          borderRadius: 4,
        }]
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: colors.text }, grid: { color: colors.grid } },
          y: { ticks: { color: colors.text }, grid: { display: false } }
        }
      }
    });
  }

  // 5. Device Models
  destroyChart("chartDeviceModels");
  const devCtx = $("chartDeviceModels");
  const devData = snap?.distributions?.device_models || app.distributions?.device_models || [];
  if (devCtx && devData.length) {
    chartInstances["chartDeviceModels"] = new Chart(devCtx, {
      type: "bar",
      data: {
        labels: devData.slice(0, 8).map(d => d.model),
        datasets: [{
          label: "事件數",
          data: devData.slice(0, 8).map(d => d.events),
          backgroundColor: colors.accent,
          borderRadius: 4,
        }]
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: colors.text }, grid: { color: colors.grid } },
          y: { ticks: { color: colors.text }, grid: { display: false } }
        }
      }
    });
  }

  // 6. OS Versions
  destroyChart("chartOSVersions");
  const osCtx = $("chartOSVersions");
  const osData = snap?.distributions?.os_versions || app.distributions?.os_versions || [];
  if (osCtx && osData.length) {
    chartInstances["chartOSVersions"] = new Chart(osCtx, {
      type: "bar",
      data: {
        labels: osData.slice(0, 8).map(o => o.os_version),
        datasets: [{
          label: "事件數",
          data: osData.slice(0, 8).map(o => o.events),
          backgroundColor: colors.warning,
          borderRadius: 4,
        }]
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { ticks: { color: colors.text }, grid: { color: colors.grid } },
          y: { ticks: { color: colors.text }, grid: { display: false } }
        }
      }
    });
  }
}

function getLifecycleBadgeHtml(lc) {
  if (!lc || !lc.status) return "";
  const st = lc.status;
  const reason = lc.reason || "";
  if (st === "new_in_latest") {
    return `<span class="badge badge-lifecycle-new" title="${esc(reason)}">🔴 新版引入</span>`;
  } else if (st === "persistent") {
    return `<span class="badge badge-lifecycle-persistent" title="${esc(reason)}">🟡 持續存在</span>`;
  } else if (st === "regressed") {
    return `<span class="badge badge-lifecycle-regressed" title="${esc(reason)}">🟣 回歸</span>`;
  } else if (st === "resolved") {
    return `<span class="badge badge-lifecycle-resolved" title="${esc(reason)}">🟢 已收斂</span>`;
  } else if (st === "not_observed_latest") {
    return `<span class="badge badge-lifecycle-not-observed" title="${esc(reason)}">⚪ 最新版未見</span>`;
  }
  return "";
}

// Render Issues Accordion & Table with Sorting
function renderIssuesList() {
  const app = getCurAppData();
  if (!app) return;
  const snap = getCurPeriodSnapshot();
  const issues = snap?.top_issues || app.top_issues || [];

  const filterErr = $("filterErrorType") ? $("filterErrorType").value : "ALL";
  const filterPlat = $("filterPlatform") ? $("filterPlatform").value : "ALL";
  const filterPrio = $("filterPriority") ? $("filterPriority").value : "ALL";
  const filterLife = $("filterLifecycle") ? $("filterLifecycle").value : "ALL";

  const filtered = issues.filter(iss => {
    if (filterErr !== "ALL" && iss.error_type !== filterErr) return false;
    if (filterPlat !== "ALL" && iss.platform !== filterPlat) return false;
    if (filterPrio !== "ALL" && iss.priority?.level !== filterPrio) return false;
    if (filterLife !== "ALL" && (iss.lifecycle?.status || "persistent") !== filterLife) return false;
    if (searchQuery) {
      const target = `${iss.title} ${iss.subtitle} ${iss.issue_id} ${iss.blame_frame?.file || ""}`.toLowerCase();
      if (!target.includes(searchQuery)) return false;
    }
    return true;
  });

  // Sort filtered list
  filtered.sort((a, b) => {
    let valA, valB;
    if (curSortField === "priority") {
      valA = a.priority?.score ?? 0;
      valB = b.priority?.score ?? 0;
    } else if (curSortField === "events") {
      valA = a.events ?? 0;
      valB = b.events ?? 0;
    } else if (curSortField === "users") {
      valA = a.affected_users ?? 0;
      valB = b.affected_users ?? 0;
    } else if (curSortField === "last_seen") {
      valA = a.last_seen_timestamp || "";
      valB = b.last_seen_timestamp || "";
    } else {
      valA = a.events ?? 0;
      valB = b.events ?? 0;
    }
    if (valA < valB) return curSortAsc ? -1 : 1;
    if (valA > valB) return curSortAsc ? 1 : -1;
    return 0;
  });

  $("issuesCountBadge").textContent = `顯示 ${filtered.length} / ${issues.length} 個問題 (排序: ${curSortField} ${curSortAsc ? '▲' : '▼'})`;

  // Overview quick preview
  const prevBody = $("topIssuesPreviewBody");
  if (prevBody) {
    prevBody.innerHTML = filtered.slice(0, 5).map(iss => {
      const pLevel = iss.priority?.level || "P2";
      const errCls = iss.error_type === "FATAL" ? "badge-fatal" : (iss.error_type === "ANR" ? "badge-anr" : "badge-nonfatal");
      return `
        <tr>
          <td><span class="badge badge-${pLevel.toLowerCase()}">${esc(pLevel)}</span></td>
          <td>
            <div style="font-weight:600">${esc(iss.title)}</div>
            <div style="font-size:11px;font-family:var(--font-mono);color:var(--text-muted)">${esc(iss.subtitle || "")}</div>
          </td>
          <td><span class="badge" style="background:var(--bg-subtle)">${esc(iss.platform)}</span></td>
          <td><span class="badge ${errCls}">${esc(iss.error_type)}</span></td>
          <td>${getLifecycleBadgeHtml(iss.lifecycle)}</td>
          <td class="mono-num">${fmt(iss.events)}</td>
          <td class="mono-num">${fmt(iss.affected_users)}</td>
          <td class="mono-num">${esc(iss.last_seen_version || "—")}</td>
        </tr>
      `;
    }).join("") || `<tr><td colspan="8" class="empty-state">尚無問題資料</td></tr>`;
  }

  // Full Issues Accordion List
  const container = $("issuesListContainer");
  if (!filtered.length) {
    container.innerHTML = `<div class="empty-state"><div class="empty-state-title">無符合條件之問題</div></div>`;
    return;
  }

  container.innerHTML = filtered.map((iss, idx) => {
    const pLevel = iss.priority?.level || "P2";
    const errCls = iss.error_type === "FATAL" ? "badge-fatal" : (iss.error_type === "ANR" ? "badge-anr" : "badge-nonfatal");
    const ai = iss.ai_analysis || {};
    const blame = iss.blame_frame || {};
    const detail = iss.detail || {};
    const lc = iss.lifecycle;

    return `
      <div class="issue-accordion" id="issue-acc-${idx}">
        <div class="issue-summary-row" onclick="toggleIssueDetail(${idx})">
          <span class="issue-rank">${String(idx + 1).padStart(2, "0")}</span>
          <span class="badge badge-${pLevel.toLowerCase()}">${esc(pLevel)}</span>
          <span class="badge ${errCls}">${esc(iss.error_type)}</span>
          ${getLifecycleBadgeHtml(lc)}
          <div class="issue-title-group">
            <div class="issue-main-title">${esc(iss.title)}</div>
            <div class="issue-sub-title">${esc(iss.subtitle || "")}</div>
          </div>
          <div class="issue-stats-group">
            <span>${esc(iss.platform.toUpperCase())}</span>
            <span><b>${fmt(iss.events)}</b> 次事件</span>
            <span><b>${fmt(iss.affected_users)}</b> 位用戶</span>
            <span style="font-size:11px">見於 ${esc(iss.last_seen_version || "?")}</span>
          </div>
        </div>

        <div class="issue-detail-panel" id="issue-detail-${idx}">
          <div class="detail-grid">
            <div class="detail-box">
              <div class="detail-box-title">AI Root Cause 推測</div>
              <div class="detail-box-content">${esc(ai.root_cause || "待分析")}</div>
            </div>
            <div class="detail-box">
              <div class="detail-box-title">AI 建議修法 (預估工作量 ${esc(ai.effort || "?")})</div>
              <div class="detail-box-content">${esc(ai.suggested_fix || "待分析")}</div>
            </div>
          </div>

          ${lc ? `
            <div class="detail-box" style="margin-bottom:12px">
              <div class="detail-box-title">生命週期與回歸狀態 (Issue Lifecycle)</div>
              <div class="detail-box-content" style="display:flex;gap:16px;flex-wrap:wrap;align-items:center;font-size:13px">
                <div>${getLifecycleBadgeHtml(lc)}</div>
                <div><b>說明：</b>${esc(lc.reason || "—")}</div>
                <div><b>版本範圍：</b><code>${esc(lc.first_seen_version)}</code> → <code>${esc(lc.last_seen_version)}</code> (見於 ${lc.versions_seen} 個版本)</div>
                <div><b>可信度：</b><span class="badge" style="background:var(--bg-subtle)">${esc(lc.confidence)}</span></div>
                ${lc.previously_absent_since ? `<div><b>曾消失自版本：</b><code>${esc(lc.previously_absent_since)}</code></div>` : ""}
                ${lc.reappeared_version ? `<div><b>回歸版本：</b><code>${esc(lc.reappeared_version)}</code></div>` : ""}
              </div>
            </div>
          ` : ""}

          ${blame.file ? `
            <div class="detail-box">
              <div class="detail-box-title">元兇程式碼位置 (Blame Frame)</div>
              <div class="detail-box-content" style="font-family:var(--font-mono);font-size:12px">
                <code>${esc(blame.file)}${blame.line ? ":" + esc(blame.line) : ""}</code>
                ${blame.symbol ? ` · <code>${esc(blame.symbol)}</code>` : ""}
              </div>
            </div>
          ` : ""}

          ${detail.stack_trace ? `
            <div class="detail-box">
              <div class="detail-box-title">Crashlytics Stack Trace</div>
              <pre class="code-stack">${esc(detail.stack_trace)}</pre>
            </div>
          ` : ""}

          <button class="btn-copy-prompt" onclick="copyFixPrompt('${esc(iss.issue_id)}')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            複製 AI 修復 Prompt
          </button>
        </div>
      </div>
    `;
  }).join("");
}

function toggleIssueDetail(idx) {
  const panel = $("issue-detail-" + idx);
  if (panel) panel.classList.toggle("open");
}

function copyFixPrompt(issueId) {
  const app = getCurAppData();
  if (!app) return;
  const snap = getCurPeriodSnapshot();
  const issues = snap?.top_issues || app.top_issues || [];
  const iss = issues.find(i => i.issue_id === issueId) || (app.top_issues || []).find(i => i.issue_id === issueId);
  if (!iss) return;

  const ai = iss.ai_analysis || {};
  const blame = iss.blame_frame || {};
  const detail = iss.detail || {};
  const lc = iss.lifecycle;

  const promptLines = [
    `# Crash 修復請求：${iss.title}`,
    "",
    `- App: ${app.metadata?.display_name || curAppId} (${iss.platform})`,
    `- Issue ID: ${iss.issue_id}`,
    `- 層級: ${iss.error_type} | 優先級: ${iss.priority?.level || "P2"} (分數: ${iss.priority?.score || 0})`,
    `- 影響: ${fmt(iss.affected_users)} 位用戶 / ${fmt(iss.events)} 次崩潰事件`,
    `- 版本範圍: ${iss.first_seen_version || "?"} → ${iss.last_seen_version || "?"}`,
  ];

  if (lc) {
    promptLines.push(`- 生命週期: ${lc.status} (${lc.reason || "無說明"})`);
  }
  if (blame.file) promptLines.push(`- 元兇位置: ${blame.file}${blame.line ? ":" + blame.line : ""}`);
  if (iss.subtitle) promptLines.push(`- 錯誤特徵: ${iss.subtitle}`);

  promptLines.push("", "## AI 分析與建議", `Root Cause: ${ai.root_cause || "需人工檢驗"}`, `建議修法: ${ai.suggested_fix || "—"}`);

  if (detail.stack_trace) {
    promptLines.push("", "## Stack Trace", "```", detail.stack_trace, "```");
  }

  promptLines.push("", "請依據上述資訊定位 Root cause 並進行代碼修復。修復完成後請說明修正方案。");

  const fullText = promptLines.join("\n");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(fullText).then(() => showToast("已複製 AI 修復 Prompt")).catch(() => fallbackCopy(fullText));
  } else {
    fallbackCopy(fullText);
  }
}

function fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try {
    document.execCommand("copy");
    showToast("已複製 AI 修復 Prompt");
  } catch (e) {
    showToast("複製失敗，請手動複製");
  }
  document.body.removeChild(ta);
}

// Render Version Health Table
function renderVersionHealth() {
  const app = getCurAppData();
  if (!app) return;
  const snap = getCurPeriodSnapshot();
  const list = snap?.version_health || app.version_health || [];
  const tbody = $("versionHealthTableBody");
  if (!tbody) return;

  if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="empty-state">尚無版本健康度資料</td></tr>`;
    return;
  }

  tbody.innerHTML = list.map(v => {
    const cfuRate = (v.crash_free_users_rate != null) ? `${(v.crash_free_users_rate * 100).toFixed(2)}%` : "Unavailable";
    const cfsRate = (v.crash_free_sessions_rate != null) ? `${(v.crash_free_sessions_rate * 100).toFixed(2)}%` : "Unavailable";
    const adopt = (v.adoption_rate != null) ? `${(v.adoption_rate * 100).toFixed(1)}%` : "—";
    const stCls = `badge-${v.status || "active"}`;

    return `
      <tr>
        <td class="mono-num"><b>${esc(v.version)}</b></td>
        <td><span class="badge" style="background:var(--bg-subtle)">${esc(v.platform)}</span></td>
        <td class="mono-num">${esc(v.release_date || "—")}</td>
        <td><span class="badge badge-status ${stCls}">${esc(v.status)}</span></td>
        <td><span class="badge" style="background:var(--bg-subtle)">${esc(v.trend)}</span></td>
        <td class="mono-num">${cfuRate}</td>
        <td class="mono-num">${cfsRate}</td>
        <td class="mono-num">${adopt}</td>
        <td class="mono-num">${fmt(v.crash_events)}</td>
        <td class="mono-num">${fmt(v.affected_users)}</td>
      </tr>
    `;
  }).join("");
}

// Render Devices Table
function renderDevicesTable() {
  const app = getCurAppData();
  if (!app) return;
  const snap = getCurPeriodSnapshot();
  const models = snap?.distributions?.device_models || app.distributions?.device_models || [];
  const tbody = $("deviceModelsTableBody");
  if (!tbody) return;

  tbody.innerHTML = models.map(m => `
    <tr>
      <td><b>${esc(m.model)}</b></td>
      <td><span class="badge" style="background:var(--bg-subtle)">${esc(m.platform)}</span></td>
      <td class="mono-num">${fmt(m.events)}</td>
      <td class="mono-num">${fmt(m.users)}</td>
      <td class="mono-num">${(m.share * 100).toFixed(1)}%</td>
    </tr>
  `).join("") || `<tr><td colspan="5" class="empty-state">尚無機型資料</td></tr>`;
}

// Render Releases Table
function renderReleasesTable() {
  const app = getCurAppData();
  if (!app) return;
  const snap = getCurPeriodSnapshot();
  const list = snap?.version_health || app.version_health || [];
  const tbody = $("releasesTableBody");
  if (!tbody) return;

  tbody.innerHTML = list.map(v => `
    <tr>
      <td class="mono-num"><b>${esc(v.version)}</b></td>
      <td class="mono-num">${esc(v.release_date || "—")}</td>
      <td><span class="badge badge-status badge-${v.status}">${esc(v.status)}</span></td>
      <td class="mono-num">${fmt(v.crash_events)}</td>
      <td class="mono-num">${fmt(v.affected_users)}</td>
      <td class="mono-num">${v.crash_free_users_rate != null ? (v.crash_free_users_rate * 100).toFixed(2) + "%" : "Unavailable"}</td>
      <td class="mono-num">${v.adoption_rate != null ? (v.adoption_rate * 100).toFixed(1) + "%" : "—"}</td>
    </tr>
  `).join("") || `<tr><td colspan="7" class="empty-state">尚無發佈版本資料</td></tr>`;
}

// Render Notifications / Pipelines
function renderPipelines() {
  const app = getCurAppData();
  if (!app) return;
  const srcs = app.sources || {};
  const grid = $("pipelineCardsGrid");
  if (!grid) return;

  const aiSrc = srcs.ai || srcs.gemini_ai;
  const aiName = (aiSrc && aiSrc.provider === "openrouter") ? "OpenRouter AI Analysis" : "Gemini AI Analysis";
  const pipelines = [
    { key: "crashlytics_bq", name: "Crashlytics BigQuery", obj: srcs.crashlytics_bq },
    { key: "firebase_sessions", name: "Firebase Sessions Export", obj: srcs.firebase_sessions },
    { key: "mcp_crashlytics", name: "Crashlytics MCP Server", obj: srcs.mcp_crashlytics },
    { key: (srcs.ai ? "ai" : "gemini_ai"), name: aiName, obj: aiSrc },
  ];

  grid.innerHTML = pipelines.map(p => {
    const res = resolveSourceHealth(p.key, p.obj, DATA.generated_at);
    return `
      <div class="chart-card col-6">
        <div class="chart-card-header">
          <div class="chart-title">${esc(p.name)}</div>
          <span class="badge badge-status ${res.badgeClass}">
            ${esc(res.label)}
          </span>
        </div>
        <div style="font-size:12.5px;color:var(--text-muted);display:flex;flex-direction:column;gap:6px">
          <div>最後同步時間: <b>${esc(res.timestamp)}</b> (${esc(res.freshness)})</div>
          <div class="data-source-note ${res.status === 'error' ? 'error' : (res.status === 'stale' ? 'warning' : '')}">
            備註: ${esc(res.note)}
          </div>
          ${res.isSupplemental ? `<div style="font-size:11.5px;color:var(--warning-text)">※ 正在使用 last-known-good supplemental 快取資料補強</div>` : ""}
        </div>
      </div>
    `;
  }).join("");

  const limits = app.limitations || [];
  const limitList = $("limitationsList");
  if (limitList) {
    limitList.innerHTML = limits.map(l => `<li>${esc(l)}</li>`).join("") || `<li>無特殊資料限制。</li>`;
  }
}

// Render Settings
function renderSettings() {
  const app = getCurAppData();
  if (!app) return;
  const meta = app.metadata || {};
  const period = app.period || {};
  const tbody = $("settingsTableBody");
  if (!tbody) return;

  tbody.innerHTML = `
    <tr><th style="width:200px">App ID</th><td><code>${esc(meta.app_id)}</code></td></tr>
    <tr><th>顯示名稱</th><td><b>${esc(meta.display_name)}</b></td></tr>
    <tr><th>Firebase Project ID</th><td><code>${esc(meta.firebase_project_id)}</code></td></tr>
    <tr><th>支援平台</th><td>${(meta.platforms || []).map(p => `<span class="badge" style="background:var(--bg-subtle)">${esc(p)}</span>`).join(" ")}</td></tr>
    <tr><th>原始碼 Repo</th><td>${meta.source_repo ? `<code>${esc(meta.source_repo)}</code>` : "未設定"}</td></tr>
    <tr><th>自訂監控 Keys</th><td>${(meta.custom_keys_monitored || []).map(k => `<code>${esc(k)}</code>`).join(", ") || "無"}</td></tr>
    <tr><th>統計回溯天數</th><td>${esc(period.days)} 天 (${esc(period.start_time || "")} ~ ${esc(period.end_time || "")})</td></tr>
    <tr><th>Schema 版本</th><td><code>${esc(DATA.schema_version || "2.0")}</code></td></tr>
    <tr><th>報表產生時間</th><td><code>${esc(DATA.generated_at || "")}</code></td></tr>
  `;
}

// Full Render
function renderAll() {
  renderHeader();
  renderDataSourcesHealth();
  renderKPIs();
  renderAISummaries();
  renderCharts();
  renderIssuesList();
  renderVersionHealth();
  renderDevicesTable();
  renderReleasesTable();
  renderPipelines();
  renderSettings();
}

// Initialize on page load
document.addEventListener("DOMContentLoaded", () => {
  renderAll();
});
if (document.readyState === "complete" || document.readyState === "interactive") {
  renderAll();
}
</script>
</body>
</html>
"""


def build_html(data: Union[dict, Any]) -> str:
    """Renders self-contained HTML for a DashboardV2Bundle data structure."""
    chartjs_code = get_vendor_chartjs()
    json_data = json.dumps(data, ensure_ascii=False)
    html = HTML_TEMPLATE.replace("__CHARTJS__", chartjs_code).replace("__DATA__", json_data)
    return html


def generate_dashboard(
    data: Optional[Union[dict, Any]] = None,
    output_path: Optional[Union[str, Path]] = None,
    data_path: Optional[Union[str, Path]] = None,
) -> Path:
    """Generates the dashboard.html file and returns the output Path."""
    if data is None:
        data = collect_data(data_path)

    out = Path(output_path) if output_path else DEFAULT_OUT_HTML
    html_content = build_html(data)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_content, encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="產生 Dashboard V2 自包含靜態 HTML 儀表板")
    parser.add_argument("--data", help="輸入之 Dashboard V2 JSON 檔案路徑")
    parser.add_argument("--out", default=str(DEFAULT_OUT_HTML), help="輸出之 HTML 檔案路徑")
    args = parser.parse_args()

    out_file = generate_dashboard(output_path=args.out, data_path=args.data)
    try:
        print(f"  ✓ 成功產生自包含 Dashboard V2 儀表板: {out_file}")
    except UnicodeEncodeError:
        print(f"  [OK] Generated self-contained Dashboard V2: {out_file}")


if __name__ == "__main__":
    main()
