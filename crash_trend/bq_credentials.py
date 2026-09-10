"""BigQuery 憑證解析與 client 工廠（唯一實作，供所有 BigQuery 進入點共用）。

解析規則（Crashlytics 與 Sessions 兩條路徑完全一致）：
- 優先序：`apps.<app_id>.bq_service_account` 覆寫全域 `credentials.bq_service_account`。
- sentinel `adc` / `none` / 空字串（或兩者皆未設定）→ 使用 Application Default Credentials。
  per-app 明確寫成空值即可在全域有 SA 的情況下把單一 App 切回 ADC。
- 指定了路徑但檔案不存在 → 拋 BQCredentialsError，絕不靜默退回 ADC
  （憑證比預期弱會讓查詢結果沉默地不完整，必須大聲失敗）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from crash_trend.config import load_config
except ImportError:
    from config import load_config  # type: ignore[import-not-found, no-redef]

# 視為「使用 ADC」的設定值（大小寫與前後空白皆不敏感）
ADC_SENTINELS = frozenset({"", "adc", "none"})


class BQCredentialsError(RuntimeError):
    """BigQuery 憑證設定有誤（例如指定的 service account json 不存在）。"""


def resolve_bq_credentials(
    app_cfg: dict | None = None,
    cfg: dict | None = None,
) -> Path | None:
    """回傳要使用的 service account json 路徑；回傳 None 代表改用 ADC。

    app_cfg 為單一 App 的設定（apps.<app_id> 那層），有 `bq_service_account` 鍵時覆寫全域。
    指定的檔案不存在時拋 BQCredentialsError。
    """
    if cfg is None:
        cfg = load_config()

    source = "credentials.bq_service_account"
    sa_path = (cfg.get("credentials") or {}).get("bq_service_account")
    if app_cfg and "bq_service_account" in app_cfg:
        sa_path = app_cfg["bq_service_account"]
        source = "apps.<app>.bq_service_account"

    if sa_path is None or str(sa_path).strip().lower() in ADC_SENTINELS:
        return None

    sa_file = Path(str(sa_path).strip()).expanduser()
    if not sa_file.exists():
        raise BQCredentialsError(f"{source} 指定的檔案不存在：{sa_file}")
    return sa_file


def make_bq_client(
    project: str,
    app_cfg: dict | None = None,
    cfg: dict | None = None,
) -> Any:
    """建立 BigQuery client：有 service account 就用它，否則用 ADC。"""
    from google.cloud import bigquery

    sa_file = resolve_bq_credentials(app_cfg, cfg=cfg)
    if sa_file is None:
        return bigquery.Client(project=project)  # ADC

    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(str(sa_file))
    return bigquery.Client(project=project, credentials=creds)
