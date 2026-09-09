"""Navigation / view-routing single-source-of-truth contract tests (Issue #71).

V3.1 把導覽定義與 routing 字串集中到 `crash_trend.dashboard.navigation`。
這些測試存在的理由是：一旦有人再度在 section module 或 assets 內硬寫
`id="view-xxx"` / `switchView('xxx')`，導覽與 view container 就會再次漂移
（點得到按鈕卻切不到頁面，或存在點不到的孤兒 view）。因此契約是
「渲染結果必須完全由 NAV_ITEMS 推導」，而非只檢查某些字串存在。
"""

from __future__ import annotations

import re
import unittest

from crash_trend.dashboard import navigation as nav
from crash_trend.dashboard.assets import get_sidebar_html
from crash_trend.dashboard.renderer import assemble_html_template


class TestNavigationRegistry(unittest.TestCase):
    def test_top_level_nav_items_are_unchanged(self) -> None:
        """一級導覽維持 V2 的 8 項與原順序；收斂成 V3 四項屬後續 ticket，不得在此悄悄改動。"""
        self.assertEqual(
            nav.view_names(),
            (
                "overview",
                "issues",
                "version_health",
                "devices",
                "releases",
                "notifications",
                "ai_insights",
                "settings",
            ),
        )

    def test_default_view_is_overview(self) -> None:
        """初始 active view 只有一個定義來源，nav 按鈕與 view container 都由它推導。"""
        self.assertEqual(nav.DEFAULT_VIEW, "overview")
        self.assertIn("view-container active", nav.get_view_container_open_tag("overview"))
        self.assertNotIn("active", nav.get_view_container_open_tag("issues"))


class TestRenderedNavigationMatchesRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.html = assemble_html_template()

    def test_every_registered_view_has_nav_button_and_container(self) -> None:
        """每個註冊的 view 都必須同時存在可點的 nav 按鈕與對應 view container。"""
        for view in nav.view_names():
            with self.subTest(view=view):
                self.assertIn(f'id="{nav.nav_button_id(view)}"', self.html)
                self.assertIn(f'id="{nav.view_container_id(view)}"', self.html)
                self.assertIn(f'onclick="{nav.get_switch_view_call(view)}"', self.html)

    def test_no_unregistered_nav_button_or_view_container(self) -> None:
        """反向契約：不得存在註冊表之外的 nav 按鈕或孤兒 view container。"""
        registered = set(nav.view_names())
        rendered_nav = set(re.findall(r'id="nav-([a-z_]+)"', self.html))
        rendered_views = set(re.findall(r'class="view-container[^"]*" id="view-([a-z_]+)"', self.html))
        self.assertEqual(rendered_nav, registered)
        self.assertEqual(rendered_views, registered)

    def test_exactly_one_view_starts_active(self) -> None:
        """同時有兩個 active view 會讓初始畫面重疊；必須恰好一個。"""
        active_views = re.findall(r'class="view-container active" id="view-([a-z_]+)"', self.html)
        self.assertEqual(active_views, [nav.DEFAULT_VIEW])
        active_nav = re.findall(r'class="nav-item active" onclick="switchView\(\'([a-z_]+)\'\)"', self.html)
        self.assertEqual(active_nav, [nav.DEFAULT_VIEW])

    def test_sidebar_nav_menu_is_generated_from_registry(self) -> None:
        """sidebar 的導覽選單必須就是 NAV_ITEMS 產生的那一段，不能另有一份手寫副本。"""
        self.assertIn(nav.get_nav_menu_html(), get_sidebar_html())


class TestViewRoutingContract(unittest.TestCase):
    def test_switch_view_uses_dom_class_routing_without_url_state(self) -> None:
        """#71 明確不引入 URL state；deep-link routing 屬 Issue #78。"""
        routing_js = nav.get_navigation_js()
        self.assertIn("function switchView(viewName)", routing_js)
        self.assertIn('classList.add("active")', routing_js)
        for url_api in ("location.hash", "pushState", "replaceState", "hashchange"):
            self.assertNotIn(url_api, assemble_html_template())

    def test_routing_id_prefixes_have_single_definition(self) -> None:
        """switchView 的 id 前綴與 Python 端推導規則必須來自同一組常數。"""
        self.assertIn(f'$("{nav.NAV_BUTTON_ID_PREFIX}" + viewName)', nav.get_navigation_js())
        self.assertIn(f'$("{nav.VIEW_CONTAINER_ID_PREFIX}" + viewName)', nav.get_navigation_js())
        self.assertEqual(nav.nav_button_id("issues"), f"{nav.NAV_BUTTON_ID_PREFIX}issues")
        self.assertEqual(nav.view_container_id("issues"), f"{nav.VIEW_CONTAINER_ID_PREFIX}issues")


if __name__ == "__main__":
    unittest.main()
