#!/bin/bash
# 每週 crash 資料同步（launchd 或容器內 supercronic 呼叫；手動跑也行）
# 自動部分：各 app 的 BigQuery 拉取 → normalize →（有 GEMINI_API_KEY 時）Gemini 月報 → 儀表板 → commit
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
  echo "--- normalize: $app"
  $PY "$CT/crash_trend/normalize.py" --app "$app" || FAILED="$FAILED normalize:$app"
  if [ -n "${GEMINI_API_KEY:-}" ]; then
    echo "--- analyze_gemini: $app"
    $PY "$CT/crash_trend/analyze_gemini.py" --app "$app" || FAILED="$FAILED analyze:$app"
  fi
done

echo "--- build_dashboard"
$PY "$CT/crash_trend/build_dashboard.py" || FAILED="$FAILED dashboard"

# 有設 CRASH_REPORT_URL 才發卡到聊天室（chat 整合，見 DEPLOY.md）
if [ -n "${CRASH_REPORT_URL:-}" ]; then
  for app in $apps; do
    echo "--- post_report: $app"
    $PY "$CT/crash_trend/post_report.py" --app "$app" || FAILED="$FAILED post:$app"
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
  MSG="crash-trend 週同步有步驟失敗：$FAILED（詳見 logs/weekly_sync.log）"
else
  MSG="crash-trend 週同步完成"
fi
if command -v osascript >/dev/null; then
  osascript -e "display notification \"$MSG\" with title \"Crash 趨勢週同步\"" || true
fi
echo "===== done（failed:${FAILED:-無}） ====="
