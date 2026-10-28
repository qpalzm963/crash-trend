"""每日健康檢查：資料有沒有更新、備份有沒有成功、Firebase 登入是否還有效。

有問題才發訊息到各 app 的聊天室（chat_webhook.py）；一切正常不打擾。
檢查結果另存 out/.health_status.json，供定期彙整複查。

判斷全部是規則（無 AI）：
  - 管線紀錄 out/pipeline_run.json 超過 MAX_AGE_HOURS 沒更新 → 管線中途中止、沒寫出紀錄（全部 app）
    （本檢查跟著同步排程一起跑，排程整個停掉時它也不會跑——那種情況要靠定期彙整複查紀錄的時間）
  - 某 app 有階段 failed → 只通知該 app（disabled / skipped 不算）
  - 有設 BACKUP_RCLONE_REMOTE 時：out/.backup_status.json 不存在、失敗或過舊 → 全部 app
  - 有 app 排程使用 Firebase（mcp: weekly）時：`firebase projects:list` 失敗 → 全部 app
    （不用 login:list：它只看本地檔案，token 失效也會顯示已登入）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

from chat_webhook import send_text, webhook_env_name, webhook_url
from config import ROOT, get_mcp_config, load_config

MAX_AGE_HOURS = 26  # 每天跑一次，留 2 小時餘裕
STAGE_ZH = {
    "crashlytics_bigquery": "BigQuery 資料",
    "sessions": "Sessions",
    "mcp": "Firebase 錯誤堆疊",
    "issue_details": "問題細節",
    "normalize": "資料整理",
    "ai": "AI 分析",
    "surge": "暴增檢查",
    "release_gate": "品質閘門",
    "alert_delivery": "告警發送",
}


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        ts = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.UTC)


def collect_problems(
    apps: list[str],
    run_summary: dict | None,
    backup_status: dict | None,
    backup_expected: bool,
    firebase_ok: bool | None,
    now: dt.datetime,
) -> dict[str, list[str]]:
    """回傳 {app: [問題說明]}；沒有問題的 app 不出現在結果裡。"""
    shared: list[str] = []
    per_app: dict[str, list[str]] = {a: [] for a in apps}
    max_age = dt.timedelta(hours=MAX_AGE_HOURS)

    finished = _parse_ts((run_summary or {}).get("finished_at"))
    if finished is None or now - finished > max_age:
        when = finished.astimezone().strftime("%m/%d %H:%M") if finished else "從未"
        shared.append(f"資料超過 {MAX_AGE_HOURS} 小時沒更新（上次完成：{when}），今天的同步可能中途中止")
    else:
        for app in apps:
            stages = (((run_summary or {}).get("apps") or {}).get(app) or {}).get("stages") or {}
            failed = [STAGE_ZH.get(name, name) for name, st in stages.items() if (st or {}).get("status") == "failed"]
            if failed:
                per_app[app].append(f"今天有步驟失敗：{'、'.join(failed)}")

    if backup_expected:
        at = _parse_ts((backup_status or {}).get("at"))
        if not backup_status:
            shared.append("找不到備份紀錄，備份可能從未成功")
        elif not backup_status.get("ok"):
            shared.append(f"備份失敗：{str(backup_status.get('error') or '原因不明')[:150]}")
        elif at is None or now - at > max_age:
            shared.append(f"備份超過 {MAX_AGE_HOURS} 小時沒有成功")

    if firebase_ok is False:
        shared.append("Firebase 登入失效，錯誤堆疊抓不到——需在容器內重新 `firebase login --no-localhost`")

    result = {}
    for app in apps:
        items = shared + per_app[app]
        if items:
            result[app] = items
    return result


def check_firebase() -> bool:
    try:
        r = subprocess.run(["firebase", "projects:list"], capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main() -> None:
    argparse.ArgumentParser(description="每日健康檢查：資料、備份、Firebase 登入；有問題才通知聊天室").parse_args()
    cfg = load_config()
    apps_cfg = cfg.get("apps") or {}
    apps = list(apps_cfg)
    uses_firebase = any(get_mcp_config(a).get("mode") == "weekly" for a in apps_cfg.values())
    now = dt.datetime.now(dt.UTC)

    problems = collect_problems(
        apps=apps,
        run_summary=_load_json(ROOT / "out" / "pipeline_run.json"),
        backup_status=_load_json(ROOT / "out" / ".backup_status.json"),
        backup_expected=bool(os.environ.get("BACKUP_RCLONE_REMOTE")),
        firebase_ok=check_firebase() if uses_firebase else None,
        now=now,
    )
    (ROOT / "out").mkdir(exist_ok=True)
    (ROOT / "out" / ".health_status.json").write_text(
        json.dumps({"at": now.isoformat(), "problems": problems}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if not problems:
        print("  ✓ 健康檢查：資料、備份、登入都正常")
        return

    send_errors = []
    for app, items in problems.items():
        print(f"  ⚠ {app}：" + "；".join(items))
        url = webhook_url(app)
        if not url:
            print(f"    （未設 {webhook_env_name(app)}，無法通知）")
            continue
        name = (apps_cfg.get(app) or {}).get("display_name", app)
        text = "\n".join([f"⚠️ *crash-trend 健康檢查：{name}*"] + [f"• {i}" for i in items])
        try:
            send_text(url, text)
        except Exception as e:  # noqa: BLE001 - 一個聊天室失敗不影響其他
            send_errors.append(f"{app}: {e}")
    # 有問題就以非 0 結束，週同步的總結會標出 health
    sys.exit("[錯誤] 健康檢查發現問題" + (f"；通知失敗：{'；'.join(send_errors)}" if send_errors else ""))


if __name__ == "__main__":
    main()
