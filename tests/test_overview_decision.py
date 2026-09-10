"""Decision-first Overview 首屏測試（Issue #73）。

這些測試存在的理由，按重要性排列：

1. **中性狀態不得被讀成綠燈。** 這是本單最重要的正確性要求，而不是樣式偏好。
   `insufficient_data` / `baseline` 代表「還無法判定」，gate 未評估（policy
   `enabled: false`）時連 `decision` 欄位都沒有、`release_gate` 本身也可能是
   `null`。這三種情況一旦被渲染成 PASS，看板會對「沒人評估過的版本」說
   「可以繼續發布」——本 epic 的 review 已經抓到同一類錯誤兩次。因此這裡的斷言
   是 negative 形式的：**不得**出現 pass 色調、**不得**出現任何建議文字。

2. **決策是讀出來的，不是算出來的。** #72 的 `release_gate.decision` 是唯一來源。
   靜態 drift test 只能保證「JS 裡沒有第二套 status → 文案表」；這裡進一步用
   sentinel 值證明渲染結果真的來自那個欄位——把 fixture 的 recommendation 換成
   一個不可能出現在程式碼裡的字串，畫面必須跟著變。

3. **首屏必須真的長出那些欄位。** platform / version / gate status / 建議 /
   WARN·FAIL 主因 / sample·baseline 狀態，缺一項就不足以做發布決策。

4. **外部連結要開到它所指的那一版。** alert 貼出來的連結若只是落到「各平台最新
   版本」，收到 FAIL 通知的人會看到另一個版本的決策，比沒有連結更糟。

Node runtime 部分刻意重用 `tests.test_dashboard_deep_link` 的 `NODE_RUNNER`
（#78 建立的 stub DOM + hash history），不另建第二套 harness：兩邊跑的是同一份
真正產出的 client JS，行為差異不會因為 harness 不同而被掩蓋。
"""

from __future__ import annotations

import copy
import html as html_mod
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.build_dashboard import build_html
from crash_trend.dashboard import assets, overview
from crash_trend.dashboard import navigation as nav
from crash_trend.gate.decision import _DECISION_TABLE
from tests.test_dashboard_deep_link import NODE_RUNNER

FIXTURE = ROOT / "tests" / "fixtures" / "dashboard_v2_release_decision.json"

#: fixture 中每個 release 的 gate 現實，作為測試的「前提」而非重新推導。
#: (app, platform, version) -> 期望的卡片色調；``None`` 代表沒有 canonical
#: decision，因此不得有任何建議。
EXPECTED_TONES: dict[tuple[str, str, str], str | None] = {
    ("shop_app", "android", "3.2.0"): "bad",
    ("shop_app", "android", "3.1.2"): "good",
    ("shop_app", "android", "3.1.0"): "neutral",
    ("shop_app", "ios", "3.3.0"): "neutral",
    ("shop_app", "ios", "3.2.0"): "warn",
    ("shop_app", "ios", "3.1.2"): "neutral",
    # gate policy 未啟用：release_gate 存在、status 仍是 "pass"，但沒有 decision。
    ("rider_app", "android", "1.8.0"): None,
    # release_gate 本身是 null。
    ("rider_app", "android", "1.7.4"): None,
}

#: 每個平台的最新 release（catalog 自己標記 status == "latest"）。
LATEST_BY_APP_PLATFORM: dict[str, dict[str, str]] = {
    "shop_app": {"android": "3.2.0", "ios": "3.3.0"},
    "rider_app": {"android": "1.8.0"},
}

#: 讀出每個 release 卡片 HTML，以及首屏 grid 的實際內容。
STEPS_DUMP_DECISIONS = r"""
  const APPS = routeAppsData();
  report.steps.cards = {};
  Object.keys(APPS).forEach(aid => {
    (APPS[aid].release_catalog || []).forEach(r => {
      report.steps.cards[aid + '|' + r.platform + '|' + r.version] =
        buildReleaseDecisionCardHtml(r.platform, r.platform, r, {});
    });
  });
  // 平台完全沒有 release 的情況（fixture 的 rider_app 沒有任何 iOS 版本）。
  report.steps.cards['__NO_RELEASE__'] = buildReleaseDecisionCardHtml('ios', 'iOS', null, {});
  report.steps.grid = elements['overviewDecisionGrid'].innerHTML;
  report.steps.subtitle = elements['overviewDecisionSubtitle'].textContent;
  report.steps.grids = {};
  Object.keys(APPS).forEach(aid => {
    switchApp(aid);
    report.steps.grids[aid] = elements['overviewDecisionGrid'].innerHTML;
  });
"""


def _canonical_wording(status: str) -> str:
    return _DECISION_TABLE[status][1]


def _canonical_action(status: str) -> str:
    return _DECISION_TABLE[status][0]


def _field(card: str, css_class: str) -> str | None:
    """回傳卡片中某個 class 的文字內容；不存在時回 ``None``（而非空字串）。

    「不存在」與「存在但空白」必須分得開：本單的核心斷言就是「未評估時
    **不存在**建議欄位」。
    """
    m = re.search(rf'class="{css_class}">(.*?)</', card, re.DOTALL)
    return m.group(1) if m else None


def _tones(card: str) -> list[str]:
    return re.findall(r"decision-tone-(\w+)", card)


def _reasons(card: str) -> list[str]:
    block = re.search(r'<ul class="decision-reasons">(.*?)</ul>', card, re.DOTALL)
    if not block:
        return []
    return [html_mod.unescape(x) for x in re.findall(r"<li>(.*?)</li>", block.group(1), re.DOTALL)]


def _permalink(card: str) -> str | None:
    m = re.search(r'class="decision-permalink" href="([^"]*)"', card)
    return html_mod.unescape(m.group(1)) if m else None


class _ClientRuntime(unittest.TestCase):
    """在 Node 內執行真正產出的 client JS，讀回渲染結果。"""

    node_bin: str | None
    bundle: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.node_bin = shutil.which("node")
        cls.bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def _render(
        self,
        bundle: dict[str, Any] | None = None,
        initial_hash: str = "",
        steps: str = STEPS_DUMP_DECISIONS,
    ) -> dict[str, Any]:
        if not self.node_bin:
            self.skipTest("Node.js runtime is not available in environment")
        html = build_html(bundle if bundle is not None else self.bundle)
        scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        self.assertGreaterEqual(len(scripts), 2, "HTML must contain at least 2 <script> tags")
        dom_ids = sorted(set(re.findall(r'id="([A-Za-z0-9_-]+)"', html)))
        self.assertIn(
            overview.DECISION_GRID_ID,
            dom_ids,
            "前提：首屏決策面的容器真的存在於 render 結果中",
        )

        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "client.js").write_text(scripts[1], encoding="utf-8")
            (t / "dom_ids.json").write_text(json.dumps(dom_ids), encoding="utf-8")
            (t / "runner.js").write_text(NODE_RUNNER, encoding="utf-8")
            (t / "steps.js").write_text(steps, encoding="utf-8")
            res = subprocess.run(
                [
                    self.node_bin,
                    str(t / "runner.js"),
                    str(t / "client.js"),
                    initial_hash,
                    str(t / "steps.js"),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
        self.assertEqual(res.returncode, 0, f"Node failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        marker = [ln for ln in res.stdout.splitlines() if ln.startswith("__REPORT__")]
        self.assertTrue(marker, f"runner produced no report:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        report = json.loads(marker[-1][len("__REPORT__") :])
        self.assertIsNone(report["error"], f"client JS threw: {report['error']}")
        return report["steps"]

    def _fixture_release(self, app_id: str, platform: str, version: str) -> dict[str, Any]:
        for r in self.bundle["apps"][app_id]["release_catalog"]:
            if r["platform"] == platform and r["version"] == version:
                return dict(r)
        raise AssertionError(f"fixture 缺少 {app_id} {platform} {version}")

    def _fixture_gate(self, app_id: str, platform: str, version: str) -> dict[str, Any]:
        gate = self._fixture_release(app_id, platform, version)["release_gate"]
        self.assertIsNotNone(gate, f"前提：{app_id} {platform} {version} 有 release_gate")
        return dict(gate)

    def _fixture_decision(self, app_id: str, platform: str, version: str) -> dict[str, Any]:
        return dict(self._fixture_gate(app_id, platform, version)["decision"])


class TestOverviewDecisionSurfaceIsWired(unittest.TestCase):
    """首屏決策面必須真的被渲染出來、且真的會被呼叫。

    這幾條是靜態的接線斷言：容器被拿掉、或 renderAll 忘了呼叫，首屏就會是空的，
    而所有 runtime 斷言都會因為「讀到空字串」而以難懂的方式失敗。
    """

    def test_overview_html_carries_the_decision_container(self) -> None:
        html = overview.get_overview_html()
        self.assertIn(f'id="{overview.DECISION_GRID_ID}"', html)
        self.assertIn(f'id="{overview.DECISION_SUBTITLE_ID}"', html)

    def test_decision_surface_precedes_the_kpi_cards(self) -> None:
        """decision-first 的字面意思：決策面在 KPI 之前，不需要捲動就看得到。"""
        html = overview.get_overview_html()
        self.assertLess(
            html.index(f'id="{overview.DECISION_GRID_ID}"'),
            html.index("KPI Cards"),
            "決策面必須排在 KPI 之前，否則首屏第一眼不是決策",
        )

    def test_render_all_invokes_the_decision_renderer(self) -> None:
        self.assertIn("renderReleaseDecisions();", assets.get_shell_js_bottom())

    def test_route_apply_refreshes_the_decision_surface(self) -> None:
        """deep link 套用 context 之後必須重畫決策面，否則連結永遠只顯示最新版。"""
        js = nav.get_navigation_js()
        self.assertIn('if (typeof renderReleaseDecisions === "function") renderReleaseDecisions();', js)

    def test_decision_tone_classes_are_styled(self) -> None:
        """色調是正確性的一部分；class 沒有對應 CSS 就等於沒有中性呈現。"""
        css = assets.get_dashboard_styles()
        for tone in ("good", "warn", "bad", "neutral"):
            self.assertIn(f".{overview.DECISION_TONE_CLASS_PREFIX}{tone} ", css)
        self.assertIn(f".{overview.DECISION_NEUTRAL_TONE_CLASS} ", css)


class TestFirstScreenShowsWhatADecisionNeeds(_ClientRuntime):
    """首屏對每個平台的最新 release 呈現的欄位。

    少了任何一項，看板就不足以支撐發布決策：沒有版本號不知道在講哪一版，
    沒有 gate status 不知道嚴重程度，沒有 reasons 不知道為什麼被擋，
    沒有 sample/baseline 狀態就無法判斷這個判定本身有多可信。
    """

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.steps = None

    def setUp(self) -> None:
        if getattr(type(self), "steps", None) is None:
            type(self).steps = self._render()

    def _grid(self, app_id: str) -> str:
        return self.steps["grids"][app_id]

    def test_both_platforms_are_rendered_side_by_side(self) -> None:
        """Android / iOS 必須同時在首屏，否則無法快速對照。"""
        grid = self._grid("shop_app")
        self.assertIn('data-decision-platform="android"', grid)
        self.assertIn('data-decision-platform="ios"', grid)

    def test_each_platform_card_shows_its_latest_version(self) -> None:
        grid = self._grid("shop_app")
        for platform, version in LATEST_BY_APP_PLATFORM["shop_app"].items():
            self.assertIn(
                f'data-decision-platform="{platform}" data-decision-version="{version}"',
                grid,
                f"{platform} 卡片必須是 catalog 標記為 latest 的 {version}",
            )

    def test_failing_platform_card_carries_status_action_recommendation_and_reasons(self) -> None:
        card = self.steps["cards"]["shop_app|android|3.2.0"]
        self.assertIn("FAIL", _field(card, "decision-gate-badge") or "")
        self.assertEqual(_field(card, "decision-version"), "3.2.0")
        self.assertEqual(
            html_mod.unescape(_field(card, "decision-recommendation") or ""),
            _canonical_wording("fail"),
        )
        self.assertEqual(_field(card, "decision-action"), _canonical_action("fail"))
        fixture_reasons = self._fixture_decision("shop_app", "android", "3.2.0")["reasons"]
        self.assertEqual(_reasons(card), fixture_reasons)
        self.assertTrue(fixture_reasons, "前提：FAIL 的 release 帶有退化原因")

    def test_warn_card_shows_the_reasons_behind_the_warning(self) -> None:
        card = self.steps["cards"]["shop_app|ios|3.2.0"]
        self.assertIn("WARN", _field(card, "decision-gate-badge") or "")
        self.assertEqual(_reasons(card), self._fixture_decision("shop_app", "ios", "3.2.0")["reasons"])

    def test_cards_show_sample_and_baseline_state(self) -> None:
        """sample / baseline 狀態決定一個判定有多可信，必須與判定本身同屏。"""
        sufficient = self.steps["cards"]["shop_app|android|3.2.0"]
        self.assertIn("樣本充足", sufficient)
        self.assertIn("前版基準 3.1.2", sufficient)

        insufficient = self.steps["cards"]["shop_app|ios|3.3.0"]
        self.assertIn("樣本不足", insufficient)

        baseline = self.steps["cards"]["shop_app|ios|3.1.2"]
        self.assertIn("無前版基準", baseline)

    def test_subtitle_says_the_cards_are_the_latest_versions(self) -> None:
        """沒有 deep link context 時，讀者必須知道自己看的是「各平台最新版」。"""
        self.assertIn("最新版本", self.steps["subtitle"])


class TestFiveStateUxNeverShowsAGreenLightItCannotJustify(_ClientRuntime):
    """五種狀態 + 兩種「沒有判定」的呈現。本單最重要的正確性要求。

    斷言刻意大量採用 negative 形式（不得是 pass 色調、不得有建議欄位）：
    這一類 bug 的失敗模式不是「畫面壞掉」，而是「畫面看起來很好、內容是錯的」，
    因此只斷言 happy path 的欄位存在完全抓不到它。
    """

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.steps = None

    def setUp(self) -> None:
        if getattr(type(self), "steps", None) is None:
            type(self).steps = self._render()

    def _card(self, app_id: str, platform: str, version: str) -> str:
        return self.steps["cards"][f"{app_id}|{platform}|{version}"]

    def test_fixture_covers_every_state_this_ticket_must_handle(self) -> None:
        """前提檢查：#92 的 fixture 真的涵蓋五種狀態 + 無 decision + null gate。

        少了任何一種，下面的斷言就會變成「沒有測到」而不是「測過了」。
        """
        observed = set()
        for app in self.bundle["apps"].values():
            for r in app.get("release_catalog") or []:
                rg = r.get("release_gate")
                if rg is None:
                    observed.add("__null_gate__")
                elif "decision" not in rg:
                    observed.add("__no_decision__")
                else:
                    observed.add(rg["decision"]["status"])
        self.assertEqual(
            observed,
            set(_DECISION_TABLE) | {"__no_decision__", "__null_gate__"},
            "fixture 必須同時涵蓋五種 decision 狀態、無 decision、以及 release_gate: null",
        )

    def test_every_state_renders_its_expected_tone(self) -> None:
        for (app_id, platform, version), tone in EXPECTED_TONES.items():
            with self.subTest(app=app_id, platform=platform, version=version):
                card = self._card(app_id, platform, version)
                expected = tone if tone is not None else "neutral"
                self.assertEqual(
                    _tones(card),
                    [expected],
                    f"{app_id} {platform} {version} 的色調必須是 {expected}",
                )

    def test_insufficient_data_and_baseline_render_neutral_never_pass(self) -> None:
        """這兩種是「還無法判定」；與 pass 同色會被讀成已驗證安全。"""
        for platform, version, status in (
            ("ios", "3.3.0", "insufficient_data"),
            ("android", "3.1.0", "insufficient_data"),
            ("ios", "3.1.2", "baseline"),
        ):
            with self.subTest(version=version, status=status):
                card = self._card("shop_app", platform, version)
                self.assertIn(overview.DECISION_NEUTRAL_TONE_CLASS, card)
                self.assertNotIn(overview.DECISION_PASS_TONE_CLASS, card)
                # 中性 ≠ 沒有內容：這兩種**有** canonical decision，建議文字仍須
                # 逐字呈現，只是色調不得是綠燈。
                self.assertEqual(
                    html_mod.unescape(_field(card, "decision-recommendation") or ""),
                    _canonical_wording(status),
                )
                self.assertEqual(_field(card, "decision-action"), _canonical_action(status))

    def test_pass_is_the_only_state_rendered_as_a_green_light(self) -> None:
        green = [
            key
            for key, card in self.steps["cards"].items()
            if overview.DECISION_PASS_TONE_CLASS in card
        ]
        self.assertEqual(
            green,
            ["shop_app|android|3.1.2"],
            "只有 decision.status == pass 的 release 可以是綠燈",
        )

    def test_gate_without_decision_field_produces_no_recommendation(self) -> None:
        """policy `enabled: false`：release_gate 存在、status 仍是 pass、但沒有 decision。

        這正是 review 抓到兩次的案例——若實作退回讀 release_gate.status，
        這張卡就會變成綠燈並宣稱「可以繼續發布」。
        """
        gate = self._fixture_gate("rider_app", "android", "1.8.0")
        self.assertEqual(gate["status"], "pass", "前提：未啟用的 gate 的 status 仍是 pass")
        self.assertNotIn("decision", gate, "前提：未啟用的 gate 沒有 decision 欄位")

        card = self._card("rider_app", "android", "1.8.0")
        self.assertIn(overview.DECISION_NEUTRAL_TONE_CLASS, card)
        self.assertNotIn(overview.DECISION_PASS_TONE_CLASS, card)
        self.assertIsNone(_field(card, "decision-recommendation"), "不得憑空生出建議")
        self.assertIsNone(_field(card, "decision-action"), "不得憑空生出建議行動")
        self.assertNotIn(_canonical_wording("pass"), card)
        self.assertNotIn(_canonical_action("pass"), card)
        self.assertIn("未評估", card)

    def test_null_release_gate_produces_no_recommendation(self) -> None:
        self.assertIsNone(
            self._fixture_release("rider_app", "android", "1.7.4")["release_gate"],
            "前提：這個 release 的 release_gate 是 null",
        )
        card = self._card("rider_app", "android", "1.7.4")
        self.assertIn(overview.DECISION_NEUTRAL_TONE_CLASS, card)
        self.assertNotIn(overview.DECISION_PASS_TONE_CLASS, card)
        self.assertIsNone(_field(card, "decision-recommendation"))
        self.assertIsNone(_field(card, "decision-action"))
        for status in _DECISION_TABLE:
            self.assertNotIn(_canonical_wording(status), card)

    def test_platform_without_any_release_produces_no_recommendation(self) -> None:
        """fixture 的 rider_app 沒有任何 iOS 版本；空平台不得長出一張綠卡。"""
        self.assertNotIn(
            "ios",
            {r["platform"] for r in self.bundle["apps"]["rider_app"]["release_catalog"]},
            "前提：rider_app 沒有 iOS release",
        )
        card = self.steps["cards"]["__NO_RELEASE__"]
        self.assertIn(overview.DECISION_NEUTRAL_TONE_CLASS, card)
        self.assertNotIn(overview.DECISION_PASS_TONE_CLASS, card)
        self.assertIsNone(_field(card, "decision-recommendation"))
        self.assertIsNone(_field(card, "decision-action"))

    def test_an_app_with_no_assessed_release_shows_no_recommendation_anywhere(self) -> None:
        """整屏層級的斷言：rider_app 兩個 release 都沒被評估過。

        因此**整個**首屏不得出現任何一句 canonical 建議，也不得有綠燈。
        個別卡片都正確、但 grid 組裝時補了一張預設卡——只有這條抓得到。
        """
        grid = self.steps["grids"]["rider_app"]
        self.assertNotIn(overview.DECISION_PASS_TONE_CLASS, grid)
        self.assertIsNone(_field(grid, "decision-recommendation"))
        self.assertIsNone(_field(grid, "decision-action"))
        for status in _DECISION_TABLE:
            self.assertNotIn(_canonical_wording(status), grid)


class TestDecisionIsReadNotRederived(_ClientRuntime):
    """渲染結果必須來自 `release_gate.decision` 這個欄位本身。

    #72 的 drift test 是靜態的（「JS 裡不存在第二套 status → 文案表」）。
    這裡是動態補強：把 fixture 的欄位換成絕不可能寫在程式碼裡的 sentinel，
    畫面必須跟著變。若哪天有人把 canonical 文案複製進 JS 當「fallback」，
    靜態測試會抓到重複字串，而這條會抓到「畫面沒跟著資料變」。
    """

    SENTINEL_REC = "SENTINEL-RECOMMENDATION-b7f2"
    SENTINEL_ACTION = "sentinel_action_b7f2"

    def test_rendered_recommendation_and_action_follow_the_contract_field(self) -> None:
        bundle = copy.deepcopy(self.bundle)
        for r in bundle["apps"]["shop_app"]["release_catalog"]:
            rg = r.get("release_gate")
            if rg and "decision" in rg:
                rg["decision"]["recommendation"] = self.SENTINEL_REC
                rg["decision"]["action"] = self.SENTINEL_ACTION

        card = self._render(bundle=bundle)["cards"]["shop_app|android|3.2.0"]
        self.assertEqual(_field(card, "decision-recommendation"), self.SENTINEL_REC)
        self.assertEqual(_field(card, "decision-action"), self.SENTINEL_ACTION)
        # 沒有任何一句 canonical 文案偷偷從 status 被重算出來。
        for status in _DECISION_TABLE:
            self.assertNotIn(_canonical_wording(status), card)

    def test_removing_the_decision_removes_the_green_light(self) -> None:
        """同一個 release，只把 decision 拿掉（status 仍是 pass）就不得再是綠燈。

        這是把「讀欄位」與「看 status」兩種實作區分開來的決定性實驗。
        """
        bundle = copy.deepcopy(self.bundle)
        for r in bundle["apps"]["shop_app"]["release_catalog"]:
            rg = r.get("release_gate")
            if rg and rg.get("status") == "pass":
                rg.pop("decision", None)

        card = self._render(bundle=bundle)["cards"]["shop_app|android|3.1.2"]
        self.assertEqual(_tones(card), ["neutral"])
        self.assertIsNone(_field(card, "decision-recommendation"))
        self.assertNotIn(_canonical_wording("pass"), card)

