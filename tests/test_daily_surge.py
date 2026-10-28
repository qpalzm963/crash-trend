"""每日暴增檢查：昨天或前天，某平台事件數 ≥ 3 倍前 7 天平均且 ≥ 50 → 發到聊天室。

為什麼這樣設計（測試守的是這些理由，不只是數字）：
- 各平台分開算：大平台的正常量會稀釋小平台的暴增，合併計算會漏報。
- 看前天：BigQuery 匯出晚一天補齊，昨天的數字常常偏低；隔天用完整數字再判一次才不會漏。
- 基準為 0 或歷史不足不判：沒有正常量可比，倍數沒有意義，只會誤報。
- 同一天同一平台只發一次：每天都會跑，不能每天重發同一則告警。
- 發送失敗不記：下次還要再發，不能因為記了就永遠漏掉。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crash_trend"))

import check_surge  # noqa: E402

TODAY = dt.date(2026, 10, 1)


def day_rows(per_platform: dict[str, list[int]], end: dt.date = TODAY) -> list[dict]:
    """per_platform 每個 list 依時間排列，最後一個值是 end 當天。"""
    n = len(next(iter(per_platform.values())))
    rows = []
    for i in range(n):
        d = end - dt.timedelta(days=n - 1 - i)
        bp = {p: {"events": vals[i]} for p, vals in per_platform.items()}
        rows.append({"date": d.isoformat(), "crash_events": sum(v["events"] for v in bp.values()), "by_platform": bp})
    return rows


class TestFindDailySurges(unittest.TestCase):
    def test_spike_two_days_ago_is_found_even_when_yesterday_is_incomplete(self):
        # 前 7 天平均 100；前天 1000（10 倍）；昨天資料還沒補齊只有 30；今天 0
        rows = day_rows({"ios": [100] * 7 + [1000, 30, 0]})
        surges = check_surge.find_daily_surges(rows, TODAY)
        self.assertEqual([(s["date"], s["platform"]) for s in surges], [("2026-09-29", "ios")])
        self.assertEqual(surges[0]["ratio"], 10.0)

    def test_small_platform_spike_is_not_masked_by_large_platform(self):
        # Android 每天 2000；iOS 從 30 跳到 200（約 6.7 倍）。合併算只有 1.08 倍，會漏報
        rows = day_rows({"android": [2000] * 9 + [0], "ios": [30] * 8 + [200, 0]})
        surges = check_surge.find_daily_surges(rows, TODAY)
        self.assertEqual([(s["date"], s["platform"]) for s in surges], [("2026-09-30", "ios")])

    def test_small_counts_below_minimum_do_not_alert(self):
        # 2 → 40 是 20 倍，但不到 50 筆：小量 app 的雜訊
        rows = day_rows({"android": [2] * 8 + [40, 0]})
        self.assertEqual(check_surge.find_daily_surges(rows, TODAY), [])

    def test_ratio_below_three_does_not_alert(self):
        rows = day_rows({"android": [100] * 8 + [290, 0]})
        self.assertEqual(check_surge.find_daily_surges(rows, TODAY), [])

    def test_zero_baseline_does_not_alert(self):
        # 平台剛開始匯出：前 7 天都是 0，沒有正常量可比
        rows = day_rows({"ios": [0] * 8 + [500, 0]})
        self.assertEqual(check_surge.find_daily_surges(rows, TODAY), [])

    def test_platform_that_just_started_exporting_does_not_alert(self):
        # daily_trend 把匯出前的日子補成 0：基準 (0×5+100+100)/7≈29，正常的 100 不能被當成暴增
        rows = day_rows({"ios": [0] * 5 + [100, 100, 100, 0, 0]})
        self.assertEqual(check_surge.find_daily_surges(rows, TODAY), [])

    def test_quiet_days_inside_real_history_still_count(self):
        # 小量 app 平常有幾天 0 是真的沒閃退（不是沒匯出），暴增仍要抓到
        rows = day_rows({"android": [5, 0, 0, 3, 0, 0, 2, 0, 80, 0]})
        surges = check_surge.find_daily_surges(rows, TODAY)
        self.assertEqual([(s["date"], s["platform"]) for s in surges], [("2026-09-30", "android")])

    def test_missing_history_does_not_alert(self):
        # 只有 5 天資料，基準期不完整
        rows = day_rows({"ios": [10, 10, 10, 500, 0]})
        self.assertEqual(check_surge.find_daily_surges(rows, TODAY), [])

    def test_thresholds_are_configurable(self):
        rows = day_rows({"android": [100] * 8 + [250, 0]})
        surges = check_surge.find_daily_surges(rows, TODAY, ratio_min=2.0, events_min=50)
        self.assertEqual(len(surges), 1)


class TestTopIssueOn(unittest.TestCase):
    def test_picks_largest_issue_of_that_day_and_platform(self):
        bq = {"tables": {
            "app_IOS": {
                "top_issues": [{"issue_id": "a", "issue_title": "BigCrash"}, {"issue_id": "b", "issue_title": "Small"}],
                "issue_daily_trend": [
                    {"issue_id": "a", "date": "2026-09-29", "events": 900,
                     "versions_json": json.dumps([{"version": "2.0.0", "events": 890}, {"version": "1.9.0", "events": 10}])},
                    {"issue_id": "b", "date": "2026-09-29", "events": 100},
                    {"issue_id": "b", "date": "2026-09-28", "events": 5000},
                ],
            },
            "app_ANDROID_REALTIME": {"issue_daily_trend": [{"issue_id": "z", "date": "2026-09-29", "events": 99999}]},
        }}
        top = check_surge.top_issue_on(bq, "2026-09-29", "ios")
        self.assertEqual(top, {"title": "BigCrash", "events": 900, "version": "2.0.0"})
        # 表名平台不一定在結尾（例如 _ANDROID_REALTIME）
        self.assertEqual(check_surge.top_issue_on(bq, "2026-09-29", "android")["events"], 99999)


class TestDailyCheck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.odir = Path(self._tmp.name)
        self.rows = day_rows({"ios": [100] * 7 + [1000, 30, 0]})
        env = {"CHAT_WEBHOOK_DEMO_APP": "https://chat.invalid/hook"}
        self._env = patch.dict("os.environ", env, clear=False)
        self._env.start()
        self._app = patch.object(check_surge, "get_app", return_value={"display_name": "Demo"})
        self._app.start()

    def tearDown(self):
        self._app.stop()
        self._env.stop()
        self._tmp.cleanup()

    def test_alert_is_sent_once_per_day_and_platform(self):
        with patch.object(check_surge, "send_text") as send:
            self.assertIsNone(check_surge.daily_check("demo_app", self.odir, self.rows, TODAY))
            self.assertIsNone(check_surge.daily_check("demo_app", self.odir, self.rows, TODAY))
        self.assertEqual(send.call_count, 1)
        self.assertIn("Demo iOS 閃退暴增", send.call_args.args[1])

    def test_failed_send_is_reported_and_retried_next_run(self):
        with patch.object(check_surge, "send_text", side_effect=RuntimeError("boom")):
            err = check_surge.daily_check("demo_app", self.odir, self.rows, TODAY)
        self.assertIn("2026-09-29:ios", err)
        with patch.object(check_surge, "send_text") as send:
            self.assertIsNone(check_surge.daily_check("demo_app", self.odir, self.rows, TODAY))
        self.assertEqual(send.call_count, 1)

    def test_no_webhook_skips_without_error(self):
        with patch.dict("os.environ", {"CHAT_WEBHOOK_DEMO_APP": ""}), patch.object(check_surge, "send_text") as send:
            self.assertIsNone(check_surge.daily_check("demo_app", self.odir, self.rows, TODAY))
        send.assert_not_called()


class TestMainRunsDailyEvenIfWeeklyFails(unittest.TestCase):
    def test_weekly_send_failure_does_not_skip_daily_check(self):
        args = type("A", (), {"app": "demo_app", "days": 90})()
        with (
            patch.object(check_surge, "app_argparser") as ap,
            patch.object(check_surge, "out_dir", return_value=Path(tempfile.gettempdir())),
            patch.object(check_surge, "load_daily_trend", return_value=[]),
            patch.object(check_surge, "daily_check", return_value=None) as daily,
        ):
            ap.return_value.parse_args.return_value = args
            for failure in (SystemExit("[錯誤] 告警發送失敗 500"), ConnectionError("告警發送失敗：連不上")):
                with self.subTest(failure=type(failure).__name__):
                    daily.reset_mock()
                    with patch.object(check_surge, "weekly_check", side_effect=failure), self.assertRaises(SystemExit) as ctx:
                        check_surge.main()
                    daily.assert_called_once()
                    self.assertIn("告警發送失敗", str(ctx.exception.code))


if __name__ == "__main__":
    unittest.main()
