"""把當月摘要 POST 給聊天整合服務（gitlab_google_chat 的 /api/crash-report），由它發 Google Chat 卡。

環境變數（放 .env）：
  CRASH_REPORT_URL    例 http://host.docker.internal:3000/api/crash-report；未設＝跳過（不算失敗）
  INTERNAL_API_TOKEN  與聊天服務共享的 service-to-service token
  DASHBOARD_URL       卡片按鈕連結（例 http://<主機>:8787）；未設則卡片不放按鈕
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

import requests

from config import ROOT, app_argparser, get_app


def main() -> None:
    args = app_argparser("發送當月摘要到聊天室").parse_args()
    url = os.environ.get("CRASH_REPORT_URL")
    if not url:
        print("  （未設 CRASH_REPORT_URL，跳過發送）")
        return
    token = os.environ.get("INTERNAL_API_TOKEN")
    if not token:
        sys.exit("[錯誤] 設了 CRASH_REPORT_URL 但缺 INTERNAL_API_TOKEN")

    app = get_app(args.app)
    month = dt.date.today().strftime("%Y-%m")
    data_dir = ROOT / "reports" / "data" / args.app
    summary_path = data_dir / f"{month}.json"
    summary = None
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            summary = None

    # 若無月快照，嘗試從 Dashboard V2 app_v2.json / dashboard_v2.json 取得
    v2_app = None
    if not summary:
        for v2_name in ["app_v2.json", "dashboard_v2.json"]:
            v2_path = ROOT / "out" / args.app / v2_name
            if v2_path.exists():
                try:
                    v2_data = json.loads(v2_path.read_text(encoding="utf-8"))
                    v2_app = v2_data.get("apps", {}).get(args.app, v2_data)
                    break
                except Exception:
                    pass

    if not summary and not v2_app:
        sys.exit(f"[錯誤] 找不到 {summary_path} 或 V2 聚合資料，先跑 weekly_sync.sh")

    kpis = (summary or {}).get("kpis")
    if not kpis and v2_app:
        vkpi = v2_app.get("kpi", {})
        kpis = {
            "events": vkpi.get("crash_events", {}).get("value", 0),
            "users": vkpi.get("affected_users", {}).get("value", 0),
            "crash_free_users_rate": vkpi.get("crash_free_users", {}).get("rate"),
            "crash_free_sessions_rate": vkpi.get("crash_free_sessions", {}).get("rate"),
            "events_fatal": vkpi.get("events_by_error_type", {}).get("fatal", 0),
            "events_anr": vkpi.get("events_by_error_type", {}).get("anr", 0),
            "events_nonfatal": vkpi.get("events_by_error_type", {}).get("non_fatal", 0),
        }

    top_issues = (summary or {}).get("top_issues")
    if not top_issues and v2_app:
        top_issues = [
            {
                "issue_id": iss.get("issue_id", ""),
                "title": iss.get("title", ""),
                "subtitle": iss.get("subtitle", ""),
                "events": iss.get("events", 0),
                "users": iss.get("affected_users", 0),
                "fatal": iss.get("error_type") == "FATAL",
            }
            for iss in (v2_app.get("top_issues") or [])[:10]
        ]

    priority_list = (summary or {}).get("priority_list", [])
    if not priority_list and v2_app:
        priority_list = [
            {
                "issue_id": iss.get("issue_id", ""),
                "title": iss.get("title", ""),
                "score": iss.get("priority", {}).get("score", 0),
                "level": iss.get("priority", {}).get("level", "P2"),
            }
            for iss in (v2_app.get("top_issues") or [])[:5]
        ]

    months = sorted(f.stem for f in data_dir.glob("*.json")) if data_dir.is_dir() else []
    prev_kpis = None
    if len(months) > 1 and months[-1] == month:
        try:
            prev_kpis = json.loads((data_dir / f"{months[-2]}.json").read_text(encoding="utf-8")).get("kpis")
        except Exception:
            pass

    dashboard = os.environ.get("DASHBOARD_URL", "")
    payload = {
        "app": args.app,
        "display_name": app.get("display_name", args.app),
        "month": month,
        # 帶 #<app> 錨點：儀表板讀 hash 直接切到該 app 分頁
        "dashboard_url": f"{dashboard}#{args.app}" if dashboard else "",
        "kpis": kpis or {},
        "prev_kpis": prev_kpis,
        "top_issues": (top_issues or [])[:10],
        "priority_list": priority_list,
        "fix_review": (summary or {}).get("fix_review"),
    }
    r = requests.post(url, json=payload, headers={"x-internal-token": token}, timeout=30)
    if r.status_code == 404:
        sys.exit(f"[注意] 聊天服務找不到綁定 crash_app_key={args.app} 的專案——到後台 Project 設定填「Crash 週報 app 代號」")
    if r.status_code != 200:
        sys.exit(f"[錯誤] 發送失敗 {r.status_code}：{r.text[:300]}")
    print(f"  ✓ 週報卡已發送（space: {r.json().get('space', '?')}）")


if __name__ == "__main__":
    main()
