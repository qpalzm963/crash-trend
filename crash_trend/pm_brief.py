"""產生給 PM 的白話簡報：解釋 crash 的由來與影響，請 PM 協助開 issue。

白話說明（pm_note）採「用到才呼叫」：執行時只為選中的、還沒有 pm_note 的 issue
呼叫一次 Gemini（省 token——月度分析不預先全量生成），生成後回寫月快照快取，
同一 issue 之後重跑零成本。

用法：pm_brief.py --app <name> [--top 1] [--issue <序號>] [--month YYYY-MM]
輸出到 stdout 直接複製貼給 PM。
"""

from __future__ import annotations

import datetime as dt
import json

from analyze_gemini import call_gemini
from config import ROOT, app_argparser, get_app, write_json

RULE = "─" * 36

PM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "items": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"issue_id": {"type": "STRING"}, "pm_note": {"type": "STRING"}},
                "required": ["issue_id", "pm_note"],
            },
        },
    },
    "required": ["items"],
}

PM_PROMPT = """你是幫工程團隊向 PM 溝通的翻譯。以下是「{display_name}」的 crash 清單（含技術研判），
為每一項寫 pm_note——給非工程師看的白話說明，2-3 句，全部繁體中文：
使用者在做什麼的時候發生什麼事、為什麼會發生（用比喻或生活化說法，不用術語與類別名）、
對使用者的實際影響。

{items}
"""


def short_title(title: str) -> str:
    """issue 標題常是「package:app/.../page.dart - _State._method」，PM 只需要最後一段。"""
    tail = title.rsplit(" - ", 1)[-1].strip()
    return tail or title


def ensure_pm_notes(summary: dict, summary_path, display_name: str, selected: list[dict]) -> None:
    """為選中且缺 pm_note 的項目呼叫一次 Gemini，並回寫月快照當快取。"""
    missing = [i for i in selected if not (i.get("pm_note") or "").strip()]
    if not missing:
        return
    payload = [
        {k: i.get(k, "") for k in ("issue_id", "title", "code_location", "root_cause", "suggested_fix", "fatal", "error_type")}
        for i in missing
    ]
    ai = call_gemini(
        PM_PROMPT.format(display_name=display_name, items=json.dumps(payload, ensure_ascii=False)),
        schema=PM_SCHEMA,
    )
    notes = {x.get("issue_id"): x.get("pm_note", "") for x in ai.get("items", [])}
    for i in summary.get("priority_list") or []:
        if i.get("issue_id") in notes and not (i.get("pm_note") or "").strip():
            i["pm_note"] = notes[i["issue_id"]]
    write_json(summary_path, summary)


def render_brief(display_name: str, month: str, item: dict, rank: int) -> str:
    users, events = item.get("users", 0), item.get("events", 0)
    severity = {
        "FATAL": "會直接閃退（嚴重）",
        "ANR": "畫面卡死無回應（ANR，體感等同當機）",
    }.get(item.get("error_type"), "會直接閃退（嚴重）" if item.get("fatal") else "功能異常但不至於閃退")
    ver = item.get("last_seen_version") or "?"
    first_ver = item.get("first_seen_version") or ver
    ver_note = f"版本 {ver}" if first_ver == ver else f"版本 {first_ver}～{ver}"
    pm_note = (item.get("pm_note") or "").strip() or f"（尚無白話說明：{item.get('root_cause', '需人工確認')}）"

    return "\n".join([
        RULE,
        f"#{rank}【{display_name}】{short_title(item.get('title', ''))}",
        RULE,
        "",
        "🔍 發生了什麼",
        pm_note,
        "",
        "📊 影響",
        f"這個月發生 {events} 次、影響 {users} 位使用者，屬於{severity}，出現在 {ver_note}。",
        "",
        "想請你幫忙開一張 issue 追蹤這個問題（技術細節工程師這邊都有，",
        f"單裡附上這行方便對照即可：crash-trend {month}・issue {item.get('issue_id', '')[:12]}），",
        "開好把連結貼回來，我們就會排修，謝謝～",
        "",
    ])


def main() -> None:
    p = app_argparser("產生給 PM 的白話 crash 簡報")
    p.add_argument("--top", type=int, default=1, help="輸出優先清單前 N 個（預設 1）")
    p.add_argument("--issue", type=int, help="只輸出優先清單第 N 名（1-based，優先於 --top）")
    p.add_argument("--month", default=dt.date.today().strftime("%Y-%m"), help="月份 YYYY-MM（預設本月）")
    args = p.parse_args()
    app = get_app(args.app)

    summary_path = ROOT / "reports" / "data" / args.app / f"{args.month}.json"
    if not summary_path.exists():
        raise SystemExit(f"[錯誤] 找不到 {summary_path}，先跑 normalize.py")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    prio = summary.get("priority_list") or []
    if not prio:
        raise SystemExit(f"[錯誤] {args.month} 尚無 priority_list——先跑 analyze_gemini.py（或 /crash-report）")

    if args.issue:
        if not 1 <= args.issue <= len(prio):
            raise SystemExit(f"[錯誤] --issue {args.issue} 超出範圍（優先清單共 {len(prio)} 項）")
        selected = [(args.issue, prio[args.issue - 1])]
    else:
        selected = list(enumerate(prio[: args.top], 1))

    display_name = app.get("display_name", args.app)
    ensure_pm_notes(summary, summary_path, display_name, [i for _, i in selected])
    for rank, item in selected:
        print(render_brief(display_name, args.month, item, rank))


if __name__ == "__main__":
    main()
