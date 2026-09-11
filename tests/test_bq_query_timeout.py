"""BigQuery 查詢逾時與取消 (Issue #66 項目 1).

在此之前每支查詢都是 `client.query(sql).result()`：BigQuery 端沒有 `job_timeout_ms`、
client 端沒有 `timeout`，GCP 一延遲 pipeline 就無限期卡住，而卡住的 job 還在對面跑。

本檔釘住三件事，缺任一件這個修補就是假的：

1. **兩端都有上限**：server 端 `job_timeout_ms`（行程被 kill 也會被終止）與 client 端
   `result(timeout=)`（pipeline 不卡住）必須同時存在。
2. **逾時要取消 job**：`result()` 逾時只是放棄等待，不會取消 job；少了 `cancel()`
   就會累積一堆沒人管的 running job。取消失敗也不得被吞掉。
3. **逾時絕不能被讀成「沒有崩潰」**：逾時必須以例外進入既有的 per-query 錯誤路徑，
   最終讓該期間的 KPI 變成 `status: "error"`。一份看起來健康的報表沒有人會去查，
   那比整支 pipeline 失敗危險得多。

設定解析另外測完整優先序與**拒絕猜測**：值寫壞時拋錯，而不是靜默退回預設
（「以為設了 30 秒、其實跑的是 300 秒」是這類設定最常見的失敗）。
"""

from __future__ import annotations

import datetime as dt
import re
import sys
import unittest
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.bq_query import (
    DEFAULT_QUERY_TIMEOUT_SEC,
    ENV_QUERY_TIMEOUT,
    BQQueryConfigError,
    BQQueryTimeout,
    execute_query,
    resolve_query_timeout,
)
from crash_trend.fetch_bigquery import run_query, transform_bq_period_snapshot
from crash_trend.fetch_sessions import run_sessions_query


class FakeJob:
    """記下 `result()` 收到什麼、`cancel()` 被叫幾次的假 job。

    刻意不用 MagicMock：MagicMock 會吞掉「多傳了一個沒人要的參數」這種錯，
    而本檔要驗的正是參數怎麼傳。
    """

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        raise_timeout: bool = False,
        cancel_raises: str | None = None,
        job_id: str | None = "job-123",
        cancel_returns: Any = True,
    ) -> None:
        self.rows = rows if rows is not None else []
        self.raise_timeout = raise_timeout
        self.cancel_raises = cancel_raises
        # `QueryJob.cancel()` 的契約是 bool：**取消請求是否送出**。假 job 必須照這個
        # 契約回傳，否則測試會在一個真實 client 不可能出現的形狀上通過。
        self.cancel_returns = cancel_returns
        self.job_id = job_id
        self.result_kwargs: dict[str, Any] | None = None
        self.cancel_calls = 0

    def result(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.result_kwargs = kwargs
        if self.raise_timeout:
            raise FuturesTimeoutError("waited too long")
        return self.rows

    def cancel(self) -> Any:
        self.cancel_calls += 1
        if self.cancel_raises:
            raise RuntimeError(self.cancel_raises)
        return self.cancel_returns


class _JobWithoutCancel:
    def __init__(self) -> None:
        self.job_id = "job-no-cancel"
        self.result_kwargs: dict[str, Any] | None = None

    def result(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.result_kwargs = kwargs
        raise FuturesTimeoutError("waited too long")


class FakeClient:
    """記下 `query()` 收到的 sql 與 kwargs。"""

    def __init__(self, job: Any) -> None:
        self.job = job
        self.query_calls: list[tuple[str, dict[str, Any]]] = []

    def query(self, sql: str, **kwargs: Any) -> Any:
        self.query_calls.append((sql, kwargs))
        return self.job

    @property
    def last_job_config(self) -> Any:
        return self.query_calls[-1][1].get("job_config")


class TestTimeoutResolution(unittest.TestCase):
    """優先序：env > app > 全域 > 預設；壞值一律拋錯，不猜。"""

    def test_default_applies_when_nothing_is_configured(self) -> None:
        self.assertEqual(
            resolve_query_timeout(app_cfg={}, cfg={}, env={}), DEFAULT_QUERY_TIMEOUT_SEC
        )

    def test_global_section_is_used(self) -> None:
        cfg = {"bigquery": {"query_timeout_sec": 45}}
        self.assertEqual(resolve_query_timeout(app_cfg={}, cfg=cfg, env={}), 45.0)

    def test_app_override_beats_global(self) -> None:
        cfg = {"bigquery": {"query_timeout_sec": 45}}
        self.assertEqual(
            resolve_query_timeout(app_cfg={"bq_query_timeout_sec": 12}, cfg=cfg, env={}), 12.0
        )

    def test_env_beats_app_and_global(self) -> None:
        cfg = {"bigquery": {"query_timeout_sec": 45}}
        self.assertEqual(
            resolve_query_timeout(
                app_cfg={"bq_query_timeout_sec": 12}, cfg=cfg, env={ENV_QUERY_TIMEOUT: "7.5"}
            ),
            7.5,
        )

    def test_an_empty_env_var_does_not_mean_no_timeout(self) -> None:
        """`export CRASH_TREND_BQ_QUERY_TIMEOUT=` 是「沒設」，不是「不設上限」。"""
        cfg = {"bigquery": {"query_timeout_sec": 45}}
        self.assertEqual(
            resolve_query_timeout(app_cfg={}, cfg=cfg, env={ENV_QUERY_TIMEOUT: "  "}), 45.0
        )

    def test_sentinels_mean_no_limit(self) -> None:
        for raw in (0, "0", "off", "none", "disabled", "false", "unlimited", False):
            with self.subTest(raw=raw):
                self.assertIsNone(
                    resolve_query_timeout(app_cfg={"bq_query_timeout_sec": raw}, cfg={}, env={})
                )

    def test_an_empty_config_value_is_not_unlimited(self) -> None:
        """YAML 的 `query_timeout_sec:`（沒寫值）會變成 None。

        把它讀成「不設上限」等於一個看起來無害的空行就能關掉整道防護，
        因此空值一律是「這一層沒設」，往下一層找。
        """
        cfg_empty_global = {"bigquery": {"query_timeout_sec": None}}
        self.assertEqual(
            resolve_query_timeout(app_cfg={}, cfg=cfg_empty_global, env={}),
            DEFAULT_QUERY_TIMEOUT_SEC,
        )
        # app 層留空 → 掉回全域，而不是取消上限。
        self.assertEqual(
            resolve_query_timeout(
                app_cfg={"bq_query_timeout_sec": None},
                cfg={"bigquery": {"query_timeout_sec": 45}},
                env={},
            ),
            45.0,
        )
        # 引號空字串與只有空白同理。
        for raw in ("", "   "):
            with self.subTest(raw=raw):
                self.assertEqual(
                    resolve_query_timeout(
                        app_cfg={"bq_query_timeout_sec": raw},
                        cfg={"bigquery": {"query_timeout_sec": 45}},
                        env={},
                    ),
                    45.0,
                )

    def test_every_level_left_empty_still_lands_on_the_default(self) -> None:
        """三層全空時必須回預設上限——絕不能一路空到「不設上限」。"""
        self.assertEqual(
            resolve_query_timeout(
                app_cfg={"bq_query_timeout_sec": None},
                cfg={"bigquery": {"query_timeout_sec": None}},
                env={ENV_QUERY_TIMEOUT: ""},
            ),
            DEFAULT_QUERY_TIMEOUT_SEC,
        )

    def test_nan_and_inf_are_rejected(self) -> None:
        """nan 的比較永遠是 False，會穿過所有邊界檢查；inf 則在換算 ms 時溢位。

        兩者都不是「不設上限」的寫法，必須在設定解析階段就擋下來，而不是等到
        `int(secs * 1000)` 爆掉或 `result(timeout=nan)` 行為未定義。
        """
        for raw in (float("nan"), float("inf"), float("-inf"), "nan", "inf", "-inf"):
            with self.subTest(raw=raw):
                with self.assertRaises(BQQueryConfigError) as ctx:
                    resolve_query_timeout(
                        app_cfg={"bq_query_timeout_sec": raw}, cfg={}, env={}
                    )
                self.assertIn("nan / inf", str(ctx.exception))

    def test_an_explicit_empty_timeout_argument_is_a_programming_error(self) -> None:
        """設定檔的空值往下找，但程式直接傳 `timeout=""` 是寫錯了，不是 unlimited。"""
        with self.assertRaises(BQQueryConfigError):
            execute_query(FakeClient(FakeJob()), "SELECT 1", timeout="")

    def test_a_broken_value_raises_instead_of_falling_back(self) -> None:
        """靜默退回預設 = 操作者以為設了 30 秒、實際跑 300 秒。"""
        for raw in ("abc", "30s", True, -1):
            with self.subTest(raw=raw):
                with self.assertRaises(BQQueryConfigError):
                    resolve_query_timeout(app_cfg={"bq_query_timeout_sec": raw}, cfg={}, env={})

    def test_the_error_names_the_setting_that_is_wrong(self) -> None:
        with self.assertRaises(BQQueryConfigError) as ctx:
            resolve_query_timeout(app_cfg={}, cfg={"bigquery": {"query_timeout_sec": "abc"}}, env={})
        self.assertIn("bigquery.query_timeout_sec", str(ctx.exception))


class TestBothEndsAreBounded(unittest.TestCase):
    """server 端與 client 端的上限必須同時出現——少任一端都還是會卡住。"""

    def test_server_side_job_timeout_is_set(self) -> None:
        client = FakeClient(FakeJob())
        execute_query(client, "SELECT 1", timeout=30)
        job_config = client.last_job_config
        self.assertIsNotNone(job_config, "缺 job_config：BigQuery 端沒有上限")
        # job_timeout_ms 是 API 的 int64 欄位，property 以字串回傳。
        self.assertEqual(int(job_config.job_timeout_ms), 30_000)

    def test_client_side_wait_is_bounded(self) -> None:
        job = FakeJob()
        execute_query(FakeClient(job), "SELECT 1", timeout=30)
        self.assertEqual(job.result_kwargs, {"timeout": 30.0})

    def test_max_results_is_forwarded_alongside_the_timeout(self) -> None:
        job = FakeJob()
        execute_query(FakeClient(job), "SELECT 1", max_results=200, timeout=30)
        self.assertEqual(job.result_kwargs, {"max_results": 200, "timeout": 30.0})

    def test_no_max_results_is_not_invented(self) -> None:
        """列數上限與時間上限是兩件事；順手補 max_results 會截斷 90 天資料。"""
        job = FakeJob()
        execute_query(FakeClient(job), "SELECT 1", timeout=30)
        self.assertNotIn("max_results", job.result_kwargs or {})

    def test_opting_out_removes_both_bounds_and_nothing_else(self) -> None:
        job = FakeJob()
        client = FakeClient(job)
        execute_query(client, "SELECT 1", max_results=5, timeout=None)
        self.assertIsNone(client.last_job_config)
        self.assertEqual(job.result_kwargs, {"max_results": 5})

    def test_an_explicit_timeout_beats_the_configured_one(self) -> None:
        job = FakeJob()
        execute_query(FakeClient(job), "SELECT 1", timeout=3)
        self.assertEqual((job.result_kwargs or {}).get("timeout"), 3.0)


class TestTimeoutCancelsTheJob(unittest.TestCase):
    """`result()` 逾時不會取消 job；沒有這一步就會留下 running job。"""

    def test_a_timeout_cancels_the_job_exactly_once(self) -> None:
        job = FakeJob(raise_timeout=True)
        with self.assertRaises(BQQueryTimeout):
            execute_query(FakeClient(job), "SELECT 1", timeout=15)
        self.assertEqual(job.cancel_calls, 1)

    def test_the_error_carries_what_an_operator_needs(self) -> None:
        job = FakeJob(raise_timeout=True, job_id="job-abc")
        with self.assertRaises(BQQueryTimeout) as ctx:
            execute_query(FakeClient(job), "SELECT 1", timeout=15)
        err = ctx.exception
        self.assertEqual(err.timeout_sec, 15.0)
        self.assertEqual(err.job_id, "job-abc")
        self.assertIsNone(err.cancel_error)
        self.assertIn("15", str(err))
        self.assertIn("job-abc", str(err))

    def test_a_cancel_request_that_was_not_sent_is_reported(self) -> None:
        """`cancel()` 回傳 False = 取消請求**沒送出**。

        把 no-exception 當成成功，訊息就會宣稱取消了一支其實還在 BigQuery 上跑的 job，
        而操作者會因此不去查它。
        """
        job = FakeJob(raise_timeout=True, cancel_returns=False)
        with self.assertRaises(BQQueryTimeout) as ctx:
            execute_query(FakeClient(job), "SELECT 1", timeout=15)
        self.assertEqual(job.cancel_calls, 1)
        self.assertIsNotNone(ctx.exception.cancel_error)
        self.assertIn("未送出", str(ctx.exception))

    def test_a_non_bool_cancel_return_is_not_assumed_to_be_success(self) -> None:
        """自訂 wrapper / 舊版 client 可能回 None：無法確認就不能樂觀當成成功。"""
        job = FakeJob(raise_timeout=True, cancel_returns=None)
        with self.assertRaises(BQQueryTimeout) as ctx:
            execute_query(FakeClient(job), "SELECT 1", timeout=15)
        self.assertIsNotNone(ctx.exception.cancel_error)
        self.assertIn("非 bool", str(ctx.exception.cancel_error or ""))

    def test_a_sent_request_is_not_described_as_a_finished_cancel(self) -> None:
        """BigQuery 的 cancel 是 best-effort：送出請求 != job 已經停了。"""
        job = FakeJob(raise_timeout=True, cancel_returns=True)
        with self.assertRaises(BQQueryTimeout) as ctx:
            execute_query(FakeClient(job), "SELECT 1", timeout=15)
        message = str(ctx.exception)
        self.assertIn("已送出取消請求", message)
        self.assertNotIn("已取消該 job", message)
        self.assertIsNone(ctx.exception.cancel_error)

    def test_a_failed_cancel_is_reported_not_swallowed(self) -> None:
        """取消失敗代表對面可能還在跑；逾時仍是主要事實，但這件事不能消失。"""
        job = FakeJob(raise_timeout=True, cancel_raises="permission denied")
        with self.assertRaises(BQQueryTimeout) as ctx:
            execute_query(FakeClient(job), "SELECT 1", timeout=15)
        self.assertEqual(ctx.exception.cancel_error, "permission denied")
        self.assertIn("permission denied", str(ctx.exception))

    def test_a_job_without_cancel_still_raises_the_timeout(self) -> None:
        job = _JobWithoutCancel()
        with self.assertRaises(BQQueryTimeout) as ctx:
            execute_query(FakeClient(job), "SELECT 1", timeout=15)
        self.assertIsNotNone(ctx.exception.cancel_error)

    def test_the_original_timeout_is_chained_for_debugging(self) -> None:
        job = FakeJob(raise_timeout=True)
        with self.assertRaises(BQQueryTimeout) as ctx:
            execute_query(FakeClient(job), "SELECT 1", timeout=15)
        self.assertIsInstance(ctx.exception.__cause__, FuturesTimeoutError)


class TestATimeoutIsNeverReadAsZeroCrashes(unittest.TestCase):
    """本項目真正要防的事故：逾時被當成「這段時間沒有崩潰」。"""

    def test_run_query_propagates_the_timeout_instead_of_returning_no_rows(self) -> None:
        """回空 list 會讓上層以為查到了 0 筆；必須讓例外往上走。"""
        job = FakeJob(raise_timeout=True)
        with self.assertRaises(BQQueryTimeout):
            run_query(FakeClient(job), "SELECT 1", timeout=10)

    def test_a_timeout_error_turns_the_period_kpi_into_an_error_state(self) -> None:
        """逾時進入既有 per-query 錯誤路徑後，KPI 必須是 error 而不是 0。"""
        timeout_err = {"events_20260901.overview": "BigQuery 查詢超過 30 秒上限 (job_id=job-abc)"}
        snap = transform_bq_period_snapshot(
            {"events_20260901": {}},
            ["android"],
            days=30,
            start_date=dt.date(2026, 8, 13),
            end_date=dt.date(2026, 9, 11),
            period_errors=timeout_err,
        )
        self.assertEqual(snap["status"], "error")
        self.assertIn("30 秒上限", str(snap.get("error_message") or ""))
        self.assertEqual(snap["kpi"]["crash_events"]["status"], "error")
        self.assertEqual(snap["kpi"]["affected_users"]["status"], "error")

    def test_the_same_snapshot_without_errors_is_not_an_error(self) -> None:
        """對照組：沒有錯誤時不得一律報 error，否則上面那條沒有鑑別力。"""
        snap = transform_bq_period_snapshot(
            {"events_20260901": {}},
            ["android"],
            days=30,
            start_date=dt.date(2026, 8, 13),
            end_date=dt.date(2026, 9, 11),
            period_errors={},
        )
        self.assertNotEqual(snap["status"], "error")
        self.assertNotEqual(snap["kpi"]["crash_events"]["status"], "error")


class TestEveryEntryPointGoesThroughTheGuard(unittest.TestCase):
    """兩個 helper 真的走 guarded path，且沒有第三條繞過去的路。"""

    def test_crashlytics_run_query_is_bounded_on_both_ends(self) -> None:
        job = FakeJob(rows=[{"events": 1}])
        client = FakeClient(job)
        rows = run_query(client, "SELECT 1", timeout=20)
        self.assertEqual(rows, [{"events": 1}])
        self.assertEqual(int(client.last_job_config.job_timeout_ms), 20_000)
        self.assertEqual((job.result_kwargs or {}).get("timeout"), 20.0)

    def test_sessions_run_query_keeps_its_row_cap_and_gains_a_timeout(self) -> None:
        job = FakeJob(rows=[{"total_sessions": 1}])
        client = FakeClient(job)
        rows = run_sessions_query(client, "SELECT 1", timeout=20)
        self.assertEqual(rows, [{"total_sessions": 1}])
        self.assertEqual(job.result_kwargs, {"max_results": 5000, "timeout": 20.0})
        self.assertEqual(int(client.last_job_config.job_timeout_ms), 20_000)

    def test_no_module_queries_bigquery_outside_bq_query(self) -> None:
        """機械掃描：`client.query(...)` 只能出現在 bq_query.py。

        沒有這條，下一支新查詢只要照著舊寫法寫，就會靜靜地繞過所有上限——
        而那種缺陷只在 GCP 真的慢下來的那天才會現形。
        """
        offenders: list[str] = []
        for path in sorted((ROOT / "crash_trend").rglob("*.py")):
            if path.name == "bq_query.py":
                continue
            for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"\bclient\.query\(|\bbq_client\.query\(", line):
                    offenders.append(f"{path.relative_to(ROOT)}:{idx}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "以下查詢繞過了 bq_query.execute_query（沒有 timeout / 沒有逾時取消）：\n"
            + "\n".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
