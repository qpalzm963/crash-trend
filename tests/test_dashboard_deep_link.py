"""Deep-link / hash routing contract tests (Issue #78).

#78 讓 dashboard 的 view + app/platform/version context 可以由 URL fragment 還原，
並提供 canonical link helper 給 Google Chat 等外部 consumer。

這些測試存在的理由：
1. **契約是對外的。** ``build_release_decision_link`` 產出的字串會被貼進 alert 訊息，
   一旦格式漂移（參數名改掉、少 encode、view 名寫錯），連結會安靜地落到預設首頁，
   而不是報錯。因此對格式本身、以及「產生 → 解析」的來回一致性下斷言。
2. **壞連結比好連結常見。** 外部 consumer 會帶著已經下架的版本、拼錯的 app、
   別的 app 的平台進來。這裡的重點不是 happy path，而是驗證這些輸入只會被降級，
   不會丟出 JS 例外、也不會把畫面弄成空白。
3. **routing 必須只有一份。** ``ROUTE_PLATFORM_SELECT_IDS`` 是 navigation 模組對
   section module DOM 的唯一假設，反向驗證它仍存在於 render 結果，避免 #71 集中化
   之後又靠字串巧合維繫。
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.build_dashboard import build_html
from crash_trend.dashboard import navigation as nav
from crash_trend.dashboard.renderer import assemble_html_template

FIXTURE = ROOT / "tests" / "fixtures" / "dashboard_v2.json"


class TestCanonicalLinkContract(unittest.TestCase):
    """canonical deep link 的字面格式；這是對外契約，不允許靜默改動。"""

    def test_view_only_link_has_no_query(self) -> None:
        self.assertEqual(nav.build_deep_link_fragment("issues"), "#issues")
        self.assertEqual(nav.build_deep_link_fragment(), f"#{nav.DEFAULT_VIEW}")

    def test_full_context_link_uses_documented_param_order(self) -> None:
        """參數順序固定為 app → platform → version：同一個 release 必須永遠產生同一條連結，
        否則 alert 去重、快取與人工比對都會把同一個目標看成兩條不同連結。"""
        self.assertEqual(
            nav.build_deep_link_fragment("releases", app="shop_app", platform="android", version="5.3.1"),
            "#releases?app=shop_app&platform=android&version=5.3.1",
        )

    def test_platform_is_normalised_to_lowercase(self) -> None:
        """alert 端常拿到 ``Android`` / ``iOS``，而 dashboard 的 filter 選項是小寫；
        不在產生端正規化，連結就會在 resolve 時被判成不存在的平台而降級。"""
        link = nav.build_deep_link_fragment("releases", app="a", platform="Android", version="1.0")
        self.assertIn("platform=android", link)

    def test_context_values_are_percent_encoded(self) -> None:
        """版本號 / app id 可能含 ``&``、``#``、空白；不 encode 會截斷後面的參數。"""
        link = nav.build_deep_link_fragment("releases", app="a b&c", version="1.0 #rc1")
        self.assertEqual(link, "#releases?app=a%20b%26c&version=1.0%20%23rc1")
        route = nav.parse_deep_link(link)
        self.assertEqual(route.app, "a b&c")
        self.assertEqual(route.version, "1.0 #rc1")

    def test_empty_context_values_are_omitted(self) -> None:
        self.assertEqual(nav.build_deep_link_fragment("releases", app="a", platform=None, version=""), "#releases?app=a")

    def test_unknown_view_raises_instead_of_emitting_a_lying_link(self) -> None:
        """未註冊的 view 若只是靜默 fallback，consumer 會送出一條號稱指向某畫面、
        實際只會開到 Overview 的連結；在產生端就要炸掉。"""
        with self.assertRaises(ValueError):
            nav.build_deep_link_fragment("does_not_exist")

    def test_base_url_is_joined_and_existing_fragment_dropped(self) -> None:
        self.assertEqual(
            nav.build_deep_link("issues", base_url="https://host/dash.html"),
            "https://host/dash.html#issues",
        )
        self.assertEqual(
            nav.build_deep_link("issues", base_url="https://host/dash.html#releases?app=x"),
            "https://host/dash.html#issues",
        )
        self.assertEqual(nav.build_deep_link("issues"), "#issues")

    def test_release_decision_link_points_at_the_decision_view(self) -> None:
        self.assertEqual(
            nav.build_release_decision_link("shop_app", "ios", "5.3.1", base_url="https://host/d.html"),
            f"https://host/d.html#{nav.DECISION_VIEW}?app=shop_app&platform=ios&version=5.3.1",
        )

    def test_release_decision_link_requires_complete_context(self) -> None:
        """半殘的 decision link（缺平台或版本）打開後看到的不是被通報的那個 release，
        比沒有連結更糟；缺任何一項就拒絕產生。"""
        for app, platform, version in (
            ("", "android", "1.0"),
            ("a", "", "1.0"),
            ("a", "android", ""),
        ):
            with self.subTest(app=app, platform=platform, version=version):
                with self.assertRaises(ValueError):
                    nav.build_release_decision_link(app, platform, version)

    def test_round_trip_through_parse_preserves_every_field(self) -> None:
        link = nav.build_release_decision_link("shop_app", "ios", "5.3.1", base_url="https://host/d.html")
        route = nav.parse_deep_link(link)
        self.assertEqual(
            (route.view, route.app, route.platform, route.version),
            (nav.DECISION_VIEW, "shop_app", "ios", "5.3.1"),
        )


class TestDeepLinkParsingIsTotal(unittest.TestCase):
    """parse 端永遠不得丟例外：來源是外部貼進來的字串。"""

    def test_missing_or_empty_fragment_falls_back_to_default_view(self) -> None:
        for raw in ("", "#", "https://host/dash.html", "https://host/dash.html#"):
            with self.subTest(raw=raw):
                route = nav.parse_deep_link(raw)
                self.assertEqual(route.view, nav.DEFAULT_VIEW)
                self.assertIsNone(route.app)
                self.assertIsNone(route.platform)
                self.assertIsNone(route.version)

    def test_unknown_view_degrades_to_default_view(self) -> None:
        route = nav.parse_deep_link("#not_a_view?app=shop_app")
        self.assertEqual(route.view, nav.DEFAULT_VIEW)
        self.assertEqual(route.app, "shop_app")

    def test_unknown_and_empty_params_are_ignored(self) -> None:
        route = nav.parse_deep_link("#issues?app=shop_app&bogus=1&version=&&platform")
        self.assertEqual(route.app, "shop_app")
        self.assertIsNone(route.version)
        self.assertIsNone(route.platform)

    def test_malformed_input_does_not_raise(self) -> None:
        for raw in ("#?????", "#releases?=&=&", "#releases?app=%%%", "###", "#releases?app=a#b"):
            with self.subTest(raw=raw):
                route = nav.parse_deep_link(raw)
                self.assertIn(route.view, nav.view_names())


class TestRenderedDeepLinkWiring(unittest.TestCase):
    """render 結果與 navigation 模組的假設必須一致。"""

    def setUp(self) -> None:
        self.html = assemble_html_template()

    def test_routing_is_initialised_after_the_first_render(self) -> None:
        """套用 route 需要 DATA 與已渲染的 DOM，初始化必須排在 renderAll 之後。"""
        self.assertIn(nav.get_routing_init_call().strip(), self.html)
        for block in re.findall(r"renderAll\(\);\s*\n\s*initDeepLinkRouting\(\);", self.html):
            self.assertIn("initDeepLinkRouting", block)
        self.assertEqual(self.html.count("initDeepLinkRouting();"), 2)

    def test_platform_select_ids_assumed_by_routing_still_exist(self) -> None:
        """navigation 模組唯一對 section module DOM 的假設；漂移了 platform deep link 會靜默失效。"""
        for select_id in nav.ROUTE_PLATFORM_SELECT_IDS:
            with self.subTest(select_id=select_id):
                self.assertIn(f'id="{select_id}"', self.html)
                self.assertIn('<option value="android">', self.html)
                self.assertIn('<option value="ios">', self.html)

    def test_client_route_views_match_the_python_registry(self) -> None:
        """client 端的合法 view 清單必須由 NAV_ITEMS 產生，不能是另一份手寫陣列。"""
        routing_js = nav.get_navigation_js()
        expected = "const ROUTE_VIEWS = [" + ", ".join(f'"{v}"' for v in nav.view_names()) + "];"
        self.assertIn(expected, routing_js)
        self.assertIn(f'const ROUTE_DEFAULT_VIEW = "{nav.DEFAULT_VIEW}";', routing_js)

    def test_alerts_package_is_not_wired_to_the_link_helper_yet(self) -> None:
        """#78 只交付 helper；把它接進 Google Chat 訊息是刻意留給後續 ticket 的動作。
        這裡把「尚未接線」寫成契約，讓真正接線的那個 PR 必須明確改掉此測試。"""
        alerts_dir = ROOT / "crash_trend" / "alerts"
        for path in sorted(alerts_dir.rglob("*.py")):
            with self.subTest(path=path.name):
                self.assertNotIn("build_release_decision_link", path.read_text(encoding="utf-8"))


NODE_RUNNER = r"""
const fs = require('fs');

const elements = {};
function getOrCreateElement(id) {
  if (!elements[id]) {
    elements[id] = {
      id,
      value: 'ALL',
      innerHTML: '',
      textContent: '',
      style: {},
      classList: {
        classes: new Set(),
        add(c) { this.classes.add(c); },
        remove(c) { this.classes.delete(c); },
        toggle(c) { if (this.classes.has(c)) this.classes.delete(c); else this.classes.add(c); },
        contains(c) { return this.classes.has(c); },
      },
      addEventListener: () => {},
    };
  }
  return elements[id];
}

global.document = {
  documentElement: { dataset: { theme: 'light' } },
  getElementById: (id) => getOrCreateElement(id),
  querySelector: (sel) => getOrCreateElement(sel.replace('#', '')),
  // switchView 依賴 querySelectorAll 把舊的 active 清掉；回傳 [] 會讓「同時只有一個
  // active view」這件事無法驗證，因此依 id 前綴模擬這兩個 class 的集合。
  querySelectorAll: (sel) => {
    const prefix = sel === '.view-container' ? 'view-' : (sel === '.nav-item' ? 'nav-' : null);
    if (!prefix) return [];
    return Object.keys(elements).filter(id => id.indexOf(prefix) === 0).map(id => elements[id]);
  },
  createElement: () => getOrCreateElement('mock-' + Math.random()),
  addEventListener: () => {},
  readyState: 'complete',
};
global.$ = (id) => getOrCreateElement(id);
global.window = global;
global.navigator = { clipboard: { writeText: () => Promise.resolve() } };
global.Chart = function () { return { destroy: () => {}, update: () => {} }; };
global.Chart.register = () => {};
global.chartInstances = {};
global.setTimeout = (fn) => 0;

// ── 最小 hash history 模擬（含 back/forward）────────────────────────
const hashListeners = [];
let curHash = process.argv[3] || '';
const stack = [curHash];
let idx = 0;
global.location = {
  get hash() { return curHash; },
  set hash(v) {
    const next = String(v).charAt(0) === '#' ? String(v) : '#' + String(v);
    if (next === curHash) return;
    curHash = next;
    stack.length = idx + 1;
    stack.push(next);
    idx = stack.length - 1;
    hashListeners.slice().forEach(fn => fn());
  },
};
global.addEventListener = (evt, fn) => { if (evt === 'hashchange') hashListeners.push(fn); };
function historyGo(delta) {
  const target = idx + delta;
  if (target < 0 || target >= stack.length) return;
  idx = target;
  curHash = stack[target];
  hashListeners.slice().forEach(fn => fn());
}

function activeViews() {
  return Object.keys(elements)
    .filter(id => id.indexOf('view-') === 0 && elements[id].classList.contains('active'))
    .map(id => id.slice('view-'.length))
    .sort();
}
function snapshot() {
  // `let curAppId` 宣告在 eval 的 scope 內，外部讀不到；改用 navigation 模組
  // 對外提供的 getRouteContext()（也就是 #73 未來會用的那個入口）來觀測。
  const ctx = typeof getRouteContext === 'function' ? getRouteContext() : null;
  return {
    hash: curHash,
    activeViews: activeViews(),
    activeNav: Object.keys(elements)
      .filter(id => id.indexOf('nav-') === 0 && elements[id].classList.contains('active'))
      .map(id => id.slice('nav-'.length))
      .sort(),
    app: ctx ? ctx.app : null,
    releasePlatformSelect: elements['filterReleasePlatform'] ? elements['filterReleasePlatform'].value : null,
    modalActive: elements['releaseDetailModal'] ? elements['releaseDetailModal'].classList.contains('active') : false,
    toast: elements['toast'] ? elements['toast'].textContent : '',
    routeContext: ctx,
    historyDepth: stack.length,
  };
}

const report = { error: null, steps: {} };
try {
  eval(fs.readFileSync(process.argv[2], 'utf-8'));
  report.steps.onLoad = snapshot();

  // 使用者主動切換 view：URL 必須跟著走，並產生一個 history entry。
  switchView('issues');
  report.steps.afterSwitchView = snapshot();

  // back 回到載入時的畫面，forward 再回到 issues。
  historyGo(-1);
  report.steps.afterBack = snapshot();
  historyGo(1);
  report.steps.afterForward = snapshot();
} catch (e) {
  report.error = String((e && e.stack) || e);
}
console.log('__REPORT__' + JSON.stringify(report));
"""


class TestDeepLinkRuntimeWithNode(unittest.TestCase):
    """在 Node 內執行真正產出的 client JS。

    Python 端只能驗證字串；deep link 真正會壞的地方是 runtime——未知 app 讓
    ``resolveRoute`` 取到 undefined、hashchange 迴圈、back 之後畫面沒跟著回去。
    這些只有實際跑一遍才抓得到。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.node_bin = shutil.which("node")
        bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.plain_bundle = copy.deepcopy(bundle)

        # 加上 release catalog，讓「有效的 version deep link」有東西可以還原。
        catalog_bundle = copy.deepcopy(bundle)
        catalog_bundle["apps"]["shop_app"]["release_catalog"] = [
            {
                "version": "3.2.0",
                "platform": "android",
                "status": "latest",
                "release_date": "2026-08-01",
                "first_seen": "2026-08-01T00:00:00Z",
                "last_seen": "2026-09-01T00:00:00Z",
                "lifetime_crashes": 10,
                "lifetime_issues": 2,
                "lifetime_affected_users": 5,
                "lifetime_fatal": 8,
                "lifetime_anr": 2,
                "stability_status": "stable",
                "recent_health": {
                    "30d": {
                        "window": "30d",
                        "crash_events": 10,
                        "affected_users": 5,
                        "crash_free_users_rate": 0.99,
                        "crash_free_sessions_rate": None,
                        "fatal_count": 8,
                        "anr_count": 2,
                        "active_issues_count": 2,
                    }
                },
                "vs_previous": None,
                "issue_lifecycle": {
                    "introduced_issues": [],
                    "persistent_issues": [],
                    "regressed_issues": [],
                    "resolved_issues": [],
                },
            }
        ]
        cls.catalog_bundle = catalog_bundle

    def _run(self, bundle: dict, initial_hash: str) -> dict:
        if not self.node_bin:
            self.skipTest("Node.js runtime is not available in environment")
        html = build_html(bundle)
        scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        self.assertGreaterEqual(len(scripts), 2, "HTML must contain at least 2 <script> tags")
        client_js = scripts[1]

        with tempfile.TemporaryDirectory() as tmp:
            js_path = Path(tmp) / "client.js"
            js_path.write_text(client_js, encoding="utf-8")
            runner_path = Path(tmp) / "runner.js"
            runner_path.write_text(NODE_RUNNER, encoding="utf-8")
            res = subprocess.run(
                [self.node_bin, str(runner_path), str(js_path), initial_hash],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
        self.assertEqual(res.returncode, 0, f"Node failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        marker = [line for line in res.stdout.splitlines() if line.startswith("__REPORT__")]
        self.assertTrue(marker, f"runner produced no report:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        report = json.loads(marker[-1][len("__REPORT__") :])
        self.assertIsNone(report["error"], f"client JS threw: {report['error']}")
        return report["steps"]

    def test_no_hash_load_opens_default_overview_and_leaves_url_untouched(self) -> None:
        """舊的無 hash 連結（既有使用方式）行為必須完全不變：Overview、且不被改寫成帶 hash 的 URL。"""
        steps = self._run(self.plain_bundle, "")
        on_load = steps["onLoad"]
        self.assertEqual(on_load["activeViews"], [nav.DEFAULT_VIEW])
        self.assertEqual(on_load["activeNav"], [nav.DEFAULT_VIEW])
        self.assertEqual(on_load["hash"], "")
        self.assertEqual(on_load["historyDepth"], 1)

    def test_deep_link_restores_view_platform_and_version(self) -> None:
        link = nav.build_deep_link_fragment("releases", app="shop_app", platform="android", version="3.2.0")
        steps = self._run(self.catalog_bundle, link)
        on_load = steps["onLoad"]
        self.assertEqual(on_load["activeViews"], ["releases"])
        self.assertEqual(on_load["app"], "shop_app")
        self.assertEqual(on_load["releasePlatformSelect"], "android")
        self.assertTrue(on_load["modalActive"], "有效的 version deep link 必須把該 release 的詳情打開")
        self.assertEqual(on_load["routeContext"]["version"], "3.2.0")
        self.assertEqual(on_load["hash"], link)

    def test_deep_link_switches_app(self) -> None:
        link = nav.build_deep_link_fragment("issues", app="rider_app", platform="android")
        steps = self._run(self.plain_bundle, link)
        self.assertEqual(steps["onLoad"]["app"], "rider_app")
        self.assertEqual(steps["onLoad"]["activeViews"], ["issues"])

    def test_unknown_app_and_version_degrade_without_blank_page_or_error(self) -> None:
        """外部 consumer 帶進不存在的 app / 已下架的版本時最常見的失敗模式是
        整頁空白或 JS 例外；此處要求它只降級並告知使用者。"""
        steps = self._run(self.plain_bundle, "#releases?app=ghost_app&platform=windows&version=99.9.9")
        on_load = steps["onLoad"]
        self.assertEqual(on_load["activeViews"], ["releases"], "view 仍然要切過去")
        self.assertEqual(on_load["app"], self.plain_bundle["default_app"], "未知 app 退回預設 app")
        self.assertIsNone(on_load["routeContext"]["platform"])
        self.assertIsNone(on_load["routeContext"]["version"])
        self.assertFalse(on_load["modalActive"])
        for dropped in ("ghost_app", "windows", "99.9.9"):
            self.assertIn(dropped, on_load["toast"], "被丟棄的 context 必須告知使用者")
        self.assertEqual(on_load["hash"], "#releases?app=ghost_app&platform=windows&version=99.9.9")

    def test_unknown_view_in_hash_falls_back_to_overview(self) -> None:
        steps = self._run(self.plain_bundle, "#totally_unknown_view?app=shop_app")
        self.assertEqual(steps["onLoad"]["activeViews"], [nav.DEFAULT_VIEW])

    def test_switching_view_writes_the_url_back(self) -> None:
        steps = self._run(self.plain_bundle, "")
        after = steps["afterSwitchView"]
        self.assertEqual(after["hash"], f"#issues?app={self.plain_bundle['default_app']}")
        self.assertEqual(after["activeViews"], ["issues"])
        self.assertEqual(after["historyDepth"], 2, "每次導覽產生一個 history entry，back/forward 才有東西可回")

    def test_browser_back_and_forward_restore_the_view(self) -> None:
        """#78 明確選擇支援 back/forward（fragment 賦值 + hashchange），因此必須有測試。"""
        link = nav.build_deep_link_fragment("releases", app="shop_app", platform="android")
        steps = self._run(self.plain_bundle, link)
        self.assertEqual(steps["afterSwitchView"]["activeViews"], ["issues"])
        self.assertEqual(steps["afterBack"]["hash"], link)
        self.assertEqual(steps["afterBack"]["activeViews"], ["releases"], "back 必須把畫面帶回上一個 view")
        self.assertEqual(steps["afterForward"]["activeViews"], ["issues"], "forward 必須再回到 issues")


if __name__ == "__main__":
    unittest.main()
