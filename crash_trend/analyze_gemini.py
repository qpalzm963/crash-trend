"""Gemini 月報分析器：unified.json → 優先修復清單 + 月報 md。

分工（判斷交給模型、計算留在程式）：
  Python：優先級評分、與上月比較的趨勢標記、原始碼片段擷取、md 排版、priority_list 回填
  Gemini：每個 top issue 的 root cause 推測 / 建議修法 / 工作量估計，與總覽/分布洞察文字

環境變數：GEMINI_API_KEY（必要）、GEMINI_MODEL（預設 gemini-2.5-flash）
用法：analyze_gemini.py --app <name>；資料為空時直接產出「無資料」月報，不呼叫 API。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import requests

from config import ROOT, app_argparser, get_app, write_json

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# 評分權重：P = U×3 ＋ fatal×2 ＋ 惡化×2 ＋ 最新版仍現×2 ＋ 核心路徑×3 ＋ E×1
# U/E 先按當期最大值正規化 0–10，其餘為 0/1；滿分 49，取整數。


def score_issues(issues: list[dict], prev_issues: list[dict], core_paths: list[str]) -> list[dict]:
    if not issues:
        return []
    max_u = max((i["users"] for i in issues), default=0) or 1
    max_e = max((i["events"] for i in issues), default=0) or 1
    latest_ver = max((i.get("last_seen_version") or "" for i in issues), default="")
    prev_by_title = {p["title"]: p for p in prev_issues}
    scored = []
    for i in issues:
        prev = prev_by_title.get(i["title"])
        worse = prev is None or i["events"] > prev.get("events", 0) * 1.2
        core = any(k.lower() in f"{i['title']} {i.get('subtitle', '')}".lower() for k in core_paths)
        p = (
            (i["users"] / max_u * 10) * 3
            + (i["events"] / max_e * 10) * 1
            + (2 if i["fatal"] else 0)
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


def call_gemini(payload_text: str) -> dict:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("[錯誤] 未設定 GEMINI_API_KEY 環境變數")
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    body = {
        "contents": [{"parts": [{"text": payload_text}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }
    for attempt in (1, 2):
        r = requests.post(API.format(model=model), params={"key": key}, json=body, timeout=120)
        if r.status_code != 200:
            sys.exit(f"[錯誤] Gemini API {r.status_code}：{r.text[:400]}")
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            if attempt == 2:
                sys.exit(f"[錯誤] Gemini 回傳非合法 JSON（已重試一次）：{text[:300]}")
    raise AssertionError


PROMPT = """你是資深 Flutter 團隊的 crash 分析師。以下是「{display_name}」本期 crash 資料與相關原始碼片段。
針對每個 top issue 推測 root cause、給建議修法與工作量（S/M/L），並寫總覽與分布洞察。

規則：
- 只根據提供的資料與程式碼推論；資訊不足時 root_cause 寫「需人工確認」，不得編造。
- suggested_fix 要具體可執行（指出改哪裡、怎麼改），不要泛泛而談。
- 全部用繁體中文。

回傳 JSON（不要其他文字）：
{{"overview": "2-3 句本期總覽（對比上期）",
  "distribution_insights": "機型/OS/版本/自訂 keys 的重點發現，1-3 句；無資料寫「本期無分布資料」",
  "items": [{{"issue_id": "...", "root_cause": "...", "suggested_fix": "...", "effort": "S|M|L"}}],
  "data_limitations": "資料缺口與侷限，1-2 句"}}

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
## 原始碼片段
{snippets}
"""


def render_md(app_name: str, display_name: str, month: str, s: dict, ai: dict, prio: list[dict]) -> str:
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
        "## Top Patterns",
        "| # | 標題 | 層級 | 事件/用戶 | 首見→最新見 | 趨勢 |",
        "|---|---|---|---|---|---|",
    ]
    trend_zh = {"new": "🆕 新增", "worse": "📈 惡化", "stable": "穩定"}
    for n, i in enumerate(prio, 1):
        lines.append(
            f"| {n} | {i['title']} | {'fatal' if i['fatal'] else 'non-fatal'} | {i['events']}/{i['users']} "
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
              f"---", f"*由 crash-trend 產生於 {dt.date.today().isoformat()}；分析模型：{os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')}*"]
    return "\n".join(lines)


def main() -> None:
    p = app_argparser("Gemini 月報分析")
    p.add_argument("--top", type=int, default=10, help="送分析的 top issues 數（預設 10）")
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
    snippets = []
    if repo.is_dir():
        for i in scored[:5]:
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
        snippets="\n\n".join(snippets)[:12000] or "（無可用原始碼片段）",
    ))

    ai_by_id = {x.get("issue_id"): x for x in ai.get("items", [])}
    prio = []
    for i in scored:
        note = ai_by_id.get(i["issue_id"], {})
        prio.append({
            "title": i["title"], "fatal": i["fatal"], "score": i["score"],
            "users": i["users"], "events": i["events"], "trend": i["trend"],
            "root_cause": note.get("root_cause", "需人工確認"),
            "code_location": i.get("subtitle", ""),
            "suggested_fix": note.get("suggested_fix", "—"),
            "effort": note.get("effort", "?"),
        })

    report_path.write_text(
        render_md(args.app, u.get("display_name", args.app), month, {"kpis": summary.get("kpis", {}), "prev_kpis": prev.get("kpis")}, ai, prio),
        encoding="utf-8",
    )
    print(f"  ✓ 月報 {report_path.relative_to(ROOT)}")

    if summary:
        summary["priority_list"] = prio
        write_json(summary_path, summary)


if __name__ == "__main__":
    main()
