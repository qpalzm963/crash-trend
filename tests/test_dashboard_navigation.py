"""Navigation / view-routing single-source-of-truth contract tests (Issue #71).

V3.1 把導覽定義與 routing 字串集中到 `crash_trend.dashboard.navigation`。
這些測試存在的理由是：一旦有人再度在 section module 或 assets 內硬寫
`id="view-xxx"` / `switchView('xxx')`，導覽與 view container 就會再次漂移
（點得到按鈕卻切不到頁面，或存在點不到的孤兒 view）。因此契約是
「渲染結果必須完全由 NAV_ITEMS 推導」，而非只檢查某些字串存在。

#78 在同一個集中點加上 deep-link routing，因此本檔的 URL routing 守門條件由
「整份 template 不得出現 URL routing」改為「navigation 模組之外不得出現 URL routing」；
deep-link 本身的契約測試在 ``tests/test_dashboard_deep_link.py``。
"""

from __future__ import annotations

import re
import unittest

from crash_trend.dashboard import navigation as nav
from crash_trend.dashboard.assets import get_sidebar_html
from crash_trend.dashboard.issues import get_issues_js
from crash_trend.dashboard.renderer import assemble_html_template


class TestNavigationRegistry(unittest.TestCase):
    def test_top_level_nav_is_the_four_v3_workspaces(self) -> None:
        """一級導覽收斂為 V3 的四個工作區與其定案順序 (#70 / #75)。

        #71 當時把「8 項一級導覽」寫成契約，是為了讓機械式重構不夾帶 IA 變更；
        #75 正是那個變更該發生的 ticket，因此這裡改為對 V3 定案的四個工作區
        下斷言。斷言強度不變（仍是完整、有序的相等比較），只是換了正確的對象；
        任何再次擴散一級導覽的改動仍會在此被擋下。
        """
        self.assertEqual(nav.workspace_ids(), ("overview", "versions", "issues", "system"))
        self.assertEqual(
            tuple(item.label for item in nav.NAV_ITEMS),
            ("總覽 (Overview)", "版本 (Versions)", "問題 (Issues)", "系統 (System)"),
        )

    def test_all_v2_views_survive_as_workspace_panels(self) -> None:
        """V3.5 只搬動 view 的歸屬，不刪 view：八個 V2 一級頁面必須全數仍是 panel。

        這是「現有功能不可因搬移而遺失」(#75 AC) 的結構性把關，也是舊 deep link
        仍然有效的前提——只要 view 還在註冊表裡，``#version_health`` 這類舊連結
        就不會掉回首頁。
        """
        self.assertEqual(
            set(nav.view_names()),
            {
                "overview",
                "issues",
                "version_health",
                "devices",
                "releases",
                "notifications",
                "ai_insights",
                "settings",
            },
        )

    def test_every_view_belongs_to_exactly_one_workspace(self) -> None:
        """view → 工作區必須是全函數且單值。

        少了對應關係，該 view 啟用時沒有任何 nav 按鈕會亮（使用者不知道自己在哪）；
        對到兩個工作區則會同時亮兩顆按鈕。兩者都只有在反向檢查時才抓得到。
        """
        seen: dict[str, str] = {}
        for item in nav.NAV_ITEMS:
            self.assertTrue(item.panels, f"工作區 {item.workspace} 沒有任何 panel")
            for panel in item.panels:
                self.assertNotIn(panel.view, seen, f"view {panel.view} 同時掛在兩個工作區")
                seen[panel.view] = item.workspace
        self.assertEqual(set(seen), set(nav.view_names()))
        for view, workspace in seen.items():
            self.assertEqual(nav.workspace_of(view), workspace)

    def test_the_five_relocated_views_moved_to_their_decided_workspace(self) -> None:
        """#75 定案的 mapping 表本身就是契約，逐項寫死避免日後被「順手」改掉。"""
        expected = {
            "overview": "overview",
            "issues": "issues",
            "devices": "issues",
            "version_health": "versions",
            "releases": "versions",
            "notifications": "system",
            "ai_insights": "system",
            "settings": "system",
        }
        for view, workspace in expected.items():
            with self.subTest(view=view):
                self.assertEqual(nav.workspace_of(view), workspace)

    def test_default_view_is_overview(self) -> None:
        """初始 active view 只有一個定義來源，nav 按鈕與 view container 都由它推導。"""
        self.assertEqual(nav.DEFAULT_WORKSPACE, "overview")
        self.assertEqual(nav.DEFAULT_VIEW, "overview")
        self.assertIn("view-container active", nav.get_view_container_open_tag("overview"))
        self.assertNotIn("active", nav.get_view_container_open_tag("issues"))


class TestRenderedNavigationMatchesRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.html = assemble_html_template()

    def test_every_registered_view_is_reachable(self) -> None:
        """每個註冊的 view 都必須有 view container，且有一條實際可點的路徑到它。

        #75 之前「可點的路徑」只有一種（一級 nav 按鈕），因此原測試直接要求
        每個 view 都有 ``nav-<view>``。收斂之後路徑有兩種：工作區的預設 panel
        由 nav 按鈕進入，其餘 panel 由 workspace tab 進入。斷言因此改為
        「view container 存在 + 有 switchView 入口 + 該入口確實被渲染出來」——
        比原本更嚴（原本不會檢查 tab 入口是否真的存在），仍然抓得到孤兒 view。
        """
        for item in nav.NAV_ITEMS:
            with self.subTest(workspace=item.workspace):
                self.assertIn(f'id="{nav.nav_button_id(item.workspace)}"', self.html)
                self.assertIn(
                    f'onclick="{nav.get_switch_view_call(item.panels[0].view)}" '
                    f'id="{nav.nav_button_id(item.workspace)}"',
                    self.html,
                    "工作區的 nav 按鈕必須開啟其第一個 panel",
                )
        for view in nav.view_names():
            with self.subTest(view=view):
                self.assertIn(f'id="{nav.view_container_id(view)}"', self.html)
                self.assertIn(f'onclick="{nav.get_switch_view_call(view)}"', self.html)
                is_workspace_default = view in {item.panels[0].view for item in nav.NAV_ITEMS}
                has_tab = f'id="{nav.workspace_tab_id(view)}"' in self.html
                self.assertTrue(
                    is_workspace_default or has_tab,
                    f"view {view} 既不是工作區預設 panel、也沒有 workspace tab，點不到",
                )

    def test_no_unregistered_nav_button_or_view_container(self) -> None:
        """反向契約：不得存在註冊表之外的 nav 按鈕、workspace tab 或孤兒 view container。

        #75 只把 nav 按鈕的鍵由 view 換成 workspace（``nav-`` 前綴仍是一級導覽的
        唯一錨點），並補上 workspace tab 的同款反向檢查。集合相等比較未被放寬。
        """
        rendered_nav = set(re.findall(r'id="nav-([a-z_]+)"', self.html))
        rendered_views = set(re.findall(r'class="view-container[^"]*" id="view-([a-z_]+)"', self.html))
        rendered_tabs = set(re.findall(r'id="wstab-([a-z_]+)"', self.html))
        rendered_strips = set(re.findall(r'id="wstrip-([a-z_]+)"', self.html))
        self.assertEqual(rendered_nav, set(nav.workspace_ids()))
        self.assertEqual(rendered_views, set(nav.view_names()))
        self.assertEqual(
            rendered_tabs,
            {p.view for item in nav.NAV_ITEMS if len(item.panels) > 1 for p in item.panels},
        )
        self.assertEqual(
            rendered_strips,
            {item.workspace for item in nav.NAV_ITEMS if len(item.panels) > 1},
        )

    def test_exactly_one_view_starts_active(self) -> None:
        """同時有兩個 active view 會讓初始畫面重疊；必須恰好一個。"""
        active_views = re.findall(r'class="view-container active" id="view-([a-z_]+)"', self.html)
        self.assertEqual(active_views, [nav.DEFAULT_VIEW])
        active_nav = re.findall(r'class="nav-item active" onclick="switchView\(\'([a-z_]+)\'\)"', self.html)
        # nav 按鈕帶的是「該工作區的預設 panel」，不再等於工作區 id；
        # 這裡刻意寫成 workspace_default_view(...) 而不是 DEFAULT_VIEW，
        # 免得日後預設工作區換成多 panel 的那天，這行仍然「剛好」通過。
        self.assertEqual(active_nav, [nav.workspace_default_view(nav.DEFAULT_WORKSPACE)])
        active_nav_ids = re.findall(r'class="nav-item active" onclick="[^"]*" id="nav-([a-z_]+)"', self.html)
        self.assertEqual(active_nav_ids, [nav.DEFAULT_WORKSPACE])

    def test_each_workspace_tab_strip_has_exactly_one_active_tab(self) -> None:
        """每個 strip 內恰好一個 active tab，且是該工作區的預設 panel。

        strip 只在其工作區啟用時可見，所以初始 HTML 有多個 active tab 並不會
        造成畫面重疊；真正會壞的是「某個 strip 一個 active tab 都沒有」——
        切到該工作區時 tab 條看起來沒有任何選中項。
        """
        for item in nav.NAV_ITEMS:
            if len(item.panels) < 2:
                continue
            with self.subTest(workspace=item.workspace):
                strip = re.search(
                    rf'id="{nav.workspace_tabstrip_id(item.workspace)}">(.*?)</div>',
                    self.html,
                    re.DOTALL,
                )
                self.assertIsNotNone(strip)
                assert strip is not None
                active = re.findall(r'class="ws-tab active"[^>]*id="wstab-([a-z_]+)"', strip.group(1))
                self.assertEqual(active, [item.panels[0].view])
                rendered = re.findall(r'id="wstab-([a-z_]+)"', strip.group(1))
                self.assertEqual(rendered, [p.view for p in item.panels])

    def test_workspace_tab_strip_is_generated_from_the_registry(self) -> None:
        """tab strip 必須就是註冊表產生的那一段，不能另有一份手寫副本。"""
        self.assertIn(nav.get_workspace_tabs_html(), self.html)

    def test_single_panel_workspace_renders_no_tab_strip(self) -> None:
        """單一 panel 的工作區不渲染 tab 條：只有一顆按鈕的 tab 條只會佔位。"""
        for item in nav.NAV_ITEMS:
            if len(item.panels) >= 2:
                continue
            with self.subTest(workspace=item.workspace):
                self.assertNotIn(f'id="{nav.workspace_tabstrip_id(item.workspace)}"', self.html)
                self.assertNotIn(f'id="{nav.workspace_tab_id(item.panels[0].view)}"', self.html)

    def test_sidebar_nav_menu_is_generated_from_registry(self) -> None:
        """sidebar 的導覽選單必須就是 NAV_ITEMS 產生的那一段，不能另有一份手寫副本。"""
        self.assertIn(nav.get_nav_menu_html(), get_sidebar_html())


#: 八個 V2 一級頁面各自的「招牌」DOM 錨點。
#: 這些 id 是各 section module 真正渲染內容的地方（表格 body / chart canvas / 卡片），
#: 因此可以用來反向證明「搬移沒有把功能弄丟」——只檢查 view container 還在是不夠的，
#: 空的 container 一樣會通過。
V2_VIEW_CONTENT_ANCHORS: dict[str, tuple[str, ...]] = {
    "overview": ("kpiCFUsers", "chartDailyTrend", "topIssuesPreviewBody"),
    "issues": ("issuesListContainer", "filterLifecycle", "filterPriority"),
    "version_health": ("versionHealthTableBody",),
    # 裝置分析：chart + 表格三件套；#75 只換它掛在哪個工作區，不得刪。
    "devices": ("chartDeviceModels", "chartOSVersions", "deviceModelsTableBody"),
    "releases": ("releasesTableBody", "filterReleaseStatus", "searchReleaseVer"),
    # Pipeline Health 與 Alert Delivery Audit 兩者都在這個 panel 裡。
    "notifications": ("pipelineCardsGrid", "alertDeliverySection", "alertDeliveryHealthGrid"),
    "ai_insights": ("aiFullOverviewText", "aiDistributionInsights", "recommendedActionsTableBody"),
    # AI Policy / AI Telemetry（observability）與 App 設定表格。
    "settings": ("settingsTableBody", "aiPolicyCard", "aiObservabilityCard"),
}


class TestNoCapabilityLostInTheMove(unittest.TestCase):
    """「現有功能不可因搬移而遺失」(#75 AC) 的結構性把關。

    導覽收斂最容易造成的損失不是「按鈕不見了」，而是某個 view 的內容在搬動時
    被順手刪掉／漏接。因此這裡對每個 V2 一級頁面的招牌 DOM 錨點下斷言，
    並要求該錨點確實落在它應該落在的那個 view container 裡（搬到別的 view
    也算漂移）。
    """

    def setUp(self) -> None:
        self.html = assemble_html_template()
        # 依 view container 切段，才能檢查錨點落在正確的 view 內。
        # 邊界必須是 ``</section>``——若只用「下一個 view container 開頭」切，
        # 最後一個 view 會把後面所有 <script> 都吃進來，反向斷言就全部失效。
        self.view_bodies = dict(
            re.findall(
                r'<section class="view-container[^"]*" id="view-([a-z_]+)">(.*?)</section>',
                self.html,
                re.DOTALL,
            )
        )
        self.assertEqual(set(self.view_bodies), set(nav.view_names()))

    def test_every_v2_view_content_anchor_is_covered(self) -> None:
        """錨點表必須覆蓋註冊表裡的每一個 view，否則新增 view 會悄悄逃過本檔檢查。"""
        self.assertEqual(set(V2_VIEW_CONTENT_ANCHORS), set(nav.view_names()))

    def test_each_v2_view_still_renders_its_own_content(self) -> None:
        for view, anchors in V2_VIEW_CONTENT_ANCHORS.items():
            body = self.view_bodies.get(view)
            self.assertIsNotNone(body, f"view {view} 的 container 不存在")
            assert body is not None
            for anchor in anchors:
                with self.subTest(view=view, anchor=anchor):
                    self.assertIn(f'id="{anchor}"', body)

    def test_release_detail_modal_stays_page_level(self) -> None:
        """release 詳情是 page-level overlay，不屬於任何 view container。

        它是 deep link 還原 version context 的落點（``applyRoute`` 會呼叫
        ``openReleaseDetail``）；若被誤搬進某個 view container，切到別的工作區
        時整個 modal 會連著 container 一起被隱藏，帶 version 的連結就開不出東西。
        """
        self.assertIn('id="releaseDetailModal"', self.html)
        for view, body in self.view_bodies.items():
            with self.subTest(view=view):
                self.assertNotIn('id="releaseDetailModal"', body)

    def test_device_breakdown_is_not_top_level_but_reachable_from_the_issue_context(self) -> None:
        """`裝置分析` 不再是一級導覽，但其 breakdown 仍必須從問題脈絡進得去 (#75 AC)。

        兩件事都要驗：不是一級項目（``nav-devices`` 不存在），以及仍然掛在
        ``問題`` 工作區底下並有一顆真的可以點的 tab。只驗前者會讓「直接把
        裝置分析刪掉」也通過。
        """
        self.assertNotIn("devices", nav.workspace_ids())
        self.assertNotIn(f'id="{nav.NAV_BUTTON_ID_PREFIX}devices"', self.html)
        self.assertEqual(nav.workspace_of("devices"), "issues")
        self.assertIn(
            f'onclick="{nav.get_switch_view_call("devices")}" '
            f'id="{nav.workspace_tab_id("devices")}"',
            self.html,
        )

    def test_issue_specific_ai_action_stays_inside_the_issues_view(self) -> None:
        """Issue-specific AI 分析／行動不得被搬進 ``系統`` (#75 AC)。

        搬進 System 的只有 generic AI admin（Policy / Telemetry）與 app 層的
        AI 摘要；單一 issue 的 AI 分析與「複製 AI 修復 Prompt」必須留在問題脈絡，
        使用者不需要先切到 ``系統`` 才能對一個 issue 動作。
        """
        issues_js = get_issues_js()
        self.assertIn("copyFixPrompt(", issues_js)
        self.assertIn("ai_analysis", issues_js)
        # 反向：這兩個 issue 層的入口不得只存在於 system 工作區的 panel 內。
        system_views = {p.view for item in nav.NAV_ITEMS if item.workspace == "system" for p in item.panels}
        for view in system_views:
            with self.subTest(view=view):
                self.assertNotIn("copyFixPrompt(", self.view_bodies[view])


class TestViewRoutingContract(unittest.TestCase):
    def test_switch_view_uses_dom_class_routing(self) -> None:
        """view 切換本體仍是 DOM class routing；#78 只在其後加上 URL state 同步。"""
        routing_js = nav.get_navigation_js()
        self.assertIn("function switchView(viewName)", routing_js)
        self.assertIn('classList.add("active")', routing_js)

    def test_no_ad_hoc_url_routing_outside_navigation_module(self) -> None:
        """URL state 的讀寫必須只存在於 navigation 模組這一份集中來源。

        #71 當時的守門條件是「整份 template 不得出現任何 URL routing」，
        目的是防止 deep-link 在「零行為變更」的機械式重構裡偷跑。
        #78 已正式交付 deep-link routing，該條件因此改為
        「不得存在 navigation 模組之外的 ad-hoc URL routing」——
        這才是真正要防的漂移：section module 各自操作 location.hash，
        導致 routing 又變成多份、與導覽註冊表脫鉤。

        強度並未下降，反而多守了一項：
        - History API 的呼叫（``history.pushState`` / ``history.replaceState``）
          在整份 template 內仍然全面禁止，因為 dashboard 常以 ``file://`` 開啟，
          該 API 會被瀏覽器擋成 SecurityError；deep-link 一律走 fragment。
        - ``location.hash`` / ``hashchange`` 的出現位置被限制在單一函式產出的區塊內。
        """
        template = assemble_html_template()
        routing_js = nav.get_navigation_js()
        self.assertIn(routing_js, template)

        outside_navigation = template.replace(routing_js, "")
        for url_api in ("location.hash", "hashchange", "pushState", "replaceState"):
            with self.subTest(url_api=url_api):
                self.assertNotIn(url_api, outside_navigation)
        for url_api in ("location.hash", "hashchange"):
            with self.subTest(url_api=url_api):
                self.assertIn(url_api, routing_js)
        # navigation 模組內雖然在註解裡提到 History API，但不得真的呼叫它。
        for history_call in ("history.pushState", "history.replaceState"):
            with self.subTest(history_call=history_call):
                self.assertNotIn(history_call, template)

    def test_routing_id_prefixes_have_single_definition(self) -> None:
        """switchView 的 id 前綴與 Python 端推導規則必須來自同一組常數。

        #75 之後 nav 按鈕的鍵由 view 換成 workspace，因此這裡斷言的是
        「``nav-`` 前綴接的是 workspace 變數、``view-`` 前綴接的是 view 變數」——
        接錯變數正是這次改動最容易犯的錯（``nav-<view>`` 永遠找不到按鈕，
        導覽會整排暗掉），原本只檢查前綴字面值的版本抓不到它。
        四個前綴各自的推導函式也一併對照，避免有人在別處手寫 ``"nav-" + x``。
        """
        routing_js = nav.get_navigation_js()
        self.assertIn(f'$("{nav.NAV_BUTTON_ID_PREFIX}" + workspace)', routing_js)
        self.assertIn(f'$("{nav.VIEW_CONTAINER_ID_PREFIX}" + viewName)', routing_js)
        self.assertIn(f'$("{nav.WORKSPACE_TABSTRIP_ID_PREFIX}" + workspace)', routing_js)
        self.assertIn(f'$("{nav.WORKSPACE_TAB_ID_PREFIX}" + viewName)', routing_js)
        self.assertEqual(nav.nav_button_id("issues"), f"{nav.NAV_BUTTON_ID_PREFIX}issues")
        self.assertEqual(nav.view_container_id("issues"), f"{nav.VIEW_CONTAINER_ID_PREFIX}issues")
        self.assertEqual(
            nav.workspace_tabstrip_id("issues"), f"{nav.WORKSPACE_TABSTRIP_ID_PREFIX}issues"
        )
        self.assertEqual(nav.workspace_tab_id("devices"), f"{nav.WORKSPACE_TAB_ID_PREFIX}devices")

    def test_the_four_id_prefixes_are_mutually_non_overlapping(self) -> None:
        """四組 DOM id 前綴不得互為前綴。

        反向契約與 Node 端的 ``querySelectorAll`` 模擬都靠「id 前綴」分辨這四種元素；
        若 ``wstab-`` 之類的前綴以 ``nav-`` / ``view-`` 開頭，workspace tab 會被
        當成未註冊的一級導覽按鈕或孤兒 view container，反向檢查就會誤報／漏報。
        """
        prefixes = (
            nav.NAV_BUTTON_ID_PREFIX,
            nav.VIEW_CONTAINER_ID_PREFIX,
            nav.WORKSPACE_TABSTRIP_ID_PREFIX,
            nav.WORKSPACE_TAB_ID_PREFIX,
        )
        self.assertEqual(len(set(prefixes)), len(prefixes))
        for outer in prefixes:
            for inner in prefixes:
                if outer is inner:
                    continue
                with self.subTest(outer=outer, inner=inner):
                    self.assertFalse(outer.startswith(inner))


if __name__ == "__main__":
    unittest.main()
