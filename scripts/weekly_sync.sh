#!/bin/bash
# 每週 crash 資料同步（launchd 或容器內 supercronic 呼叫；手動跑也行）
# 自動部分：各 app 的 BigQuery 拉取 → normalize →（有 Gemini key 時）Gemini 月報 → 儀表板 → commit
# 無法自動的：console 快照（需使用者登入態）→ macOS 上發通知提醒（容器內自動略過）
set -u
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

for app in $apps; do
  echo "--- fetch_bigquery: $app"
  # BQ 未連結/無資料時腳本會以非零碼結束並說明原因（屬預期，不算失敗）
  $PY "$CT/crash_trend/fetch_bigquery.py" --app "$app" || echo "    （$app 本次無 BQ 資料，原因見上）"
  echo "--- fetch_stacktraces: $app"
  # 需 firebase CLI ≥15.23 ＋ firebase login user token；失敗只影響 root cause 品質，不擋流程
  $PY "$CT/crash_trend/fetch_stacktraces.py" --app "$app" || echo "    （$app 本次無 stack trace，原因見上）"
  echo "--- normalize: $app"
  $PY "$CT/crash_trend/normalize.py" --app "$app" || FAILED="$FAILED normalize:$app"
  echo "--- check_surge: $app"
  # 暴增偵測每週跑（不受發卡月頻 gate）；失敗只記不擋
  $PY "$CT/crash_trend/check_surge.py" --app "$app" || FAILED="$FAILED surge:$app"
  # 有 Gemini key 就跑 AI 分析——GEMINI_API_KEY（直接）或 GEMINI_KEY_URL（後台取 key）任一即可。
  # 少了這個 analyze 不跑 → priority_list 空 → 聊天卡退化成「其他 N 個 issue」而非優先修復 TOP 3。
  if [ -n "${GEMINI_API_KEY:-}${GEMINI_KEY_URL:-}" ]; then
    echo "--- analyze_gemini: $app"
    $PY "$CT/crash_trend/analyze_gemini.py" --app "$app" || FAILED="$FAILED analyze:$app"
  fi
done

echo "--- build_dashboard"
$PY "$CT/crash_trend/build_dashboard.py" || FAILED="$FAILED dashboard"

# 發卡到聊天室（chat 整合，見 DEPLOY.md）：資料每週同步，但卡片「每月一次」——
# 用當月標記檔 gate（out/ 已 gitignore）。當月未發過才發；全部成功才記為已發，
# 失敗則不記、下週自動補發，同月最多一張。
CARD_MARK="$CT/out/.card_sent_month"
THIS_MONTH="$(date '+%Y-%m')"
if [ -n "${CRASH_REPORT_URL:-}" ]; then
  mkdir -p "$CT/out"
  if [ "$(cat "$CARD_MARK" 2>/dev/null)" = "$THIS_MONTH" ]; then
    echo "--- post_report: 本月（$THIS_MONTH）已發過卡，跳過（月頻）"
  else
    CARD_OK=1
    for app in $apps; do
      echo "--- post_report: $app（每月一次）"
      $PY "$CT/crash_trend/post_report.py" --app "$app" || { FAILED="$FAILED post:$app"; CARD_OK=0; }
    done
    [ "$CARD_OK" = 1 ] && echo "$THIS_MONTH" > "$CARD_MARK" && echo "--- 本月發卡完成，已記 $THIS_MONTH"
  fi
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
  MSG="crash-trend 週同步有步驟失敗：$FAILED（詳見 logs/weekly_sync.log）"
else
  MSG="crash-trend 週同步完成"
fi
if command -v osascript >/dev/null; then
  osascript -e "display notification \"$MSG\" with title \"Crash 趨勢週同步\"" || true
fi
echo "===== done（failed:${FAILED:-無}） ====="
