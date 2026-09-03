"""Gemini AI 分析器與確定性優先級計算引擎 (Dashboard V2 Schema 相容)。

分工原則（判斷交給模型、計算留在程式）：
  1. 程式計算（確定性）：
     - Priority Score (0-100)：基於受影響用戶數 (users)、事件數 (events)、致命/凍結 (fatal/ANR)、
       惡化趨勢 (worsening)、最新版影響 (latest_version)、核心路徑 (core_paths) 權重加總。
     - Priority Level：明確 mapping 為 P0、P1、P2、P3。
     - Trend 標記：new、worsening、stable、improving。
     - Score Breakdown：各指標維度加分細項。
  2. Gemini AI（深度分析）：
     - 各 Issue Root Cause 推測（依據 stack trace、blame frame、版本分布）。
     - 具體可執行的修復建議 (suggested_fix)。
     - 工作量估計 (effort: S/M/L) 與信心度 (confidence: high/medium/low/needs_manual_review)。
     - 首頁 AI 策略摘要 (ai_summary: overview, key_takeaways, distribution_insights, recommended_actions)。
     - 資訊不足時標註「需人工確認」，絕不編造。
  3. 優雅降級 (Graceful Degradation)：
     - 當未配置 GEMINI_API_KEY 時，ai_summary.status 標為 disabled，各 issue ai_analysis.status 標為 unavailable。
     - 不影響 Priority Score 計算與整體 Dashboard 運作。

環境變數：GEMINI_API_KEY 或 GEMINI_KEY_URL（後台代管）擇一、GEMINI_MODEL（預設 gemini-flash-latest）
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import requests

try:
    from .config import ROOT, app_argparser, get_app, write_json
    from .schema_v2 import (
        AIIssueAnalysis,
        AISummary,
        IssueSummary,
        PriorityBreakdown,
        PriorityInfo,
        RecommendedAction,
        is_valid_iso8601_utc,
        validate_app_dashboard_v2,
    )
    from .versions import max_version, min_version, version_key
    from .ai_provider import (
        AIProvider,
        CANONICAL_AI_RESPONSE_SCHEMA,
        GeminiProvider,
        OpenRouterProvider,
        get_ai_provider,
        resolve_gemini_key,
    )
    from .pipeline_health import sanitize_error_message
except ImportError:
    try:
        from config import ROOT, app_argparser, get_app, write_json
        from schema_v2 import (
            AIIssueAnalysis,
            AISummary,
            IssueSummary,
            PriorityBreakdown,
            PriorityInfo,
            RecommendedAction,
            is_valid_iso8601_utc,
            validate_app_dashboard_v2,
        )
        from versions import max_version, min_version, version_key
        from ai_provider import (
            AIProvider,
            CANONICAL_AI_RESPONSE_SCHEMA,
            GeminiProvider,
            OpenRouterProvider,
            get_ai_provider,
            resolve_gemini_key,
        )
        from pipeline_health import sanitize_error_message
    except ImportError:
        from crash_trend.config import ROOT, app_argparser, get_app, write_json
        from crash_trend.schema_v2 import (
            AIIssueAnalysis,
            AISummary,
            IssueSummary,
            PriorityBreakdown,
            PriorityInfo,
            RecommendedAction,
            is_valid_iso8601_utc,
            validate_app_dashboard_v2,
        )
        from crash_trend.versions import max_version, min_version, version_key
        from crash_trend.ai_provider import (
            AIProvider,
            CANONICAL_AI_RESPONSE_SCHEMA,
            GeminiProvider,
            OpenRouterProvider,
            get_ai_provider,
            resolve_gemini_key,
        )
        from crash_trend.pipeline_health import sanitize_error_message


API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def iso_utc_now() -> str:
    """Returns current UTC timestamp in ISO 8601 format (YYYY-MM-DDTHH:MM:SSZ)."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 1. 確定性 Priority Score 計算 (0-100) 與 P0~P3 等級 Mapping
# ---------------------------------------------------------------------------

def map_score_to_level(score: int) -> Literal["P0", "P1", "P2", "P3"]:
    """Maps a priority score (0-100) to P0, P1, P2, P3 levels.

    - P0: 80 - 100 (緊急/阻斷性問題，高用戶影響、致命或核心路徑)
    - P1: 60 - 79  (高優先級問題)
    - P2: 40 - 59  (中優先級問題)
    - P3: 0  - 39  (低優先級問題)
    """
    if score >= 80:
        return "P0"
    if score >= 60:
        return "P1"
    if score >= 40:
        return "P2"
    return "P3"


def calculate_priority(
    issue: dict,
    max_users: int,
    max_events: int,
    prev_issue: dict | None = None,
    core_paths: list[str] | None = None,
    latest_app_version: str | None = None,
    has_baseline: bool = True,
) -> PriorityInfo:
    """Calculates deterministic PriorityInfo and score breakdown for a single issue.

    Weights & Points:
      - Users normalized (0-10) * 3.0 = max 30 pts
      - Events normalized (0-10) * 1.0 = max 10 pts
      - Fatal or ANR boost = 2 pts
      - Worsening or New trend boost = 2 pts
      - Latest version occurrence boost = 2 pts
      - Core path match boost = 3 pts
      Total raw points = 49 pts max.
      Scaled to 0 - 100 scale: score = round((raw_points / 49.0) * 100).
    """
    core_paths = core_paths or []
    users = issue.get("affected_users") if issue.get("affected_users") is not None else issue.get("users", 0)
    events = issue.get("events", 0)

    # 1. Trend calculation
    if not has_baseline:
        trend: Literal["new", "worsening", "stable", "improving"] = "stable"
    elif prev_issue is None:
        trend = "new"
    else:
        prev_e = prev_issue.get("events", 0)
        if prev_e > 0 and events > prev_e * 1.2:
            trend = "worsening"
        elif prev_e > 0 and events < prev_e * 0.8:
            trend = "improving"
        elif prev_e == 0 and events > 0:
            trend = "worsening"
        else:
            trend = "stable"

    # 2. Normalization (0.0 to 10.0)
    u_norm = round((users / max(max_users, 1)) * 10.0, 1)
    e_norm = round((events / max(max_events, 1)) * 10.0, 1)

    # 3. Boost factors
    error_type = (issue.get("error_type") or "").upper()
    is_fatal = issue.get("fatal") is True or error_type in ("FATAL", "ANR")
    fatal_anr_boost = 2 if is_fatal else 0

    worsening_boost = 2 if (has_baseline and trend in ("worsening", "new")) else 0

    last_ver = issue.get("last_seen_version") or ""
    latest_version_boost = 2 if (latest_app_version and last_ver == latest_app_version) else 0

    # Core path check in title, subtitle, or blame_frame.file
    blame_f = ""
    if isinstance(issue.get("blame_frame"), dict):
        blame_f = issue.get("blame_frame", {}).get("file") or ""
    search_target = f"{issue.get('title', '')} {issue.get('subtitle', '')} {blame_f}".lower()
    core_matched = any(k.lower() in search_target for k in core_paths) if core_paths else False
    core_path_boost = 3 if core_matched else 0

    # Lifecycle regression check
    lc = issue.get("lifecycle") or {}
    regressed_boost = 2 if lc.get("status") == "regressed" else 0

    # 4. Raw score sum (max 49 points)
    raw_points = (
        (u_norm * 3.0)
        + (e_norm * 1.0)
        + fatal_anr_boost
        + worsening_boost
        + latest_version_boost
        + core_path_boost
        + regressed_boost
    )

    # Scale to 0-100
    score = min(100, max(0, int(round((raw_points / 49.0) * 100))))
    level = map_score_to_level(score)

    breakdown: PriorityBreakdown = {
        "users_normalized": u_norm,
        "events_normalized": e_norm,
        "fatal_anr_boost": fatal_anr_boost,
        "worsening_boost": worsening_boost,
        "latest_version_boost": latest_version_boost,
        "core_path_boost": core_path_boost,
        "regressed_boost": regressed_boost,
    }

    return {
        "score": score,
        "level": level,
        "trend": trend,
        "score_breakdown": breakdown,
    }


def get_latest_app_version(app_data: dict) -> str | None:
    """Extracts the true latest app version from version_health or distributions.

    Priority:
    1. version_health item where status == 'latest'
    2. Max semver version among version_health items
    3. Max semver version in distributions.app_versions
    4. None (do NOT infer from top_issues.last_seen_version to avoid false positives when latest version has 0 crashes)
    """
    if not isinstance(app_data, dict):
        return None

    vh = app_data.get("version_health") or []
    for v in vh:
        if isinstance(v, dict) and v.get("status") == "latest" and v.get("version"):
            return str(v["version"]).strip()

    vh_versions = [str(v.get("version")).strip() for v in vh if isinstance(v, dict) and v.get("version")]
    if vh_versions:
        return max_version(vh_versions)

    dist_versions = app_data.get("distributions", {}).get("app_versions") or []
    dist_v_list = [str(v.get("app_version")).strip() for v in dist_versions if isinstance(v, dict) and v.get("app_version")]
    if dist_v_list:
        return max_version(dist_v_list)

    return None


def score_issues(
    issues: list[dict],
    prev_issues: list[dict] | None = None,
    core_paths: list[str] | None = None,
    latest_app_version: str | None = None,
    has_baseline: bool = True,
) -> list[dict]:
    """Scores a list of issues and returns them sorted by priority score descending."""
    if not issues:
        return []
    prev_issues = prev_issues or []
    core_paths = core_paths or []

    max_u = max(
        (i.get("affected_users") if i.get("affected_users") is not None else i.get("users", 0) for i in issues),
        default=0,
    ) or 1
    max_e = max((i.get("events", 0) for i in issues), default=0) or 1

    prev_by_id = {p["issue_id"]: p for p in prev_issues if p.get("issue_id")}
    prev_by_title = {p["title"]: p for p in prev_issues if p.get("title")}

    scored = []
    for i in issues:
        prev = prev_by_id.get(i.get("issue_id")) or prev_by_title.get(i.get("title"))
        prio = calculate_priority(
            i,
            max_users=max_u,
            max_events=max_e,
            prev_issue=prev,
            core_paths=core_paths,
            latest_app_version=latest_app_version,
            has_baseline=has_baseline,
        )
        scored.append({
            **i,
            "priority": prio,
            "score": prio["score"],  # Backward compatibility
            "trend": prio["trend"],  # Backward compatibility
        })

    return sorted(scored, key=lambda x: -x["priority"]["score"])


# ---------------------------------------------------------------------------
# 2. 原始碼片段擷取 (Source Snippet Extraction)
# ---------------------------------------------------------------------------

def source_snippet(source_repo: Path | str | None, subtitle_or_path: str, max_lines: int = 50) -> str:
    """從 issue 位置（如 CheckoutActivity.kt:142 或 services/auth.dart:50）抓取原始碼片段。"""
    if not source_repo or not subtitle_or_path:
        return ""
    repo_path = Path(source_repo).expanduser()
    if not repo_path.is_dir():
        return ""

    m = re.search(r"([\w/.-]+\.(?:dart|kt|java|swift|m|mm|cpp|c|h|ts|js|py))(?::| line )?(\d+)?", subtitle_or_path)
    if not m:
        return ""

    filename = Path(m.group(1)).name
    try:
        hits = list(repo_path.rglob(filename))
    except Exception:
        return ""
    if not hits:
        return ""

    try:
        lines = hits[0].read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return ""

    center = int(m.group(2)) - 1 if m.group(2) else 0
    lo, hi = max(0, center - max_lines // 2), min(len(lines), center + max_lines // 2)
    body = "\n".join(f"{n + 1}| {lines[n]}" for n in range(lo, hi))
    try:
        rel_path = hits[0].relative_to(repo_path)
    except Exception:
        rel_path = hits[0]
    return f"// {rel_path}\n{body}"


# ---------------------------------------------------------------------------
# 3. Gemini API Key 解析與優雅降級預設結構
# ---------------------------------------------------------------------------
def resolve_api_key(raise_on_missing: bool = False) -> str | None:
    """取得 Gemini API Key。順序：GEMINI_API_KEY env → GEMINI_KEY_URL。"""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key.strip()
    key_url = os.environ.get("GEMINI_KEY_URL")
    if key_url:
        try:
            r = requests.get(
                key_url,
                headers={"x-internal-token": os.environ.get("INTERNAL_API_TOKEN", "")},
                timeout=15,
            )
            if r.status_code == 200:
                k = r.json().get("api_key")
                if k:
                    return k.strip()
        except Exception as e:
            if raise_on_missing:
                sys.exit(f"[錯誤] 向後台取 Gemini key 失敗：{e}")
    if raise_on_missing:
        sys.exit("[錯誤] 未設定 GEMINI_API_KEY，也未設定 GEMINI_KEY_URL")
    return None


def generate_disabled_ai_summary(reason: str = "未配置 AI API 金鑰", provider: Optional[str] = None) -> AISummary:
    """未啟用 AI 時的優雅降級 AISummary 結構。"""
    return {
        "status": "disabled",
        "provider": provider,
        "model": None,
        "generated_at": None,
        "overview": f"AI 分析功能未啟用（{reason}）",
        "key_takeaways": [],
        "distribution_insights": "未啟用 AI 分析",
        "recommended_actions": [],
        "data_limitations": None,
    }


def generate_disabled_issue_analysis() -> AIIssueAnalysis:
    """未啟用 AI 時單一 Issue 的優雅降級 AIIssueAnalysis 結構。"""
    return {
        "status": "unavailable",
        "root_cause": None,
        "suggested_fix": None,
        "effort": None,
        "confidence": None,
        "reasoning_sources": None,
    }


def generate_error_ai_summary(error_msg: str, provider: Optional[str] = None) -> AISummary:
    """AI 呼叫失敗時的降級 AISummary 結構。"""
    return {
        "status": "error",
        "provider": provider,
        "model": None,
        "generated_at": None,
        "overview": f"AI 分析過程發生錯誤：{error_msg}",
        "key_takeaways": [],
        "distribution_insights": "分析失敗",
        "recommended_actions": [],
        "data_limitations": error_msg,
    }


# ---------------------------------------------------------------------------
# 4. Gemini Structured Output Schema & API 調用 (Backward Compatibility)
# ---------------------------------------------------------------------------

RESPONSE_SCHEMA_V2 = CANONICAL_AI_RESPONSE_SCHEMA


def call_gemini(
    payload_text: str,
    schema: dict | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    """呼叫 Gemini generateContent API，強制使用 Structured JSON Schema 輸出（向下相容包裝）。"""
    provider = GeminiProvider(api_key=api_key, model=model)
    return provider.analyze(payload_text, schema=schema or CANONICAL_AI_RESPONSE_SCHEMA)


# ---------------------------------------------------------------------------
# 5. Prompt 建構與 Response 解析
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """你是資深行動 App 穩定性工程師與 Crashlytics 崩潰分析專家。
以下是「{display_name}」本期崩潰監控指標、Top Issues、維度分布、真實 Stack Trace 與元兇程式碼片段。
請為工程團隊產出整體 AI 策略摘要，並針對各 Top Issue 提供深度診斷。

【分析與輸出規則】：
1. 僅能依據提供的數據、Stack Trace 與 Blame Frame 進行嚴謹推論。若資訊不足，root_cause 填寫「需人工確認」，絕不可編造。
2. suggested_fix 必須具體且具可執行性（指出可能修改的函式、邊界保護或執行緒切換），避免泛泛而談。
3. 信心度 confidence 請依據事證充分程度選擇：high / medium / low / needs_manual_review。
4. 全文請使用繁體中文。

【回傳欄位規格】：
- overview: 2-3 句本期穩定性總覽（對比上期變化與核心風險）
- key_takeaways: 2-4 點重點摘要（每點一句話）
- distribution_insights: 針對平台、機型、OS 或版本分布的交叉洞察
- recommended_actions: 優先建議行動清單（包含 priority: P0-P3, issue_id, action, effort: S/M/L）
- data_limitations: 數據限制與缺口說明
- items: 各 Issue 的深度分析列表（issue_id, root_cause, suggested_fix, effort, confidence, reasoning_sources）

## 本期 KPI
{kpis}

## 上期 KPI（無則為 null）
{prev_kpis}

## Top Issues（含程式計算之確定性優先分與趨勢標記）
{issues}

## 維度分布摘要
{distributions}

## 自訂 Keys 分布
{custom_keys}

## 趨勢數據
{trend_data}

## Stack Trace 與元兇原始碼片段
{snippets}
"""


def build_ai_prompt(
    display_name: str,
    kpi: dict | None,
    prev_kpi: dict | None,
    scored_issues: list[dict],
    distributions: dict | None = None,
    custom_keys: list | None = None,
    trend_data: list | None = None,
    snippets: list[str] | None = None,
) -> str:
    """建構傳送給 Gemini 的結構化 Prompt。"""
    issues_clean = []
    for i in scored_issues:
        clean_i = {
            "issue_id": i.get("issue_id", ""),
            "title": i.get("title", ""),
            "subtitle": i.get("subtitle", ""),
            "error_type": i.get("error_type", ""),
            "events": i.get("events", 0),
            "affected_users": i.get("affected_users", i.get("users", 0)),
            "priority": i.get("priority", {}),
            "first_seen_version": i.get("first_seen_version", ""),
            "last_seen_version": i.get("last_seen_version", ""),
            "blame_frame": i.get("blame_frame"),
        }
        issues_clean.append(clean_i)

    return PROMPT_TEMPLATE.format(
        display_name=display_name,
        kpis=json.dumps(kpi or {}, ensure_ascii=False, indent=2),
        prev_kpis=json.dumps(prev_kpi, ensure_ascii=False, indent=2) if prev_kpi else "null",
        issues=json.dumps(issues_clean, ensure_ascii=False, indent=2),
        distributions=json.dumps(distributions or {}, ensure_ascii=False)[:3000] if distributions else "（無分布資料）",
        custom_keys=json.dumps(custom_keys or [], ensure_ascii=False)[:2000] if custom_keys else "（無自訂 keys）",
        trend_data=json.dumps(trend_data or [], ensure_ascii=False)[:2000] if trend_data else "（無趨勢數據）",
        snippets="\n\n".join(snippets or [])[:12000] if snippets else "（無可用原始碼片段）",
    )


def parse_gemini_response(
    ai_json: dict,
    scored_issues: list[dict],
    model_name: str | None = None,
    provider_name: str | None = None,
) -> tuple[AISummary, dict[str, AIIssueAnalysis]]:
    """解析並防禦性清洗 AI 回傳的 JSON 結構，對齊 Dashboard V2/V2.3 Schema。"""
    valid_priorities = {"P0", "P1", "P2", "P3"}
    valid_efforts = {"S", "M", "L"}
    valid_confidences = {"high", "medium", "low", "needs_manual_review"}

    # 1. Recommended actions
    raw_actions = ai_json.get("recommended_actions") or []
    cleaned_actions: list[RecommendedAction] = []
    if isinstance(raw_actions, list):
        for act in raw_actions:
            if not isinstance(act, dict):
                continue
            prio = str(act.get("priority", "P1")).upper()
            if prio not in valid_priorities:
                prio = "P1"
            eff = str(act.get("effort", "M")).upper()
            if eff not in valid_efforts:
                eff = "M"
            cleaned_actions.append({
                "priority": prio,  # type: ignore
                "issue_id": str(act.get("issue_id", "")),
                "action": str(act.get("action", "")),
                "effort": eff,  # type: ignore
            })

    # 2. Key Takeaways
    raw_takeaways = ai_json.get("key_takeaways")
    if isinstance(raw_takeaways, list):
        takeaways = [str(x) for x in raw_takeaways if x]
    elif isinstance(raw_takeaways, str) and raw_takeaways.strip():
        takeaways = [raw_takeaways.strip()]
    else:
        takeaways = []

    ai_summary: AISummary = {
        "status": "available",
        "provider": provider_name or "gemini",
        "model": model_name or os.environ.get("GEMINI_MODEL", "gemini-flash-latest"),
        "generated_at": iso_utc_now(),
        "overview": str(ai_json.get("overview") or "本期穩定性分析完成。"),
        "key_takeaways": takeaways,
        "distribution_insights": str(ai_json.get("distribution_insights") or ""),
        "recommended_actions": cleaned_actions,
        "data_limitations": str(ai_json.get("data_limitations")) if ai_json.get("data_limitations") else None,
    }

    # 3. Issue items mapping
    items_by_id: dict[str, dict] = {}
    for it in ai_json.get("items") or []:
        if isinstance(it, dict) and it.get("issue_id"):
            items_by_id[str(it["issue_id"])] = it

    analysis_map: dict[str, AIIssueAnalysis] = {}
    for issue in scored_issues:
        iid = issue.get("issue_id", "")
        item = items_by_id.get(iid)
        if item:
            eff = str(item.get("effort", "M")).upper()
            if eff not in valid_efforts:
                eff = "M"
            conf = str(item.get("confidence", "high")).lower()
            if conf not in valid_confidences:
                conf = "needs_manual_review"

            reasons = item.get("reasoning_sources")
            if not isinstance(reasons, list):
                reasons = ["stack_trace", "blame_frame"] if issue.get("blame_frame") else ["version_distribution"]

            analysis_map[iid] = {
                "status": "available",
                "root_cause": str(item.get("root_cause") or "需人工確認"),
                "suggested_fix": str(item.get("suggested_fix") or "—"),
                "effort": eff,  # type: ignore
                "confidence": conf,  # type: ignore
                "reasoning_sources": reasons,
            }
        else:
            # Issue omitted by model -> mark as skipped
            analysis_map[iid] = {
                "status": "skipped",
                "root_cause": "需人工確認（模型未回傳）",
                "suggested_fix": None,
                "effort": None,
                "confidence": "needs_manual_review",
                "reasoning_sources": [],
            }

    return ai_summary, analysis_map


parse_ai_response = parse_gemini_response


def _sync_periods_priority_and_ai(
    app_data: dict,
    prev_app_data: dict | list | None = None,
    core_paths: list[str] | None = None,
    latest_ver: str | None = None,
) -> None:
    if "periods" not in app_data or not isinstance(app_data["periods"], dict):
        return

    ai_analysis_by_id = {
        iss["issue_id"]: iss.get("ai_analysis")
        for iss in app_data.get("top_issues", [])
        if iss.get("issue_id") and iss.get("ai_analysis")
    }

    main_period_days = str(app_data.get("period", {}).get("days") or "90")
    main_ai = app_data.get("ai_summary") or {}

    # Support backward-compatibility if caller passes prev_issues as a list
    if isinstance(prev_app_data, list):
        prev_periods = {}
        prev_main_days = None
        fallback_prev_issues = prev_app_data
    elif isinstance(prev_app_data, dict):
        prev_periods = prev_app_data.get("periods") or {}
        prev_main_days = str(prev_app_data.get("period", {}).get("days") or "")
        fallback_prev_issues = prev_app_data.get("top_issues") or []
    else:
        prev_periods = {}
        prev_main_days = None
        fallback_prev_issues = []

    for p_key, snap in app_data["periods"].items():
        if not isinstance(snap, dict):
            continue

        # 同 period baseline 解析：優先使用 prev_app_data.periods[p_key].top_issues
        if str(p_key) in prev_periods and isinstance(prev_periods[str(p_key)], dict):
            snap_prev_issues = prev_periods[str(p_key)].get("top_issues") or []
            has_baseline = True
        elif prev_main_days and str(p_key) == prev_main_days:
            snap_prev_issues = fallback_prev_issues
            has_baseline = True
        elif isinstance(prev_app_data, list) and str(p_key) == main_period_days:
            snap_prev_issues = fallback_prev_issues
            has_baseline = True
        else:
            snap_prev_issues = []
            has_baseline = False

        # snapshot-specific latest_ver scope
        snap_latest_ver = get_latest_app_version(snap) or latest_ver

        snap_issues = snap.get("top_issues", [])
        if snap_issues and isinstance(snap_issues, list):
            snap_scored = score_issues(
                snap_issues,
                prev_issues=snap_prev_issues,
                core_paths=core_paths,
                latest_app_version=snap_latest_ver,
                has_baseline=has_baseline,
            )
            for si in snap_scored:
                iid = si.get("issue_id")
                if iid in ai_analysis_by_id:
                    si["ai_analysis"] = ai_analysis_by_id[iid]
                elif "ai_analysis" not in si or not si["ai_analysis"]:
                    si["ai_analysis"] = generate_disabled_issue_analysis()
            snap["top_issues"] = snap_scored

        if str(p_key) == main_period_days:
            snap["ai_summary"] = main_ai
        else:
            snap_days = snap.get("period", {}).get("days", p_key)
            snap_ai = dict(main_ai)
            if snap_ai.get("status") == "available":
                orig_ov = snap_ai.get("overview", "")
                prefix = f"【注意：當前檢視為 {snap_days} 天指標；AI 綜合診斷係依據全期 ({main_period_days} 天) 資料深度分析】\n"
                if not orig_ov.startswith("【注意：當前檢視為"):
                    snap_ai["overview"] = prefix + orig_ov
            snap["ai_summary"] = snap_ai


# ---------------------------------------------------------------------------
# 6. 端到端資料契約整合 (Enrich App Dashboard V2 Data)
# ---------------------------------------------------------------------------

def enrich_app_data_with_priority_and_ai(
    app_data: dict,
    prev_app_data: dict | None = None,
    core_paths: list[str] | None = None,
    source_repo: Path | str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    top_limit: int = 10,
    provider: Optional[AIProvider] = None,
    app_cfg: Optional[dict] = None,
) -> dict:
    """將 AppDashboardV2Data 資料字典進行確定性優先級計算與 AI 分析擴充。

    流程：
      1. 計算 top_issues 的確定性 Priority Score (0-100), Level (P0-P3), Trend。
      2. 根據設定解析 AIProvider (Gemini 或 OpenRouter)。
      3. 若 Provider configured，擷取 stack traces / blame frame 原始碼片段並呼叫 Provider。
      4. 解析回傳填入 ai_summary 與各 issue 的 ai_analysis，同步更新 sources.ai 與 sources.gemini_ai。
      5. 若未配置 Key，執行優雅降級，標為 disabled / unavailable。
    """
    # 0. 刷新確定性 Lifecycle 狀態（確保已整合最新的 Sessions adoption evidence）
    try:
        from crash_trend.lifecycle import enrich_app_data_with_lifecycle
        app_id = app_data.get("metadata", {}).get("app_id")
        enrich_app_data_with_lifecycle(app_data, app_name=app_id)
    except ImportError:
        try:
            from lifecycle import enrich_app_data_with_lifecycle
            app_id = app_data.get("metadata", {}).get("app_id")
            enrich_app_data_with_lifecycle(app_data, app_name=app_id)
        except ImportError:
            pass

    raw_issues = app_data.get("top_issues", [])
    prev_issues = (prev_app_data.get("top_issues") if prev_app_data else None) or []
    repo = source_repo or app_data.get("metadata", {}).get("source_repo")

    # 1. 確定性 Priority Score 計算
    latest_ver = get_latest_app_version(app_data)
    scored_issues = score_issues(
        raw_issues,
        prev_issues=prev_issues,
        core_paths=core_paths,
        latest_app_version=latest_ver,
    )

    # 2. 解析 AIProvider
    if provider is not None:
        active_provider = provider
    else:
        base_provider = get_ai_provider(app_cfg)
        p_name = base_provider.provider_name
        p_model = model or base_provider.model_name
        p_key = api_key or getattr(base_provider, "_explicit_key", None)
        if p_name == "openrouter":
            active_provider = OpenRouterProvider(api_key=p_key, model=p_model)
        else:
            active_provider = GeminiProvider(api_key=p_key, model=p_model)

    is_configured = active_provider.is_configured()
    provider_name = active_provider.provider_name
    model_name = active_provider.model_name

    if not is_configured or not scored_issues:
        # 優雅降級：未啟用 AI
        reason = f"未配置 {provider_name.upper()} API 金鑰" if not is_configured else "本期無 Issue 資料"
        ai_summary = generate_disabled_ai_summary(reason, provider=provider_name)
        disabled_analysis = generate_disabled_issue_analysis()
        for i in scored_issues:
            i["ai_analysis"] = disabled_analysis

        app_data["top_issues"] = scored_issues
        app_data["ai_summary"] = ai_summary
        if "sources" in app_data:
            app_data["sources"]["ai"] = {
                "status": "disabled",
                "provider": provider_name,
                "model": None,
                "last_sync_timestamp": None,
                "error_message": None,
            }
            if "gemini_ai" in app_data["sources"]:
                app_data["sources"]["gemini_ai"]["status"] = "disabled"
                app_data["sources"]["gemini_ai"]["last_sync_timestamp"] = None
                app_data["sources"]["gemini_ai"]["model"] = None
                app_data["sources"]["gemini_ai"]["error_message"] = None
        _sync_periods_priority_and_ai(app_data, prev_app_data=prev_app_data, core_paths=core_paths, latest_ver=latest_ver)
        return app_data

    # 3. 準備原始碼片段與 Prompt
    snippets = []
    for issue in scored_issues[:top_limit]:
        detail = issue.get("detail") or {}
        st_text = detail.get("stack_trace") or ""
        bf = issue.get("blame_frame") or {}
        parts = []
        if st_text:
            parts.append(f"[issue {issue.get('issue_id')}] Stack Trace:\n{st_text}")
        if bf.get("file"):
            snip = source_snippet(repo, f"{bf['file']}:{bf.get('line', '')}")
            if snip:
                parts.append(f"Blame Frame 原始碼：\n{snip}")
        elif issue.get("subtitle"):
            snip = source_snippet(repo, issue.get("subtitle", ""))
            if snip:
                parts.append(f"Subtitle 對應原始碼：\n{snip}")
        if parts:
            snippets.append("\n".join(parts))

    display_name = app_data.get("metadata", {}).get("display_name", "App")
    prompt = build_ai_prompt(
        display_name=display_name,
        kpi=app_data.get("kpi"),
        prev_kpi=prev_app_data.get("kpi") if prev_app_data else None,
        scored_issues=scored_issues[:top_limit],
        distributions=app_data.get("distributions"),
        custom_keys=app_data.get("distributions", {}).get("custom_keys"),
        trend_data=app_data.get("daily_trend"),
        snippets=snippets,
    )

    # 4. 呼叫 Provider 並防禦性解析
    try:
        raw_ai_res = active_provider.analyze(prompt, schema=CANONICAL_AI_RESPONSE_SCHEMA)
        ai_summary, analysis_map = parse_gemini_response(
            raw_ai_res, scored_issues, model_name=model_name, provider_name=provider_name
        )
        for issue in scored_issues:
            issue["ai_analysis"] = analysis_map.get(issue.get("issue_id", ""), generate_disabled_issue_analysis())

        app_data["top_issues"] = scored_issues
        app_data["ai_summary"] = ai_summary
        now_ts = iso_utc_now()
        if "sources" in app_data:
            app_data["sources"]["ai"] = {
                "status": "available",
                "provider": provider_name,
                "model": model_name,
                "last_sync_timestamp": now_ts,
                "error_message": None,
            }
            if "gemini_ai" in app_data["sources"]:
                app_data["sources"]["gemini_ai"]["status"] = "available"
                app_data["sources"]["gemini_ai"]["last_sync_timestamp"] = now_ts
                app_data["sources"]["gemini_ai"]["model"] = model_name
                app_data["sources"]["gemini_ai"]["error_message"] = None

    except Exception as e:
        # API 呼叫失敗時的優雅降級：對所有錯誤字串進行脫敏，避免洩漏金鑰、URL 或 token
        safe_err = sanitize_error_message(str(e))
        ai_summary = generate_error_ai_summary(safe_err, provider=provider_name)
        disabled_analysis = generate_disabled_issue_analysis()
        for i in scored_issues:
            i["ai_analysis"] = disabled_analysis

        app_data["top_issues"] = scored_issues
        app_data["ai_summary"] = ai_summary
        if "sources" in app_data:
            app_data["sources"]["ai"] = {
                "status": "error",
                "provider": provider_name,
                "model": model_name,
                "last_sync_timestamp": None,
                "error_message": safe_err,
            }
            if "gemini_ai" in app_data["sources"]:
                app_data["sources"]["gemini_ai"]["status"] = "error"
                app_data["sources"]["gemini_ai"]["last_sync_timestamp"] = None
                app_data["sources"]["gemini_ai"]["model"] = model_name
                app_data["sources"]["gemini_ai"]["error_message"] = safe_err

    _sync_periods_priority_and_ai(app_data, prev_app_data=prev_app_data, core_paths=core_paths, latest_ver=latest_ver)
    return app_data


# ---------------------------------------------------------------------------
# 7. Markdown 月報輸出支援 (Backward Compatibility & Export)
# ---------------------------------------------------------------------------

def render_md(
    app_name: str,
    display_name: str,
    month: str,
    s: dict,
    ai: dict,
    prio: list[dict],
    fix_review: dict | None = None,
) -> str:
    """產出可讀 Markdown 月報文字。相容 V1/V2 結構。"""
    k, pk = s.get("kpis", s.get("kpi", {})), (s.get("prev_kpis") or (s.get("prev_kpi") or {}))
    events_val = k.get("crash_events", {}).get("value") if isinstance(k.get("crash_events"), dict) else k.get("events", 0)
    users_val = k.get("affected_users", {}).get("value") if isinstance(k.get("affected_users"), dict) else k.get("users", 0)

    lines = [
        f"# {display_name} Crash 月報 {month}",
        "",
        "## 總覽",
        ai.get("overview", ""),
        "",
        "| 指標 | 本期 | 上期 |",
        "|---|---|---|",
        f"| 事件數 | {events_val} | {pk.get('events', '—')} |",
        f"| 受影響用戶 | {users_val} | {pk.get('users', '—')} |",
        "",
        "## Top Issues (Priority Sorted)",
        "| # | 標題 | 等級 | Priority | 事件/用戶 | 趨勢 |",
        "|---|---|---|---|---|---|",
    ]
    trend_zh = {"new": "🆕 新增", "worsening": "📈 惡化", "worse": "📈 惡化", "stable": "穩定", "improving": "📉 改善"}
    for n, i in enumerate(prio, 1):
        p_info = i.get("priority") or {}
        score_val = p_info.get("score", i.get("score", 0))
        level_val = p_info.get("level", "P2")
        trend_val = p_info.get("trend", i.get("trend", "stable"))
        u_val = i.get("affected_users", i.get("users", 0))
        e_val = i.get("events", 0)
        lines.append(
            f"| {n} | {i.get('title', '')} | {level_val} | P={score_val} | {e_val}/{u_val} | {trend_zh.get(trend_val, trend_val)} |"
        )

    lines += ["", "## 分布交叉洞察", ai.get("distribution_insights", ""), "", "## 優先修復清單與建議"]
    for n, i in enumerate(prio, 1):
        ai_ia = i.get("ai_analysis") or {}
        rc = ai_ia.get("root_cause") or i.get("root_cause", "需人工確認")
        fix = ai_ia.get("suggested_fix") or i.get("suggested_fix", "—")
        eff = ai_ia.get("effort") or i.get("effort", "?")
        u_val = i.get("affected_users", i.get("users", 0))
        e_val = i.get("events", 0)
        p_info = i.get("priority") or {}
        score_val = p_info.get("score", i.get("score", 0))

        lines += [
            f"### {n}. {i.get('title', '')} (Score: {score_val})",
            f"- **Root Cause 推測**：{rc}",
            f"- **程式碼位置**：`{i.get('subtitle') or (i.get('blame_frame') or {}).get('file') or '—'}`",
            f"- **建議修法**：{fix}",
            f"- **工作量**：{eff}　**影響**：{u_val} 用戶 / {e_val} 事件",
            "",
        ]

    if ai.get("data_limitations"):
        lines += ["## 資料侷限", ai["data_limitations"], ""]

    lines += [
        "---",
        f"*由 crash-trend 產生於 {dt.date.today().isoformat()}；分析模型：{ai.get('model') or os.environ.get('GEMINI_MODEL', 'gemini-flash-latest')}*",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 8. CLI 主程式
# ---------------------------------------------------------------------------

def main() -> None:
    """向下相容 CLI 入口：委託 analyze_ai.main 執行。"""
    try:
        from crash_trend.analyze_ai import main as ai_main
    except ImportError:
        from analyze_ai import main as ai_main
    ai_main()


if __name__ == "__main__":
    main()
