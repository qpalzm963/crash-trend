"""SQLite 連線建立與生命週期（唯一實作，供三個 store 共用）。

`catalog_authority.sqlite3`、`release_gate_history.sqlite3`、`alert_delivery.sqlite3`
各自手寫過一份 `sqlite3.connect(...)`，於是三份設定悄悄地長得不一樣：

- `check_same_thread` 只有兩個 store 傳了 `False`；
- `synchronous` / `busy_timeout` 只在 authority store 的**初始化那一條連線**上設定過，
  之後每一條新連線都回到預設值（pragma 是 per-connection 的，不是 per-database）；
- `ReleaseGateHistoryStore.__init__` 用 `with sqlite3.connect(...) as conn:` 建表——
  而 `with` 在 sqlite3 是**交易**的 context manager，**不會關閉連線**。那條連線就這樣
  漏掉了，而 `get_gate_history_store()` 是在 release_catalog 的 per-platform /
  per-version 迴圈裡呼叫的，一次 dashboard build 會漏掉數十條。

本模組把「一條連線該長什麼樣」與「誰負責關它」收成一份：`connection()` 是
contextmanager，成功則 commit、失敗則 rollback、**一律 close**；in-memory 連線由
呼叫端持有（關掉就等於刪庫），因此本模組不碰它們的生命週期，只負責 file-backed 的
那一半。
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

#: 等鎖時間。三個 store 原本都是 10 秒，統一保留。
DEFAULT_TIMEOUT_SEC = 10.0

#: 每一條 file-backed 連線都要套的 pragma。
#:
#: 只有 `journal_mode`（per-database，三個 store 原本各自都已設定 WAL）。刻意**不**在
#: 這裡設另外兩個，各有原因：
#:
#: * `busy_timeout`：它與 `sqlite3.connect(timeout=...)` 是**同一個** busy handler，
#:   後設的會覆蓋前者。原本 authority store 的 `PRAGMA busy_timeout = 5000` 其實是把
#:   自己 `timeout=10.0` 的等鎖時間砍半（只在初始化那條連線上）。等鎖時間只用
#:   `DEFAULT_TIMEOUT_SEC` 一個旋鈕表達。
#: * `synchronous`：預設的 FULL 才是這三個資料庫該有的耐久度。`alert_delivery`
#:   參與去重、冷卻與復原判定，`release_gate_history` 參與狀態轉換判定——WAL + NORMAL
#:   在 OS crash / 斷電時可能回滾最近已 commit 的交易，而那會變成「重複發出的告警」
#:   或「被吞掉的復原通知」。這些不是隨時可重建的顯示快取。
FILE_PRAGMAS: tuple[str, ...] = ("PRAGMA journal_mode = WAL;",)


def connect(
    db_path: str | Path,
    read_only: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> sqlite3.Connection:
    """建立一條 file-backed 連線（`row_factory` 為 `sqlite3.Row`，已套好 pragma）。

    `check_same_thread=False`：pipeline 會在 thread pool 裡讀寫這些 store，而每個
    呼叫點都是「開一條、用完關掉」，不會跨執行緒共用同一條連線。

    等鎖時間由 `timeout` 表達（sqlite3 會把它設成該連線的 busy handler）；本模組
    **不再**額外下 `PRAGMA busy_timeout`，因為那會覆蓋掉這個參數。

    pragma 失敗不視為致命（唯讀連線或掛在不支援 WAL 的檔案系統上都可能失敗）——
    連線本身仍然可用，而真正該大聲失敗的是查詢，不是最佳化設定。
    """
    if read_only:
        uri = f"file:{Path(db_path).resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout, check_same_thread=False)
    else:
        conn = sqlite3.connect(str(db_path), timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if not read_only:
        for pragma in FILE_PRAGMAS:
            try:
                conn.execute(pragma)
            except sqlite3.Error:
                pass
    return conn


@contextlib.contextmanager
def connection(
    db_path: str | Path,
    read_only: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> Iterator[sqlite3.Connection]:
    """借一條連線：成功 commit、失敗 rollback、離開時**一定** close。

    刻意不提供「不關閉」的選項：`with sqlite3.connect(...)` 只管交易不管關閉，
    那個陷阱正是本模組要消滅的東西。需要跨多次呼叫共用一條連線的情境（store 的
    explicit connection / in-memory），由 store 自己持有連線，不走這裡。
    """
    conn = connect(db_path, read_only=read_only, timeout=timeout)
    try:
        yield conn
        if not read_only:
            conn.commit()
    except Exception:
        # 明確 rollback 是防禦性的：下面的 close() 本身就會丟棄未 commit 的交易，
        # 因此沒有任何測試能區分有沒有這一行。留著是為了「連線之後若被共用／pooled」
        # 時語意仍然正確，而不是因為現在缺了它會出錯。
        if not read_only:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
        raise
    finally:
        conn.close()
