"""月報卡片 payload 的契約測試（post_report.py → 聊天服務 /api/crash-report）。

這張卡每月發一次到團隊聊天室，是 crash-trend 對外最直接的輸出。測試守住三件事：

1. 週趨勢迷你圖的資料必須送出。月摘要本來就有 weekly_trend（normalize 產生），
   但卡片一度沒帶，聊天服務只好整塊不渲染——資料在、卻沒人看得到。
2. 儀表板按鈕必須是 canonical deep link（#overview?app=<app>）。舊格式 #<app>
   會被新版路由當成未知頁面而退回預設 app：多 app 部署時，第二個 app 的卡片會開到第一個 app。
3. 發卡前要先產生 TOP 1 的白話說明（pm_note），卡片才有給非工程師看的那一行。
4. 週趨勢的資料來源要真的存在。抓取端不再跑週彙總查詢，月摘要的 weekly_trend 必須由
   daily_trend 換算，否則第 1 點只是送出一個永遠空的清單。
5. 什麼資料都沒抓到時不發卡。全部來源失敗時摘要的數字全是 0，發出去就變成「本月 0 次當機」。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crash_trend"))

import check_surge  # noqa: E402
import normalize  # noqa: E402
import post_report  # noqa: E402


class TestBuildWeeklyTrend(unittest.TestCase):
    def test_sums_platforms_per_week_and_sorts(self):
        """weekly_trend 在摘要裡是各平台分列；卡片要的是跨平台總量，否則同一週會畫成兩個點。"""
        summary = {"weekly_trend": [
            {"week": "2026-W38", "events": 3, "platform": "ios"},
            {"week": "2026-W37", "events": 1, "platform": "android"},
            {"week": "2026-W38", "events": 5, "platform": "android"},
        ]}
        self.assertEqual(
            post_report.build_weekly_trend(summary),
            [{"week": "2026-W37", "events": 1}, {"week": "2026-W38", "events": 8}],
        )

    def test_keeps_only_most_recent_weeks(self):
        summary = {"weekly_trend": [{"week": f"2026-W{w:02d}", "events": w} for w in range(1, 21)]}
        trend = post_report.build_weekly_trend(summary)
        self.assertEqual(len(trend), post_report.WEEKLY_TREND_WEEKS)
        self.assertEqual(trend[-1]["week"], "2026-W20")

    def test_missing_data_yields_empty_list(self):
        """沒有趨勢資料時送空清單——聊天服務對空清單不渲染該區塊，不會畫出一條假的 0 線。"""
        self.assertEqual(post_report.build_weekly_trend(None), [])
        self.assertEqual(post_report.build_weekly_trend({}), [])
        self.assertEqual(post_report.build_weekly_trend({"weekly_trend": [{"events": 3}]}), [])


class _CardSender:
    """以實際 main() 組卡的共用輔助：卡片漏欄位的問題就出在組裝處，不能只測輔助函式。"""

    def _send(self, app_id: str, summary: dict | None = None, v2_app: dict | None = None,
              bq: dict | None = None) -> dict | None:
        """以 main() 實際組卡；回傳送出的 payload，沒送出則回傳 None。

        給 v2_app 時不寫月摘要，改寫 out/<app>/dashboard_v2.json，走 V2 fallback 路徑。
        給 bq 時寫成 out/<app>/crashlytics_bq.json（本次 BigQuery 抓取結果）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "reports" / "data" / app_id
            data_dir.mkdir(parents=True)
            month = post_report.dt.date.today().strftime("%Y-%m")
            (root / "out" / app_id).mkdir(parents=True)
            if bq is not None:
                (root / "out" / app_id / "crashlytics_bq.json").write_text(json.dumps(bq), encoding="utf-8")
            if v2_app is not None:
                (root / "out" / app_id / "dashboard_v2.json").write_text(json.dumps(v2_app), encoding="utf-8")
            elif summary is None:
                summary = {
                    "kpis": {"events": 10, "users": 4},
                    "top_issues": [],
                    "priority_list": [],
                    "weekly_trend": [{"week": "2026-W38", "events": 7, "platform": "ios"}],
                }
            if v2_app is None:
                (data_dir / f"{month}.json").write_text(json.dumps(summary), encoding="utf-8")

            response = MagicMock(status_code=200)
            response.json.return_value = {"space": "s"}
            env = {"CRASH_REPORT_URL": "http://chat/api/crash-report", "INTERNAL_API_TOKEN": "t",
                   "DASHBOARD_URL": "http://dash:8787"}
            with patch.object(post_report, "ROOT", root), \
                 patch.object(post_report, "get_app", return_value={"display_name": app_id}), \
                 patch.object(post_report.requests, "post", return_value=response) as post, \
                 patch.dict(os.environ, env, clear=False), \
                 patch.object(sys, "argv", ["post_report.py", "--app", app_id]):
                post_report.main()
            return post.call_args.kwargs["json"] if post.called else None


class TestCardPayload(_CardSender, unittest.TestCase):

    def test_payload_carries_weekly_trend(self):
        payload = self._send("second_app")
        self.assertEqual(payload["weekly_trend"], [{"week": "2026-W38", "events": 7}])

    def test_dashboard_link_targets_the_app_via_canonical_route(self):
        payload = self._send("second_app")
        self.assertEqual(payload["dashboard_url"], "http://dash:8787#overview?app=second_app")
        self.assertNotIn("#second_app", payload["dashboard_url"])


class TestNoDataNoCard(_CardSender, unittest.TestCase):
    """要擋的只有一種情況：卡片寫「本月 0 次當機」，實際上是什麼都沒抓到。

    規則：當機數 > 0 照發；為 0 時只有確認算出這個 0 的查詢（每個平台的 top_issues）都成功，
    或有手動匯出，才發。下方各測試對應這條規則曾被繞過的實際路徑。
    """

    ZERO = {"kpis": {"events": 0, "users": 0}, "top_issues": [], "priority_list": []}

    def _zero(self, **sources):
        return {**self.ZERO, "sources": sources}

    def test_all_sources_failed_skips_the_card(self):
        summary = self._zero(crashlytics_bq=False, mcp_report=False, manual_console=False)
        self.assertIsNone(self._send("blind_app", summary))

    BQ_OK = {"tables": {"x_IOS": {"top_issues": [], "by_device": []}, "x_ANDROID": {"top_issues": []}}}

    def test_genuinely_quiet_month_with_working_bigquery_still_posts(self):
        """每個平台的 top_issues 都查詢成功、只是真的沒事件——那是好消息，要發。"""
        self.assertIsNotNone(self._send("quiet_app", self._zero(crashlytics_bq=True), bq=self.BQ_OK))

    def test_partial_bigquery_failure_does_not_vouch_for_zero(self):
        """一個平台的 top_issues 失敗、其他查詢成功：當機數少算一整個平台，0 不可信。"""
        partial = {
            "tables": {"x_IOS": {"by_device": [{"device_model": "d", "events": 3}]}, "x_ANDROID": {"top_issues": []}},
            "errors": {"x_IOS.top_issues": "timeout"},
        }
        self.assertIsNone(self._send("partial_app", self._zero(crashlytics_bq=True), bq=partial))

    def test_kpi_query_success_requires_top_issues_on_every_table(self):
        self.assertTrue(normalize.bq_kpi_queries_succeeded(self.BQ_OK))
        self.assertFalse(normalize.bq_kpi_queries_succeeded({"tables": {"x_IOS": {"by_device": []}}}))
        self.assertFalse(normalize.bq_kpi_queries_succeeded({"tables": {}}))
        self.assertFalse(normalize.bq_kpi_queries_succeeded(None))

    def test_cached_stack_traces_do_not_count_as_data(self):
        """stack trace 檔是上次成功留下的快取、不提供 KPI；不能讓它替一張全 0 的卡背書。"""
        summary = self._zero(crashlytics_bq=False, mcp_report=False, manual_console=False, mcp_crashlytics=True)
        self.assertIsNone(self._send("stale_app", summary))

    def test_zero_issue_mcp_report_is_not_trusted(self):
        """刻意的取捨：mcp_report 為 0 筆時，無法分辨是健康的空結果還是跨次保留的舊檔。

        寧可少發一張「0 次當機」的好消息卡，也不發一張可能是假的。
        """
        self.assertIsNone(self._send("mcp_only_app", self._zero(crashlytics_bq=False, mcp_report=False)))

    def test_nonzero_events_always_post(self):
        """有當機數代表確實取得了資料；來源旗標怎麼記都不影響發卡。"""
        summary = {"kpis": {"events": 3, "users": 1}, "top_issues": [], "priority_list": [],
                   "sources": {"crashlytics_bq": False}}
        self.assertIsNotNone(self._send("busy_app", summary))

    def test_legacy_summary_without_sources_still_posts(self):
        """舊月摘要沒有 sources 欄位：有當機數就照發，不因新增的檢查讓既有部署停止發卡。"""
        summary = {"kpis": {"events": 3, "users": 1}, "top_issues": [], "priority_list": []}
        self.assertIsNotNone(self._send("legacy_app", summary))

    def test_all_bq_queries_failed_is_not_a_source(self):
        """抓取端在查詢全失敗時仍會留下空的表位置；那不是「有資料」，下游不得據此發卡。"""
        failed = {"tables": {"x_IOS": {}, "x_ANDROID": {}}, "errors": {"x_IOS.top_issues": "timeout"}}
        zero_rows = {"tables": {"x_IOS": {"top_issues": [], "daily_trend": []}}}
        self.assertFalse(normalize.bq_has_results(failed))
        # 查詢成功但 0 列＝真的沒當機，仍算可用（該發卡報好消息）
        self.assertTrue(normalize.bq_has_results(zero_rows))
        self.assertFalse(normalize.bq_has_results(None))

    def test_v2_fallback_zero_events_is_not_trusted(self):
        """V2 bundle 的 crashlytics_bq.status 只看表是否存在，查詢全失敗時仍是 available。"""
        failed_but_available = {
            "sources": {"crashlytics_bq": {"status": "available"}},
            "kpi": {"crash_events": {"value": 0}, "affected_users": {"value": 0}},
            "top_issues": [], "daily_trend": [],
        }
        self.assertIsNone(self._send("v2_blind_app", v2_app=failed_but_available))
        self.assertTrue(post_report.card_data_is_trustworthy(None, {"kpi": {"crash_events": {"value": 4}}}))


class TestV2FallbackCard(_CardSender, unittest.TestCase):
    def test_v2_fallback_card_still_carries_weekly_trend(self):
        """沒有月摘要、改由 V2 聚合資料組卡時，週趨勢不得整塊消失——daily_trend 足以換算。"""
        v2_app = {
            "sources": {"crashlytics_bq": {"status": "available"}},
            "kpi": {"crash_events": {"value": 7}, "affected_users": {"value": 3}},
            "top_issues": [],
            "daily_trend": [
                {"date": "2026-09-14", "crash_events": 2},
                {"date": "2026-09-22", "crash_events": 5},
            ],
        }
        payload = self._send("v2_app", v2_app=v2_app)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["weekly_trend"], [{"week": "2026-37", "events": 2}, {"week": "2026-38", "events": 5}])


class TestWeeklyTrendFromDaily(unittest.TestCase):
    def test_daily_rows_are_summed_per_week(self):
        rows = [
            {"date": "2026-09-14", "events": 2},  # 週一
            {"date": "2026-09-20", "events": 3},  # 同週週日
            {"date": "2026-09-21", "events": 5},  # 下一週週一
        ]
        self.assertEqual(
            normalize.weekly_from_daily(rows),
            [{"week": "2026-37", "events": 5}, {"week": "2026-38", "events": 5}],
        )

    def test_week_key_matches_surge_detection_across_year_boundary(self):
        """卡片週趨勢與暴增偵測必須用同一套週切法，否則同一週在兩處會是不同的 key。

        跨年那週最容易分岔：若以每天自己的 %W 計，12/29–12/31 與 1/1–1/4 會被拆成
        2025-52 與 2026-00 兩個不完整的點。
        """
        rows = [{"date": d, "events": i + 1} for i, d in enumerate(["2025-12-29", "2026-01-01", "2026-01-04", "2026-01-05"])]
        ours = {r["week"]: r["events"] for r in normalize.weekly_from_daily(rows)}
        surge = check_surge.weekly_totals_from_daily([{"date": r["date"], "crash_events": r["events"]} for r in rows])
        self.assertEqual(ours, surge)
        self.assertEqual(len(ours), 2)

    def test_users_are_not_summed_across_days(self):
        """同一人在不同天出現會被重複計算；週層級的去重人數無法由日資料還原，寧可不給。"""
        rows = [{"date": "2026-09-14", "events": 1, "users": 1}, {"date": "2026-09-15", "events": 1, "users": 1}]
        self.assertNotIn("users", normalize.weekly_from_daily(rows)[0])

    def test_bq_output_without_weekly_query_gets_weekly_from_widest_period(self):
        """抓取端沒有週彙總查詢時，改由涵蓋最久的那期 daily_trend 換算。"""
        bq = {
            "tables": {"x_IOS": {"daily_trend": [{"date": "2026-09-15", "events": 1}]}},
            "periods": {
                "7": {"tables": {"x_IOS": {"daily_trend": [{"date": "2026-09-15", "events": 1}]}}},
                "90": {"tables": {"x_IOS": {"daily_trend": [
                    {"date": "2026-08-04", "events": 4}, {"date": "2026-09-15", "events": 1},
                ]}}},
            },
        }
        weekly = normalize.bq_issues_to_unified(bq)[3]
        self.assertEqual([w["week"] for w in weekly], ["2026-31", "2026-37"])
        self.assertTrue(all(w["platform"] == "ios" for w in weekly))

    def test_existing_weekly_trend_is_kept(self):
        """仍有週彙總資料的來源（例如較舊的抓取結果）照用，不被日資料換算覆蓋。"""
        bq = {"tables": {"x_ANDROID": {
            "weekly_trend": [{"week": "2026-30", "events": 9, "users": 2}],
            "daily_trend": [{"date": "2026-09-15", "events": 1}],
        }}}
        weekly = normalize.bq_issues_to_unified(bq)[3]
        self.assertEqual(weekly, [{"week": "2026-30", "events": 9, "users": 2, "platform": "android"}])


class TestWeeklySyncCardOrder(unittest.TestCase):
    def test_pm_brief_runs_before_each_card_is_posted(self):
        """pm_note 由 pm_brief 寫進月摘要、post_report 再讀出送卡；順序顛倒卡片就永遠沒有白話。"""
        script = (ROOT / "scripts" / "weekly_sync.sh").read_text(encoding="utf-8")
        pm = script.find('crash_trend/pm_brief.py" --app "$app"')
        post = script.find('crash_trend/post_report.py" --app "$app"')
        self.assertNotEqual(pm, -1, "weekly_sync.sh 未呼叫 pm_brief")
        self.assertNotEqual(post, -1)
        self.assertLess(pm, post)


if __name__ == "__main__":
    unittest.main()
