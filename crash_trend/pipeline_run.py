"""Pipeline Runner and Orchestrator (Dashboard V2.2 - Issue #22).

Executes end-to-end crash-trend pipeline stages across configured apps, records
structured stage outcomes, duration, and sanitized error messages, and saves
`out/pipeline_run.json`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent

try:
    from crash_trend.config import get_app, get_mcp_config, is_sessions_enabled, load_config
    from crash_trend.pipeline_health import (
        DEFAULT_RUN_SUMMARY_PATH,
        PipelineRunTracker,
        StageStatus,
        now_utc_iso,
        sanitize_error_message,
    )
except ImportError:
    from config import get_app, get_mcp_config, is_sessions_enabled, load_config
    from pipeline_health import (
        DEFAULT_RUN_SUMMARY_PATH,
        PipelineRunTracker,
        StageStatus,
        now_utc_iso,
        sanitize_error_message,
    )


def run_stage_process(
    cmd: List[str],
    cwd: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
) -> tuple[int, str, str]:
    """Runs a pipeline stage command and returns (returncode, stdout, stderr)."""
    current_env = dict(os.environ)
    if env:
        current_env.update(env)

    proc = subprocess.run(
        cmd,
        cwd=str(cwd or ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=current_env,
    )
    return proc.returncode, proc.stdout, proc.stderr


def run_pipeline(
    app_names: Optional[List[str]] = None,
    days: int = 30,
    summary_path: Optional[Path] = None,
    skip_dashboard: bool = False,
    verbose: bool = True,
) -> Dict[str, Any]:
    """Orchestrates pipeline execution across apps and returns the run summary."""
    cfg = load_config()
    all_apps = list((cfg.get("apps") or {}).keys())
    target_apps = app_names if app_names is not None else all_apps

    if not target_apps:
        print("[警告] apps.yaml 中未設定任何 app", file=sys.stderr)

    tracker = PipelineRunTracker()
    py_exec = sys.executable

    for app in target_apps:
        if verbose:
            print(f"\n==================== [Pipeline: {app}] ====================")

        app_cfg = get_app(app)

        # -------------------------------------------------------------------
        # 1. BigQuery (Core Stage)
        # -------------------------------------------------------------------
        t0 = now_utc_iso()
        if verbose:
            print(f"--- 1. fetch_bigquery: {app}")
        rc, out, err = run_stage_process([py_exec, str(ROOT / "crash_trend" / "fetch_bigquery.py"), "--app", app, "--days", str(days)])
        if verbose and out:
            print(out, end="")
        t1 = now_utc_iso()

        bq_failed = rc != 0
        if bq_failed:
            err_msg = err.strip() or out.strip() or "BigQuery fetch returned non-zero exit code"
            tracker.record_stage(app, "crashlytics_bigquery", "failed", t0, t1, error_message=err_msg)
            if verbose:
                print(f"  [Error] BigQuery 查詢失敗：{sanitize_error_message(err_msg)}", file=sys.stderr)
        else:
            tracker.record_stage(app, "crashlytics_bigquery", "success", t0, t1)

        # -------------------------------------------------------------------
        # 2. Firebase Sessions (Optional Stage)
        # -------------------------------------------------------------------
        t0 = now_utc_iso()
        sess_enabled = is_sessions_enabled(app_cfg)
        if verbose:
            print(f"--- 2. fetch_sessions: {app} (enabled: {sess_enabled})")

        rc, out, err = run_stage_process([py_exec, str(ROOT / "crash_trend" / "fetch_sessions.py"), "--app", app, "--days", str(days)])
        if verbose and out:
            print(out, end="")
        t1 = now_utc_iso()

        if not sess_enabled:
            tracker.record_stage(
                app,
                "sessions",
                "disabled",
                t0,
                t1,
                error_message=None,
                details={"reason": "Sessions export disabled in config"},
            )
        elif rc != 0:
            err_msg = err.strip() or out.strip() or "Sessions fetch failed"
            tracker.record_stage(app, "sessions", "failed", t0, t1, error_message=err_msg)
            if verbose:
                print(f"  [Warning] Sessions 查詢失敗（優雅降級）：{sanitize_error_message(err_msg)}", file=sys.stderr)
        else:
            tracker.record_stage(app, "sessions", "success", t0, t1)

        # -------------------------------------------------------------------
        # 3. MCP Stacktraces (Optional Stage)
        # -------------------------------------------------------------------
        t0 = now_utc_iso()
        mcp_cfg = get_mcp_config(app_cfg)
        mcp_mode = mcp_cfg["mode"]
        if verbose:
            print(f"--- 3. fetch_mcp: {app} (mode: {mcp_mode})")

        if mcp_mode == "off":
            t1 = now_utc_iso()
            tracker.record_stage(
                app,
                "mcp",
                "disabled",
                t0,
                t1,
                details={"reason": "MCP mode is off in config"},
            )
        elif mcp_mode == "manual":
            t1 = now_utc_iso()
            tracker.record_stage(
                app,
                "mcp",
                "skipped",
                t0,
                t1,
                details={"reason": "MCP mode is manual, skipped in weekly run"},
            )
        else:
            # weekly mode
            rc, out, err = run_stage_process([py_exec, str(ROOT / "crash_trend" / "fetch_stacktraces.py"), "--app", app, "--weekly-check"])
            if verbose and out:
                print(out, end="")
            t1 = now_utc_iso()

            if "快取仍有效" in out:
                tracker.record_stage(
                    app,
                    "mcp",
                    "skipped",
                    t0,
                    t1,
                    details={"reason": "Cache is fresh, skipped refresh"},
                )
            elif rc != 0:
                err_msg = err.strip() or out.strip() or "MCP refresh failed"
                tracker.record_stage(app, "mcp", "failed", t0, t1, error_message=err_msg)
                if verbose:
                    print(f"  [Warning] MCP 刷新失敗（優雅降級）：{sanitize_error_message(err_msg)}", file=sys.stderr)
            else:
                tracker.record_stage(app, "mcp", "success", t0, t1)

        # -------------------------------------------------------------------
        # 4. Issue Details (Optional / Enrichment Stage)
        # -------------------------------------------------------------------
        t0 = now_utc_iso()
        if verbose:
            print(f"--- 4. fetch_issue_details: {app}")
        rc, out, err = run_stage_process([py_exec, str(ROOT / "crash_trend" / "fetch_issue_details.py"), "--app", app, "--days", str(days)])
        if verbose and out:
            print(out, end="")
        t1 = now_utc_iso()

        if rc != 0:
            err_msg = err.strip() or out.strip() or "Issue details enrichment failed"
            tracker.record_stage(app, "issue_details", "failed", t0, t1, error_message=err_msg)
            if verbose:
                print(f"  [Warning] Issue details 抓取失敗（優雅降級）：{sanitize_error_message(err_msg)}", file=sys.stderr)
        else:
            tracker.record_stage(app, "issue_details", "success", t0, t1)

        # -------------------------------------------------------------------
        # 5. Normalize (Core / History Stage)
        # -------------------------------------------------------------------
        t0 = now_utc_iso()
        if verbose:
            print(f"--- 5. normalize: {app}")
        rc, out, err = run_stage_process([py_exec, str(ROOT / "crash_trend" / "normalize.py"), "--app", app])
        if verbose and out:
            print(out, end="")
        t1 = now_utc_iso()

        if rc != 0:
            err_msg = err.strip() or out.strip() or "Normalize failed"
            tracker.record_stage(app, "normalize", "failed", t0, t1, error_message=err_msg)
        else:
            tracker.record_stage(app, "normalize", "success", t0, t1)

        # -------------------------------------------------------------------
        # 6. Analyze Gemini / Priority (Optional Stage)
        # -------------------------------------------------------------------
        t0 = now_utc_iso()
        has_gemini_key = bool(os.environ.get("GEMINI_API_KEY"))
        if verbose:
            print(f"--- 6. analyze_gemini: {app} (has_gemini_key: {has_gemini_key})")
        rc, out, err = run_stage_process([py_exec, str(ROOT / "crash_trend" / "analyze_gemini.py"), "--app", app])
        if verbose and out:
            print(out, end="")
        t1 = now_utc_iso()

        if not has_gemini_key:
            tracker.record_stage(
                app,
                "ai",
                "disabled",
                t0,
                t1,
                details={"reason": "GEMINI_API_KEY not set"},
            )
        elif rc != 0:
            err_msg = err.strip() or out.strip() or "AI analysis failed"
            tracker.record_stage(app, "ai", "failed", t0, t1, error_message=err_msg)
            if verbose:
                print(f"  [Warning] AI 分析失敗（優雅降級）：{sanitize_error_message(err_msg)}", file=sys.stderr)
        else:
            tracker.record_stage(app, "ai", "success", t0, t1)

        # -------------------------------------------------------------------
        # 7. Check Surge (Optional Stage)
        # -------------------------------------------------------------------
        t0 = now_utc_iso()
        if verbose:
            print(f"--- 7. check_surge: {app}")
        rc, out, err = run_stage_process([py_exec, str(ROOT / "crash_trend" / "check_surge.py"), "--app", app])
        if verbose and out:
            print(out, end="")
        t1 = now_utc_iso()

        if rc != 0:
            err_msg = err.strip() or out.strip() or "Check surge failed"
            tracker.record_stage(app, "surge", "failed", t0, t1, error_message=err_msg)
        else:
            tracker.record_stage(app, "surge", "success", t0, t1)

    # -----------------------------------------------------------------------
    # 8. Build Dashboard (Core Stage)
    # -----------------------------------------------------------------------
    if not skip_dashboard:
        t0 = now_utc_iso()
        if verbose:
            print("\n--- 8. build_dashboard (Dashboard V2 Bundle)")
        rc, out, err = run_stage_process([py_exec, str(ROOT / "crash_trend" / "build_dashboard.py")])
        if verbose and out:
            print(out, end="")
        t1 = now_utc_iso()

        if rc != 0:
            err_msg = err.strip() or out.strip() or "Dashboard build failed"
            tracker.record_stage(None, "build_dashboard", "failed", t0, t1, error_message=err_msg)
            if verbose:
                print(f"  [Error] Dashboard 產出失敗：{sanitize_error_message(err_msg)}", file=sys.stderr)
        else:
            tracker.record_stage(None, "build_dashboard", "success", t0, t1)

    saved_file = tracker.save_summary(summary_path or DEFAULT_RUN_SUMMARY_PATH)
    summary = tracker.build_summary()

    if verbose:
        print("\n==================== [Pipeline Run Summary] ====================")
        print(f"  Status:   {summary['status'].upper()}")
        print(f"  Duration: {summary['duration_sec']}s")
        print(f"  Report:   {saved_file}")
        for a_name, a_sum in summary["apps"].items():
            print(f"  - App [{a_name}]: {a_sum['status']}")
            for s_name, s_res in a_sum["stages"].items():
                st_icon = "✓" if s_res["status"] == "success" else ("—" if s_res["status"] in {"disabled", "skipped"} else "⚠")
                err_disp = f" ({s_res['error_message']})" if s_res.get("error_message") else ""
                print(f"      {st_icon} {s_name:<22} [{s_res['status']}] {s_res['duration_sec']}s{err_disp}")
        print("===============================================================\n")

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="執行 Crash-trend 端到端管線並產出 Run Summary")
    parser.add_argument("--app", action="append", dest="apps", help="指定執行的 App 名稱（可指定多次，預設為 apps.yaml 全部）")
    parser.add_argument("--days", type=int, default=30, help="回溯天數（預設 30 天）")
    parser.add_argument("--summary-out", type=Path, default=DEFAULT_RUN_SUMMARY_PATH, help="輸出之 pipeline_run.json 路徑")
    parser.add_argument("--skip-dashboard", action="store_true", help="略過 build_dashboard 階段")
    parser.add_argument("--quiet", action="store_true", help="減少詳細輸出")
    args = parser.parse_args()

    summary = run_pipeline(
        app_names=args.apps,
        days=args.days,
        summary_path=args.summary_out,
        skip_dashboard=args.skip_dashboard,
        verbose=not args.quiet,
    )

    # Return non-zero if overall pipeline status is failed
    if summary["status"] == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
