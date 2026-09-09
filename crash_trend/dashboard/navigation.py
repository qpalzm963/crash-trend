"""Dashboard 一級導覽與 view routing 的單一集中來源 (Issue #71).

V3.1 為純機械式重構：本模組僅把原先散落在 `assets.py` 與各 section module
的導覽定義與 routing 字串集中管理，產出之 HTML/JS 與重構前逐位元相同。

集中管理的內容：
- ``NAV_ITEMS``：一級導覽項目的順序、view 名稱、標籤與圖示。
- ``DEFAULT_VIEW``：初始啟用的 view（決定 nav 按鈕與 view container 的 ``active``）。
- ``nav_button_id`` / ``view_container_id``：由 view 名稱推導 DOM id 的唯一規則。
- ``get_nav_menu_html``：由 ``NAV_ITEMS`` 產生 sidebar 導覽選單。
- ``get_view_container_open_tag``：供各 section module 產生 view container 開頭標籤，
  避免 module 內硬寫 ``id="view-xxx"``。
- ``get_switch_view_call``：供 section module 產生 ``onclick`` routing 呼叫。
- ``get_navigation_js``：產生 client 端 ``switchView()``（維持既有 DOM-class routing，
  不引入 URL/hash state）。
"""

from __future__ import annotations

from dataclasses import dataclass

NAV_BUTTON_ID_PREFIX = "nav-"
VIEW_CONTAINER_ID_PREFIX = "view-"


@dataclass(frozen=True)
class NavItem:
    """單一一級導覽項目；``view`` 同時決定 nav 按鈕與 view container 的 DOM id。"""

    view: str
    label: str
    icon_svg: str


NAV_ITEMS: tuple[NavItem, ...] = (
    NavItem(
        view="overview",
        label="總覽 (Overview)",
        icon_svg='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>',
    ),
    NavItem(
        view="issues",
        label="問題列表 (Issues)",
        icon_svg='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    ),
    NavItem(
        view="version_health",
        label="版本健康度 (Version Health)",
        icon_svg='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>',
    ),
    NavItem(
        view="devices",
        label="裝置分析 (Devices)",
        icon_svg='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="5" y="2" width="14" height="20" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>',
    ),
    NavItem(
        view="releases",
        label="發佈版本 (Releases)",
        icon_svg='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/></svg>',
    ),
    NavItem(
        view="notifications",
        label="通知 (Notifications)",
        icon_svg='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>',
    ),
    NavItem(
        view="ai_insights",
        label="AI 分析 (AI Insights)",
        icon_svg='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83"/></svg>',
    ),
    NavItem(
        view="settings",
        label="設定 (Settings)",
        icon_svg='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    ),
)

#: 初始啟用的 view；nav 按鈕與 view container 的 ``active`` class 皆由此推導。
DEFAULT_VIEW = NAV_ITEMS[0].view


def view_names() -> tuple[str, ...]:
    """回傳所有一級導覽 view 名稱（依導覽顯示順序）。"""
    return tuple(item.view for item in NAV_ITEMS)


def nav_button_id(view: str) -> str:
    """回傳導覽按鈕的 DOM id。"""
    return f"{NAV_BUTTON_ID_PREFIX}{view}"


def view_container_id(view: str) -> str:
    """回傳 view container 的 DOM id。"""
    return f"{VIEW_CONTAINER_ID_PREFIX}{view}"


def get_switch_view_call(view: str) -> str:
    """回傳切換 view 的 client 端呼叫字串，供 ``onclick`` 使用。"""
    return f"switchView('{view}')"


def get_view_container_open_tag(view: str) -> str:
    """回傳 view container 的開頭標籤；``DEFAULT_VIEW`` 自動帶 ``active``。"""
    classes = "view-container active" if view == DEFAULT_VIEW else "view-container"
    return f'<section class="{classes}" id="{view_container_id(view)}">'


def get_nav_item_html(item: NavItem) -> str:
    """回傳單一導覽按鈕的 HTML。"""
    classes = "nav-item active" if item.view == DEFAULT_VIEW else "nav-item"
    return (
        f'    <button class="{classes}" onclick="{get_switch_view_call(item.view)}" id="{nav_button_id(item.view)}">\n'
        f'      <span class="nav-icon">\n'
        f"        {item.icon_svg}\n"
        f"      </span>\n"
        f'      <span class="nav-label">{item.label}</span>\n'
        f"    </button>\n"
    )


def get_nav_menu_html() -> str:
    """回傳 sidebar 導覽選單 HTML（由 ``NAV_ITEMS`` 產生）。"""
    return (
        '  <nav class="nav-menu">\n'
        + "".join(get_nav_item_html(item) for item in NAV_ITEMS)
        + "  </nav>\n"
    )


def get_navigation_js() -> str:
    """回傳 client 端 view routing 邏輯。

    維持既有 DOM-class routing（移除所有 ``active`` 後為目標 nav/view 加上），
    不使用 URL/hash/pushState state；deep-link routing 屬 Issue #78 範圍。
    """
    return (
        "// Navigation between views\n"
        "function switchView(viewName) {\n"
        '  document.querySelectorAll(".nav-item").forEach(el => el.classList.remove("active"));\n'
        f'  const btn = $("{NAV_BUTTON_ID_PREFIX}" + viewName);\n'
        "  if (btn) btn.classList.add(\"active\");\n"
        "\n"
        '  document.querySelectorAll(".view-container").forEach(el => el.classList.remove("active"));\n'
        f'  const v = $("{VIEW_CONTAINER_ID_PREFIX}" + viewName);\n'
        '  if (v) v.classList.add("active");\n'
        "\n"
        "  if (window.innerWidth <= 768) {\n"
        '    $("sidebar").classList.remove("mobile-open");\n'
        "  }\n"
        "}\n"
        "\n"
    )
