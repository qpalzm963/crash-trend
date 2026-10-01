#!/bin/bash
# 每日資料備份：打包 out/（版本歷史、資料庫）＋ reports/（月摘要）＋ apps.yaml，上傳到雲端（rclone）。
# BigQuery 只留 60 天，out/ 又不進 git——部署機硬碟是唯一一份，必須另存一份到機器外。
#
# 設定（.env）：BACKUP_RCLONE_REMOTE=<rclone remote>:<資料夾>，例如 gdrive:crash-trend-backup
#   未設定＝跳過（不算失敗）。
# 保留：daily/ 留 30 天；每週一另存一份到 weekly/，留 12 週。
# 結果寫入 out/.backup_status.json（健康檢查與定期彙整據此確認備份真的成功）。
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"  # /sbin：macOS 的 md5

CT="${CT:-$(cd "$(dirname "$0")/.." && pwd)}"
REMOTE="${BACKUP_RCLONE_REMOTE:-}"
RCLONE="${RCLONE:-rclone}"  # 測試時換成假的 rclone
STATUS="$CT/out/.backup_status.json"
mkdir -p "$CT/out"

if [ -z "$REMOTE" ]; then
  echo "  （未設 BACKUP_RCLONE_REMOTE，跳過備份）"
  exit 0
fi

NAME="crash-trend-data-$(date '+%Y%m%d-%H%M').tgz"
TMP="$(mktemp -d)"
FILE="$TMP/$NAME"
trap 'rm -rf "$TMP"' EXIT

fail() {
  echo "  [錯誤] 備份失敗：$1"
  printf '{"ok": false, "at": "%s", "error": "%s"}\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$(echo "$1" | tr '"\n' "' ")" > "$STATUS"
  exit 1
}

PARTS=""
for p in out reports apps.yaml; do [ -e "$CT/$p" ] && PARTS="$PARTS $p"; done
[ -n "$PARTS" ] || fail "沒有可備份的資料"
# shellcheck disable=SC2086  # PARTS 是刻意分詞的路徑清單
tar -czf "$FILE" -C "$CT" $PARTS || fail "打包失敗"
tar -tzf "$FILE" > /dev/null || fail "壓縮檔驗證失敗"

BYTES="$(wc -c < "$FILE" | tr -d ' ')"
MD5="$( (md5sum "$FILE" 2>/dev/null || md5 -r "$FILE") | awk '{print $1}')"

"$RCLONE" copyto "$FILE" "$REMOTE/daily/$NAME" 2>&1 | tail -3 || true
# 上傳後回讀雲端的 md5 比對：rclone 結束碼為 0 不代表檔案真的完整在雲端
REMOTE_MD5="$("$RCLONE" md5sum "$REMOTE/daily/$NAME" 2>/dev/null | awk '{print $1}')"
[ "$REMOTE_MD5" = "$MD5" ] || fail "雲端檔案比對不符（本機 ${MD5}，雲端 ${REMOTE_MD5:-無}）"

if [ "$(date '+%u')" = 1 ]; then
  "$RCLONE" copyto "$REMOTE/daily/$NAME" "$REMOTE/weekly/$NAME" || fail "每週備份複製失敗"
fi
# 清舊檔失敗不影響這次備份的成功（下次會再清）
"$RCLONE" delete "$REMOTE/daily" --min-age 30d 2>&1 | tail -2 || true
"$RCLONE" delete "$REMOTE/weekly" --min-age 84d 2>&1 | tail -2 || true

printf '{"ok": true, "at": "%s", "file": "%s", "bytes": %s, "md5": "%s", "remote": "%s"}\n' \
  "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$NAME" "$BYTES" "$MD5" "$REMOTE" > "$STATUS"
echo "  ✓ 備份完成：${NAME}（${BYTES} bytes，雲端 md5 相符）"
