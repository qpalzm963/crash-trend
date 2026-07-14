"""Crashlytics BigQuery export 查詢（google-cloud-bigquery，免裝 gcloud SDK）。

憑證解析順序：
  1. apps.yaml `credentials.bq_service_account`（服務帳號 json 路徑）— 伺服器部署用
  2. Application Default Credentials — 筆電上沿用 `gcloud auth login`
前置：Firebase Console → Integrations → BigQuery 已勾 Crashlytics。
Spark 方案走 BigQuery sandbox 免費可用（無串流、表 60 天過期）。
輸出 out/<app>/crashlytics_bq.json：top issues、機型/OS/版本分布、自訂 keys 分布、週趨勢。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from google.cloud import bigquery

from config import app_argparser, get_app, load_config, out_dir, write_json


def make_client(project: str) -> bigquery.Client:
    creds_cfg = (load_config().get("credentials") or {})
    sa_path = creds_cfg.get("bq_service_account")
    if sa_path:
        sa_file = Path(sa_path).expanduser()
        if not sa_file.exists():
            sys.exit(f"[錯誤] credentials.bq_service_account 指定的檔案不存在：{sa_file}")
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(str(sa_file))
        return bigquery.Client(project=project, credentials=creds)
    return bigquery.Client(project=project)  # ADC（gcloud auth login）


def list_crash_tables(client: bigquery.Client, project: str, dataset: str) -> list[str]:
    tables = [t.table_id for t in client.list_tables(f"{project}.{dataset}")]
    # 排除 _REALTIME（批次表才完整）；表名如 com_example_app_ANDROID / com_example_app_IOS
    return [t for t in tables if not t.endswith("_REALTIME")]


SQLS = {
    "top_issues": """
        SELECT issue_id, issue_title, issue_subtitle, error_type,
               COUNT(*) AS events,
               COUNT(DISTINCT installation_uuid) AS users,
               MIN(application.display_version) AS first_seen_version,
               MAX(application.display_version) AS last_seen_version
        FROM `{table}`
        WHERE event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
        GROUP BY 1, 2, 3, 4 ORDER BY events DESC LIMIT 50""",
    "by_device": """
        SELECT device.model AS device_model, COUNT(*) AS events,
               COUNT(DISTINCT installation_uuid) AS users
        FROM `{table}`
        WHERE event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
        GROUP BY 1 ORDER BY events DESC LIMIT 30""",
    "by_os": """
        SELECT operating_system.display_version AS os_version, COUNT(*) AS events,
               COUNT(DISTINCT installation_uuid) AS users
        FROM `{table}`
        WHERE event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
        GROUP BY 1 ORDER BY events DESC LIMIT 30""",
    "by_app_version": """
        SELECT application.display_version AS app_version, COUNT(*) AS events,
               COUNT(DISTINCT installation_uuid) AS users
        FROM `{table}`
        WHERE event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
        GROUP BY 1 ORDER BY events DESC LIMIT 30""",
    # 逐 issue 版本分布（修復驗證用：判斷某 issue 是否僅剩舊版出現）
    "issue_versions": """
        SELECT issue_id, application.display_version AS app_version,
               COUNT(*) AS events, COUNT(DISTINCT installation_uuid) AS users
        FROM `{table}`
        WHERE event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
        GROUP BY 1, 2 ORDER BY events DESC LIMIT 500""",
    # custom_keys 查詢在 main() 依 apps.yaml 該 app 的 custom_keys 動態組出（沒設定就跳過）
    "weekly_trend": """
        SELECT FORMAT_TIMESTAMP('%Y-%W', event_timestamp) AS week,
               COUNT(*) AS events, COUNT(DISTINCT installation_uuid) AS users
        FROM `{table}`
        WHERE event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
        GROUP BY 1 ORDER BY 1""",
}


def run_query(client: bigquery.Client, sql: str) -> list[dict]:
    rows = client.query(sql).result(max_results=500)
    return [dict(r) for r in rows]


def main() -> None:
    args = app_argparser("查詢 Crashlytics BigQuery export").parse_args()
    app = get_app(args.app)
    project, dataset = app["firebase_project"], app.get("bq_dataset", "firebase_crashlytics")
    result: dict = {"project": project, "dataset": dataset, "tables": {}, "errors": {}}

    try:
        client = make_client(project)
        tables = list_crash_tables(client, project, dataset)
    except Exception as e:  # dataset 不存在 / 憑證 / 權限
        write_json(out_dir(args.app) / "crashlytics_bq.json", {**result, "errors": {"dataset": str(e)[:800]}})
        sys.exit(
            f"[注意] 無法列出 {project}:{dataset} —— 尚未連結 BigQuery export、無資料，或憑證問題。\n"
            f"  憑證設定：apps.yaml 填 credentials.bq_service_account（SA json 路徑），"
            f"或本機跑一次 `gcloud auth application-default login`。\n"
            f"  {str(e)[:400]}"
        )

    if not tables:
        write_json(out_dir(args.app) / "crashlytics_bq.json", result)
        sys.exit("[注意] dataset 存在但沒有批次表（啟用當日屬正常，隔日再跑）")

    sqls = dict(SQLS)
    keys = [k for k in app.get("custom_keys", []) if re.fullmatch(r"[\w-]+", k)]
    if keys:
        key_list = ", ".join(f"'{k}'" for k in keys)
        sqls["custom_keys"] = f"""
        SELECT key.key AS custom_key, key.value AS value, COUNT(*) AS events
        FROM `{{table}}`, UNNEST(custom_keys) AS key
        WHERE event_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {{days}} DAY)
          AND key.key IN ({key_list})
        GROUP BY 1, 2 ORDER BY events DESC LIMIT 60"""

    for table in tables:
        fq = f"{project}.{dataset}.{table}"
        result["tables"][table] = {}
        for name, sql in sqls.items():
            try:
                result["tables"][table][name] = run_query(client, sql.format(table=fq, days=args.days))
                print(f"  ✓ {table}.{name}: {len(result['tables'][table][name])} 列")
            except Exception as e:
                result["errors"][f"{table}.{name}"] = str(e)[:800]
                print(f"  ⚠ {table}.{name} 失敗：{str(e)[:200]}", file=sys.stderr)

    write_json(out_dir(args.app) / "crashlytics_bq.json", result)
    if result["errors"]:
        sys.exit("[注意] 部分查詢失敗（詳見輸出 errors 欄位）")


if __name__ == "__main__":
    main()
