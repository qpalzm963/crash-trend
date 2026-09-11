"""Overview 視覺層級與版面契約 (Issue #102, #70 follow-up).

#102 的問題不是「功能有沒有存在」，而是「使用者第一眼看到什麼」。那種東西最容易在
後續改動中悄悄退化：多加一個區塊、改一個字級、把某張輔助卡加上陰影，畫面就又變回
「每個區塊同等重量」。因此這一檔把四件**可機械驗證**的事釘住：

1. **閱讀順序寫在 DOM 裡，不是靠 CSS 事後搬動。**
   `決策 → 為什麼 → 與上一版相比 → 現在該做什麼 → 輔助資訊 → 一般 KPI`
   這個順序就是 `get_overview_html()` 的組裝順序，因此可以直接用字串位置核對。
2. **層級是量出來的。** 四個 section 標題的字級與顏色 token 逐項比大小：
   決策 > action ≥ 比較 > 輔助。任何一次「順手放大輔助面板標題」都會轉紅。
3. **狀態不靠大面積色底，也不只靠顏色。** 決策卡的色調落在 badge 上（帶文字標籤），
   卡片本身不得用高飽和底色鋪滿；neutral 必須與 pass 不同色。
4. **窄版不得裁掉主要動作、也不得把頁面推寬。** 兩者都是這一單踩到的真實 bug：
   `.table-container` 原本是 `overflow: hidden`（10 欄的 Top Issues 在窄版會直接
   裁掉最右邊的「查看分析 / 複製修復 Prompt」），而 header 右叢的 flex item 沒有
   `min-width: 0`（768px 視窗量到 `scrollWidth` 994）。

5. **手機寬度的每個控制項都還能用。** header 放不下時換行而不是互相重疊；
   `<select>` 與搜尋框可以縮；重複呈現的來源徽章條是該讓的那一個。

另外釘一條通則：CSS 裡用到的每個 `var(--token)` 都必須在 `:root` 定義過。
`.comparison-metric-reason` 原本寫著未定義的 `--text-secondary`，結果次級說明文字
繼承成主文字色——「層級」就是這樣一個 typo 就沒了，而且畫面上看起來只是「比較黑」。
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.dashboard import overview
from crash_trend.dashboard.assets import get_dashboard_styles
from crash_trend.dashboard.navigation import get_switch_view_call

# 字級／規則解析沿用輔助面板那一檔的 helper：同一種解析只有一份，
# 否則兩檔的 CSS 解析遲早各自漂移。
from tests.test_overview_aux_panels import _font_size_px, _rule_body

FIXTURE = ROOT / "tests" / "fixtures" / "dashboard_v2_release_decision.json"

#: 首屏各面的 DOM 標記，順序即要求的閱讀順序。
READING_ORDER: tuple[tuple[str, str], ...] = (
    ("發布決策", 'id="overviewDecisionSection"'),
    ("與上一版相比", 'id="overviewComparisonSection"'),
    ("Top Issues", f'id="{overview.ACTION_SECTION_ID}"'),
    ("輔助資訊", 'id="overviewAuxSection"'),
    ("一般 KPI", 'class="kpi-grid"'),
    ("圖表", 'class="charts-grid"'),
)


class TestOverviewReadingOrder(unittest.TestCase):
    """順序是產品目標本身（#102「數秒內形成的閱讀順序」），因此逐對比較位置。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = overview.get_overview_html()

    def test_every_surface_is_present_exactly_once(self) -> None:
        for name, marker in READING_ORDER:
            with self.subTest(surface=name):
                self.assertEqual(self.html.count(marker), 1, f"{name} 不是恰好出現一次")

    def test_surfaces_appear_in_the_required_reading_order(self) -> None:
        positions = [(name, self.html.find(marker)) for name, marker in READING_ORDER]
        for (prev_name, prev_at), (name, at) in zip(positions, positions[1:], strict=False):
            with self.subTest(after=prev_name, before=name):
                self.assertLess(prev_at, at, f"{name} 不得排在 {prev_name} 之前")

    def test_the_action_surface_sits_between_comparison_and_auxiliary(self) -> None:
        """Top Issues 是主要 action surface：在「為什麼」之後、輔助資訊之前。"""
        comparison_at = self.html.find('id="overviewComparisonSection"')
        action_at = self.html.find(f'id="{overview.ACTION_SECTION_ID}"')
        aux_at = self.html.find('id="overviewAuxSection"')
        self.assertLess(comparison_at, action_at, "action surface 不得排在比較面之前")
        self.assertLess(action_at, aux_at, "lifecycle / gate history 不得排在 action surface 之前")

    def test_moving_the_preview_kept_its_wiring(self) -> None:
        """搬動區塊最容易掉的是連線：表身 id、表頭欄位、完整清單入口。"""
        action = self.html[self.html.find(f'id="{overview.ACTION_SECTION_ID}"'):]
        action = action[: action.find("</div>\n\n")]
        self.assertIn(f'id="{overview.TOP_ISSUES_BODY_ID}"', action)
        self.assertIn(get_switch_view_call("issues"), action)
        for column in overview.TOP_ISSUES_COLUMNS:
            with self.subTest(column=column):
                self.assertIn(column, action)


class TestSectionTitleHierarchyIsMeasured(unittest.TestCase):
    """字級與顏色 token 逐項比大小——「層級」在這個 repo 裡是可測的東西。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.css = get_dashboard_styles()

    def test_decision_is_the_largest_title_on_the_first_screen(self) -> None:
        decision = _font_size_px(self.css, ".decision-section-title")
        for selector in (
            ".comparison-section-title",
            ".action-section-title",
            ".aux-section-title",
        ):
            with self.subTest(selector=selector):
                self.assertGreater(
                    decision,
                    _font_size_px(self.css, selector),
                    "發布決策必須是首屏最大的標題",
                )

    def test_the_action_surface_outranks_the_auxiliary_information(self) -> None:
        """#102 驗收條件：Top Issues 的視覺優先級高於 lifecycle / gate history。"""
        self.assertGreater(
            _font_size_px(self.css, ".action-section-title"),
            _font_size_px(self.css, ".aux-section-title"),
        )

    def test_primary_surfaces_use_primary_text_and_auxiliary_does_not(self) -> None:
        for selector in (
            ".decision-section-title",
            ".comparison-section-title",
            ".action-section-title",
        ):
            with self.subTest(selector=selector):
                self.assertIn("var(--text-main)", _rule_body(self.css, selector))
        aux = _rule_body(self.css, ".aux-section-title")
        self.assertIn("var(--text-muted)", aux)
        self.assertNotIn("var(--text-main)", aux)

    def test_the_decision_version_is_the_largest_number_on_the_page(self) -> None:
        """#102 驗收條件：首屏第一視覺焦點是 Release Decision，而非一般 KPI。

        位置（決策在最上面、KPI 在輔助資訊之後）已經決定了閱讀順序，但字級不能反過來
        講另一個故事——`.kpi-value` 原本是 28px，比決策卡的版本號還大。
        """
        version = _font_size_px(self.css, ".decision-version")
        for selector in (".comparison-metric-change", ".kpi-value"):
            with self.subTest(selector=selector):
                self.assertGreaterEqual(version, _font_size_px(self.css, selector))


class TestStatusToneIsRestrainedAndLabelled(unittest.TestCase):
    """「不靠大面積高飽和底色」與「不只靠顏色辨識」都是 #102 明寫的驗收條件。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.css = get_dashboard_styles()

    def test_pass_warn_fail_cards_do_not_fill_the_card_with_a_status_colour(self) -> None:
        for tone in ("good", "warn", "bad"):
            with self.subTest(tone=tone):
                body = _rule_body(self.css, f".{overview.DECISION_TONE_CLASS_PREFIX}{tone}")
                self.assertIn("border-left-color", body, "色調仍要有可辨識的載體")
                self.assertNotIn(
                    "background",
                    body,
                    "狀態色不得鋪滿整張決策卡（restrained status tone）",
                )

    def test_every_tone_colours_the_labelled_badge(self) -> None:
        """色調落在 badge 上，而 badge 帶著 statusLabel 文字——因此不是只靠顏色。"""
        for tone in ("good", "warn", "bad", "neutral"):
            with self.subTest(tone=tone):
                selector = f".{overview.DECISION_TONE_CLASS_PREFIX}{tone} .decision-gate-badge"
                self.assertIn("color", _rule_body(self.css, selector))

    def test_the_neutral_badge_is_not_painted_like_pass(self) -> None:
        """insufficient_data / baseline 不得被讀成綠燈——這是正確性，不是樣式。"""
        good = _rule_body(
            self.css, f".{overview.DECISION_PASS_TONE_CLASS} .decision-gate-badge"
        )
        neutral = _rule_body(
            self.css, f".{overview.DECISION_NEUTRAL_TONE_CLASS} .decision-gate-badge"
        )
        self.assertNotEqual(good.strip(), neutral.strip())
        self.assertNotIn("--success", neutral)

    def test_every_rendered_state_still_prints_a_text_status_label(self) -> None:
        """色調之外必須有字：fixture 的四種狀態逐一檢查 badge 有非空文字。"""
        bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
        js = overview.get_release_decision_js()
        labels = re.search(r"DECISION_STATUS_LABELS\s*=\s*\{([^}]*)\}", js)
        self.assertIsNotNone(labels, "找不到狀態標籤表")
        assert labels is not None
        seen = set()
        for app in bundle["apps"].values():
            for release in app.get("release_catalog", []) or []:
                decision = (release.get("release_gate") or {}).get("decision") or {}
                status = decision.get("status")
                if status:
                    seen.add(status)
        self.assertTrue(seen, "fixture 應該帶著多種決策狀態")
        for status in sorted(seen):
            with self.subTest(status=status):
                self.assertIn(f"{status}:", labels.group(1), f"{status} 沒有文字標籤")


class TestComparisonTilesAreScannable(unittest.TestCase):
    """#102 scope 2：compact metric tiles，change 與 classification 要能快速掃讀。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.css = get_dashboard_styles()

    def test_metrics_are_laid_out_as_tiles_not_a_stacked_list(self) -> None:
        body = _rule_body(self.css, ".comparison-metrics")
        self.assertIn("display: grid", body)
        self.assertIn("grid-template-columns", body)

    def test_the_change_is_the_scannable_element_of_a_tile(self) -> None:
        """label 是說明、change 是要被掃到的那個數字。"""
        self.assertGreater(
            _font_size_px(self.css, ".comparison-metric-change"),
            _font_size_px(self.css, ".comparison-metric-label"),
        )

    def test_threshold_and_source_stay_secondary(self) -> None:
        for selector in (".comparison-metric-threshold", ".comparison-metric-source"):
            with self.subTest(selector=selector):
                body = _rule_body(self.css, selector)
                self.assertNotIn("var(--text-main)", body)
        self.assertLess(
            _font_size_px(self.css, ".comparison-metric-source"),
            _font_size_px(self.css, ".comparison-metric-change"),
        )

    def test_tiles_collapse_before_they_get_too_narrow_to_read(self) -> None:
        """窄版必須有收成單欄的規則，否則 tile 會被壓到只剩幾個字寬。"""
        self.assertRegex(
            self.css,
            r"@media[^{]*max-width:\s*1240px[^{]*\{\s*\.comparison-metrics\s*\{[^}]*grid-template-columns:\s*1fr",
        )


class TestNarrowViewportsKeepTheContentUsable(unittest.TestCase):
    """兩條都是這一單量出來的真實 bug，不是預防性斷言。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.css = get_dashboard_styles()

    def test_the_table_scrolls_instead_of_clipping_the_action_column(self) -> None:
        """`overflow: hidden` 會讓 10 欄表格在窄版直接裁掉最右邊的「操作」欄。"""
        body = _rule_body(self.css, ".table-container")
        self.assertIn("overflow-x: auto", body)
        self.assertNotRegex(body, r"overflow:\s*hidden")
        self.assertEqual(overview.TOP_ISSUES_COLUMNS[-1], "操作", "最右欄仍是操作欄")

    def test_the_header_clusters_can_shrink(self) -> None:
        """flex item 的 min-width 預設 auto：不設 0 的話，header 會把整頁推寬。"""
        body = _rule_body(self.css, ".header-left,\n  .header-right")
        self.assertIn("min-width: 0", body)

    def test_the_source_badge_strip_clips_instead_of_pushing_the_page(self) -> None:
        body = _rule_body(self.css, ".source-badges")
        self.assertIn("min-width: 0", body)
        self.assertIn("overflow: hidden", body)

    def test_the_first_screen_grids_collapse_to_one_column(self) -> None:
        m = re.search(
            r"@media[^{]*max-width:\s*900px[^{]*\{(.*?)\n  \}",
            self.css,
            re.DOTALL,
        )
        self.assertIsNotNone(m, "找不到 900px 的 breakpoint")
        assert m is not None
        block = m.group(1)
        for selector in (".decision-grid", ".comparison-grid", ".aux-grid"):
            with self.subTest(selector=selector):
                self.assertRegex(
                    block, re.escape(selector) + r"\s*\{[^}]*grid-template-columns:\s*1fr"
                )


class TestPhoneWidthsKeepEveryControlUsable(unittest.TestCase):
    """手機寬度（#103 review 的 blocker）。

    `min-width: 0` 只讓 header 的**父** flex item 可以縮，子控制項各自還有固定或
    min-content 寬度：`<select>` 被最長的 option 撐到 244px、搜尋框 140px、
    7/30/90 122px。390px 實際量到的結果是「控制項互相重疊 + 頁面水平溢出 96px」，
    不是單純被裁掉。

    而且這件事不能只靠一個手機 breakpoint：561px（剛好在 560 之上）原本會讓搜尋框
    壓在 7/30/90 上、theme toggle 被推到視窗外。因此 header 改成「放不下就換行」，
    breakpoint 只負責手機版的兩列排法。

    320~1920px 掃過 22 個寬度後的實測（iframe 內量，media query 會照寬度生效）：
    水平溢出全部 0、控制項零重疊、沒有任何控制項超出視窗。這裡把讓那個結果成立的
    CSS 條件逐條釘住。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.css = get_dashboard_styles()

    def _media_block(self, max_width: int) -> str:
        m = re.search(
            r"@media[^{]*max-width:\s*" + str(max_width) + r"px[^{]*\{(.*?)\n  \}",
            self.css,
            re.DOTALL,
        )
        self.assertIsNotNone(m, f"找不到 {max_width}px 的 breakpoint")
        assert m is not None
        return m.group(1)

    def test_the_header_wraps_instead_of_overlapping(self) -> None:
        """固定 height 會讓換行後的第二列溢出 header，因此必須是 min-height。"""
        body = _rule_body(self.css, "header.top-header")
        self.assertIn("flex-wrap: wrap", body)
        self.assertIn("min-height: var(--header-h)", body)
        self.assertNotRegex(
            body, r"(?<!min-)height:", "header 不得有固定高度，否則換行的那一列會溢出"
        )

    def test_the_two_stretchable_controls_can_actually_shrink(self) -> None:
        """`<select>` 與搜尋框是唯一該讓的兩個控制項；其餘寬度是固定的。"""
        for selector in (".app-select-wrap", ".app-selector", ".search-box"):
            with self.subTest(selector=selector):
                self.assertIn("min-width: 0", _rule_body(self.css, selector))
        search = _rule_body(self.css, ".search-input")
        self.assertIn("min-width: 0", search)
        self.assertIn("max-width: 100%", search)

    def test_the_phone_breakpoint_puts_the_clusters_on_their_own_rows(self) -> None:
        block = self._media_block(560)
        self.assertRegex(
            block,
            r"\.header-left,\s*\n\s*\.header-right \{[^}]*flex: 1 1 100%",
            "手機版 header 必須兩列",
        )
        self.assertRegex(block, r"\.app-selector \{[^}]*width: 100%")
        self.assertRegex(block, r"\.search-input,\s*\n\s*\.search-input:focus \{[^}]*width: 100%")

    def test_a_focused_search_box_cannot_grow_past_the_row_on_a_phone(self) -> None:
        """`.search-input:focus` 在 768 的區塊裡是 180px；手機版必須一起被覆蓋，
        否則點一下搜尋框就會把那一列推寬。"""
        self.assertIn("width: 180px", self._media_block(768))
        self.assertIn(".search-input:focus", self._media_block(560))

    def test_source_chips_never_wrap_their_own_label(self) -> None:
        """沒有 nowrap 時「Gemini AI」會折行，把 header 從 64px 撐成 90px。"""
        self.assertIn("white-space: nowrap", _rule_body(self.css, ".src-chip"))

    def test_the_duplicate_badge_strip_yields_before_the_real_controls(self) -> None:
        """徽章條是首屏「資料來源健康度」卡片的重複呈現，因此它是該讓的那一個。"""
        self.assertRegex(
            self._media_block(1360), r"\.source-badges \{[^}]*display: none"
        )


class TestThePolishDidNotInventData(unittest.TestCase):
    """#102 非目標：不新增假 rollout percentage、不新增第二套判定。"""

    def test_the_overview_never_mentions_rollout_coverage(self) -> None:
        """mockup 有 Rollout %／user coverage，但真實 rollout 資料屬於 #77。"""
        surface = overview.get_overview_html() + overview.get_overview_js()
        self.assertNotIn("rollout", surface.lower())

    def test_every_css_variable_used_is_actually_defined(self) -> None:
        """`--text-secondary` 這種 typo 會讓次級文字悄悄變成主文字色。"""
        css = get_dashboard_styles()
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
        used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
        self.assertTrue(defined, "解析不到任何 token 定義")
        self.assertEqual(used - defined, set(), "CSS 使用了未定義的 token")


if __name__ == "__main__":
    unittest.main()
