"""Issue #83: 錯誤層級 badge 的 tooltip 與單一 helper 契約。

這些測試的存在理由：FATAL / ANR / NON_FATAL 對非工程角色不直觀（尤其
NON_FATAL 常被誤讀為「不重要」），因此三者都必須帶說明；而說明一旦被
複製到多個 render 點就會漂移，所以同時鎖住「只有一個 helper」這件事。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.dashboard.assets import get_dashboard_styles
from crash_trend.dashboard.formatting import get_formatting_js
from crash_trend.dashboard.issues import get_issues_js
from crash_trend.dashboard.overview import get_overview_js

ERROR_TYPES = ("FATAL", "ANR", "NON_FATAL")


class TestErrorTypeBadgeHelper(unittest.TestCase):
    def test_helper_is_defined_exactly_once(self) -> None:
        """helper 必須只有一份定義，否則兩份文案會各自演化。"""
        producers = {
            "formatting": get_formatting_js(),
            "issues": get_issues_js(),
            "overview": get_overview_js(),
        }
        definitions = [n for n, js in producers.items() if "function getErrorTypeBadgeHtml" in js]
        self.assertEqual(definitions, ["formatting"], "helper 應僅定義於 formatting")

    def test_every_error_type_carries_a_tooltip(self) -> None:
        js = get_formatting_js()
        for et in ERROR_TYPES:
            self.assertIn(f'"{et}"', js, f"{et} 應有對應分支")
        # 每個分支都要透過 title 屬性提供說明
        self.assertGreaterEqual(js.count('title="【'), len(ERROR_TYPES))

    def test_both_render_sites_call_the_helper_and_do_not_reimplement_it(self) -> None:
        """Issues 列表與 Overview 預覽都必須呼叫 helper，且不得重建 class 對映。"""
        for name, js in (("issues", get_issues_js()), ("overview", get_overview_js())):
            with self.subTest(site=name):
                self.assertIn("getErrorTypeBadgeHtml(", js)
                self.assertNotIn("badge-fatal", js, f"{name} 不應自行拼 badge class")
                self.assertNotIn("badge-anr", js, f"{name} 不應自行拼 badge class")

    def test_nonfatal_badge_has_a_border_for_contrast(self) -> None:
        """NON_FATAL 用 subtle 背景，缺 border 在兩種主題下都會與底色黏在一起。"""
        css = get_dashboard_styles()
        idx = css.index(".badge-nonfatal {")
        rule = css[idx:css.index("}", idx)]
        self.assertIn("border:", rule)
        self.assertIn("var(--border)", rule, "border 顏色須使用主題變數,才能同時適用淺色與深色")
