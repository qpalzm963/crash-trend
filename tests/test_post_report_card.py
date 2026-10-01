"""月報卡片 payload 的契約測試（post_report.py → 聊天服務 /api/crash-report）。

這張卡每月發一次到團隊聊天室，是 crash-trend 對外最直接的輸出。測試守住三件事：

1. 週趨勢迷你圖的資料必須送出。月摘要本來就有 weekly_trend（normalize 產生），
   但卡片一度沒帶，聊天服務只好整塊不渲染——資料在、卻沒人看得到。
2. 儀表板按鈕必須是 canonical deep link（#overview?app=<app>）。舊格式 #<app>
   會被新版路由當成未知頁面而退回預設 app：多 app 部署時，第二個 app 的卡片會開到第一個 app。
3. 發卡前要先產生 TOP 1 的白話說明（pm_note），卡片才有給非工程師看的那一行。
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


class TestCardPayload(unittest.TestCase):
    """以實際 main() 組出的 payload 驗證，而不是只測輔助函式——卡片漏欄位的問題就出在組裝處。"""

    def _send(self, app_id: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "reports" / "data" / app_id
            data_dir.mkdir(parents=True)
            month = post_report.dt.date.today().strftime("%Y-%m")
            (data_dir / f"{month}.json").write_text(json.dumps({
                "kpis": {"events": 10, "users": 4},
                "top_issues": [],
                "priority_list": [],
                "weekly_trend": [{"week": "2026-W38", "events": 7, "platform": "ios"}],
            }), encoding="utf-8")

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
            return post.call_args.kwargs["json"]

    def test_payload_carries_weekly_trend(self):
        payload = self._send("second_app")
        self.assertEqual(payload["weekly_trend"], [{"week": "2026-W38", "events": 7}])

    def test_dashboard_link_targets_the_app_via_canonical_route(self):
        payload = self._send("second_app")
        self.assertEqual(payload["dashboard_url"], "http://dash:8787#overview?app=second_app")
        self.assertNotIn("#second_app", payload["dashboard_url"])


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
