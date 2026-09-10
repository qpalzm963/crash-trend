"""首屏 Top Issues 的欄位、排序與行動入口 (Issue #74)。

這些測試存在的理由：

1. **排序必須重用既有 deterministic priority，而非另建一套 UI ranking**（#74 明列
   的非目標）。release regression 已經以 ``regressed_boost`` 計入
   ``priority.score``（``analyze_gemini.calculate_priority()``），所以「依
   ``priority.score`` 遞減」同時滿足「反映 regression」與「重用既有 priority」。
   這裡同時反向證明那個 boost 真的在分數裡，否則這個說法只是空話。
2. **「複製修復 Prompt」在沒有 AI 資料時必須消失，而不是壞掉。** 一顆按下去只複製
   出空殼 prompt 的按鈕比沒有按鈕更糟——使用者會把它貼給工程師。
3. **「查看分析」必須走 #78 的 canonical helper。** 自行拼 hash 會在參數順序或
   encode 規則改動時安靜地落到預設首頁。
4. **#83 的單一 helper 契約與 #75 的可達性契約不得被本單破壞。**
"""

from __future__ import annotations

import copy
import html as html_mod
import json
import re
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.analyze_gemini import calculate_priority
from crash_trend.dashboard import navigation as nav
from crash_trend.dashboard import overview
from crash_trend.dashboard.assets import get_dashboard_styles
from crash_trend.dashboard.formatting import get_formatting_js
from crash_trend.dashboard.issues import get_issues_js
from tests.test_overview_comparison import OverviewClientRuntime

FIXTURE = ROOT / "tests" / "fixtures" / "dashboard_v2_release_decision.json"

#: #74 驗收條件要求首屏 Top Issues 顯示的資訊。
REQUIRED_COLUMNS = ("等級", "趨勢", "生命週期", "受影響用戶", "最近出現", "操作")

#: dump 首屏 Top Issues 的 tbody 內容。
STEPS_DUMP_TOP_ISSUES = r"""
  report.steps.tbody = elements['topIssuesPreviewBody'].innerHTML;
  report.steps.byApp = {};
  const APPS = routeAppsData();
  Object.keys(APPS).forEach(aid => {
    switchApp(aid);
    report.steps.byApp[aid] = elements['topIssuesPreviewBody'].innerHTML;
  });
"""


def rows(tbody: str) -> list[str]:
    return re.findall(r"<tr\b.*?</tr>", tbody, re.DOTALL)


def row_issue_ids(tbody: str) -> list[str]:
    return re.findall(r'data-top-issue-id="([^"]*)"', tbody)


def analysis_hrefs(tbody: str) -> list[str]:
    return [
        html_mod.unescape(h)
        for h in re.findall(r'class="top-issue-action" href="([^"]*)"', tbody)
    ]


class TestPreviewColumnsCoverWhatTheTicketRequires(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = overview.get_overview_html()
        cls.js = overview.get_overview_js()

    def test_every_required_column_has_a_header(self) -> None:
        for col in REQUIRED_COLUMNS:
            with self.subTest(column=col):
                self.assertIn(f"<th>{col}</th>", self.html)

    def test_empty_state_colspan_matches_the_column_count(self) -> None:
        """欄位數與 colspan 讀同一份常數，否則空狀態會排錯位而沒人發現。"""
        self.assertEqual(
            self.html.count("<th>"),
            overview.TOP_ISSUES_COLUMN_COUNT,
            "表頭欄位數必須等於 TOP_ISSUES_COLUMN_COUNT",
        )
        self.assertIn(f'colspan="{overview.TOP_ISSUES_COLUMN_COUNT}"', self.js)

    def test_preview_reads_the_lifecycle_and_trend_fields(self) -> None:
        self.assertIn("getLifecycleBadgeHtml(iss.lifecycle)", self.js)
        self.assertIn("getTrendBadgeHtml(iss.priority?.trend)", self.js)
        self.assertIn("fmt(iss.affected_users)", self.js)
        self.assertIn("last_seen_timestamp", self.js)


class TestSharedBadgeHelperContractsSurvive(unittest.TestCase):
    """#83：badge helper 只能有一份定義，render 點不得自行拼 class。"""

    def test_trend_helper_is_defined_only_in_formatting(self) -> None:
        producers = {
            "formatting": get_formatting_js(),
            "issues": get_issues_js(),
            "overview": overview.get_overview_js(),
        }
        definitions = [n for n, js in producers.items() if "function getTrendBadgeHtml" in js]
        self.assertEqual(definitions, ["formatting"], "helper 應僅定義於 formatting")

    def test_overview_does_not_hand_assemble_trend_or_error_badge_classes(self) -> None:
        js = overview.get_overview_js()
        for cls in ("badge-fatal", "badge-anr", "badge-trend-new", "badge-trend-worsening"):
            with self.subTest(css_class=cls):
                self.assertNotIn(cls, js, "Overview 不應自行拼 badge class")

    def test_every_trend_value_is_styled(self) -> None:
        css = get_dashboard_styles()
        for trend in ("new", "worsening", "improving", "stable"):
            with self.subTest(trend=trend):
                self.assertIn(f"badge-trend-{trend}", css)

    def test_trend_tooltips_do_not_borrow_triage_priority_vocabulary(self) -> None:
        """#83：error severity / trend / P0-P3 priority 是三個獨立模型，文案不得混談。"""
        js = get_formatting_js()
        block = js[js.index("function getTrendBadgeHtml") : js.index("function getErrorTypeBadgeHtml")]
        tooltips = re.findall(r'title="(【趨勢[^"]*)"', block)
        self.assertGreaterEqual(len(tooltips), 4, "四種 trend 都必須帶說明")
        for word in ("優先級", "優先順序", "priority", "P0", "P1", "P2", "P3"):
            for tip in tooltips:
                with self.subTest(word=word):
                    self.assertNotIn(word, tip)


class TestCopyPromptReachabilityContract(unittest.TestCase):
    """#75：issue 層的 AI 行動不得只存在於「系統」工作區。"""

    def test_copy_fix_prompt_is_still_reachable_outside_the_system_workspace(self) -> None:
        self.assertIn("copyFixPrompt(", get_issues_js())
        self.assertIn("copyFixPrompt(", overview.get_overview_js())

    def test_overview_is_not_a_system_workspace_view(self) -> None:
        """前提：把 copy-prompt 加到 Overview 之所以不違反 #75，是因為 Overview 不在 System。"""
        system_views = {
            p.view for item in nav.NAV_ITEMS if item.workspace == "system" for p in item.panels
        }
        self.assertNotIn("overview", system_views)

    def test_overview_reuses_the_existing_copy_prompt_function(self) -> None:
        """Overview 不得自建一份 prompt 組裝邏輯——那會與 Issues 的版本各自演化。"""
        js = overview.get_overview_js()
        self.assertNotIn("function copyFixPrompt", js)
        self.assertIn("copyFixPrompt('${esc(iss.issue_id)}', '${esc(iss.platform)}')", js)


class TestOrderingReusesTheExistingDeterministicPriority(unittest.TestCase):
    """排序語意的來源證明：regression 已在 priority.score 內。"""

    def test_regressed_lifecycle_raises_the_existing_priority_score(self) -> None:
        """反向證明「依 priority.score 排序已反映 regression」不是空話。

        兩個各方面相同的 issue，只差 lifecycle.status；regressed 的那個分數必須更高。
        沒有這條，本單就會需要一套 UI-only 的 regression 分桶——那是明列的非目標。
        """
        base: dict[str, Any] = {
            "issue_id": "x",
            "title": "t",
            "events": 100,
            "affected_users": 50,
            "error_type": "NON_FATAL",
            "last_seen_version": "1.0.0",
        }
        plain = calculate_priority(
            dict(base, lifecycle={"status": "persistent"}),
            max_users=50,
            max_events=100,
            prev_issue={"events": 100},
        )
        regressed = calculate_priority(
            dict(base, lifecycle={"status": "regressed"}),
            max_users=50,
            max_events=100,
            prev_issue={"events": 100},
        )
        self.assertEqual(regressed["score_breakdown"]["regressed_boost"], 2)
        self.assertEqual(plain["score_breakdown"]["regressed_boost"], 0)
        self.assertGreater(regressed["score"], plain["score"])

    def test_preview_sorts_by_the_contract_score_and_does_not_compute_its_own(self) -> None:
        js = overview.get_overview_js()
        self.assertIn("iss.priority ? iss.priority.score : null", js)
        # 不得在前端重算分數：這幾個既有的 boost 名稱只能出現在 Python 端。
        for token in ("regressed_boost", "fatal_anr_boost", "worsening_boost", "core_path_boost"):
            with self.subTest(token=token):
                self.assertNotIn(token, js, "Overview 不得重建 priority 計分")


class TestTopIssuesRuntime(OverviewClientRuntime):
    """在 Node 內執行真正產出的 client JS。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_rows_are_ordered_by_the_existing_priority_score(self) -> None:
        """把 fixture 的順序刻意打亂，畫面順序仍必須是 priority.score 遞減。"""
        bundle = copy.deepcopy(self.bundle)
        issues = bundle["apps"]["shop_app"]["top_issues"]
        bundle["apps"]["shop_app"]["top_issues"] = list(reversed(issues))
        for snap in (bundle["apps"]["shop_app"].get("periods") or {}).values():
            if isinstance(snap, dict) and isinstance(snap.get("top_issues"), list):
                snap["top_issues"] = list(reversed(snap["top_issues"]))

        steps = self.render(bundle, STEPS_DUMP_TOP_ISSUES)
        rendered = row_issue_ids(steps["tbody"])
        self.assertTrue(rendered, "首屏必須有 Top Issues 列")

        by_id = {i["issue_id"]: i for i in issues}
        scores = [by_id[iid]["priority"]["score"] for iid in rendered]
        self.assertEqual(scores, sorted(scores, reverse=True), f"順序未依 priority.score 遞減：{scores}")

    def test_equal_scores_are_ordered_deterministically(self) -> None:
        """同分時必須有穩定決勝，否則同一份資料會渲染出兩種順序。"""
        bundle = copy.deepcopy(self.bundle)
        for app in bundle["apps"].values():
            for issue in app.get("top_issues") or []:
                issue["priority"]["score"] = 50
            for snap in (app.get("periods") or {}).values():
                if isinstance(snap, dict):
                    for issue in snap.get("top_issues") or []:
                        issue["priority"]["score"] = 50

        first = row_issue_ids(self.render(bundle, STEPS_DUMP_TOP_ISSUES)["tbody"])
        shuffled = copy.deepcopy(bundle)
        for app in shuffled["apps"].values():
            if isinstance(app.get("top_issues"), list):
                app["top_issues"] = list(reversed(app["top_issues"]))
            for snap in (app.get("periods") or {}).values():
                if isinstance(snap, dict) and isinstance(snap.get("top_issues"), list):
                    snap["top_issues"] = list(reversed(snap["top_issues"]))
        second = row_issue_ids(self.render(shuffled, STEPS_DUMP_TOP_ISSUES)["tbody"])
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))

    def test_each_row_shows_priority_users_trend_lifecycle_and_last_seen(self) -> None:
        bundle = copy.deepcopy(self.bundle)
        issues = bundle["apps"]["shop_app"]["top_issues"]
        for issue in issues:
            issue["lifecycle"] = {
                "status": "regressed",
                "latest_version": "3.2.0",
                "first_seen_version": "3.1.0",
                "last_seen_version": "3.2.0",
                "versions_seen": 2,
                "confidence": "high",
                "previously_absent_since": "3.1.2",
                "reappeared_version": "3.2.0",
                "reason": "在 3.1.2 消失後於 3.2.0 復發",
            }
        for snap in (bundle["apps"]["shop_app"].get("periods") or {}).values():
            if isinstance(snap, dict):
                for issue in snap.get("top_issues") or []:
                    issue["lifecycle"] = dict(issues[0]["lifecycle"])

        tbody = self.render(bundle, STEPS_DUMP_TOP_ISSUES)["tbody"]
        top = issues[0]
        self.assertIn(f'>{top["priority"]["level"]}<', tbody)
        self.assertIn("🟣 回歸", tbody)
        self.assertIn("🆕 新出現", tbody)
        self.assertIn(top["last_seen_timestamp"][:10], tbody)
        self.assertIn(f'v{top["last_seen_version"]}', tbody)

    def test_analysis_entry_uses_the_canonical_deep_link_helper(self) -> None:
        """畫面上的連結必須與 #78 Python 端 helper 逐位元相同。"""
        steps = self.render(self.bundle, STEPS_DUMP_TOP_ISSUES)
        hrefs = analysis_hrefs(steps["byApp"]["shop_app"])
        self.assertTrue(hrefs, "每一列都必須有「查看分析」入口")
        by_id = {i["issue_id"]: i for i in self.bundle["apps"]["shop_app"]["top_issues"]}
        expected = [
            nav.build_deep_link_fragment(
                "issues",
                app="shop_app",
                platform=by_id[iid]["platform"],
                version=by_id[iid]["last_seen_version"],
            )
            for iid in row_issue_ids(steps["byApp"]["shop_app"])
        ]
        self.assertEqual(hrefs, expected)

    def test_copy_prompt_entry_appears_when_ai_fix_data_exists(self) -> None:
        tbody = self.render(self.bundle, STEPS_DUMP_TOP_ISSUES)["tbody"]
        self.assertIn("複製修復 Prompt", tbody)
        top_id = row_issue_ids(tbody)[0]
        self.assertIn(f"copyFixPrompt('{top_id}'", tbody)

    def test_copy_prompt_entry_degrades_gracefully_without_ai_fix_data(self) -> None:
        """沒有 AI 修復資料時不得留下一顆會複製空殼 prompt 的按鈕。"""
        bundle = copy.deepcopy(self.bundle)
        for app in bundle["apps"].values():
            for issue in app.get("top_issues") or []:
                issue["ai_analysis"] = {
                    "status": "unavailable",
                    "root_cause": None,
                    "suggested_fix": None,
                    "effort": None,
                    "confidence": None,
                    "reasoning_sources": None,
                }
            for snap in (app.get("periods") or {}).values():
                if isinstance(snap, dict):
                    for issue in snap.get("top_issues") or []:
                        issue["ai_analysis"] = {
                            "status": "unavailable",
                            "root_cause": None,
                            "suggested_fix": None,
                            "effort": None,
                            "confidence": None,
                            "reasoning_sources": None,
                        }

        steps = self.render(bundle, STEPS_DUMP_TOP_ISSUES)
        for app_id, tbody in steps["byApp"].items():
            with self.subTest(app=app_id):
                self.assertNotIn("複製修復 Prompt", tbody)
                self.assertNotIn("copyFixPrompt(", tbody)
                self.assertIn("無 AI 修復資料", tbody)
                # 其餘資訊仍必須在，降級不等於整列消失。
                self.assertTrue(row_issue_ids(tbody))
                self.assertTrue(analysis_hrefs(tbody))

    def test_ai_status_available_but_empty_content_is_also_degraded(self) -> None:
        """status 說 available 但根因/修復都是空的，同樣沒有可複製的內容。"""
        bundle = copy.deepcopy(self.bundle)
        for app in bundle["apps"].values():
            for issue in app.get("top_issues") or []:
                issue["ai_analysis"] = dict(issue["ai_analysis"], root_cause=None, suggested_fix=None)
            for snap in (app.get("periods") or {}).values():
                if isinstance(snap, dict):
                    for issue in snap.get("top_issues") or []:
                        issue["ai_analysis"] = dict(
                            issue["ai_analysis"], root_cause=None, suggested_fix=None
                        )
        tbody = self.render(bundle, STEPS_DUMP_TOP_ISSUES)["tbody"]
        self.assertNotIn("copyFixPrompt(", tbody)
        self.assertIn("無 AI 修復資料", tbody)

    def test_no_issues_renders_an_empty_state_spanning_every_column(self) -> None:
        bundle = copy.deepcopy(self.bundle)
        for app in bundle["apps"].values():
            app["top_issues"] = []
            for snap in (app.get("periods") or {}).values():
                if isinstance(snap, dict):
                    snap["top_issues"] = []
        tbody = self.render(bundle, STEPS_DUMP_TOP_ISSUES)["tbody"]
        self.assertIn(f'colspan="{overview.TOP_ISSUES_COLUMN_COUNT}"', tbody)
        self.assertIn("尚無問題資料", tbody)

    def test_preview_is_capped_at_the_documented_limit(self) -> None:
        bundle = copy.deepcopy(self.bundle)
        template = bundle["apps"]["shop_app"]["top_issues"][0]
        many = []
        for idx in range(overview.TOP_ISSUES_PREVIEW_LIMIT + 3):
            clone = copy.deepcopy(template)
            clone["issue_id"] = f"gen{idx:03d}"
            clone["priority"] = dict(clone["priority"], score=90 - idx)
            many.append(clone)
        bundle["apps"]["shop_app"]["top_issues"] = many
        for snap in (bundle["apps"]["shop_app"].get("periods") or {}).values():
            if isinstance(snap, dict) and isinstance(snap.get("top_issues"), list):
                snap["top_issues"] = copy.deepcopy(many)
        tbody = self.render(bundle, STEPS_DUMP_TOP_ISSUES)["tbody"]
        self.assertEqual(len(rows(tbody)), overview.TOP_ISSUES_PREVIEW_LIMIT)


if __name__ == "__main__":
    unittest.main()
