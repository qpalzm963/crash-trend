"""High-level Dashboard assembly, validation, and rendering orchestration (Issue #53).

Provides:
- assemble_html_template: Assembles modular styles, shell HTML, section views, and client JS.
- assemble_bundle_from_apps: Scans out/<app_id>/ for app-level V2 data and bundles them into a DashboardV2Bundle.
- collect_data: Loads Dashboard V2 bundle data from specified path or standard locations.
- build_html: Renders self-contained HTML for a DashboardV2Bundle data structure.
- generate_dashboard: Generates dashboard.html file and returns Path.
- main: CLI entrypoint.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

from crash_trend.dashboard.ai import get_ai_html, get_ai_js
from crash_trend.dashboard.assets import (
    DEFAULT_ROOT,
    get_dashboard_styles,
    get_header_html,
    get_shell_bottom_html,
    get_shell_js_bottom,
    get_shell_js_top,
    get_shell_top_html,
    get_sidebar_html,
    get_vendor_chartjs,
)
from crash_trend.dashboard.formatting import get_formatting_js
from crash_trend.dashboard.issues import get_issues_html, get_issues_js
from crash_trend.dashboard.overview import get_overview_html, get_overview_js
from crash_trend.dashboard.releases import get_releases_html, get_releases_js
from crash_trend.dashboard.settings import get_settings_html, get_settings_js
from crash_trend.dashboard.sources import get_sources_html, get_sources_js
from crash_trend.schema_v2 import SCHEMA_VERSION, validate_dashboard_v2

ROOT = DEFAULT_ROOT
DEFAULT_OUT_HTML = ROOT / "dashboard.html"



def assemble_html_template() -> str:
    """Assembles all modular section HTML and JS components into the complete HTML template."""
    return "".join([
        get_shell_top_html(),
        get_dashboard_styles(),
        "</head>\n<body>\n\n",
        get_sidebar_html(),
        "\n\n",
        get_header_html(),
        "\n  <main class=\"content-area\">\n\n",
        get_overview_html(),
        "\n",
        get_issues_html(),
        "\n",
        get_releases_html(),
        "\n",
        get_sources_html(),
        "\n",
        get_ai_html(),
        "\n",
        get_settings_html(),
        "\n",
        get_shell_bottom_html(),
        "\n\n<!-- Chart.js Embedded -->\n<script>\n__CHARTJS__\n</script>\n\n"
        "<!-- Embedded Dashboard V2 Bundle Data & Client Logic -->\n<script>\n",
        get_shell_js_top(),
        "\n",
        get_formatting_js(),
        "\n",
        get_settings_js(),
        "\n",
        get_sources_js(),
        "\n",
        get_overview_js(),
        "\n",
        get_ai_js(),
        "\n",
        get_issues_js(),
        "\n",
        get_releases_js(),
        "\n",
        get_shell_js_bottom(),
        "\n</script>\n</body>\n</html>\n",
    ])


HTML_TEMPLATE = assemble_html_template()


def assemble_bundle_from_apps(cfg: dict | None = None, root_dir: str | Path | None = None) -> dict | None:
    """Scans out/<app_id>/ for app-level V2 data and bundles them into a DashboardV2Bundle."""
    eff_root = Path(root_dir) if root_dir is not None else ROOT
    if cfg is None:
        try:
            import yaml
            apps_yaml = eff_root / "apps.yaml"
            if not apps_yaml.exists():
                apps_yaml = eff_root / "apps.example.yaml"
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
        app_out_dir = eff_root / "out" / app_id
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
        out_root = eff_root / "out"
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
    now_utc = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_utc,
        "default_app": default_app,
        "apps": collected_apps,
    }

    run_sum_env = os.environ.get("PIPELINE_RUN_SUMMARY")
    run_sum_path = Path(run_sum_env) if run_sum_env else eff_root / "out" / "pipeline_run.json"
    if run_sum_path.is_file():
        try:
            bundle["pipeline_run"] = json.loads(run_sum_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Enrich AI Policy and AI Usage Observability (Dashboard V2.5 - Issues #41, #42)
    try:
        from crash_trend.ai_config_service import get_effective_ai_policy
        bundle["global_ai_policy"] = get_effective_ai_policy(None, cfg)
        for app_id, a_data in collected_apps.items():
            if isinstance(a_data, dict):
                a_data["ai_policy"] = get_effective_ai_policy(app_id, cfg)
    except Exception:
        pass

    try:
        from crash_trend.ai_telemetry import aggregate_ai_usage
        bundle["ai_usage"] = aggregate_ai_usage(days=7)
    except Exception:
        pass

    # Ensure all collected apps have lifecycle enriched before strict Schema V2.3 validation
    for app_id, a_data in collected_apps.items():
        if isinstance(a_data, dict) and isinstance(a_data.get("top_issues"), list):
            has_missing_lc = any(isinstance(i, dict) and "lifecycle" not in i for i in a_data["top_issues"])
            if has_missing_lc:
                try:
                    from crash_trend.catalog import enrich_app_data_with_lifecycle
                    enrich_app_data_with_lifecycle(a_data, app_name=app_id, out_dir=eff_root / "out")
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
    out_dir = eff_root / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "dashboard_v2.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")

    reports_dir = eff_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "dashboard_v2.json").write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")

    return bundle


def collect_data(
    data_path: str | Path | None = None,
    root_dir: str | Path | None = None,
) -> dict:
    """Loads Dashboard V2 bundle data from specified path or standard locations."""
    if data_path:
        p = Path(data_path)
        if p.is_file():
            return json.loads(p.read_text(encoding="utf-8"))
        raise FileNotFoundError(f"Specified data file not found: {data_path}")

    eff_root = Path(root_dir) if root_dir is not None else ROOT

    # 1. Try to assemble from multi-app out/<app>/ data
    assembled = assemble_bundle_from_apps(root_dir=eff_root)
    if assembled:
        return assembled

    # 2. Search production locations (嚴禁在正式環境偷偷 fallback 至測試 fixture)
    candidates = [
        eff_root / "reports" / "dashboard_v2.json",
        eff_root / "out" / "dashboard_v2.json",
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


def build_html(
    data: dict | Any,
    vendor_chartjs_path: str | Path | None = None,
) -> str:
    """Renders self-contained HTML for a DashboardV2Bundle data structure."""
    chartjs_code = get_vendor_chartjs(vendor_chartjs_path)
    json_data = json.dumps(data, ensure_ascii=False)
    tmpl = assemble_html_template()
    html = tmpl.replace("__CHARTJS__", chartjs_code).replace("__DATA__", json_data)
    return html


def generate_dashboard(
    data: dict | Any | None = None,
    output_path: str | Path | None = None,
    data_path: str | Path | None = None,
    root_dir: str | Path | None = None,
) -> Path:
    """Generates the dashboard.html file and returns the output Path."""
    eff_root = Path(root_dir) if root_dir is not None else ROOT
    if data is None:
        data = collect_data(data_path, root_dir=eff_root)

    out = Path(output_path) if output_path else eff_root / "dashboard.html"
    vendor_js = eff_root / "vendor" / "chart.umd.min.js"
    html_content = build_html(data, vendor_chartjs_path=vendor_js)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_content, encoding="utf-8")
    return out


def main(
    argv: list[str] | None = None,
    root_dir: str | Path | None = None,
) -> None:
    parser = argparse.ArgumentParser(description="產生 Dashboard V2 自包含靜態 HTML 儀表板")
    parser.add_argument("--data", help="輸入之 Dashboard V2 JSON 檔案路徑")
    parser.add_argument("--out", default=None, help="輸出之 HTML 檔案路徑")
    args = parser.parse_args(argv)

    eff_root = Path(root_dir) if root_dir is not None else ROOT
    out_target = args.out or str(eff_root / "dashboard.html")
    out_file = generate_dashboard(output_path=out_target, data_path=args.data, root_dir=eff_root)
    try:
        print(f"  ✓ 成功產生自包含 Dashboard V2 儀表板: {out_file}")
    except UnicodeEncodeError:
        print(f"  [OK] Generated self-contained Dashboard V2: {out_file}")
