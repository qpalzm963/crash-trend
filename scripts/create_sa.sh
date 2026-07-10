#!/bin/bash
# 一鍵建立 crash-trend 用的 BigQuery 唯讀服務帳號並產生金鑰。
# 前置：gcloud auth login（帳號需有該 Firebase 專案的 IAM 管理權）
# 用法：scripts/create_sa.sh <firebase_project_id> [金鑰輸出路徑]
#   多個 Firebase 專案共用同一把金鑰時，對其餘專案只授權不重建：
#   scripts/create_sa.sh <另一個專案> --grant-only <既有SA的email>
set -euo pipefail

PROJECT="${1:?用法：create_sa.sh <firebase_project_id> [金鑰路徑] | <專案> --grant-only <sa_email>}"
SA_NAME="crash-trend-reader"

if [ "${2:-}" = "--grant-only" ]; then
  SA_EMAIL="${3:?--grant-only 需要既有 SA 的 email}"
else
  KEY_PATH="${2:-$HOME/.config/crash-trend/sa.json}"
  SA_EMAIL="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
  gcloud iam service-accounts create "$SA_NAME" --project="$PROJECT" \
    --display-name="crash-trend BigQuery reader" 2>/dev/null || echo "（SA 已存在，沿用）"
fi

for ROLE in roles/bigquery.dataViewer roles/bigquery.jobUser; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$SA_EMAIL" --role="$ROLE" --condition=None >/dev/null
  echo "✓ $PROJECT ← $ROLE"
done

if [ "${2:-}" != "--grant-only" ]; then
  mkdir -p "$(dirname "$KEY_PATH")"
  gcloud iam service-accounts keys create "$KEY_PATH" --iam-account="$SA_EMAIL"
  chmod 600 "$KEY_PATH"
  echo "✓ 金鑰：$KEY_PATH（記得填入 apps.yaml credentials.bq_service_account）"
fi
