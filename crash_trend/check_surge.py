"""Crash 暴增偵測：上一個完整週事件數 ≥ N 倍於前一週 → 發即時告警卡到聊天室。

與月報卡（post_report.py）分開：月報是月頻摘要，這支跟著週同步跑、逮突發異常
（如某版上線後單週事件暴增十幾倍——等月卡才知道就晚了）。

判斷邏輯（純程式，無 AI）：
  - 資料源 unified.json 的 weekly_trend（BQ 精確或 MCP 近似，normalize 已統一）
  - 只比「完整週」：排除本週（進行中，數字必然偏低）；取最近兩個完整週
  - 觸發門檻：ratio ≥ SURGE_RATIO（預設 2）且事件數 ≥ SURGE_MIN_EVENTS（預設 500，
    小量 app 的 2→5 件雜訊不觸發）
  - 同一週只告警一次（out/<app>/.surge_alerted 記上次告警的週 key）

環境變數：CRASH_REPORT_URL / INTERNAL_API_TOKEN（同 post_report）、DASHBOARD_URL、
SURGE_RATIO、SURGE_MIN_EVENTS。未設 CRASH_REPORT_URL＝跳過。失敗不擋 pipeline。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

import requests

from config import app_argparser, get_app, out_dir


def weekly_totals(unified: dict) -> dict[str, int]:
    """weekly_trend（各平台分列）→ {週key: 總事件數}。"""
    totals: dict[str, int] = {}
    for r in unified.get("weekly_trend") or []:
        wk = r.get("week")
        if wk:
            totals[wk] = totals.get(wk, 0) + int(r.get("events") or 0)
    return totals


def main() -> None:
    args = app_argparser("Crash 暴增偵測（週跑）").parse_args()
    url = os.environ.get("CRASH_REPORT_URL")
    if not url:
        print("  （未設 CRASH_REPORT_URL，跳過暴增偵測）")
        return

    ratio_min = float(os.environ.get("SURGE_RATIO", "2"))
    events_min = int(os.environ.get("SURGE_MIN_EVENTS", "500"))

    unified_path = out_dir(args.app) / "unified.json"
    if not unified_path.exists():
        print(f"  （無 {unified_path.name}，跳過）")
        return
    totals = weekly_totals(json.loads(unified_path.read_text(encoding="utf-8")))

    # 排除本週（進行中）；週 key 與 BQ/fetch_stacktraces 同用「週一起點 %Y-%W」
    now = dt.datetime.now(dt.timezone.utc)
    this_week = (now - dt.timedelta(days=now.weekday())).strftime("%Y-%W")
    weeks = sorted(k for k in totals if k < this_week)
    if len(weeks) < 2:
        print("  （完整週不足 2 週，無從比較）")
        return
    last, prev = weeks[-1], weeks[-2]
    last_n, prev_n = totals[last], totals[prev]

    if not (prev_n > 0 and last_n >= events_min and last_n >= prev_n * ratio_min):
        print(f"  無暴增（{prev}: {prev_n} → {last}: {last_n}；門檻 ×{ratio_min} 且 ≥{events_min}）")
        return

    mark = out_dir(args.app) / ".surge_alerted"
    if mark.exists() and mark.read_text().strip() == last:
        print(f"  （週 {last} 已告警過，跳過）")
        return

    ratio = round(last_n / prev_n, 1)
    dashboard = os.environ.get("DASHBOARD_URL", "")
    payload = {
        "type": "surge_alert",
        "app": args.app,
        "display_name": get_app(args.app).get("display_name", args.app),
        "week": last,
        "events": last_n,
        "prev_events": prev_n,
        "ratio": ratio,
        "dashboard_url": f"{dashboard}#{args.app}" if dashboard else "",
    }
    r = requests.post(url, json=payload,
                      headers={"x-internal-token": os.environ.get("INTERNAL_API_TOKEN", "")}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"[錯誤] 告警發送失敗 {r.status_code}：{r.text[:200]}")
    mark.write_text(last)
    print(f"  🚨 已發暴增告警：週 {last} 事件 {last_n}（前一週 {prev_n} 的 {ratio} 倍）")


if __name__ == "__main__":
    main()
