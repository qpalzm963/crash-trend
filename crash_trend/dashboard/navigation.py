"""Dashboard 一級導覽、view routing 與 deep-link 契約的單一集中來源 (Issue #71, #78, #75).

V3.1 (#71) 為純機械式重構：把原先散落在 `assets.py` 與各 section module
的導覽定義與 routing 字串集中管理。
V3.1.1 (#78) 在同一個集中點上加入 URL/hash deep-link routing，
不另建第二份 routing registry。
V3.5 (#75) 在同一份註冊表上把一級導覽收斂成四個**工作區 (workspace)**
``總覽 / 版本 / 問題 / 系統``，原本的八個一級頁面成為工作區底下的 panel。

集中管理的內容：
- ``NAV_ITEMS``：一級導覽工作區的順序、標籤、圖示，以及其底下的 panel（view）。
- ``DEFAULT_WORKSPACE`` / ``DEFAULT_VIEW``：初始啟用的工作區與 view。
- ``nav_button_id``（workspace） / ``view_container_id``（view） /
  ``workspace_tabstrip_id`` / ``workspace_tab_id``：DOM id 的唯一推導規則。
- ``workspace_of`` / ``workspace_default_view`` / ``resolve_route_view``：
  view ↔ 工作區的唯一映射。
- ``get_nav_menu_html``：由 ``NAV_ITEMS`` 產生 sidebar 導覽選單。
- ``get_workspace_tabs_html``：由 ``NAV_ITEMS`` 產生工作區內的 panel 切換 tab。
- ``get_view_container_open_tag``：供各 section module 產生 view container 開頭標籤，
  避免 module 內硬寫 ``id="view-xxx"``。
- ``get_switch_view_call``：供 section module 產生 ``onclick`` routing 呼叫。
- ``get_navigation_js``：產生 client 端 ``switchView()`` 與 deep-link routing。
- ``build_deep_link`` / ``build_release_decision_link``：canonical deep link 的唯一產生器，
  供 Google Chat alert 等 consumer 使用，避免各自拼 URL。

V3.5 的 IA 收斂與 routing 相容性 (#75)：
- **routing 粒度不變，仍是 panel view**。V3.5 沒有刪掉任何 view container，
  六個不再是一級項目的頁面（``version_health`` / ``releases`` / ``devices``
  / ``notifications`` / ``ai_insights`` / ``settings``）全部成為工作區底下的
  panel，因此舊的 ``#version_health`` 等 deep link 依然是**一等公民**，
  會開到同一份內容，只是 nav 亮的是新的工作區。
  這是刻意選擇「保留」而非「別名對照表」：沒有 old→new 映射需要維護，
  也不會有「舊連結掉回首頁」。``resolveRoute`` 對未知 view 的 Overview
  fallback 因此完全碰不到這六個名字。
- **工作區 id 是額外接受的別名**：使用者看到四個一級項目後可能手打
  ``#system``；``resolve_route_view`` 會把工作區 id 正規化成該工作區的預設
  panel view，canonical 形式永遠落在 panel view 空間，同一個目標不會有兩種寫法。
- **一級導覽按鈕以 workspace 為鍵**（``nav-<workspace>``），view container 仍以
  view 為鍵（``view-<view>``）。``switchView`` 一次同時處理三者（nav 按鈕、
  view container、workspace tab），避免「內容切了、導覽停在別的工作區」，
  並先把傳入的 token 正規化成 panel view——否則傳進工作區 id 會把所有
  view container 的 ``active`` 清掉卻沒有任何一個補回來，內容區整片空白。

Deep-link URL 契約 (#78)::

    <base_url>#<view>?app=<app>&platform=<platform>&version=<version>

只使用 URL fragment：dashboard 是自包含靜態 HTML，經常以 ``file://`` 或純靜態
空間開啟，query string 需要 server/reload 配合且可能被剝除，fragment 則保證
純 client 端可讀且不觸發 reload。同理，本模組刻意不使用 History API
（``pushState`` / ``replaceState`` 在 ``file://`` 下會被瀏覽器擋成 SecurityError），
history 進出完全靠 ``location.hash`` 賦值 + ``hashchange``。

已定案的行為決策 (#78)：
- **支援 browser back/forward**：每次 view / context 變更都以 ``location.hash``
  賦值產生一個 history entry，``hashchange`` 再把該 entry 套回畫面。
  自己寫進去的 hash 由 ``curRouteHash`` 濾掉，只有真正的 back/forward 會重新套用。
- **無效 context 一律降級而非改寫 URL**：不存在的 app / platform / version 會被丟棄
  （以 toast 告知），畫面退回可顯示的狀態，但 URL 保持原樣。若在載入時把 URL
  正規化成一個新的 history entry，使用者按 back 會回到原始壞連結並再次被正規化，
  形成回不去的 history 迴圈；URL 會在使用者下一次主動導覽時自然被寫成正規形式。
- **context 驗證是串聯的（cascading），降級只保留安全前綴**：``app`` → ``platform``
  → ``version`` 是一條依賴鏈，下游的意義由上游界定。若明確要求的 app 不存在，
  platform / version 不得改用 fallback app 去驗證；若明確要求的 platform 不存在，
  version 不得在「不限平台」的條件下命中。否則一條半殘的連結會安靜地開到
  **另一個** release，比沒有連結更糟。「完全沒要求」（無 ``app`` 參數 → 用目前/預設 app）
  與「要求了但無效」是不同狀況，兩者必須分開判斷。
- **套用 route 是取代而非疊加**：route 移除掉的 context 必須連同其 UI 一起清掉
  （關掉 release 詳情、platform select 回到中性 ``ALL``），否則 back/forward 會留下
  與 URL 不一致的殘留畫面。清除都在 ``routeApplyDepth > 0`` 的窗口內完成，
  不會產生額外 history entry。
- **canonical 編碼兩端逐位元一致**：見 ``encode_route_value``；client 端不直接用
  ``encodeURIComponent``（它放行 ``!'()*``），而是額外 escape 這些字元來對齊 Python。
- **version context 的還原方式是開啟該 release 的詳情**，不去改動 ``filterVersion``
  下拉選單（其選項由 ``updateVersionFilterOptions`` 動態產生，寫入不存在的值會讓
  篩選器變成空白）。Decision-first Overview 對 version context 的呈現屬 #73。
- 本模組只提供 ``build_release_decision_link``；把它接進 Google Chat 訊息是
  刻意留給後續 ticket 的動作，#78 不改 ``crash_trend/alerts``。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, unquote

NAV_BUTTON_ID_PREFIX = "nav-"
VIEW_CONTAINER_ID_PREFIX = "view-"
#: workspace tab strip 與其中單一 tab 按鈕的 DOM id 前綴 (#75)。
#: 刻意不沿用 ``nav-`` / ``view-``：那兩個前綴是「一級導覽按鈕」與「view container」
#: 的反向契約錨點（見 ``tests/test_dashboard_navigation.py``），workspace tab
#: 若混用同一前綴會被誤判成孤兒 view 或未註冊的導覽按鈕。
WORKSPACE_TABSTRIP_ID_PREFIX = "wstrip-"
WORKSPACE_TAB_ID_PREFIX = "wstab-"

#: client 端 IA map 的變數名；由 Python 端的 ``NAV_ITEMS`` 產生其內容，
#: 名稱集中在此以便契約測試比對，不在測試裡另抄一份字面值。
ROUTE_VIEW_WORKSPACE_CONST = "ROUTE_VIEW_WORKSPACE"
ROUTE_WORKSPACE_DEFAULT_CONST = "ROUTE_WORKSPACE_DEFAULT_VIEW"

#: deep link 的 fragment query 參數名（Python 端與 client 端共用同一組字面值）。
ROUTE_PARAM_APP = "app"
ROUTE_PARAM_PLATFORM = "platform"
ROUTE_PARAM_VERSION = "version"

#: canonical release decision link 所指向的 view；#73 的 Decision-first Overview 在此。
DECISION_VIEW = "overview"

#: deep link 還原 platform context 時要同步的既有 filter select id。
#: 這些 id 定義在 `releases.py` / `issues.py` 的 HTML 內，
#: 由 ``tests/test_dashboard_deep_link.py`` 反向驗證其仍存在於 render 結果，避免漂移。
ROUTE_PLATFORM_SELECT_IDS = ("filterReleasePlatform", "filterPlatform")

#: platform filter select 的「中性」選項值（全部平台）。route 移除 platform context 時
#: 要把這些 select 回復成此值，而不是空字串（空字串不是有效 option，會讓篩選器變空白）。
#: 同樣由 ``tests/test_dashboard_deep_link.py`` 反向驗證其仍是 render 結果中的 option。
ROUTE_PLATFORM_ALL = "ALL"

#: ``encodeURIComponent`` 不會編碼、但 Python ``quote(safe="")`` 會編碼的字元。
#: 兩端 canonical encoder 必須逐位元一致（同一個 release 產生同一條連結），
#: 因此 client 端在 ``encodeURIComponent`` 之後額外補上這幾個字元的 escape。
#: 由 ``tests/test_dashboard_deep_link.py`` 的 parity 測試逐字元把關。
ROUTE_EXTRA_ESCAPE_CHARS = "!'()*"


@dataclass(frozen=True)
class NavPanel:
    """工作區內的一個 panel；``view`` 決定 view container 與 workspace tab 的 DOM id。"""

    view: str
    label: str


@dataclass(frozen=True)
class NavItem:
    """單一一級導覽項目（V3.5 起代表一個「工作區 workspace」）。

    ``workspace`` 決定 nav 按鈕的 DOM id；``panels`` 是這個工作區底下的 view
    （順序即 workspace tab 的顯示順序，第一個為點擊 nav 時的預設 panel）。
    單一 panel 的工作區不會渲染 tab strip，行為與 V2 的一級頁面完全相同。
    """

    workspace: str
    label: str
    icon_svg: str
    panels: tuple[NavPanel, ...]


NAV_ITEMS: tuple[NavItem, ...] = (
    NavItem(
        workspace="overview",
        label="總覽 (Overview)",
        icon_svg='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/></svg>',
        panels=(
            NavPanel(view="overview", label="總覽 (Overview)"),
        ),
    ),
    NavItem(
        workspace="versions",
        label="版本 (Versions)",
        icon_svg='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/></svg>',
        panels=(
            NavPanel(view="version_health", label="版本健康度 (Version Health)"),
            NavPanel(view="releases", label="發佈版本 (Release Catalog)"),
        ),
    ),
    NavItem(
        workspace="issues",
        label="問題 (Issues)",
        icon_svg='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
        panels=(
            NavPanel(view="issues", label="問題列表 (Issue List)"),
            NavPanel(view="devices", label="裝置與系統 Breakdown (Devices & OS)"),
        ),
    ),
    NavItem(
        workspace="system",
        label="系統 (System)",
        icon_svg='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
        panels=(
            NavPanel(view="notifications", label="數據管道與通知 (Pipeline & Alerts)"),
            NavPanel(view="ai_insights", label="AI 分析 (AI Insights)"),
            NavPanel(view="settings", label="設定與 AI 治理 (Settings & AI Governance)"),
        ),
    ),
)

#: 初始啟用的工作區與 view；nav 按鈕、view container 與 workspace tab 的
#: ``active`` class 皆由這兩個常數推導。
DEFAULT_WORKSPACE = NAV_ITEMS[0].workspace
DEFAULT_VIEW = NAV_ITEMS[0].panels[0].view


def workspace_ids() -> tuple[str, ...]:
    """回傳所有一級導覽工作區 id（依導覽顯示順序）。"""
    return tuple(item.workspace for item in NAV_ITEMS)


def view_names() -> tuple[str, ...]:
    """回傳所有 view 名稱（依工作區順序、工作區內 panel 順序）。

    這是 routing 的粒度：deep link 的 ``#<view>`` 一律指 panel view，
    V3.5 的導覽收斂沒有移除任何 view，只是改變它們掛在哪個工作區底下。
    """
    return tuple(panel.view for item in NAV_ITEMS for panel in item.panels)


def panels_of(workspace: str) -> tuple[NavPanel, ...]:
    """回傳某工作區底下的 panel（依顯示順序）。"""
    for item in NAV_ITEMS:
        if item.workspace == workspace:
            return item.panels
    raise KeyError(f"未註冊的 workspace: {workspace!r}；可用值：{workspace_ids()}")


def workspace_of(view: str) -> str:
    """回傳某 view 所屬的工作區 id（決定該 view 啟用時哪個 nav 按鈕要亮）。"""
    for item in NAV_ITEMS:
        for panel in item.panels:
            if panel.view == view:
                return item.workspace
    raise KeyError(f"未註冊的 dashboard view: {view!r}；可用值：{view_names()}")


def workspace_default_view(workspace: str) -> str:
    """回傳點擊某工作區 nav 按鈕時要開啟的 view。"""
    return panels_of(workspace)[0].view


def resolve_route_view(token: str) -> str | None:
    """把 route token 解析成 panel view；無法解析時回傳 ``None``。

    token 可以是 panel view（canonical 形式）或工作區 id（IA 層的別名，
    例如使用者看到四個一級項目後手打 ``#system``）。工作區 id 一律正規化為
    該工作區的預設 panel view，因此 canonical 連結永遠落在 panel view 空間，
    同一個目標不會有兩種寫法。
    """
    if token in view_names():
        return token
    if token in workspace_ids():
        return workspace_default_view(token)
    return None


def nav_button_id(workspace: str) -> str:
    """回傳一級導覽按鈕的 DOM id（V3.5 起以 workspace 為鍵，不再是 view）。"""
    return f"{NAV_BUTTON_ID_PREFIX}{workspace}"


def view_container_id(view: str) -> str:
    """回傳 view container 的 DOM id。"""
    return f"{VIEW_CONTAINER_ID_PREFIX}{view}"


def workspace_tabstrip_id(workspace: str) -> str:
    """回傳某工作區 tab strip 的 DOM id。"""
    return f"{WORKSPACE_TABSTRIP_ID_PREFIX}{workspace}"


def workspace_tab_id(view: str) -> str:
    """回傳某 panel 對應 workspace tab 按鈕的 DOM id。"""
    return f"{WORKSPACE_TAB_ID_PREFIX}{view}"


def get_switch_view_call(view: str) -> str:
    """回傳切換 view 的 client 端呼叫字串，供 ``onclick`` 使用。"""
    return f"switchView('{view}')"


def get_view_container_open_tag(view: str) -> str:
    """回傳 view container 的開頭標籤；``DEFAULT_VIEW`` 自動帶 ``active``。"""
    classes = "view-container active" if view == DEFAULT_VIEW else "view-container"
    return f'<section class="{classes}" id="{view_container_id(view)}">'


def get_nav_item_html(item: NavItem) -> str:
    """回傳單一一級導覽按鈕的 HTML；點擊時開啟該工作區的預設 panel。"""
    classes = "nav-item active" if item.workspace == DEFAULT_WORKSPACE else "nav-item"
    default_view = item.panels[0].view
    return (
        f'    <button class="{classes}" onclick="{get_switch_view_call(default_view)}" id="{nav_button_id(item.workspace)}">\n'
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


def get_workspace_tabs_html() -> str:
    """回傳所有多 panel 工作區的 tab strip HTML (#75)。

    只有 panel 數 > 1 的工作區才會產生 strip：單一 panel 的工作區沒有可切換的
    對象，渲染一個只有一顆按鈕的 tab 條只會佔位並讓「目前在哪」變得更難讀。
    strip 與 view container 是兩套不同的 DOM 前綴，切換由 ``switchView`` 一次
    同時處理（見 ``get_navigation_js``），因此不會出現 tab 與內容不一致的狀態。
    """
    blocks = []
    for item in NAV_ITEMS:
        if len(item.panels) < 2:
            continue
        strip_classes = "ws-tabstrip active" if item.workspace == DEFAULT_WORKSPACE else "ws-tabstrip"
        tabs = "".join(
            f'      <button class="{"ws-tab active" if panel.view == item.panels[0].view else "ws-tab"}"'
            f' onclick="{get_switch_view_call(panel.view)}" id="{workspace_tab_id(panel.view)}">'
            f"{panel.label}</button>\n"
            for panel in item.panels
        )
        blocks.append(
            f'    <div class="{strip_classes}" id="{workspace_tabstrip_id(item.workspace)}">\n'
            f"{tabs}"
            f"    </div>\n"
        )
    return "    <!-- WORKSPACE TABS (工作區內 panel 切換) -->\n" + "".join(blocks)


@dataclass(frozen=True)
class DeepLinkRoute:
    """一條 canonical deep link 所描述的 routing 意圖。

    ``view`` 保證是註冊過的 view；未知 view 於 parse 時降級為 ``DEFAULT_VIEW``。
    ``app`` / ``platform`` / ``version`` 為選填 context；此處僅代表「連結要求的內容」，
    是否真的存在於 bundle 由 client 端 resolve 時判定。
    """

    view: str
    app: str | None = None
    platform: str | None = None
    version: str | None = None


def encode_route_value(value: str) -> str:
    """canonical deep link 的參數值編碼（Python 與 client 端共用同一條規則）。

    規則採「RFC 3986 unreserved 之外全部 percent-encode」，也就是
    ``quote(safe="")``：只有 ``A-Za-z0-9-_.~`` 保持字面。刻意不採用
    ``encodeURIComponent`` 的較寬鬆集合（它額外放行 ``!'()*``）——
    連結會被貼進 Google Chat 等會自動偵測 URL 邊界的環境，
    ``(`` / ``)`` 留字面容易被切斷；較嚴格的一端才是安全的 canonical 形式。
    client 端以 ``encodeURIComponent`` + 補escape 這幾個字元來對齊（見
    ``ROUTE_EXTRA_ESCAPE_CHARS``）。
    """
    return quote(str(value), safe="")


def build_deep_link_fragment(
    view: str = DEFAULT_VIEW,
    *,
    app: str | None = None,
    platform: str | None = None,
    version: str | None = None,
) -> str:
    """回傳 canonical deep link 的 fragment（含開頭 ``#``）。

    未註冊的 view 直接 raise：deep link 是對外契約，寧可在產生端炸掉，
    也不要送出一條只會 fallback 到 Overview 的假連結。

    ``view`` 也接受工作區 id（例如 ``system``），會被正規化成該工作區的預設
    panel view：canonical 形式永遠落在 panel view 空間，同一個目標不會有兩種寫法。
    """
    resolved_view = resolve_route_view(view)
    if resolved_view is None:
        raise ValueError(
            f"未註冊的 dashboard view: {view!r}；可用值：{view_names() + workspace_ids()}"
        )
    view = resolved_view
    params = (
        (ROUTE_PARAM_APP, app),
        (ROUTE_PARAM_PLATFORM, platform.lower() if platform else platform),
        (ROUTE_PARAM_VERSION, version),
    )
    query = "&".join(f"{key}={encode_route_value(value)}" for key, value in params if value)
    return f"#{view}?{query}" if query else f"#{view}"


def build_deep_link(
    view: str = DEFAULT_VIEW,
    *,
    app: str | None = None,
    platform: str | None = None,
    version: str | None = None,
    base_url: str | None = None,
) -> str:
    """回傳 canonical deep link；``base_url`` 省略時回傳純 fragment。

    ``base_url`` 既有的 fragment 會被丟棄，避免疊出 ``...#a#b``。
    """
    fragment = build_deep_link_fragment(view, app=app, platform=platform, version=version)
    if not base_url:
        return fragment
    return f"{base_url.split('#', 1)[0]}{fragment}"


def build_release_decision_link(
    app: str,
    platform: str,
    version: str,
    *,
    base_url: str | None = None,
) -> str:
    """回傳指向某 release decision context 的 canonical link (#78 對外契約)。

    Google Chat alert 等 consumer 一律呼叫本函式，不自行拼 URL；
    三個 context 皆為必填，避免送出只有一半 context 的半殘連結。
    """
    if not app or not platform or not version:
        raise ValueError(
            f"release decision link 需要完整 context：app={app!r} platform={platform!r} version={version!r}"
        )
    return build_deep_link(
        DECISION_VIEW,
        app=app,
        platform=platform,
        version=version,
        base_url=base_url,
    )


def parse_deep_link(link: str) -> DeepLinkRoute:
    """解析 canonical deep link（可為完整 URL 或純 fragment）。

    與 client 端 ``parseRouteHash`` 對稱：工作區 id 正規化為該工作區的預設 panel、
    未知 view 降級為 ``DEFAULT_VIEW``、未知參數忽略、空值視為未提供，
    因此壞連結不會產生例外。
    """
    fragment = link.split("#", 1)[1] if "#" in link else link
    view_token, _, query = fragment.partition("?")
    view = unquote(view_token)
    parsed: dict[str, str] = {}
    for pair in query.split("&"):
        if not pair:
            continue
        key, _, raw_value = pair.partition("=")
        value = unquote(raw_value)
        if value:
            parsed[unquote(key)] = value
    platform = parsed.get(ROUTE_PARAM_PLATFORM)
    return DeepLinkRoute(
        view=resolve_route_view(view) or DEFAULT_VIEW,
        app=parsed.get(ROUTE_PARAM_APP),
        platform=platform.lower() if platform else None,
        version=parsed.get(ROUTE_PARAM_VERSION),
    )


def get_routing_init_call() -> str:
    """回傳 deep-link routing 的初始化呼叫；由 shell bootstrap 在首次 render 後呼叫。"""
    return "initDeepLinkRouting();\n"


def get_route_revalidate_call() -> str:
    """回傳「重新驗證 context 並寫回 URL」的呼叫，供既有 app 切換點 (``switchApp``) 使用。"""
    return "revalidateRouteContext();\n"


def get_navigation_js() -> str:
    """回傳 client 端 view routing 與 deep-link routing 邏輯。

    view 切換維持既有 DOM-class routing（移除所有 ``active`` 後為目標 nav/view 加上），
    並在其後把目前 route 寫回 ``location.hash``。所有 URL state 讀寫都只在本區塊內，
    section module 不得自行操作 URL（由 deep-link 契約測試反向把關）。
    """
    view_list = ", ".join(f'"{view}"' for view in view_names())
    platform_select_ids = ", ".join(f'"{sid}"' for sid in ROUTE_PLATFORM_SELECT_IDS)
    view_workspace_pairs = ", ".join(f'{view}: "{workspace_of(view)}"' for view in view_names())
    workspace_default_pairs = ", ".join(
        f'{ws}: "{workspace_default_view(ws)}"' for ws in workspace_ids()
    )
    return (
        "// Navigation between views\n"
        "// V3.5 (#75)：一級導覽是「工作區 (workspace)」，view 是工作區底下的 panel。\n"
        "// switchView 仍以 view 為單位（deep link 與 routing 粒度不變），但同時要點亮\n"
        "// 擁有該 view 的工作區 nav 按鈕與對應的 workspace tab，否則會出現「內容切了、\n"
        "// 導覽卻停在別的工作區」的狀態。\n"
        "// 未註冊的 token（最典型是 consumer 直接傳工作區 id，例如 switchView('system')）\n"
        "// 必須先正規化成真正的 panel view：否則所有 view container 的 active 都會被清掉、\n"
        "// 卻沒有任何一個被加回來，內容區會整片空白——比「切錯頁」嚴重得多。\n"
        "function switchView(viewName) {\n"
        "  viewName = resolveRouteView(viewName) || ROUTE_DEFAULT_VIEW;\n"
        f"  const workspace = {ROUTE_VIEW_WORKSPACE_CONST}[viewName];\n"
        '  document.querySelectorAll(".nav-item").forEach(el => el.classList.remove("active"));\n'
        f'  const btn = $("{NAV_BUTTON_ID_PREFIX}" + workspace);\n'
        "  if (btn) btn.classList.add(\"active\");\n"
        "\n"
        '  document.querySelectorAll(".view-container").forEach(el => el.classList.remove("active"));\n'
        f'  const v = $("{VIEW_CONTAINER_ID_PREFIX}" + viewName);\n'
        '  if (v) v.classList.add("active");\n'
        "\n"
        '  document.querySelectorAll(".ws-tabstrip").forEach(el => el.classList.remove("active"));\n'
        f'  const strip = $("{WORKSPACE_TABSTRIP_ID_PREFIX}" + workspace);\n'
        '  if (strip) strip.classList.add("active");\n'
        '  document.querySelectorAll(".ws-tab").forEach(el => el.classList.remove("active"));\n'
        f'  const tab = $("{WORKSPACE_TAB_ID_PREFIX}" + viewName);\n'
        '  if (tab) tab.classList.add("active");\n'
        "\n"
        "  if (window.innerWidth <= 768) {\n"
        '    $("sidebar").classList.remove("mobile-open");\n'
        "  }\n"
        "\n"
        "  curRouteView = viewName;\n"
        "  syncRouteHash();\n"
        "}\n"
        "\n"
        "// ── Deep-link routing (Issue #78) ─────────────────────────────\n"
        "// URL 契約：#<view>?app=<app>&platform=<platform>&version=<version>\n"
        "// 只用 fragment，不用 History API（pushState/replaceState 在 file:// 會被擋）。\n"
        f"const ROUTE_VIEWS = [{view_list}];\n"
        "// V3.5 (#75)：view → 所屬工作區，以及工作區 → 預設 panel view。\n"
        "// 兩份 map 都由 Python 端的 NAV_ITEMS 產生，client 不另有一份手寫 IA。\n"
        f"const {ROUTE_VIEW_WORKSPACE_CONST} = {{{view_workspace_pairs}}};\n"
        f"const {ROUTE_WORKSPACE_DEFAULT_CONST} = {{{workspace_default_pairs}}};\n"
        f'const ROUTE_DEFAULT_VIEW = "{DEFAULT_VIEW}";\n'
        "// release decision 的落點 view；#73 的首屏連結由此產生，\n"
        "// 與 Python 端 build_release_decision_link 指向同一個 view。\n"
        f'const ROUTE_DECISION_VIEW = "{DECISION_VIEW}";\n'
        f'const ROUTE_PARAM_APP = "{ROUTE_PARAM_APP}";\n'
        f'const ROUTE_PARAM_PLATFORM = "{ROUTE_PARAM_PLATFORM}";\n'
        f'const ROUTE_PARAM_VERSION = "{ROUTE_PARAM_VERSION}";\n'
        f"const ROUTE_PLATFORM_SELECT_IDS = [{platform_select_ids}];\n"
        f'const ROUTE_PLATFORM_ALL = "{ROUTE_PLATFORM_ALL}";\n'
        f'const ROUTE_EXTRA_ESCAPE_CHARS = "{ROUTE_EXTRA_ESCAPE_CHARS}";\n'
        "\n"
        "let curRouteView = ROUTE_DEFAULT_VIEW;\n"
        "let curRouteContext = { platform: null, version: null };\n"
        "// 目前畫面所反映的 hash；自己寫進去的 hash 不需要再套用一次，\n"
        "// 只有真正的 back/forward（hash 與此值不同）才會觸發 applyRoute。\n"
        "let curRouteHash = null;\n"
        "// > 0 表示正在「由 URL 套用 route」，此時一律不寫回 URL，避免迴圈與多餘 history entry。\n"
        "let routeApplyDepth = 0;\n"
        "let routeListenerBound = false;\n"
        "\n"
        "function decodeRoutePart(raw) {\n"
        '  try { return decodeURIComponent(String(raw == null ? "" : raw)); }\n'
        '  catch (e) { return String(raw == null ? "" : raw); }\n'
        "}\n"
        "\n"
        "// route token → panel view。token 可以是 panel view（canonical 形式），\n"
        "// 也可以是工作區 id（使用者看到四個一級項目後手打 #system 之類的別名）。\n"
        "// 無法解析時回傳 null，由呼叫端決定降級目標；與 Python 端\n"
        "// resolve_route_view() 是同一條規則。\n"
        "function resolveRouteView(token) {\n"
        "  if (ROUTE_VIEWS.indexOf(token) >= 0) return token;\n"
        f"  if (Object.prototype.hasOwnProperty.call({ROUTE_WORKSPACE_DEFAULT_CONST}, token))"
        f" return {ROUTE_WORKSPACE_DEFAULT_CONST}[token];\n"
        "  return null;\n"
        "}\n"
        "\n"
        "function parseRouteHash(rawHash) {\n"
        '  let raw = String(rawHash == null ? "" : rawHash);\n'
        '  if (raw.charAt(0) === "#") raw = raw.slice(1);\n'
        '  const qi = raw.indexOf("?");\n'
        "  const viewToken = decodeRoutePart(qi >= 0 ? raw.slice(0, qi) : raw);\n"
        '  const query = qi >= 0 ? raw.slice(qi + 1) : "";\n'
        "  const route = {\n"
        "    view: resolveRouteView(viewToken) || ROUTE_DEFAULT_VIEW,\n"
        "    app: null, platform: null, version: null\n"
        "  };\n"
        '  query.split("&").forEach(pair => {\n'
        "    if (!pair) return;\n"
        '    const eq = pair.indexOf("=");\n'
        "    const key = decodeRoutePart(eq >= 0 ? pair.slice(0, eq) : pair);\n"
        '    const val = eq >= 0 ? decodeRoutePart(pair.slice(eq + 1)) : "";\n'
        "    if (!val) return;\n"
        "    if (key === ROUTE_PARAM_APP) route.app = val;\n"
        "    else if (key === ROUTE_PARAM_PLATFORM) route.platform = val.toLowerCase();\n"
        "    else if (key === ROUTE_PARAM_VERSION) route.version = val;\n"
        "  });\n"
        "  return route;\n"
        "}\n"
        "\n"
        "// canonical 參數值編碼；必須與 Python 端 encode_route_value()（quote(safe=\"\")）\n"
        "// 逐位元一致，否則同一個 release 會產生兩條不同的「canonical」連結。\n"
        "// encodeURIComponent 額外放行 ROUTE_EXTRA_ESCAPE_CHARS 這幾個字元，在此補上。\n"
        "function encodeRouteValue(value) {\n"
        "  let out = encodeURIComponent(String(value));\n"
        "  for (let i = 0; i < ROUTE_EXTRA_ESCAPE_CHARS.length; i++) {\n"
        "    const c = ROUTE_EXTRA_ESCAPE_CHARS.charAt(i);\n"
        '    out = out.split(c).join("%" + c.charCodeAt(0).toString(16).toUpperCase());\n'
        "  }\n"
        "  return out;\n"
        "}\n"
        "\n"
        "function buildRouteHash(route) {\n"
        "  const r = route || {};\n"
        "  const view = resolveRouteView(r.view) || ROUTE_DEFAULT_VIEW;\n"
        "  const parts = [];\n"
        "  if (r.app) parts.push(ROUTE_PARAM_APP + \"=\" + encodeRouteValue(r.app));\n"
        "  if (r.platform) parts.push(ROUTE_PARAM_PLATFORM + \"=\" + encodeRouteValue(r.platform));\n"
        "  if (r.version) parts.push(ROUTE_PARAM_VERSION + \"=\" + encodeRouteValue(r.version));\n"
        '  return "#" + view + (parts.length ? "?" + parts.join("&") : "");\n'
        "}\n"
        "\n"
        "// curAppId 由 assets.py 的 shell state 宣告；此處集中做一次存在性防護，\n"
        "// 讓 routing 即使在 shell state 之外被載入也不會拋 ReferenceError。\n"
        "function routeCurAppId() {\n"
        '  return typeof curAppId !== "undefined" ? curAppId : null;\n'
        "}\n"
        "\n"
        "function routeAppsData() {\n"
        '  return (typeof DATA !== "undefined" && DATA && DATA.apps) ? DATA.apps : {};\n'
        "}\n"
        "\n"
        "function routeKnownVersions(appData) {\n"
        "  if (!appData) return [];\n"
        "  const out = [];\n"
        "  const collect = arr => {\n"
        "    if (!Array.isArray(arr)) return;\n"
        "    arr.forEach(v => {\n"
        "      if (v && v.version) {\n"
        '        out.push({ version: String(v.version), platform: String(v.platform || "").toLowerCase() });\n'
        "      }\n"
        "    });\n"
        "  };\n"
        "  collect(appData.release_catalog);\n"
        "  collect(appData.version_health);\n"
        "  const periods = appData.periods || {};\n"
        "  Object.keys(periods).forEach(k => {\n"
        "    collect(periods[k] && periods[k].release_catalog);\n"
        "    collect(periods[k] && periods[k].version_health);\n"
        "  });\n"
        "  return out;\n"
        "}\n"
        "\n"
        "function routeKnownPlatforms(appData) {\n"
        "  const out = [];\n"
        "  const md = appData && appData.metadata;\n"
        "  if (md && Array.isArray(md.platforms)) {\n"
        "    md.platforms.forEach(p => out.push(String(p).toLowerCase()));\n"
        "  }\n"
        "  routeKnownVersions(appData).forEach(v => {\n"
        '    if (v.platform && v.platform !== "all") out.push(v.platform);\n'
        "  });\n"
        "  return out;\n"
        "}\n"
        "\n"
        "// 把連結要求的 context 對 bundle 實際資料做驗證；不存在者一律降級並記在 dropped。\n"
        "//\n"
        "// app → platform → version 是一條**依賴鏈**：platform 的意義由 app 界定，\n"
        "// version 的意義又由 platform 界定。因此驗證必須是串聯的（cascading）——\n"
        "// 一旦某層被明確要求卻無效，其下游一律連帶丟棄，只保留「安全前綴」。\n"
        "// 否則會出現比沒有連結更糟的情況：把 app 丟掉後改拿 fallback app 去比對\n"
        "// platform/version，或把無效 platform 當成「沒指定 platform」而讓 version\n"
        "// 在任意平台上命中——兩者都會開到**另一個** release。\n"
        "// 注意「完全沒要求」與「要求了但無效」必須分開判斷，所以用 r.* 而非 resolved.*。\n"
        "function resolveRoute(route) {\n"
        "  const r = route || {};\n"
        "  const resolved = {\n"
        "    view: resolveRouteView(r.view) || ROUTE_DEFAULT_VIEW,\n"
        "    app: null, platform: null, version: null, dropped: []\n"
        "  };\n"
        "  const apps = routeAppsData();\n"
        "  // 依賴鏈是否已在上游斷掉；斷掉之後下游只記 dropped、不再嘗試解析。\n"
        "  let broken = false;\n"
        "  if (r.app) {\n"
        "    if (apps[r.app]) resolved.app = r.app;\n"
        "    else {\n"
        '      resolved.dropped.push(ROUTE_PARAM_APP + "=" + r.app);\n'
        "      broken = true;\n"
        "    }\n"
        "  }\n"
        "  // 沒要求 app 時退回目前/預設 app 是刻意保留的行為（無 app 參數的連結仍可用）；\n"
        "  // 但「要求了一個不存在的 app」不得退回 fallback app 來解析 platform/version。\n"
        "  const appId = resolved.app || routeCurAppId();\n"
        "  const appData = (!broken && appId && apps[appId]) ? apps[appId] : null;\n"
        "  if (r.platform) {\n"
        "    if (!broken && routeKnownPlatforms(appData).indexOf(r.platform) >= 0) {\n"
        "      resolved.platform = r.platform;\n"
        "    } else {\n"
        '      resolved.dropped.push(ROUTE_PARAM_PLATFORM + "=" + r.platform);\n'
        "      broken = true;\n"
        "    }\n"
        "  }\n"
        "  if (r.version) {\n"
        "    const match = !broken && routeKnownVersions(appData).some(v =>\n"
        "      v.version === r.version &&\n"
        '      (!r.platform || !v.platform || v.platform === "all" || v.platform === resolved.platform)\n'
        "    );\n"
        "    if (match) resolved.version = r.version;\n"
        '    else resolved.dropped.push(ROUTE_PARAM_VERSION + "=" + r.version);\n'
        "  }\n"
        "  return resolved;\n"
        "}\n"
        "\n"
        "function applyRoutePlatform(platform) {\n"
        "  ROUTE_PLATFORM_SELECT_IDS.forEach(id => {\n"
        "    const sel = $(id);\n"
        "    if (sel) sel.value = platform;\n"
        "  });\n"
        '  if (typeof handlePlatformFilterChange === "function") handlePlatformFilterChange();\n'
        '  if (typeof renderReleasesTable === "function") renderReleasesTable();\n'
        "}\n"
        "\n"
        "// route 不含 platform（例如 back 回到一個沒有 platform 的 entry）時，畫面上殘留的\n"
        "// select 值會與 URL/context 說的不一致，必須回復成既有的中性選項 ROUTE_PLATFORM_ALL\n"
        "// （不是空字串——空字串不是有效 option，會讓篩選器變空白）。\n"
        "// 只在真的有殘留值時才動作，避免每次套用 route 都多跑一輪 render。\n"
        "function clearRoutePlatform() {\n"
        "  const stale = ROUTE_PLATFORM_SELECT_IDS.some(id => {\n"
        "    const sel = $(id);\n"
        "    return !!sel && !!sel.value && sel.value !== ROUTE_PLATFORM_ALL;\n"
        "  });\n"
        "  if (stale) applyRoutePlatform(ROUTE_PLATFORM_ALL);\n"
        "}\n"
        "\n"
        "function applyRoute(route) {\n"
        "  const resolved = resolveRoute(route);\n"
        "  routeApplyDepth++;\n"
        "  try {\n"
        '    if (resolved.app && typeof switchApp === "function" && resolved.app !== routeCurAppId()) {\n'
        "      switchApp(resolved.app);\n"
        "    }\n"
        "    curRouteContext = { platform: resolved.platform, version: resolved.version };\n"
        "    // 套用 route 是**取代**而非疊加：route 移除掉的 context 必須連同其 UI 一起清掉，\n"
        "    // 否則 back 回到沒有 version 的 entry 時，release 詳情會繼續開著（URL 說沒有版本，\n"
        "    // 畫面卻停在某個版本），platform 下拉選單同理。清除動作全部發生在\n"
        "    // routeApplyDepth > 0 的窗口內，syncRouteHash 會早退，不會多產生 history entry。\n"
        "    if (resolved.platform) applyRoutePlatform(resolved.platform);\n"
        "    else clearRoutePlatform();\n"
        "    switchView(resolved.view);\n"
        "    // #73 的首屏決策面要反映連結指定的 release，而不是永遠只顯示各平台最新版；\n"
        "    // 因此在 context 已解析、view 已切換之後重畫一次。\n"
        '    if (typeof renderReleaseDecisions === "function") renderReleaseDecisions();\n'
        '    // #74 的比較面與決策面共用同一個 pinned release，必須一起重畫。\n'
        '    if (typeof renderReleaseComparison === "function") renderReleaseComparison();\n'
        '    // #91 的輔助面板（lifecycle / gate history）同樣跟著 pinned release。\n'
        '    if (typeof renderReleaseAux === "function") renderReleaseAux();\n'
        '    if (resolved.version && typeof openReleaseDetail === "function") {\n'
        "      openReleaseDetail(resolved.version, resolved.platform || null);\n"
        '    } else if (!resolved.version && typeof closeReleaseDetail === "function") {\n'
        "      closeReleaseDetail();\n"
        "    }\n"
        '    if (resolved.dropped.length && typeof showToast === "function") {\n'
        '      showToast("連結中的部分內容在此資料中不存在，已忽略：" + resolved.dropped.join(", "));\n'
        "    }\n"
        "  } finally {\n"
        "    routeApplyDepth--;\n"
        "  }\n"
        "  return resolved;\n"
        "}\n"
        "\n"
        "function getLocationHash() {\n"
        '  try { return (window.location && window.location.hash) || ""; }\n'
        '  catch (e) { return ""; }\n'
        "}\n"
        "\n"
        "// 把目前 view + app/platform/version context 寫回 URL；\n"
        "// 相同時不寫，避免產生無意義的 history entry 與 hashchange 迴圈。\n"
        "function syncRouteHash() {\n"
        "  if (routeApplyDepth > 0) return;\n"
        "  const target = buildRouteHash({\n"
        "    view: curRouteView,\n"
        "    app: routeCurAppId(),\n"
        "    platform: curRouteContext.platform,\n"
        "    version: curRouteContext.version\n"
        "  });\n"
        "  curRouteHash = target;\n"
        "  if (target === getLocationHash()) return;\n"
        "  try { window.location.hash = target; } catch (e) {}\n"
        "}\n"
        "\n"
        "// 供既有互動（如開啟 release 詳情）回報 context，使 URL 保持可分享。\n"
        "function setRouteContext(patch) {\n"
        "  if (!patch) return;\n"
        '  if ("platform" in patch) curRouteContext.platform = patch.platform || null;\n'
        '  if ("version" in patch) curRouteContext.version = patch.version || null;\n'
        "  syncRouteHash();\n"
        "}\n"
        "\n"
        "// 切換 app 後重新驗證既有 context：舊 app 的 platform/version 未必存在於新 app，\n"
        "// 不重驗會讓 URL 帶出一條開起來只會被降級的連結。\n"
        "function revalidateRouteContext() {\n"
        "  const resolved = resolveRoute({\n"
        "    view: curRouteView,\n"
        "    app: routeCurAppId(),\n"
        "    platform: curRouteContext.platform,\n"
        "    version: curRouteContext.version\n"
        "  });\n"
        "  curRouteContext = { platform: resolved.platform, version: resolved.version };\n"
        "  syncRouteHash();\n"
        "}\n"
        "\n"
        "// #73 等 consumer 讀取「已驗證過的」目前 route context，不自行 parse URL。\n"
        "function getRouteContext() {\n"
        "  return {\n"
        "    view: curRouteView,\n"
        "    app: routeCurAppId(),\n"
        "    platform: curRouteContext.platform,\n"
        "    version: curRouteContext.version\n"
        "  };\n"
        "}\n"
        "\n"
        "// back/forward 專用：hash 與畫面現況不同才重新套用。\n"
        "function handleRouteHashChange() {\n"
        "  const raw = getLocationHash();\n"
        "  if (raw === curRouteHash) return null;\n"
        "  curRouteHash = raw;\n"
        "  return applyRoute(parseRouteHash(raw));\n"
        "}\n"
        "\n"
        "function initDeepLinkRouting() {\n"
        "  if (!routeListenerBound) {\n"
        "    routeListenerBound = true;\n"
        "    try {\n"
        '      window.addEventListener("hashchange", handleRouteHashChange);\n'
        "    } catch (e) {}\n"
        "  }\n"
        "  // 無 hash 時 parseRouteHash 回傳預設 view，等同既有初始畫面；\n"
        "  // 此處刻意不寫回 URL：舊的無 hash 連結行為完全不變，\n"
        "  // 被降級的 context 也不會被改寫成另一個 history entry（否則 back 會卡在原地）。\n"
        "  curRouteHash = getLocationHash();\n"
        "  return applyRoute(parseRouteHash(curRouteHash));\n"
        "}\n"
        "\n"
    )
