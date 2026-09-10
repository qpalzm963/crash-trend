"""Overview 輔助面板：問題生命週期摘要與品質閘門歷程 (Issue #91).

本檔要釘住的東西比「畫得出來」嚴格得多：

* **「沒有資料」不得被畫成 0。** ``issue_lifecycle`` 缺席（或缺其中一個計數）時
  必須明說沒有資料；退回 0 會把「沒統計到」讀成「這個版本沒有新引入問題」。
  因此本檔同時測「真的是 0 要印 0」與「沒有資料不能印 0」兩個方向——只測一邊
  的話，把整個面板改成永遠印「無資料」也會通過。
* **``insufficient_data`` / ``baseline`` 不得呈現為 PASS。** 這兩種狀態代表「還
  無法判定」，畫成綠色會讓沒被評估過的版本看起來像已驗證安全（#72／#73 修過
  兩次的同一類錯誤）。色調來源因此只有 #73 的 ``DECISION_TONES`` 一份，本檔
  以機械掃描確認這段 JS 沒有自己再列一次那五種狀態。
* **視覺層級低於發布決策面。** 這是 #70 的設計前提，不是品味問題：輔助資訊搶過
  主決策會改變讀者的注意力順序。因此以 CSS 字級／顏色／卡片裝飾逐項比對，而不是
  靠 code review 記得。

Node runtime 重用 ``tests.test_overview_comparison.OverviewClientRuntime``（其本身
重用 ``tests.test_dashboard_deep_link.NODE_RUNNER``）——不另建第三套 JS harness。
"""

from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.dashboard import navigation as nav
from crash_trend.dashboard import overview
from crash_trend.dashboard.assets import get_dashboard_styles, get_shell_js_bottom
from crash_trend.schema_v2 import validate_dashboard_v2
from tests.test_overview_comparison import OverviewClientRuntime

FIXTURE = ROOT / "tests" / "fixtures" / "dashboard_v2_release_decision.json"
LEGACY_FIXTURE = ROOT / "tests" / "fixtures" / "dashboard_v2.json"

#: fixture 中各 app / 平台被 pin 的 release（status == "latest"）與其 lifecycle 計數。
#: 寫成字面常數而非從 fixture 推導：從 fixture 推導的話，fixture 被改壞時測試會
#: 跟著「同意」新的錯誤值。
PINNED = {
    ("shop_app", "android"): "3.2.0",
    ("shop_app", "ios"): "3.3.0",
}
LIFECYCLE_SHOP_ANDROID = ["6", "2", "3", "4"]
#: shop ios 最新版天然帶著三個**真正的 0**，是「0 不是無資料」那一半的自然案例。
LIFECYCLE_SHOP_IOS = ["1", "0", "0", "0"]
GATE_HISTORY_SHOP_ANDROID = ["pass", "warn", "fail"]


def _rule_body(css: str, selector: str) -> str:
    m = re.search(r"(?m)^\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m is not None, f"CSS 找不到規則 {selector}"
    return m.group(1)


def _font_size_px(css: str, selector: str) -> float:
    body = _rule_body(css, selector)
    m = re.search(r"font-size:\s*([0-9.]+)px", body)
    assert m is not None, f"{selector} 沒有宣告 font-size"
    return float(m.group(1))


def _cards(grid: str) -> dict[str, str]:
    """把 grid 拆成 platform -> 卡片 HTML。"""
    out: dict[str, str] = {}
    parts = re.split(r'(?=<div class="aux-card[^"]*" data-aux-platform=")', grid)
    for part in parts:
        m = re.search(r'data-aux-platform="([^"]+)"', part)
        if m:
            out[m.group(1)] = part
    return out


def _lifecycle_cells(card: str) -> list[tuple[str, str, str]]:
    """回傳 (欄位名, has_value, 顯示值)，順序即呈現順序。"""
    rows = re.findall(
        r'data-aux-lifecycle="([^"]+)" data-aux-has-value="([^"]+)".*?'
        r'<span class="aux-metric-value[^"]*">(.*?)</span>',
        card,
        re.DOTALL,
    )
    return [(k, has, val.strip()) for k, has, val in rows]


def _gate_points(card: str) -> list[tuple[str, str]]:
    """回傳 (gate_status, 色調 class)，順序即呈現順序。"""
    pts = re.findall(
        r'data-aux-gate-status="([^"]+)".*?class="aux-gate-time".*?'
        r'<span class="(aux-gate-\w+)"',
        card,
        re.DOTALL,
    )
    return [(st, tone) for st, tone in pts]


def _gate_dates(card: str) -> list[str]:
    return re.findall(r'<span class="aux-gate-time"[^>]*>([^<]*)</span>', card)


class TestAuxSurfaceIsWired(unittest.TestCase):
    """面板要真的被掛進首屏與兩個 render 觸發點，而不是只有一份沒人呼叫的 JS。"""

    def test_overview_html_carries_the_aux_containers(self) -> None:
        html = overview.get_overview_html()
        self.assertIn(f'id="{overview.AUX_GRID_ID}"', html)
        self.assertIn(f'id="{overview.AUX_SUBTITLE_ID}"', html)

    def test_aux_surface_follows_the_decision_and_comparison_surfaces(self) -> None:
        """輔助資訊排在兩個決策相關面之後——首屏的閱讀順序就是 #70 的設計。"""
        html = overview.get_overview_html()
        decision_at = html.find('id="overviewDecisionSection"')
        comparison_at = html.find('id="overviewComparisonSection"')
        aux_at = html.find('id="overviewAuxSection"')
        kpi_at = html.find('class="kpi-grid"')
        self.assertGreater(aux_at, decision_at, "輔助面板不得排在發布決策面之前")
        self.assertGreater(aux_at, comparison_at, "輔助面板不得排在比較面之前")
        self.assertGreater(kpi_at, aux_at, "輔助面板仍屬首屏（KPI 之前）")

    def test_render_all_invokes_the_aux_renderer(self) -> None:
        self.assertIn("renderReleaseAux();", get_shell_js_bottom())

    def test_route_apply_refreshes_the_aux_surface(self) -> None:
        """deep link pin 到別的 release 時，三個面必須一起重畫。"""
        js = nav.get_navigation_js()
        self.assertIn('if (typeof renderReleaseAux === "function") renderReleaseAux();', js)


class TestAuxSurfaceIsVisuallySubordinate(unittest.TestCase):
    """視覺層級低於 Release Decision（#91 驗收條件）以 CSS 逐項比對。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.css = get_dashboard_styles()

    def test_section_title_is_smaller_than_the_decision_section_title(self) -> None:
        self.assertLess(
            _font_size_px(self.css, ".aux-section-title"),
            _font_size_px(self.css, ".decision-section-title"),
            "輔助面板的標題字級必須小於發布決策面",
        )

    def test_section_title_is_not_painted_as_primary_text(self) -> None:
        """決策面標題用 --text-main；輔助面板用較弱的 --text-muted。"""
        self.assertIn("var(--text-main)", _rule_body(self.css, ".decision-section-title"))
        aux = _rule_body(self.css, ".aux-section-title")
        self.assertIn("var(--text-muted)", aux)
        self.assertNotIn("var(--text-main)", aux)

    def test_aux_card_has_no_decision_level_chrome(self) -> None:
        """決策卡有陰影與 4px 色條；輔助卡是扁平的，不得長出同等重量的裝飾。"""
        decision = _rule_body(self.css, ".decision-card")
        self.assertIn("box-shadow", decision)
        self.assertIn("border-left: 4px", decision)
        aux = _rule_body(self.css, ".aux-card")
        self.assertNotIn("box-shadow", aux)
        self.assertNotIn("border-left", aux)

    def test_every_gate_tone_class_is_styled(self) -> None:
        """JS 可能產生的每一個色調 class 都必須有樣式，否則狀態會變成無色文字。"""
        for tone in ("good", "warn", "bad", "neutral"):
            with self.subTest(tone=tone):
                self.assertIn(f".{overview.AUX_GATE_TONE_PREFIX}{tone} ", self.css)
        self.assertIn(f".{overview.AUX_GATE_NEUTRAL_TONE_CLASS} ", self.css)


class TestAuxJsDoesNotRestateTheGateSemantics(unittest.TestCase):
    """機械掃描產出的 JS：不得出現第二套狀態語意，也不得出現任何發布建議。"""

    @classmethod
    def setUpClass(cls) -> None:
        # 掃描的是**程式碼**，不是註解：註解裡解釋「為什麼不自己列狀態」時難免會
        # 提到那些狀態名，而註解不會改變行為。
        cls.js = "\n".join(
            re.sub(r"//.*$", "", line) for line in overview.get_release_aux_js().splitlines()
        )

    def test_status_tone_and_label_come_from_the_decision_module(self) -> None:
        self.assertIn("DECISION_TONES", self.js)
        self.assertIn("DECISION_STATUS_LABELS", self.js)

    def test_the_five_gate_states_are_not_enumerated_again(self) -> None:
        """一旦這段 JS 自己列出狀態名，就是第二套狀態語意的入口。

        `insufficient_data` / `baseline` 尤其危險：#73 已經把它們定為中性，這裡
        若自行判斷就可能把它們畫成 pass。
        """
        for state in ("insufficient_data", "baseline", '"pass"', '"warn"', '"fail"'):
            with self.subTest(state=state):
                self.assertNotIn(state, self.js)

    def test_no_release_recommendation_or_action_is_emitted(self) -> None:
        """輔助面板不得成為第二條發布建議路徑（建議只有 release_gate.decision）。"""
        for forbidden in ("decision-recommendation", "建議行動", "recommendation", ".action"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.js)

    def test_missing_counts_never_fall_back_to_zero(self) -> None:
        """`|| 0` 這種寫法正是把「沒有資料」變成 0 的那一行。"""
        self.assertNotIn("|| 0", self.js)
        self.assertIn("AUX_NO_DATA_TEXT", self.js)

    def test_the_timeline_does_not_reorder_the_history(self) -> None:
        """排序規則屬於產生 history 的後端；前端自行排序只會與後端分歧。"""
        self.assertNotIn(".sort(", self.js)


#: 逐一 dump 每個 release 的輔助卡，以及首屏 grid 的實際內容。
STEPS_DUMP_AUX = r"""
  const APPS = routeAppsData();
  report.steps.cards = {};
  Object.keys(APPS).forEach(aid => {
    (APPS[aid].release_catalog || []).forEach(r => {
      report.steps.cards[aid + '|' + r.platform + '|' + r.version] =
        buildReleaseAuxCardHtml(r.platform, r.platform, r, {});
    });
  });
  report.steps.cards['__NO_RELEASE__'] = buildReleaseAuxCardHtml('ios', 'iOS', null, {});
  report.steps.grid = elements['overviewAuxGrid'].innerHTML;
  report.steps.subtitle = elements['overviewAuxSubtitle'].textContent;
"""


class AuxRuntime(OverviewClientRuntime):
    """在 Node 內跑真正產出的 client JS，讀回輔助面板的渲染結果。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def _dump(self, bundle: dict[str, Any] | None = None, initial_hash: str = "") -> dict[str, Any]:
        return self.render(bundle or copy.deepcopy(self.bundle), STEPS_DUMP_AUX, initial_hash)

    @staticmethod
    def _release(bundle: dict[str, Any], app_id: str, platform: str, version: str) -> dict[str, Any]:
        for item in bundle["apps"][app_id]["release_catalog"]:
            if item["platform"] == platform and item["version"] == version:
                return item
        raise AssertionError(f"fixture 沒有 {app_id}/{platform}/{version}")


class TestLifecycleSummaryRendersTheData(AuxRuntime):
    """四個計數只讀 issue_lifecycle，順序即 #91 驗收條件的順序。"""

    def setUp(self) -> None:
        self.dump = self._dump()

    def test_all_four_lifecycle_fields_are_rendered_in_contract_order(self) -> None:
        card = _cards(self.dump["grid"])["android"]
        cells = _lifecycle_cells(card)
        self.assertEqual(
            [k for k, _, _ in cells],
            [f[0] for f in overview.AUX_LIFECYCLE_FIELDS],
        )
        self.assertEqual([v for _, _, v in cells], LIFECYCLE_SHOP_ANDROID)

    def test_a_real_zero_is_rendered_as_zero(self) -> None:
        """「數量為 0」必須真的印 0——否則把整個面板改成永遠印「無資料」也會過。"""
        card = _cards(self.dump["grid"])["ios"]
        cells = _lifecycle_cells(card)
        self.assertEqual([v for _, _, v in cells], LIFECYCLE_SHOP_IOS)
        self.assertEqual({has for _, has, _ in cells}, {"true"})
        self.assertNotIn("無資料", card.split('data-aux-block="gate_history"')[0])

    def test_missing_lifecycle_says_no_data_instead_of_zero(self) -> None:
        bundle = copy.deepcopy(self.bundle)
        release = self._release(bundle, "shop_app", "android", "3.2.0")
        release.pop("issue_lifecycle")
        self.assertEqual(validate_dashboard_v2(bundle), [], "前提：拿掉後仍是合法 bundle")

        card = _cards(self._dump(bundle)["grid"])["android"]
        self.assertEqual(_lifecycle_cells(card), [], "缺 issue_lifecycle 時不得畫出任何計數格")
        self.assertIn("沒有問題生命週期資料 (issue_lifecycle)", card)
        lifecycle_block = card.split('data-aux-block="gate_history"')[0]
        self.assertNotIn(">0<", lifecycle_block, "「沒有資料」不得被畫成 0")

    def test_a_lifecycle_without_any_count_degrades_as_a_whole(self) -> None:
        """舊 bundle 可能只帶 issue id 清單而沒有任何計數欄位。

        此時四格全印「無資料」只是噪音，應該退回一句說明——但**不得**因為
        「物件存在」就把它當成有摘要可呈現。
        """
        bundle = copy.deepcopy(self.bundle)
        release = self._release(bundle, "shop_app", "android", "3.2.0")
        release["issue_lifecycle"] = {
            k: v for k, v in release["issue_lifecycle"].items() if not k.endswith("_count")
        }
        self.assertTrue(release["issue_lifecycle"], "前提：物件仍存在，只是沒有計數欄位")

        card = _cards(self._dump(bundle)["grid"])["android"]
        self.assertEqual(_lifecycle_cells(card), [])
        self.assertIn("沒有問題生命週期資料 (issue_lifecycle)", card)
        self.assertNotIn(">0<", card.split('data-aux-block="gate_history"')[0])

    def test_a_partially_missing_lifecycle_degrades_per_field(self) -> None:
        """少一個計數只讓那一格降級，其餘三格仍是可信的觀測值。"""
        bundle = copy.deepcopy(self.bundle)
        release = self._release(bundle, "shop_app", "android", "3.2.0")
        release["issue_lifecycle"].pop("resolved_count")

        card = _cards(self._dump(bundle)["grid"])["android"]
        cells = {k: (has, val) for k, has, val in _lifecycle_cells(card)}
        self.assertEqual(cells["resolved_count"], ("false", "無資料"))
        self.assertEqual(cells["introduced_count"], ("true", "6"))
        self.assertEqual(cells["regressed_count"], ("true", "2"))
        self.assertEqual(cells["persistent_count"], ("true", "3"))

    def test_a_platform_without_a_release_renders_no_counts(self) -> None:
        card = self.dump["cards"]["__NO_RELEASE__"]
        self.assertEqual(_lifecycle_cells(card), [])
        self.assertIn("沒有發佈版本", card)


class TestGateHistoryTimeline(AuxRuntime):
    """timeline 只呈現既有的 gate_history 欄位，不重算任何判定。"""

    def setUp(self) -> None:
        self.dump = self._dump()

    def test_each_evaluation_is_rendered_in_data_order(self) -> None:
        card = _cards(self.dump["grid"])["android"]
        self.assertEqual([st for st, _ in _gate_points(card)], GATE_HISTORY_SHOP_ANDROID)
        self.assertEqual(_gate_dates(card), ["2026-08-21", "2026-08-28", "2026-09-02"])

    def test_status_tones_follow_the_decision_module(self) -> None:
        card = _cards(self.dump["grid"])["android"]
        self.assertEqual(
            _gate_points(card),
            [("pass", "aux-gate-good"), ("warn", "aux-gate-warn"), ("fail", "aux-gate-bad")],
        )

    def test_missing_gate_history_renders_no_timeline_at_all(self) -> None:
        """shop ios 最新版天然沒有 gate_history：不得憑空生出一個 PASS 起點。"""
        card = _cards(self.dump["grid"])["ios"]
        self.assertEqual(_gate_points(card), [])
        self.assertIn("gate_history", card)
        self.assertNotIn("aux-gate-good", card)

    def test_a_deep_linked_release_shows_its_own_history(self) -> None:
        """pin 到 ios 3.2.0 時，輔助面板必須換成那一版的歷程（而非最新版的）。"""
        link = nav.build_release_decision_link("shop_app", "ios", "3.2.0")
        grid = self._dump(initial_hash=link)["grid"]
        card = _cards(grid)["ios"]
        self.assertIn('data-aux-version="3.2.0"', card)
        self.assertEqual([st for st, _ in _gate_points(card)], ["pass", "warn"])
        self.assertIn("連結指定版本", card)
        # 另一個平台仍顯示自己的最新版，對照能力不因 deep link 消失。
        self.assertIn(
            f'data-aux-version="{PINNED[("shop_app", "android")]}"',
            _cards(grid)["android"],
        )

    def test_only_the_recent_points_are_kept(self) -> None:
        """首屏是「近期趨勢」；完整歷程留給 Releases 頁的詳細 timeline（#61）。"""
        bundle = copy.deepcopy(self.bundle)
        release = self._release(bundle, "shop_app", "android", "3.2.0")
        template = release["gate_history"][0]
        release["gate_history"] = [
            {**copy.deepcopy(template), "evaluated_at": f"2026-07-{day:02d}T02:00:00Z"}
            for day in range(1, 1 + overview.AUX_GATE_HISTORY_LIMIT + 4)
        ]
        self.assertEqual(validate_dashboard_v2(bundle), [], "前提：注入後仍是合法 bundle")

        card = _cards(self._dump(bundle)["grid"])["android"]
        dates = _gate_dates(card)
        self.assertEqual(len(dates), overview.AUX_GATE_HISTORY_LIMIT)
        # 保留的是**最近**幾筆（尾端），不是最早幾筆。
        self.assertEqual(dates[-1], f"2026-07-{overview.AUX_GATE_HISTORY_LIMIT + 4:02d}")

    def test_an_insufficient_sample_point_is_flagged(self) -> None:
        bundle = copy.deepcopy(self.bundle)
        release = self._release(bundle, "shop_app", "android", "3.2.0")
        release["gate_history"][-1]["sample_sufficient"] = False

        card = _cards(self._dump(bundle)["grid"])["android"]
        self.assertIn("樣本不足", card)


class TestNeutralStatesAreNeverRenderedAsPass(AuxRuntime):
    """#91 驗收條件：insufficient_data / baseline 不得呈現為 PASS。

    這是本檔最重要的一條：把「還無法判定」畫成綠色，讀者會把沒被評估過的版本
    當成已驗證安全——與 #72 兩度修過的失敗模式相同。
    """

    #: 未知狀態一併測：預設值必須是中性，而不是 pass 的綠色。
    STATES = ("pass", "warn", "fail", "insufficient_data", "baseline", "totally_unknown")

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        bundle = copy.deepcopy(cls.bundle)
        for item in bundle["apps"]["shop_app"]["release_catalog"]:
            if item["platform"] == "android" and item["version"] == "3.2.0":
                template = item["gate_history"][0]
                item["gate_history"] = [
                    {
                        **copy.deepcopy(template),
                        "gate_status": state,
                        "evaluated_at": f"2026-07-{idx + 1:02d}T02:00:00Z",
                    }
                    for idx, state in enumerate(cls.STATES)
                ]
        cls.injected = bundle

    def test_the_injected_bundle_is_contract_valid(self) -> None:
        """前提：測的是一份合法 bundle，而不是靠壞資料製造的假通過。"""
        self.assertEqual(validate_dashboard_v2(copy.deepcopy(self.injected)), [])

    def test_every_state_gets_the_tone_the_decision_module_defines(self) -> None:
        card = _cards(self._dump(copy.deepcopy(self.injected))["grid"])["android"]
        self.assertEqual(
            _gate_points(card),
            [
                ("pass", "aux-gate-good"),
                ("warn", "aux-gate-warn"),
                ("fail", "aux-gate-bad"),
                ("insufficient_data", "aux-gate-neutral"),
                ("baseline", "aux-gate-neutral"),
                ("totally_unknown", "aux-gate-neutral"),
            ],
        )

    def test_exactly_one_point_is_painted_as_pass(self) -> None:
        """反向斷言：六種狀態裡只有 pass 能拿到 good 色調。"""
        card = _cards(self._dump(copy.deepcopy(self.injected))["grid"])["android"]
        self.assertEqual(card.count("aux-gate-good"), 1)


class TestAuxSurfaceDegradesSafely(AuxRuntime):
    """缺資料的四種現實都不得留下空白區塊或 JS error。"""

    def test_legacy_bundle_without_release_catalog_renders_a_neutral_surface(self) -> None:
        """dashboard_v2.json 完全沒有 release_catalog（#90 的向後相容基準）。"""
        legacy = json.loads(LEGACY_FIXTURE.read_text(encoding="utf-8"))
        self.assertNotIn(
            "release_catalog",
            legacy["apps"]["shop_app"],
            "前提：這個 fixture 不得被補上 release_catalog",
        )
        dump = self._dump(legacy)
        self.assertIn("release_catalog", dump["grid"])
        self.assertEqual(dump["subtitle"], "無發佈版本資料")
        self.assertEqual(_gate_points(dump["grid"]), [])
        self.assertEqual(_lifecycle_cells(dump["grid"]), [])

    def test_a_release_missing_both_panels_still_renders_one_card(self) -> None:
        """兩個面板都沒資料時，卡片本身仍在（版本資訊與說明都還看得到）。"""
        bundle = copy.deepcopy(self.bundle)
        release = self._release(bundle, "shop_app", "android", "3.2.0")
        release.pop("issue_lifecycle")
        release["gate_history"] = None

        card = _cards(self._dump(bundle)["grid"])["android"]
        self.assertIn('data-aux-version="3.2.0"', card)
        self.assertIn("issue_lifecycle", card)
        self.assertIn("gate_history", card)
        self.assertEqual(_lifecycle_cells(card), [])
        self.assertEqual(_gate_points(card), [])

    def test_an_empty_gate_history_list_is_treated_as_no_data(self) -> None:
        """空陣列與缺欄位是同一個事實：沒有評估紀錄，不是「評估過且沒問題」。"""
        bundle = copy.deepcopy(self.bundle)
        self._release(bundle, "shop_app", "android", "3.2.0")["gate_history"] = []

        card = _cards(self._dump(bundle)["grid"])["android"]
        self.assertEqual(_gate_points(card), [])
        self.assertIn("gate_history", card)

    def test_the_subtitle_names_both_panels(self) -> None:
        dump = self._dump()
        self.assertIn("問題生命週期", dump["subtitle"])
        self.assertIn("品質閘門歷程", dump["subtitle"])


if __name__ == "__main__":
    unittest.main()
