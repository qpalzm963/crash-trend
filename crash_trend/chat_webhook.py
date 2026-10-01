"""Google Chat 聊天室 Webhook：每日暴增告警、健康檢查、定期彙整用的純文字訊息。

與 CRASH_REPORT_URL（公司 Chat 服務，只收月報卡與週暴增卡兩種固定格式）分開：
這裡直接打聊天室自己的 Webhook，內容是自由文字。

每個 app 一個 Webhook，放在環境變數 `CHAT_WEBHOOK_<APP>`（app 名稱轉大寫、非英數轉 `_`，
例如 app `rider_app` → `CHAT_WEBHOOK_RIDER_APP`）。Webhook 網址等同發文權限，放 .env，不進 git。

CLI（給排程中的彙整使用）：
    python crash_trend/chat_webhook.py --app <app> --text-file summary.txt
    echo "..." | python crash_trend/chat_webhook.py --app <app>
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import requests


def webhook_env_name(app: str) -> str:
    return "CHAT_WEBHOOK_" + re.sub(r"[^A-Z0-9]", "_", app.upper())


def webhook_url(app: str) -> str | None:
    url = (os.environ.get(webhook_env_name(app)) or "").strip()
    return url or None


def send_text(url: str, text: str) -> None:
    """送出一則純文字訊息；非 200 一律拋錯（呼叫端決定是否擋流程）。"""
    r = requests.post(url, json={"text": text}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Chat Webhook 回應 {r.status_code}：{r.text[:200]}")


def main() -> None:
    p = argparse.ArgumentParser(description="送一則純文字訊息到 app 的 Chat 聊天室 Webhook")
    p.add_argument("--app", required=True, help="apps.yaml 中的 app 名稱")
    p.add_argument("--text-file", help="訊息內容檔；省略則讀 stdin")
    args = p.parse_args()

    url = webhook_url(args.app)
    if not url:
        sys.exit(f"[錯誤] 未設定 {webhook_env_name(args.app)}，無法送出")
    text = Path(args.text_file).read_text(encoding="utf-8") if args.text_file else sys.stdin.read()
    if not text.strip():
        sys.exit("[錯誤] 訊息內容是空的")
    try:
        send_text(url, text)
    except Exception as e:  # noqa: BLE001 - 網路錯誤與非 200 一樣要大聲失敗
        sys.exit(f"[錯誤] {e}")
    print(f"  ✓ 已送出到 {args.app} 的聊天室")


if __name__ == "__main__":
    main()
