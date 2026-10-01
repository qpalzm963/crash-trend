#!/bin/bash
# 每週 crash 資料同步（launchd 或容器內 supercronic 呼叫；手動跑也行）
# 自動部分：各 app 的 BigQuery 拉取 → normalize →（有 Gemini key 時）Gemini 月報 → 儀表板 → commit
# 無法自動的：console 快照（需使用者登入態）→ macOS 上發通知提醒（容器內自動略過）
set -u
# 變數後面緊接全形字時一律寫成 ${var}：macOS 內建 bash 3.2 會把全形字的第一個位元組當成變數名的一部分，
# 在 set -u 下直接中止（Linux 的 bash 5 不會，所以只在 macOS 主機以 launchd 直接執行時爆）。
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

CT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$CT/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
LOG="$CT/logs/weekly_sync.log"
mkdir -p "$CT/logs"
exec >>"$LOG" 2>&1
# 機敏設定（GEMINI_API_KEY 等）放 $CT/.env，gitignored；launchd/cron 不繼承 shell 環境
# set -a：source 進來的變數自動 export，子行程（python）才看得到
if [ -f "$CT/.env" ]; then set -a; . "$CT/.env"; set +a; fi

echo "===== weekly_sync $(date '+%F %T')（$($PY --version 2>&1)）====="
FAILED=""

apps=$(CT="$CT" $PY - <<'EOF'
import os, yaml, pathlib
cfg = yaml.safe_load((pathlib.Path(os.environ["CT"]) / "apps.yaml").read_text())
print(" ".join((cfg.get("apps") or {}).keys()))
EOF
)

echo "--- pipeline_run (Dashboard V2.2 Pipeline Health & Stages)"
$PY "$CT/crash_trend/pipeline_run.py" || FAILED="$FAILED pipeline_run"


# 發卡到聊天室（chat 整合，見 DEPLOY.md）：資料每週同步，但卡片「每個 app 每月一次」——
# 以「每個 app 各自」的當月標記檔 gate（out/ 已 gitignore）。某個 app 失敗或延後只影響它自己：
# 不記標記、下週自動補發；已發過的其他 app 不會被連帶重發。
#   post_report 結束碼：0＝已發送 → 記標記；3＝資料不完整或無法確認 → 延後重試；其他＝發送失敗
# 舊版是單一的 out/.card_sent_month（全部成功才記）。升級當月若它已記本月，視為所有 app 都發過，
# 避免重複發卡；之後只看各 app 自己的標記。
LEGACY_CARD_MARK="$CT/out/.card_sent_month"
THIS_MONTH="$(date '+%Y-%m')"
if [ -n "${CRASH_REPORT_URL:-}" ]; then
  LEGACY_SENT=""
  [ "$(cat "$LEGACY_CARD_MARK" 2>/dev/null)" = "$THIS_MONTH" ] && LEGACY_SENT=1
  for app in $apps; do
    CARD_MARK="$CT/out/$app/.card_sent_month"
    if [ -n "$LEGACY_SENT" ] || [ "$(cat "$CARD_MARK" 2>/dev/null)" = "$THIS_MONTH" ]; then
      echo "--- post_report: ${app} 本月（${THIS_MONTH}）已發過卡，跳過（月頻）"
      continue
    fi
    # 發卡前先為 TOP 1 補白話說明（pm_note 快取進月快照，卡片顯示給非工程師看）；
    # 失敗只影響卡片少一行白話，不擋發卡
    $PY "$CT/crash_trend/pm_brief.py" --app "$app" --top 1 >/dev/null 2>&1 || echo "    （$app pm_note 生成失敗，卡片不帶白話）"
    echo "--- post_report: ${app}（每月一次）"
    $PY "$CT/crash_trend/post_report.py" --app "$app"
    rc=$?
    if [ "$rc" = 0 ]; then
      mkdir -p "$CT/out/$app" && echo "$THIS_MONTH" > "$CARD_MARK" && echo "--- $app 本月發卡完成，已記 $THIS_MONTH"
    elif [ "$rc" = 3 ]; then
      FAILED="$FAILED post-延後:$app"
    else
      FAILED="$FAILED post:$app"
    fi
  done
fi

cd "$CT"
if [ -n "$(git status --porcelain)" ]; then
  git add -A
  # 身分沿用 repo/全域 git config；沒有就退回工具身分（可用 GIT_USER/GIT_EMAIL 覆寫）
  git -c user.name="${GIT_USER:-$(git config user.name || echo crash-trend-bot)}" \
      -c user.email="${GIT_EMAIL:-$(git config user.email || echo crash-trend@localhost)}" \
      commit -q -m "chore: weekly sync $(date '+%F')" && echo "--- committed"
fi

if [ -n "$FAILED" ]; then
  MSG="crash-trend 週同步有步驟失敗：${FAILED}（詳見 logs/weekly_sync.log）"
else
  MSG="crash-trend 週同步完成"
fi
# CRASH_TREND_NO_NOTIFY=1：測試／CI 執行時不跳 macOS 通知
if [ -z "${CRASH_TREND_NO_NOTIFY:-}" ] && command -v osascript >/dev/null; then
  osascript -e "display notification \"$MSG\" with title \"Crash 趨勢週同步\"" || true
fi
echo "===== done（failed:${FAILED:-無}） ====="
