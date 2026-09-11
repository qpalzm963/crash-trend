"""BigQuery 查詢的 timeout 與逾時取消（唯一實作，供所有查詢進入點共用）。

在此之前，所有查詢都是 `client.query(sql).result()`——兩端都沒有上限。GCP 一出現
transient 延遲，pipeline 就會無限期卡在某一支查詢上，而且卡住的那個 job 在 BigQuery
端仍在跑（沒人會去取消它）。本模組把「一支查詢最多等多久」變成設定，並保證逾時
**一定是一個明確的錯誤**，不會退化成一筆空結果。

三層防護，缺一不可：

1. `job_timeout_ms`：**BigQuery 端**自己的上限。即使本地行程被 kill、網路斷掉，
   job 也會被 BigQuery 終止，不會留下一支沒人管的查詢繼續燒錢。
2. `result(timeout=...)`：**client 端**等待上限。這一層才是 pipeline 不卡住的保證
   （光有 job_timeout_ms 的話，等待本身仍可能因網路問題無限延長）。
3. client 端逾時後主動 `job.cancel()`：`result()` 逾時**不會**取消 job，只是放棄等待。
   少了這一步就會累積一堆 running job。

逾時一律以 `BQQueryTimeout` 拋出，因此會走各進入點既有的 per-query 錯誤路徑
（在 `fetch_bigquery` 是 `errors[<table>.<query>]`，最終讓該期間的 KPI 變成
`status: "error"`）。**逾時絕不能被讀成「這段時間沒有崩潰」**——那比整支 pipeline
失敗更危險，因為沒有人會去查一個看起來健康的報表。

設定優先序（與 `bq_credentials` 的憑證解析同一套風格）：

- 環境變數 `CRASH_TREND_BQ_QUERY_TIMEOUT`（秒）
- `apps.<app_id>.bq_query_timeout_sec`
- 全域 `bigquery.query_timeout_sec`
- 預設 `DEFAULT_QUERY_TIMEOUT_SEC`（300 秒）

值為 `0` / `none` / `off` / `disabled` / `false` 時代表**明確**取消上限（回 `None`）；
這是一個要自己寫下來的選擇，不是預設。

**空值不是取消上限**：YAML 的 `query_timeout_sec:`（沒寫值）會解析成 `None`，環境變數
`CRASH_TREND_BQ_QUERY_TIMEOUT=` 同理。這種寫法一律視為「這一層沒設」並往下一層找，
否則一個看起來無害的空行就能悄悄關掉整張單要加的 guard。

設成無法解析的值（含 `nan` / `inf`）則拋 `BQQueryConfigError`——不靜默退回預設，
因為「以為設了 30 秒、其實跑的是 300 秒」正是這類設定最常見的失敗。
"""

from __future__ import annotations

import math
import os
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

try:
    from crash_trend.config import load_config
except ImportError:  # pragma: no cover - 供 repo 根目錄直接執行腳本使用
    from config import load_config  # type: ignore[import-not-found, no-redef]

#: 單一查詢的預設等待上限（秒）。這個數字的用途不是「調效能」，而是「不要無限期卡住」，
#: 因此刻意寬鬆：正常的 90 天彙總查詢是數秒到數十秒，跑到 5 分鐘就代表出事了。
DEFAULT_QUERY_TIMEOUT_SEC = 300.0

#: 以秒為單位覆寫上限的環境變數（CI / 一次性排查用，優先於設定檔）。
ENV_QUERY_TIMEOUT = "CRASH_TREND_BQ_QUERY_TIMEOUT"

#: 視為「不設上限」的設定值（大小寫與前後空白皆不敏感）。
NO_TIMEOUT_SENTINELS = frozenset({"0", "none", "off", "disabled", "false", "unlimited"})

#: app 層與全域層的設定鍵。命名沿用既有慣例（app 層 `bq_*`、全域分節）。
APP_TIMEOUT_KEY = "bq_query_timeout_sec"
GLOBAL_TIMEOUT_SECTION = "bigquery"
GLOBAL_TIMEOUT_KEY = "query_timeout_sec"


class BQQueryConfigError(RuntimeError):
    """query timeout 設定無法解析（例如寫成非數字）。"""


class BQQueryTimeout(RuntimeError):
    """查詢超過等待上限；已嘗試取消該 job。

    `job_id` 保留下來是為了讓操作者能在 BigQuery console 追那一支 job；
    `cancel_error` 不為 `None` 代表取消也失敗了（job 可能還在跑，需要人工確認）。
    """

    def __init__(
        self,
        message: str,
        timeout_sec: float,
        job_id: str | None = None,
        cancel_error: str | None = None,
    ) -> None:
        super().__init__(message)
        self.timeout_sec = timeout_sec
        self.job_id = job_id
        self.cancel_error = cancel_error


class _Unset:
    """`timeout` 參數未指定的哨兵；與「明確傳 None（不設上限）」區分開。"""


UNSET = _Unset()


def _is_unset(raw: Any) -> bool:
    """判斷一個設定值是否等於「這一層沒設」。

    YAML 的 `query_timeout_sec:`（沒寫值）會解析成 `None`，`""` 同理。這種寫法**不是**
    「不設上限」——那會讓一個看起來無害的空行悄悄關掉整個 guard。沒寫值一律往下一層
    找（app → 全域 → 預設），要取消上限必須明確寫 `0` / `off`。
    """
    return raw is None or (isinstance(raw, str) and raw.strip() == "")


def _coerce_timeout(raw: Any, source: str) -> float | None:
    """把設定值轉成秒數；回 `None` 代表不設上限。無法解析時拋錯，不猜。

    呼叫端必須先用 `_is_unset()` 濾掉「沒設」——空值到這裡就是程式錯誤（例如有人直接
    傳 `timeout=""`），不是 unlimited。
    """
    if _is_unset(raw):
        raise BQQueryConfigError(
            f"{source} 是空值；沒有要設定就整行移除，要取消上限請明確寫 0 / off"
        )
    if isinstance(raw, bool):
        # YAML 的 `false` 會被解析成 bool，語意同 sentinel；`true` 沒有對應秒數。
        if raw is False:
            return None
        raise BQQueryConfigError(f"{source} 不接受 true；請給秒數或 0 / off（不設上限）")
    if isinstance(raw, (int, float)):
        secs = float(raw)
    else:
        text = str(raw).strip()
        if text.lower() in NO_TIMEOUT_SENTINELS:
            return None
        try:
            secs = float(text)
        except ValueError:
            raise BQQueryConfigError(
                f"{source} 必須是秒數或 0 / off（不設上限），實際為：{raw!r}"
            ) from None
    if not math.isfinite(secs):
        # nan 比對永遠為 False，會一路穿過下面的檢查；inf 則在 int(secs * 1000) 溢位。
        # 兩者都不是「不設上限」的寫法——不設上限請明確寫 0 / off。
        raise BQQueryConfigError(
            f"{source} 必須是有限的秒數（不接受 nan / inf），實際為：{raw!r}"
        )
    if secs == 0:
        return None
    if secs < 0:
        raise BQQueryConfigError(f"{source} 不能是負數，實際為：{raw!r}")
    return secs


def resolve_query_timeout(
    app_cfg: dict | None = None,
    cfg: dict | None = None,
    env: dict[str, str] | None = None,
) -> float | None:
    """回傳單一查詢的等待上限（秒）；`None` 代表不設上限。

    優先序見模組 docstring。`env` 可注入以便測試，省略時讀 `os.environ`。
    """
    environ = env if env is not None else os.environ
    raw_env = environ.get(ENV_QUERY_TIMEOUT)
    if not _is_unset(raw_env):
        return _coerce_timeout(raw_env, f"環境變數 {ENV_QUERY_TIMEOUT}")

    if app_cfg and not _is_unset(app_cfg.get(APP_TIMEOUT_KEY)):
        return _coerce_timeout(app_cfg[APP_TIMEOUT_KEY], f"apps.<app>.{APP_TIMEOUT_KEY}")

    if cfg is None:
        cfg = load_config()
    section = cfg.get(GLOBAL_TIMEOUT_SECTION) or {}
    if isinstance(section, dict) and not _is_unset(section.get(GLOBAL_TIMEOUT_KEY)):
        return _coerce_timeout(
            section[GLOBAL_TIMEOUT_KEY], f"{GLOBAL_TIMEOUT_SECTION}.{GLOBAL_TIMEOUT_KEY}"
        )

    return DEFAULT_QUERY_TIMEOUT_SEC


def _job_config_kwargs(timeout_sec: float | None) -> dict[str, Any]:
    """組出帶 `job_timeout_ms` 的 `job_config`（不設上限時回空 dict）。"""
    if timeout_sec is None:
        return {}
    from google.cloud import bigquery

    return {"job_config": bigquery.QueryJobConfig(job_timeout_ms=int(timeout_sec * 1000))}


def _cancel_quietly(job: Any) -> str | None:
    """嘗試取消 job；回傳「取消請求沒送出」的原因（送出成功則 `None`）。

    `QueryJob.cancel()` 的契約是 **bool：取消請求是否送出**（不是「job 已結束」）。
    因此 `False` 代表請求根本沒送出，必須當成失敗回報——把 no-exception 當成成功會
    讓訊息宣稱取消了一支其實還在跑的 job。回傳非 bool（自訂 wrapper / 舊版 client）
    時同樣無法確認，一律回報而不是樂觀假設。

    取消失敗不得蓋掉原本的逾時錯誤——逾時才是呼叫端要處理的事實——但也不能被吞掉，
    因為那代表 BigQuery 端可能還有一支 job 在跑。
    """
    cancel = getattr(job, "cancel", None)
    if not callable(cancel):
        return "job 物件沒有 cancel()"
    try:
        sent = cancel()
    except Exception as exc:  # noqa: BLE001 - 任何取消失敗都只是附註，不改變控制流
        return str(exc)[:300]
    if sent is True:
        return None
    if sent is False:
        return "cancel() 回傳 False"
    return f"cancel() 回傳非 bool（{sent!r}），無法確認取消請求是否送出"


def execute_query(
    client: Any,
    sql: str,
    max_results: int | None = None,
    timeout: float | None | _Unset = UNSET,
) -> Any:
    """執行查詢並回傳 row iterator，保證不會無限期等待。

    `timeout` 省略時由 `resolve_query_timeout()` 解析（環境變數 + 全域設定）；
    知道 app 設定的呼叫端應該自己解析一次再傳進來，才吃得到 per-app 覆寫。
    明確傳 `None` 代表不設上限。

    逾時（client 端）會先取消 job，再拋 `BQQueryTimeout`。
    """
    if isinstance(timeout, _Unset):
        secs = resolve_query_timeout()
    elif timeout is None:
        # 呼叫端**明確**傳 None 才是不設上限（設定檔的空值不是，見 `_is_unset`）。
        secs = None
    else:
        secs = _coerce_timeout(timeout, "timeout 參數")

    job = client.query(sql, **_job_config_kwargs(secs))

    result_kwargs: dict[str, Any] = {}
    if max_results is not None:
        result_kwargs["max_results"] = max_results
    if secs is not None:
        result_kwargs["timeout"] = secs

    try:
        return job.result(**result_kwargs)
    except FuturesTimeoutError as exc:
        job_id = getattr(job, "job_id", None)
        cancel_error = _cancel_quietly(job)
        # 送出取消請求 != job 已結束（BigQuery 的 cancel 是 best-effort），因此兩個
        # 分支都只敘述「請求」的狀態，不宣稱 job 已經停了。
        suffix = (
            f"；取消請求未送出：{cancel_error}"
            if cancel_error
            else "；已送出取消請求（BigQuery 端可能仍在收尾）"
        )
        assert secs is not None  # 沒設上限時 result() 不會逾時
        raise BQQueryTimeout(
            f"BigQuery 查詢超過 {secs:g} 秒上限 (job_id={job_id}){suffix}",
            timeout_sec=secs,
            job_id=str(job_id) if job_id is not None else None,
            cancel_error=cancel_error,
        ) from exc
