"""Provider-neutral Crash Intelligence analysis CLI entrypoint (Dashboard V2.3 - Issue #26).

Provides unified analysis for both Dashboard V2 data and legacy monthly reports,
resolving the configured AIProvider (Gemini / OpenRouter) from app and global config.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Optional

from crash_trend.ai_provider import (
    AIProvider,
    CANONICAL_AI_RESPONSE_SCHEMA,
    GeminiProvider,
    OpenRouterProvider,
    get_ai_provider,
)
from crash_trend.analyze_gemini import (
    AIIssueAnalysis,
    AISummary,
    IssueSummary,
    PriorityBreakdown,
    PriorityInfo,
    RecommendedAction,
    build_ai_prompt,
    calculate_priority,
    call_gemini,
    enrich_app_data_with_priority_and_ai,
    generate_disabled_ai_summary,
    generate_disabled_issue_analysis,
    generate_error_ai_summary,
    get_latest_app_version,
    iso_utc_now,
    map_score_to_level,
    parse_ai_response,
    parse_gemini_response,
    render_md,
    resolve_api_key,
    score_issues,
    source_snippet,
)
from crash_trend.config import ROOT, app_argparser, get_app, load_config, write_json
from crash_trend.pipeline_health import sanitize_error_message
from crash_trend.schema_v2 import is_valid_iso8601_utc, validate_app_dashboard_v2


def main() -> None:
    p = app_argparser("AI 智慧分析與策略建議 (Dashboard V2.3)")
    p.add_argument("--top", type=int, default=5, help="送分析的 top issues 數（預設 5）")
    args = p.parse_args()

    cfg = load_config()
    app = get_app(args.app, cfg)
    provider = get_ai_provider(app_cfg=app, global_cfg=cfg)
    month = dt.date.today().strftime("%Y-%m")

    # 1. Dashboard V2 契約分析
    v2_path = ROOT / "out" / args.app / "dashboard_v2.json"
    if v2_path.exists():
        try:
            app_v2 = json.loads(v2_path.read_text(encoding="utf-8"))
            enriched_v2 = enrich_app_data_with_priority_and_ai(
                app_v2,
                provider=provider,
                app_cfg=app,
                core_paths=app.get("core_paths", []),
                top_limit=args.top,
            )
            val_errors = validate_app_dashboard_v2(enriched_v2)
            if val_errors:
                print(f"  [警告] Schema V2 驗證警告：{val_errors[:3]}", file=sys.stderr)
            write_json(v2_path, enriched_v2)
            print(
                f"  ✓ 已更新 {v2_path.relative_to(ROOT)} Priority Score 與 AI 策略摘要 "
                f"(provider: {provider.provider_name}, model: {provider.model_name})"
            )
        except Exception as e:
            safe_err = sanitize_error_message(str(e))
            print(f"  ⚠ 更新 dashboard_v2.json AI 優先級分析失敗：{safe_err}", file=sys.stderr)

    # 2. 傳統月報 Markdown 與 reports/data 產出（若 unified.json 存在）
    unified_path = ROOT / "out" / args.app / "unified.json"
    if not unified_path.exists():
        if v2_path.exists():
            print(f"  （已完成 V2 分析，未找到 {unified_path.name}，略過傳統 Markdown 月報）")
            return
        sys.exit(f"[錯誤] 找不到 {unified_path}，先跑 fetch_bigquery.py 或 normalize.py")

    u = json.loads(unified_path.read_text(encoding="utf-8"))
    summary_path = ROOT / "reports" / "data" / args.app / f"{month}.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    months = sorted(f.stem for f in summary_path.parent.glob("*.json")) if summary_path.parent.is_dir() else []
    prev = {}
    if len(months) > 1 and months[-1] == month:
        prev = json.loads((summary_path.parent / f"{months[-2]}.json").read_text(encoding="utf-8"))

    report_dir = ROOT / "reports" / args.app
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{month}.md"

    issues = u.get("issues", [])
    if not issues:
        report_path.write_text(
            f"# {u.get('display_name', args.app)} Crash 月報 {month}\n\n本期無 crash 資料"
            f"（來源狀態：{u.get('sources')}）。\n",
            encoding="utf-8",
        )
        print(f"  ✓ 本期無資料，已產出空月報 {report_path.relative_to(ROOT)}")
        return

    # 計算 Priority Score
    scored = score_issues(issues, prev.get("top_issues", []), app.get("core_paths", []))

    # 抓取真實 stack trace 與 blame frame
    st_path = ROOT / "out" / args.app / "stacktraces.json"
    stacks = json.loads(st_path.read_text(encoding="utf-8")).get("issues", {}) if st_path.exists() else {}
    repo = Path(app.get("source_repo", "")).expanduser() if app.get("source_repo") else None

    for i in scored:
        st = stacks.get(i.get("issue_id") or "") or {}
        if st:
            if "blame_frame" not in i and st.get("blame_frame"):
                i["blame_frame"] = st["blame_frame"]
            if "detail" not in i:
                i["detail"] = {
                    "stack_trace": st.get("stack_trace"),
                    "breadcrumbs": [],
                    "logs": [],
                    "custom_keys": None,
                    "top_devices": None,
                    "top_os": None,
                }

    if provider.is_configured():
        snippets = []
        for i in scored[:args.top]:
            st = stacks.get(i.get("issue_id") or "")
            if st and st.get("stack_trace"):
                parts = [f"[issue {i.get('issue_id')}] Stack Trace:\n{st['stack_trace']}"]
                bf = st.get("blame_frame") or {}
                if repo and repo.is_dir() and bf.get("file"):
                    snip = source_snippet(repo, f"{bf['file']}:{bf.get('line', '')}")
                    if snip:
                        parts.append(f"元兇 frame 原始碼：\n{snip}")
                snippets.append("\n".join(parts))
            elif repo and repo.is_dir() and i.get("subtitle"):
                snip = source_snippet(repo, i["subtitle"])
                if snip:
                    snippets.append(f"[issue {i.get('issue_id')}]\n{snip}")

        prompt = build_ai_prompt(
            display_name=u.get("display_name", args.app),
            kpi=summary.get("kpis", {}),
            prev_kpi=prev.get("kpis"),
            scored_issues=scored[:args.top],
            distributions=u.get("distributions", {}),
            custom_keys=u.get("custom_keys", []),
            trend_data=u.get("weekly_trend", []),
            snippets=snippets,
        )
        try:
            raw_ai = provider.analyze(prompt, schema=CANONICAL_AI_RESPONSE_SCHEMA)
            ai_summary, analysis_map = parse_gemini_response(
                raw_ai, scored, model_name=provider.model_name, provider_name=provider.provider_name
            )
            for i in scored:
                i["ai_analysis"] = analysis_map.get(i.get("issue_id", ""), generate_disabled_issue_analysis())
        except Exception as e:
            safe_err = sanitize_error_message(str(e))
            print(f"  ⚠ {provider.provider_name.upper()} 分析失敗，優雅降級：{safe_err}")
            ai_summary = generate_error_ai_summary(safe_err, provider=provider.provider_name)
            for i in scored:
                i["ai_analysis"] = generate_disabled_issue_analysis()
    else:
        ai_summary = generate_disabled_ai_summary(
            f"未設定 {provider.provider_name.upper()} API 金鑰", provider=provider.provider_name
        )
        for i in scored:
            i["ai_analysis"] = generate_disabled_issue_analysis()

    report_path.write_text(
        render_md(
            args.app,
            u.get("display_name", args.app),
            month,
            {"kpis": summary.get("kpis", {}), "prev_kpis": prev.get("kpis")},
            ai_summary,
            scored[:args.top],
            summary.get("fix_review"),
        ),
        encoding="utf-8",
    )
    print(f"  ✓ 月報 {report_path.relative_to(ROOT)}")

    if summary:
        summary["priority_list"] = scored
        summary["ai_summary"] = ai_summary
        write_json(summary_path, summary)


if __name__ == "__main__":
    main()
