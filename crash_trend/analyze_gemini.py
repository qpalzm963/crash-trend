"""Gemini 月報分析器：unified.json → 優先修復清單 + 月報 md。

分工（判斷交給模型、計算留在程式）：
  Python：優先級評分、與上月比較的趨勢標記、原始碼片段擷取、md 排版、priority_list 回填
  Gemini：每個 top issue 的 root cause 推測 / 建議修法 / 工作量估計，與總覽/分布洞察文字

環境變數：GEMINI_API_KEY 或 GEMINI_KEY_URL（後台代管）擇一、GEMINI_MODEL（預設 gemini-flash-latest）
用法：analyze_gemini.py --app <name>；資料為空時直接產出「無資料」月報，不呼叫 API。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

from config import ROOT, app_argparser, get_app, write_json
from versions import max_version

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# 評分權重：P = U×3 ＋ fatal或ANR×2 ＋ 惡化×2 ＋ 最新版仍現×2 ＋ 核心路徑×3 ＋ E×1
# （ANR＝畫面卡死無回應，體感等同當機，與 fatal 同權）
# U/E 先按當期最大值正規化 0–10，其餘為 0/1；滿分 49，取整數。


def score_issues(issues: list[dict], prev_issues: list[dict], core_paths: list[str]) -> list[dict]:
    if not issues:
        return []
    max_u = max((i["users"] for i in issues), default=0) or 1
    max_e = max((i["events"] for i in issues), default=0) or 1
    latest_ver = max_version(i.get("last_seen_version") or "" for i in issues) or ""
    prev_by_id = {p["issue_id"]: p for p in prev_issues if p.get("issue_id")}
    prev_by_title = {p["title"]: p for p in prev_issues}
    scored = []
    for i in issues:
        prev = prev_by_id.get(i.get("issue_id")) or prev_by_title.get(i["title"])
        worse = prev is None or i["events"] > prev.get("events", 0) * 1.2
        core = any(k.lower() in f"{i['title']} {i.get('subtitle', '')}".lower() for k in core_paths)
        p = (
            (i["users"] / max_u * 10) * 3
            + (i["events"] / max_e * 10) * 1
            + (2 if i["fatal"] or i.get("error_type") == "ANR" else 0)
            + (2 if worse else 0)
            + (2 if latest_ver and i.get("last_seen_version") == latest_ver else 0)
            + (3 if core else 0)
        )
        scored.append({**i, "score": round(p), "trend": "new" if prev is None else ("worse" if worse else "stable")})
    return sorted(scored, key=lambda x: -x["score"])


def source_snippet(source_repo: Path, subtitle: str, max_lines: int = 50) -> str:
    """從 issue 位置（如 xxx.dart:120）抓原始碼片段，供 Gemini 推測 root cause。"""
    m = re.search(r"([\w/.-]+\.dart)(?::| line )?(\d+)?", subtitle or "")
    if not m:
        return ""
    hits = list(source_repo.rglob(Path(m.group(1)).name))
    if not hits:
        return ""
    lines = hits[0].read_text(encoding="utf-8", errors="ignore").splitlines()
    center = int(m.group(2)) - 1 if m.group(2) else 0
    lo, hi = max(0, center - max_lines // 2), min(len(lines), center + max_lines // 2)
    body = "\n".join(f"{n + 1}| {lines[n]}" for n in range(lo, hi))
    return f"// {hits[0].relative_to(source_repo)}\n{body}"


def resolve_api_key() -> str:
    """取 key 順序：GEMINI_API_KEY env → 聊天服務後台（GEMINI_KEY_URL ＋ INTERNAL_API_TOKEN）。"""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    key_url = os.environ.get("GEMINI_KEY_URL")
    if key_url:
        r = requests.get(key_url, headers={"x-internal-token": os.environ.get("INTERNAL_API_TOKEN", "")}, timeout=15)
        if r.status_code == 200:
            return r.json()["api_key"]
        sys.exit(f"[錯誤] 向後台取 Gemini key 失敗 {r.status_code}：{r.text[:200]}（後台是否已設定？）")
    sys.exit("[錯誤] 未設定 GEMINI_API_KEY，也未設定 GEMINI_KEY_URL（後台取用）")


# 約束解碼（structured output）：欄位名、必填、effort enum 在生成時強制，
# JSON mode 只保證合法 JSON、不保證 schema。
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "overview": {"type": "STRING"},
        "distribution_insights": {"type": "STRING"},
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "issue_id": {"type": "STRING"},
                    "root_cause": {"type": "STRING"},
                    "suggested_fix": {"type": "STRING"},
                    "effort": {"type": "STRING", "enum": ["S", "M", "L"]},
                },
                "required": ["issue_id", "root_cause", "suggested_fix", "effort"],
            },
        },
        "data_limitations": {"type": "STRING"},
    },
    "required": ["overview", "distribution_insights", "items", "data_limitations"],
}


def call_gemini(payload_text: str, schema: dict | None = None) -> dict:
    key = resolve_api_key()
    model = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
    body = {
        "contents": [{"parts": [{"text": payload_text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema or RESPONSE_SCHEMA,
            "temperature": 0.2,
        },
    }
    for attempt in (1, 2, 3):
        try:
            # 富 prompt（含 stack trace/週趨勢/分布）生成較久 → 300s；逾時/連線錯誤也退避重試
            r = requests.post(API.format(model=model), params={"key": key}, json=body, timeout=300)
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt < 3:
                time.sleep(5 * attempt)
                continue
            sys.exit(f"[錯誤] Gemini API 連線逾時（已重試）：{str(e)[:200]}")
        if r.status_code in (429, 500, 503) and attempt < 3:  # 暫時性限流/過載 → 退避重試
            time.sleep(5 * attempt)
            continue
        if r.status_code != 200:
            sys.exit(f"[錯誤] Gemini API {r.status_code}：{r.text[:400]}")
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if attempt == 3:
                sys.exit(f"[錯誤] Gemini 回傳非合法 JSON（已重試）：{text[:300]}")
    raise AssertionError


PROMPT = """你是資深 Flutter 團隊的 crash 分析師。以下是「{display_name}」本期 crash 資料、真實 stack trace 與相關原始碼片段。
針對每個 top issue 推測 root cause、給建議修法與工作量（S/M/L），並寫總覽與分布洞察。

規則：
- 只根據提供的資料與程式碼推論；資訊不足時 root_cause 寫「需人工確認」，不得編造。
- suggested_fix 要具體可執行（指出改哪裡、怎麼改），不要泛泛而談。
- 全部用繁體中文。

回傳欄位語意（格式由 response schema 強制）：
- overview：2-3 句本期總覽（對比上期）
- distribution_insights：機型/OS/版本/自訂 keys 的重點發現，1-3 句；無資料寫「本期無分布資料」
- items：每個 top issue 一項，issue_id 對應輸入資料
- data_limitations：資料缺口與侷限，1-2 句

## 本期 KPI
{kpis}
## 上期 KPI（無則為 null）
{prev_kpis}
## Top issues（含程式計算的優先分 score 與趨勢標記）
{issues}
## 分布摘要
{dists}
## 自訂 keys 分布（Crashlytics custom keys）
{custom_keys}
## 週趨勢（事件數；BQ 精確或 MCP 近似）
{weekly}
## Stack trace／原始碼片段
{snippets}
"""


FIX_STATUS_ZH = {"resolved": "✅ 本期未再出現", "old_versions_only": "🟡 僅舊版仍出現", "still_occurring": "🔴 最新版仍出現"}


def render_fix_review(fr: dict | None) -> list[str]:
    """「上期清單回顧」md 區塊；無上期資料回空列表（整節省略）。"""
    if not fr or not fr.get("items"):
        return []
    lines = [
        f"## 上期清單回顧（{fr.get('prev_month', '?')} → 本期驗證）",
        "| # | 標題 | 上期 事件/用戶 | 本期 事件/用戶 | 版本 | 狀態 |",
        "|---|---|---|---|---|---|",
    ]
    for n, it in enumerate(fr["items"], 1):
        prev, cur = it.get("prev", {}), it.get("cur", {})
        if it["status"] == "resolved":
            ver = "—"
        elif not it.get("version_known"):
            ver = "版本不明"
        else:
            ver = f"最新見 {it.get('cur_last_seen_version') or '?'}（全域最新 {it.get('latest_app_version') or '?'}）"
        lines.append(
            f"| {n} | {it.get('title', '')} | {prev.get('events', 0)}/{prev.get('users', 0)} "
            f"| {cur.get('events', 0)}/{cur.get('users', 0)} | {ver} | {FIX_STATUS_ZH.get(it['status'], it['status'])} |"
        )
    src = "上期優先修復清單" if fr.get("source") == "priority_list" else "上期 Top Issues（上期無 AI 清單）"
    note = f"（來源：{src}"
    if dt.date.today().day < 15:
        note += "；本月資料未滿月，「未再出現」僅供參考"
    lines += [note + "）", ""]
    return lines


def render_md(app_name: str, display_name: str, month: str, s: dict, ai: dict, prio: list[dict], fix_review: dict | None = None) -> str:
    k, pk = s.get("kpis", {}), (s.get("prev_kpis") or {})
    fatal_pct = "—" if k.get("fatal_share") is None else f"{round(k['fatal_share'] * 100)}%"
    lines = [
        f"# {display_name} Crash 月報 {month}",
        "",
        "## 總覽",
        ai.get("overview", ""),
        "",
        f"| 指標 | 本期 | 上期 |",
        f"|---|---|---|",
        f"| 事件數 | {k.get('events', 0)} | {pk.get('events', '—')} |",
        f"| 受影響用戶 | {k.get('users', 0)} | {pk.get('users', '—')} |",
        f"| Fatal 佔比 | {fatal_pct} | — |",
        f"| Issue 數 | {k.get('issue_count', 0)} | {pk.get('issue_count', '—')} |",
        "",
    ] + render_fix_review(fix_review) + [
        "## Top Patterns",
        "| # | 標題 | 層級 | 事件/用戶 | 首見→最新見 | 趨勢 |",
        "|---|---|---|---|---|---|",
    ]
    trend_zh = {"new": "🆕 新增", "worse": "📈 惡化", "stable": "穩定"}
    level_zh = {"FATAL": "閃退", "ANR": "凍結(ANR)", "NON_FATAL": "非致命"}
    for n, i in enumerate(prio, 1):
        level = level_zh.get(i.get("error_type"), "閃退" if i["fatal"] else "非致命")
        lines.append(
            f"| {n} | {i['title']} | {level} | {i['events']}/{i['users']} "
            f"| {i.get('first_seen_version', '')}→{i.get('last_seen_version', '')} | {trend_zh.get(i.get('trend'), '')} |"
        )
    lines += ["", "## 分布交叉", ai.get("distribution_insights", ""), "", "## 優先修復清單"]
    for n, i in enumerate(prio, 1):
        lines += [
            f"### {n}. {i['title']}（P={i['score']}）",
            f"- **Root cause 推測**：{i.get('root_cause', '需人工確認')}",
            f"- **程式碼位置**：`{i.get('code_location') or i.get('subtitle') or '—'}`",
            f"- **建議修法**：{i.get('suggested_fix', '—')}",
            f"- **工作量**：{i.get('effort', '?')}　**影響**：{i['users']} 用戶 / {i['events']} 事件",
            "",
        ]
    lines += ["## 資料侷限", ai.get("data_limitations", ""), "",
              f"---", f"*由 crash-trend 產生於 {dt.date.today().isoformat()}；分析模型：{os.environ.get('GEMINI_MODEL', 'gemini-flash-latest')}*"]
    return "\n".join(lines)


def main() -> None:
    p = app_argparser("Gemini 月報分析")
    # 預設 5：10 個 issue 的結構化生成（含 stack trace）常超過 API timeout；卡片只取 3、月報 5 筆已足
    p.add_argument("--top", type=int, default=5, help="送分析的 top issues 數（預設 5）")
    args = p.parse_args()
    app = get_app(args.app)
    month = dt.date.today().strftime("%Y-%m")

    unified_path = ROOT / "out" / args.app / "unified.json"
    if not unified_path.exists():
        sys.exit(f"[錯誤] 找不到 {unified_path}，先跑 normalize.py")
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
            f"（來源狀態：{u.get('sources')}）。屬正常情況時無需處理；若預期應有資料，檢查 BigQuery 連結與憑證。\n",
            encoding="utf-8",
        )
        print(f"  ✓ 本期無資料，已產出空月報 {report_path.relative_to(ROOT)}（未呼叫 Gemini）")
        return

    scored = score_issues(issues, prev.get("top_issues", []), app.get("core_paths", []))[: args.top]

    repo = Path(app.get("source_repo", "")).expanduser()
    # 真實 stack trace（fetch_stacktraces.py 抓的）優先；沒有才 fallback 用 subtitle 反猜原始碼位置
    st_path = ROOT / "out" / args.app / "stacktraces.json"
    stacks = json.loads(st_path.read_text(encoding="utf-8")).get("issues", {}) if st_path.exists() else {}
    snippets = []
    for i in scored[:5]:
        st = stacks.get(i.get("issue_id") or "")
        if st and st.get("stack_trace"):
            parts = [f"[issue {i['issue_id']}]（Crashlytics 真實 stack trace）", st["stack_trace"]]
            bf = st.get("blame_frame") or {}
            if repo.is_dir() and bf.get("file"):
                snip = source_snippet(repo, f"{bf['file']}:{bf.get('line', '')}")
                if snip:
                    parts.append(f"元兇 frame 對應原始碼：\n{snip}")
            snippets.append("\n".join(parts))
        elif repo.is_dir():
            snip = source_snippet(repo, i.get("subtitle", ""))
            if snip:
                snippets.append(f"[issue {i['issue_id']}]\n{snip}")

    ai = call_gemini(PROMPT.format(
        display_name=u.get("display_name", args.app),
        kpis=json.dumps(summary.get("kpis", {}), ensure_ascii=False),
        prev_kpis=json.dumps(prev.get("kpis"), ensure_ascii=False),
        issues=json.dumps([{k: v for k, v in i.items() if k != "version_dist"} for i in scored], ensure_ascii=False),
        dists=json.dumps(u.get("distributions", {}), ensure_ascii=False)[:4000],
        custom_keys=json.dumps(u.get("custom_keys", []), ensure_ascii=False)[:2000],
        weekly=json.dumps(u.get("weekly_trend", []), ensure_ascii=False)[:2000] or "（無週趨勢資料）",
        snippets="\n\n".join(snippets)[:12000] or "（無可用原始碼片段）",
    ))

    ai_by_id = {x.get("issue_id"): x for x in ai.get("items", [])}
    prio = []
    for i in scored:
        note = ai_by_id.get(i["issue_id"], {})
        # MCP 抓的真實 stack trace 與元兇 frame（file:line）——供儀表板「複製給 agent」用
        st = stacks.get(i.get("issue_id") or "") or {}
        bf = st.get("blame_frame") or {}
        prio.append({
            "issue_id": i.get("issue_id", ""), "platform": i.get("platform", ""),
            "title": i["title"], "fatal": i["fatal"],
            "error_type": i.get("error_type", "FATAL" if i["fatal"] else "NON_FATAL"), "score": i["score"],
            "users": i["users"], "events": i["events"], "trend": i["trend"],
            "first_seen_version": i.get("first_seen_version", ""),
            "last_seen_version": i.get("last_seen_version", ""),
            "root_cause": note.get("root_cause", "需人工確認"),
            "code_location": i.get("subtitle", ""),
            "suggested_fix": note.get("suggested_fix", "—"),
            "effort": note.get("effort", "?"),
            "stack_trace": st.get("stack_trace", ""),
            "blame_file": bf.get("file", ""),
            "blame_line": str(bf.get("line", "")),
        })

    report_path.write_text(
        render_md(args.app, u.get("display_name", args.app), month, {"kpis": summary.get("kpis", {}), "prev_kpis": prev.get("kpis")}, ai, prio, summary.get("fix_review")),
        encoding="utf-8",
    )
    print(f"  ✓ 月報 {report_path.relative_to(ROOT)}")

    if summary:
        summary["priority_list"] = prio
        write_json(summary_path, summary)


if __name__ == "__main__":
    main()
