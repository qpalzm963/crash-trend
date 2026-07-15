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
    if not summary_path.exists():
        sys.exit(f"[錯誤] 找不到 {summary_path}，先跑 normalize.py")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    months = sorted(f.stem for f in data_dir.glob("*.json"))
    prev_kpis = None
    if len(months) > 1 and months[-1] == month:
        prev_kpis = json.loads((data_dir / f"{months[-2]}.json").read_text(encoding="utf-8")).get("kpis")

    dashboard = os.environ.get("DASHBOARD_URL", "")
    payload = {
        "app": args.app,
        "display_name": app.get("display_name", args.app),
        "month": month,
        # 帶 #<app> 錨點：儀表板讀 hash 直接切到該 app 分頁
        "dashboard_url": f"{dashboard}#{args.app}" if dashboard else "",
        "kpis": summary.get("kpis", {}),
        "prev_kpis": prev_kpis,
        "top_issues": summary.get("top_issues", [])[:10],
        "priority_list": summary.get("priority_list", []),
        "fix_review": summary.get("fix_review"),
    }
    r = requests.post(url, json=payload, headers={"x-internal-token": token}, timeout=30)
    if r.status_code == 404:
        sys.exit(f"[注意] 聊天服務找不到綁定 crash_app_key={args.app} 的專案——到後台 Project 設定填「Crash 週報 app 代號」")
    if r.status_code != 200:
        sys.exit(f"[錯誤] 發送失敗 {r.status_code}：{r.text[:300]}")
    print(f"  ✓ 週報卡已發送（space: {r.json().get('space', '?')}）")


if __name__ == "__main__":
    main()
