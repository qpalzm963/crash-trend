"""Pipeline Runner and Orchestrator (Dashboard V2.2 - Issue #22).

Executes end-to-end crash-trend pipeline stages across configured apps, records
structured stage outcomes, duration, and sanitized error messages, and saves
`out/pipeline_run.json`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent

try:
    from crash_trend.ai_provider import get_ai_provider, get_ai_router
    from crash_trend.config import get_app, get_mcp_config, is_sessions_enabled, load_config
    from crash_trend.pipeline_health import (
        DEFAULT_RUN_SUMMARY_PATH,
        PipelineRunTracker,
        StageStatus,
        now_utc_iso,
        sanitize_error_message,
    )
except ImportError:
    try:
        from ai_provider import get_ai_provider, get_ai_router
        from config import get_app, get_mcp_config, is_sessions_enabled, load_config
        from pipeline_health import (
            DEFAULT_RUN_SUMMARY_PATH,
            PipelineRunTracker,
            StageStatus,
            now_utc_iso,
            sanitize_error_message,
        )
    except ImportError:
        from .ai_provider import get_ai_provider, get_ai_router  # type: ignore
        from .config import get_app, get_mcp_config, is_sessions_enabled, load_config  # type: ignore
        from .pipeline_health import (  # type: ignore
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

        try:
            app_cfg = get_app(app, cfg)
        except TypeError:
            app_cfg = get_app(app)
        app_out_dir = ROOT / "out" / app
        v2_path = app_out_dir / "dashboard_v2.json"

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
        err_msg = err.strip() or out.strip() or ""

        # Inspect canonical artifact even if rc == 0
        if not bq_failed and v2_path.is_file():
            try:
                v2_data = json.loads(v2_path.read_text(encoding="utf-8"))
                bq_src = v2_data.get("sources", {}).get("crashlytics_bq", {})
                if bq_src.get("status") == "error":
                    bq_failed = True
                    err_msg = bq_src.get("error_message") or "BigQuery query failed"
            except Exception:
                pass
        elif not bq_failed and (app_out_dir / "crashlytics_bq.json").is_file():
            try:
                bq_data = json.loads((app_out_dir / "crashlytics_bq.json").read_text(encoding="utf-8"))
                if bq_data.get("errors") and not bq_data.get("tables"):
                    bq_failed = True
                    err_msg = str(bq_data.get("errors"))
            except Exception:
                pass

        if bq_failed:
            tracker.record_stage(app, "crashlytics_bigquery", "failed", t0, t1, error_message=err_msg or "BigQuery fetch failed")
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
            # Crucial: Check canonical artifact status even when rc == 0
            sess_json_path = app_out_dir / "sessions.json"
            sess_status = "available"
            sess_err_msg = None

            if sess_json_path.is_file():
                try:
                    s_data = json.loads(sess_json_path.read_text(encoding="utf-8"))
                    src_obj = s_data.get("sources", {})
                    sess_status = src_obj.get("status", "available")
                    sess_err_msg = src_obj.get("error_message")
                except Exception:
                    pass
            elif v2_path.is_file():
                try:
                    v2_data = json.loads(v2_path.read_text(encoding="utf-8"))
                    src_obj = v2_data.get("sources", {}).get("firebase_sessions", {})
                    sess_status = src_obj.get("status", "available")
                    sess_err_msg = src_obj.get("error_message")
                except Exception:
                    pass

            if sess_status == "error":
                tracker.record_stage(app, "sessions", "failed", t0, t1, error_message=sess_err_msg or "Sessions query returned error")
                if verbose:
                    print(f"  [Warning] Sessions 查詢失敗（優雅降級）：{sanitize_error_message(sess_err_msg)}", file=sys.stderr)
            elif sess_status == "disabled" or (sess_err_msg and ("disabled" in sess_err_msg.lower() or "已停用" in sess_err_msg)):
                tracker.record_stage(app, "sessions", "disabled", t0, t1, error_message=sess_err_msg)
            elif sess_status == "unavailable" and sess_err_msg and "disabled" not in sess_err_msg.lower():
                tracker.record_stage(app, "sessions", "failed", t0, t1, error_message=sess_err_msg)
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
            mcp_err_file = app_out_dir / "stacktraces_last_error.json"
            rc, out, err = run_stage_process([py_exec, str(ROOT / "crash_trend" / "fetch_stacktraces.py"), "--app", app, "--weekly-check"])
            if verbose and out:
                print(out, end="")
            t1 = now_utc_iso()

            # Check output: if stdout clearly indicates fresh cache skip, prioritize skipped!
            # Even if an old stacktraces_last_error.json exists from a previous run, a fresh cache skip must not fail!
            is_cache_fresh_skip = rc == 0 and ("快取仍有效" in out or "略過" in out)

            # Check if an error artifact exists
            mcp_has_error = False
            mcp_err_msg = None
            if mcp_err_file.is_file():
                try:
                    err_data = json.loads(mcp_err_file.read_text(encoding="utf-8"))
                    if err_data.get("errors") or err_data.get("error_message"):
                        mcp_has_error = True
                        mcp_err_msg = err_data.get("error_message") or str(err_data.get("errors"))
                except Exception:
                    mcp_has_error = True

            if is_cache_fresh_skip:
                tracker.record_stage(
                    app,
                    "mcp",
                    "skipped",
                    t0,
                    t1,
                    details={"reason": "Cache is fresh, skipped refresh"},
                )
            elif rc != 0 or mcp_has_error:
                err_text = mcp_err_msg or err.strip() or out.strip() or "MCP refresh failed"
                tracker.record_stage(app, "mcp", "failed", t0, t1, error_message=err_text)
                if verbose:
                    print(f"  [Warning] MCP 刷新失敗（優雅降級）：{sanitize_error_message(err_text)}", file=sys.stderr)
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
        # 6. Analyze AI / Priority (Optional Stage)
        # -------------------------------------------------------------------
        t0 = now_utc_iso()
        config_error: Optional[str] = None
        mode_name = "auto"
        routing_reason = ""
        try:
            router = get_ai_router(app_cfg, cfg)
            decision = router.route("deep_analysis")
            has_ai_key = router.is_configured("deep_analysis")
            provider_name = decision.selected_provider
            model_name = decision.selected_model
            mode_name = decision.mode
            routing_reason = decision.routing_reason
        except (ValueError, RuntimeError) as e:
            config_error = str(e)
            has_ai_key = False
            provider_name = "unknown"
            model_name = None
            routing_reason = str(e)

        if config_error:
            t1 = now_utc_iso()
            safe_err = sanitize_error_message(config_error)
            tracker.record_stage(
                app,
                "ai",
                "failed",
                t0,
                t1,
                error_message=safe_err,
                details={"reason": "invalid_ai_config", "error": safe_err},
            )
            if verbose:
                print(f"  [Warning] AI 設定無效（優雅降級）：{safe_err}", file=sys.stderr)
        else:
            if verbose:
                print(f"--- 6. analyze_ai: {app} (mode: {mode_name}, provider: {provider_name}, model: {model_name}, configured: {has_ai_key})")
            rc, out, err = run_stage_process([py_exec, "-m", "crash_trend.analyze_ai", "--app", app])
            if verbose and out:
                print(out, end="")
            t1 = now_utc_iso()

            # Crucial: Check canonical artifact status even when rc == 0
            ai_status = "available" if has_ai_key else "disabled"
            ai_err_msg = None
            ai_details = {
                "mode": mode_name,
                "provider": provider_name,
                "model": model_name,
                "routing_reason": routing_reason,
            }
            if v2_path.is_file():
                try:
                    v2_data = json.loads(v2_path.read_text(encoding="utf-8"))
                    ai_src = v2_data.get("sources", {}).get("ai") or v2_data.get("sources", {}).get("gemini_ai", {})
                    ai_sum = v2_data.get("ai_summary", {})
                    ai_status = ai_src.get("status") or ai_sum.get("status") or ai_status
                    ai_err_msg = ai_src.get("error_message") or ai_sum.get("data_limitations")
                    if ai_src.get("provider"):
                        ai_details["provider"] = ai_src["provider"]
                    if ai_src.get("model"):
                        ai_details["model"] = ai_src["model"]
                    if ai_src.get("requested_mode"):
                        ai_details["mode"] = ai_src["requested_mode"]
                    if ai_src.get("routing_reason"):
                        ai_details["routing_reason"] = ai_src["routing_reason"]
                    if "fallback_used" in ai_src:
                        ai_details["fallback_used"] = ai_src["fallback_used"]
                except Exception:
                    pass

            if rc != 0 or ai_status == "error":
                err_text = ai_err_msg or err.strip() or out.strip() or "AI analysis failed"
                tracker.record_stage(
                    app,
                    "ai",
                    "failed",
                    t0,
                    t1,
                    error_message=err_text,
                    details=ai_details,
                )
                if verbose:
                    print(f"  [Warning] AI 分析失敗（優雅降級）：{sanitize_error_message(err_text)}", file=sys.stderr)
            elif ai_status == "disabled" or not has_ai_key:
                ai_details["reason"] = f"{ai_details['provider'].upper()} API key not configured"
                tracker.record_stage(
                    app,
                    "ai",
                    "disabled",
                    t0,
                    t1,
                    details=ai_details,
                )
            else:
                tracker.record_stage(
                    app,
                    "ai",
                    "success",
                    t0,
                    t1,
                    details=ai_details,
                )

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
    effective_summary_path = summary_path or DEFAULT_RUN_SUMMARY_PATH
    dashboard_rc = 0
    if not skip_dashboard:
        # Save provisional summary BEFORE build_dashboard so builder has current run,
        # but finalize=False ensures finished_at and duration_sec are not frozen early!
        tracker.save_summary(effective_summary_path, finalize=False)

        t0 = now_utc_iso()
        if verbose:
            print("\n--- 8. build_dashboard (Dashboard V2 Bundle)")
        dashboard_rc, out, err = run_stage_process(
            [py_exec, str(ROOT / "crash_trend" / "build_dashboard.py")],
            env={"PIPELINE_RUN_SUMMARY": str(effective_summary_path)},
        )
        if verbose and out:
            print(out, end="")
        t1 = now_utc_iso()

        if dashboard_rc != 0:
            err_msg = err.strip() or out.strip() or "Dashboard build failed"
            tracker.record_stage(None, "build_dashboard", "failed", t0, t1, error_message=err_msg)
            if verbose:
                print(f"  [Error] Dashboard 產出失敗：{sanitize_error_message(err_msg)}", file=sys.stderr)
        else:
            tracker.record_stage(None, "build_dashboard", "success", t0, t1)

    # Finalize summary after all stages have completed
    tracker.reset_finish()
    saved_file = tracker.save_summary(effective_summary_path, finalize=True)
    summary = tracker.build_summary(finalize=True)

    # Ensure the finalized summary (including build_dashboard stage and full duration)
    # is updated in the saved bundle AND in dashboard.html
    if not skip_dashboard and dashboard_rc == 0:
        for b_path in [ROOT / "out" / "dashboard_v2.json", ROOT / "reports" / "dashboard_v2.json"]:
            if b_path.is_file():
                try:
                    b_data = json.loads(b_path.read_text(encoding="utf-8"))
                    b_data["pipeline_run"] = summary
                    b_path.write_text(json.dumps(b_data, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass

        # Re-render dashboard.html with finalized summary so UI displays final duration
        v2_bundle_path = ROOT / "out" / "dashboard_v2.json"
        if v2_bundle_path.is_file():
            try:
                from crash_trend.build_dashboard import generate_dashboard
                final_bundle = json.loads(v2_bundle_path.read_text(encoding="utf-8"))
                generate_dashboard(final_bundle, output_path=ROOT / "dashboard.html")
            except Exception:
                pass

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
