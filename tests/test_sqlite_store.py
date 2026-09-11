"""SQLite 連線生命週期與三個 store 的共用設定 (Issue #66 項目 2).

三個 store（catalog authority / release gate history / alert delivery）原本各自手寫
一份 `sqlite3.connect(...)`，設定因此悄悄地長得不一樣，而其中一處是真的會壞的：

`ReleaseGateHistoryStore.__init__` 用 `with sqlite3.connect(...) as conn:` 建表，
但 `with` 在 sqlite3 是**交易**的 context manager，**不會關閉連線**。
`get_gate_history_store()` 又是在 release_catalog 的 per-platform / per-version 迴圈
裡呼叫的，因此一次 dashboard build 會漏掉數十條連線（跑得夠久就會撞到 fd 上限）。

本檔釘住三件事：

1. **不漏連線**：file-backed 連線開幾條就要關幾條——包含 store 建構、讀、寫、
   以及寫到一半拋例外的路徑。
2. **pragma 是 per-connection**：`synchronous` / `busy_timeout` 只在初始化那條連線上
   設定等於之後每條新連線都回到預設值。因此對**新開的**連線斷言這些值。
3. **並行讀寫不炸**：pipeline 會在 thread pool 裡碰這些 store，連線必須
   `check_same_thread=False`，且每個呼叫點都是開一條、用完關掉。
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import sys
import tempfile
import unittest
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.alerts.state import AlertDeliveryStore
from crash_trend.authority_store import CatalogAuthorityStore
from crash_trend.gate.history import GateSnapshot, ReleaseGateHistoryStore
from crash_trend.sqlite_store import (
    DEFAULT_TIMEOUT_SEC,
    FILE_PRAGMAS,
    connect,
    connection,
)

#: sqlite 的 `synchronous = FULL`。這三個資料庫參與去重 / 冷卻 / 狀態轉換判定，
#: 耐久度不得被降級（#98 review）。
SQLITE_FULL_SYNCHRONOUS = 2


class Tracker:
    """統計 file-backed 連線的開啟與關閉次數。

    `:memory:` 連線刻意不計：那些是 store 自己持有的（關掉就等於刪庫），
    生命週期不歸 sqlite_store 管。
    """

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.closed: list[str] = []

    @property
    def leaked(self) -> int:
        return len(self.opened) - len(self.closed)


@contextlib.contextmanager
def track_connections() -> Iterator[Tracker]:
    tracker = Tracker()
    real_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        _target = ""

        def close(self) -> None:
            tracker.closed.append(self._target)
            super().close()

    def fake_connect(target: Any, *args: Any, **kwargs: Any) -> Any:
        if ":memory:" in str(target):
            return real_connect(target, *args, **kwargs)
        kwargs.setdefault("factory", TrackedConnection)
        conn = real_connect(target, *args, **kwargs)
        conn._target = str(target)
        tracker.opened.append(str(target))
        return conn

    sqlite3.connect = fake_connect  # type: ignore[assignment]
    try:
        yield tracker
    finally:
        sqlite3.connect = real_connect  # type: ignore[assignment]


def make_snapshot(version: str, app_id: str = "shop_app") -> GateSnapshot:
    return GateSnapshot(
        app_id=app_id,
        platform="android",
        version=version,
        previous_version=None,
        gate_status="pass",
        sample_sufficient=True,
        policy_version="1.0",
        comparison_window="30d",
        evaluated_at="2026-09-11T02:00:00Z",
        evaluation_key="",
        triggered_reasons=[],
        rule_results=[],
        normalized_metrics={},
        summary=f"版本 {version} 通過",
    )


class TestNoConnectionIsLeaked(unittest.TestCase):
    """開幾條就要關幾條——這是本項目真正修掉的那個缺陷。"""

    def test_constructing_the_gate_history_store_closes_its_setup_connection(self) -> None:
        """回歸測試：原本每建一次 store 就漏一條連線。"""
        with tempfile.TemporaryDirectory() as td:
            with track_connections() as tracker:
                for _ in range(5):
                    ReleaseGateHistoryStore(Path(td) / "history.sqlite3")
            self.assertGreaterEqual(len(tracker.opened), 5, "前提：真的有開 file-backed 連線")
            self.assertEqual(tracker.leaked, 0, f"漏了 {tracker.leaked} 條連線")

    def test_reads_and_writes_close_their_connections(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "history.sqlite3"
            store = ReleaseGateHistoryStore(db)
            with track_connections() as tracker:
                for idx in range(3):
                    store.record_snapshot(make_snapshot(f"1.0.{idx}"))
                store.get_release_gate_history("shop_app", "android", "1.0.0")
                store.get_recent_release_gate_trend("shop_app", "android")
            self.assertGreater(len(tracker.opened), 0)
            self.assertEqual(tracker.leaked, 0)

    def test_the_authority_store_closes_its_connections(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with track_connections() as tracker:
                store = CatalogAuthorityStore(Path(td) / "authority.sqlite3")
                with store._connection() as conn:
                    conn.execute("SELECT 1").fetchone()
            self.assertGreater(len(tracker.opened), 0)
            self.assertEqual(tracker.leaked, 0)

    def test_the_alert_store_closes_its_connections(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with track_connections() as tracker:
                store = AlertDeliveryStore(Path(td) / "alerts.sqlite3")
                with store._connection() as conn:
                    conn.execute("SELECT 1").fetchone()
            self.assertGreater(len(tracker.opened), 0)
            self.assertEqual(tracker.leaked, 0)

    def test_a_failing_operation_still_closes_the_connection(self) -> None:
        """寫到一半炸掉是最容易漏連線的路徑，因此單獨測。"""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "x.sqlite3"
            with track_connections() as tracker:
                with self.assertRaises(sqlite3.OperationalError):
                    with connection(db) as conn:
                        conn.execute("SELECT * FROM table_that_does_not_exist")
            self.assertEqual(len(tracker.opened), 1)
            self.assertEqual(tracker.leaked, 0)


class TestEveryConnectionIsConfiguredConsistently(unittest.TestCase):
    """WAL 套在每條連線上；等鎖時間與耐久度不得被 pragma 悄悄改掉。

    `journal_mode` 是 per-database，但仍對每條連線下一次（成本為零，且新建的資料庫
    第一條連線就會進 WAL）。另外兩件事是 #98 review 抓到的：`busy_timeout` 與
    `synchronous` 都**不該**由這裡設定。
    """

    def _read(self, conn: sqlite3.Connection, pragma: str) -> Any:
        return conn.execute(f"PRAGMA {pragma};").fetchone()[0]

    def test_wal_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with connection(Path(td) / "a.sqlite3") as conn:
                self.assertEqual(str(self._read(conn, "journal_mode")).lower(), "wal")

    def test_the_connect_timeout_is_not_overridden_by_a_pragma(self) -> None:
        """`busy_timeout` 與 `sqlite3.connect(timeout=...)` 是同一個 busy handler。

        後設的會蓋掉前者：原本 `PRAGMA busy_timeout = 5000` 搭 `timeout=10.0` 的實際
        效果是把等鎖時間砍半。等鎖時間只能有一個旋鈕。
        """
        with tempfile.TemporaryDirectory() as td:
            with connection(Path(td) / "a.sqlite3") as conn:
                self.assertEqual(
                    self._read(conn, "busy_timeout"),
                    int(DEFAULT_TIMEOUT_SEC * 1000),
                    "等鎖時間與 connect(timeout=) 不一致 → 有 pragma 覆蓋了它",
                )

    def test_durability_is_not_downgraded(self) -> None:
        """這三個資料庫不是可隨時重建的顯示快取。

        `alert_delivery` 參與去重 / 冷卻 / 復原判定，`release_gate_history` 參與狀態
        轉換判定。WAL + NORMAL 在 OS crash / 斷電時可能回滾最近已 commit 的交易，
        而那會變成重複發出的告警或被吞掉的復原通知。
        """
        with tempfile.TemporaryDirectory() as td:
            with connection(Path(td) / "a.sqlite3") as conn:
                self.assertEqual(
                    self._read(conn, "synchronous"),
                    SQLITE_FULL_SYNCHRONOUS,
                    "synchronous 被降級了（FULL=2）",
                )

    def test_no_pragma_touches_durability_or_lock_waiting(self) -> None:
        """機械檢查：共用 pragma 清單裡不得再出現這兩個名字。"""
        joined = " ".join(FILE_PRAGMAS).lower()
        self.assertIn("journal_mode", joined)
        self.assertNotIn("synchronous", joined)
        self.assertNotIn("busy_timeout", joined)

    def test_each_store_gets_the_same_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = CatalogAuthorityStore(Path(td) / "authority.sqlite3")
            with store._connection() as conn:
                self.assertEqual(self._read(conn, "busy_timeout"), int(DEFAULT_TIMEOUT_SEC * 1000))
                self.assertEqual(self._read(conn, "synchronous"), SQLITE_FULL_SYNCHRONOUS)

            history = ReleaseGateHistoryStore(Path(td) / "history.sqlite3")
            conn = history._connect()
            try:
                self.assertEqual(self._read(conn, "busy_timeout"), int(DEFAULT_TIMEOUT_SEC * 1000))
                self.assertEqual(self._read(conn, "synchronous"), SQLITE_FULL_SYNCHRONOUS)
            finally:
                conn.close()


class TestConnectionSemantics(unittest.TestCase):
    """commit / rollback / read-only 的行為。"""

    def test_success_commits(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "a.sqlite3"
            with connection(db) as conn:
                conn.execute("CREATE TABLE t (v INTEGER)")
                conn.execute("INSERT INTO t VALUES (1)")
            with connection(db) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 1)

    def test_an_exception_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "a.sqlite3"
            with connection(db) as conn:
                conn.execute("CREATE TABLE t (v INTEGER)")
            with contextlib.suppress(RuntimeError):
                with connection(db) as conn:
                    conn.execute("INSERT INTO t VALUES (1)")
                    raise RuntimeError("boom")
            with connection(db) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM t").fetchone()[0],
                    0,
                    "例外之後那筆寫入仍在 → 沒有 rollback",
                )

    def test_read_only_connections_reject_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "a.sqlite3"
            with connection(db) as conn:
                conn.execute("CREATE TABLE t (v INTEGER)")
            with self.assertRaises(sqlite3.OperationalError):
                with connection(db, read_only=True) as conn:
                    conn.execute("INSERT INTO t VALUES (1)")

    def test_rows_come_back_as_mappings(self) -> None:
        """三個 store 都靠 `sqlite3.Row` 以欄位名取值；預設 tuple 會讓它們全炸。"""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "a.sqlite3"
            with connection(db) as conn:
                conn.execute("CREATE TABLE t (v INTEGER)")
                conn.execute("INSERT INTO t VALUES (7)")
                row = conn.execute("SELECT v FROM t").fetchone()
                self.assertEqual(row["v"], 7)


class TestConcurrentAccess(unittest.TestCase):
    """pipeline 會在 thread pool 裡碰這些 store。"""

    def test_the_gate_history_store_survives_concurrent_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = ReleaseGateHistoryStore(Path(td) / "history.sqlite3")
            versions = [f"2.0.{i}" for i in range(8)]
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda v: store.record_snapshot(make_snapshot(v)), versions))
            trend = store.get_recent_release_gate_trend("shop_app", "android", limit=20)
            self.assertEqual(
                {item.version for item in trend},
                set(versions),
                "並行寫入有資料遺失",
            )

    def test_a_connection_can_be_used_off_the_creating_thread(self) -> None:
        """`check_same_thread=False`：少了它，跨執行緒使用會丟 ProgrammingError。"""
        with tempfile.TemporaryDirectory() as td:
            conn = connect(Path(td) / "a.sqlite3")
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    value = pool.submit(
                        lambda: conn.execute("SELECT 1").fetchone()[0]
                    ).result()
                self.assertEqual(value, 1)
            finally:
                conn.close()


class TestOnlyOneModuleOpensFileBackedDatabases(unittest.TestCase):
    def test_no_module_connects_to_a_file_database_outside_sqlite_store(self) -> None:
        """機械掃描：`sqlite3.connect(` 只允許出現在 sqlite_store.py 或 `:memory:` 那行。

        沒有這條，下一個 store 只要照舊寫法各自 connect，設定又會開始各自漂移——
        而「漏一條連線」這種缺陷只在跑得夠久之後才現形。
        """
        offenders: list[str] = []
        for path in sorted((ROOT / "crash_trend").rglob("*.py")):
            if path.name == "sqlite_store.py":
                continue
            for idx, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                # 掃的是程式碼：註解裡解釋「為什麼不這樣寫」時難免會提到那個寫法。
                line = raw_line.split("#", 1)[0]
                if re.search(r"sqlite3\.connect\(", line) and ":memory:" not in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{idx}: {raw_line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "以下連線沒有走 sqlite_store（設定與關閉責任會各自漂移）：\n"
            + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
