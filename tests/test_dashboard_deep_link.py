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

    def test_neutral_platform_option_used_when_clearing_context_exists(self) -> None:
        """route 移除 platform 時會把 select 寫成 ``ROUTE_PLATFORM_ALL``；
        該值必須真的是一個 option，否則篩選器會變成空白（比留著舊值更糟）。"""
        ids = "|".join(re.escape(sid) for sid in nav.ROUTE_PLATFORM_SELECT_IDS)
        select_blocks = re.findall(
            rf'<select[^>]*id="(?:{ids})"[^>]*>(.*?)</select>',
            self.html,
            re.DOTALL,
        )
        self.assertEqual(len(select_blocks), len(nav.ROUTE_PLATFORM_SELECT_IDS))
        for block in select_blocks:
            self.assertIn(f'<option value="{nav.ROUTE_PLATFORM_ALL}">', block)

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


#: 只 eval navigation 模組產出的 routing JS（其 top-level 全是宣告，不需要 DOM），
#: 直接呼叫 client 端的 canonical builder 與裸的 encodeURIComponent，
#: 讓 parity 測試同時證明「兩端一致」以及「這幾個字元真的是兩個標準編碼器的分歧點」。
ENCODER_PARITY_RUNNER = r"""
const fs = require('fs');
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
let out = null;
eval(
  fs.readFileSync(process.argv[2], 'utf-8') +
  '\nout = cases.map(c => [buildRouteHash(c), encodeURIComponent(String(c.app))]);'
);
console.log('__REPORT__' + JSON.stringify(out));
"""


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

// 預設互動腳本：載入 → 主動切 view → back → forward。
// argv[4] 可指定另一份腳本檔以驗證其他 back/forward 情境（見 STEPS_*）。
const DEFAULT_STEPS = `
  // 使用者主動切換 view：URL 必須跟著走，並產生一個 history entry。
  switchView('issues');
  report.steps.afterSwitchView = snapshot();

  // back 回到載入時的畫面，forward 再回到 issues。
  historyGo(-1);
  report.steps.afterBack = snapshot();
  historyGo(1);
  report.steps.afterForward = snapshot();
`;
const stepsScript = process.argv[4] ? fs.readFileSync(process.argv[4], 'utf-8') : DEFAULT_STEPS;

const report = { error: null, steps: {} };
try {
  eval(fs.readFileSync(process.argv[2], 'utf-8'));
  report.steps.onLoad = snapshot();
  // 與 client JS 同一層的 direct eval，因此腳本可以呼叫 client 的 function 宣告
  // （switchView / closeReleaseDetail / getRouteContext…）以及本檔的 snapshot / historyGo。
  eval(stepsScript);
} catch (e) {
  report.error = String((e && e.stack) || e);
}
console.log('__REPORT__' + JSON.stringify(report));
"""


#: 從「帶 version 的 deep link」出發，走一遍會**移除** context 的 back/forward。
#: 重點不是「back 能不能把畫面帶回去」（那是 view 還原），而是「forward 到一個
#: 沒有 version 的 entry 時，先前開著的 release 詳情有沒有被關掉」。
STEPS_VERSION_CONTEXT_REMOVAL = """
  // 使用者關掉詳情 → setRouteContext({version:null}) 寫出一個沒有 version 的新 entry。
  closeReleaseDetail();
  report.steps.afterClose = snapshot();

  // back：回到帶 version 的 entry，詳情要重新打開。
  historyGo(-1);
  report.steps.afterBack = snapshot();

  // forward：回到沒有 version 的 entry，詳情必須關閉（否則 URL 與畫面互相矛盾）。
  historyGo(1);
  report.steps.afterForward = snapshot();
"""

#: 同一件事的 platform 版本：導覽到一個沒有 platform 的 route，再 back / forward。
STEPS_PLATFORM_CONTEXT_REMOVAL = """
  // 前往一條沒有 platform / version 的連結（等同使用者點了另一條分享連結）。
  location.hash = '#issues?app=shop_app';
  report.steps.afterNoPlatformRoute = snapshot();

  // back：回到帶 platform 的 entry，select 要跟著回去。
  historyGo(-1);
  report.steps.afterBack = snapshot();

  // forward：回到沒有 platform 的 entry，select 必須回到中性 ALL。
  historyGo(1);
  report.steps.afterForward = snapshot();
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

    def _run(self, bundle: dict, initial_hash: str, steps: str | None = None) -> dict:
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
            argv = [self.node_bin, str(runner_path), str(js_path), initial_hash]
            if steps is not None:
                steps_path = Path(tmp) / "steps.js"
                steps_path.write_text(steps, encoding="utf-8")
                argv.append(str(steps_path))
            res = subprocess.run(
                argv,
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

    # ── 依賴鏈：無效的上游 context 不得讓下游在 fallback 上命中 ──────────
    #
    # 這幾個測試用的是 fixture 裡刻意存在的碰撞：``3.2.0`` 存在於 **預設 app**
    # (``shop_app``) 的 version_health，``1.8.0`` 存在於 ``rider_app`` 的
    # **android** 平台。因此一條半殘的連結若被「逐欄獨立驗證」，就會安靜地開到
    # 另一個 release——這比連結完全失效更糟，因為使用者不會發現看錯了版本。

    def test_invalid_app_does_not_resolve_version_against_the_fallback_app(self) -> None:
        """``app`` 無效時，同名版本剛好存在於 fallback app，也不得被打開。"""
        default_app = self.plain_bundle["default_app"]
        self.assertTrue(
            any(
                v["version"] == "3.2.0"
                for v in self.plain_bundle["apps"][default_app].get("version_health", [])
            ),
            "此測試的前提：3.2.0 必須真的存在於 fallback app，否則測不到誤命中",
        )
        steps = self._run(self.plain_bundle, "#releases?app=ghost_app&platform=android&version=3.2.0")
        on_load = steps["onLoad"]
        self.assertEqual(on_load["app"], default_app, "未知 app 仍退回預設 app（既有行為）")
        self.assertIsNone(
            on_load["routeContext"]["platform"],
            "app 無效時 platform 是被那個 app 界定的，不得改用 fallback app 驗證",
        )
        self.assertIsNone(
            on_load["routeContext"]["version"],
            "app 無效時不得開啟 fallback app 中同名的版本",
        )
        self.assertFalse(on_load["modalActive"], "不得開啟另一個 app 的 release 詳情")
        for dropped in ("ghost_app", "android", "3.2.0"):
            self.assertIn(dropped, on_load["toast"], "被丟棄的 context 仍必須告知使用者")

    def test_invalid_platform_does_not_resolve_version_on_another_platform(self) -> None:
        """``platform`` 無效時，同名版本存在於**別的**平台，也不得被打開。"""
        rider = self.plain_bundle["apps"]["rider_app"]
        self.assertEqual(
            [(v["version"], v["platform"]) for v in rider["version_health"]],
            [("1.8.0", "android")],
            "此測試的前提：1.8.0 只存在於 rider_app 的 android",
        )
        steps = self._run(self.plain_bundle, "#releases?app=rider_app&platform=ios&version=1.8.0")
        on_load = steps["onLoad"]
        self.assertEqual(on_load["app"], "rider_app", "app 本身有效，仍要切過去")
        self.assertIsNone(on_load["routeContext"]["platform"])
        self.assertIsNone(
            on_load["routeContext"]["version"],
            "platform 無效不等於『沒指定 platform』：version 不得在任意平台上命中",
        )
        self.assertFalse(on_load["modalActive"], "不得開啟另一個平台的 release 詳情")
        for dropped in ("ios", "1.8.0"):
            self.assertIn(dropped, on_load["toast"])

    def test_version_requested_on_a_platform_it_does_not_exist_on_is_dropped(self) -> None:
        """platform 有效、但該版本不在該平台上：仍必須丟棄 version。"""
        steps = self._run(self.plain_bundle, "#releases?app=rider_app&platform=android&version=3.2.0")
        on_load = steps["onLoad"]
        self.assertEqual(on_load["routeContext"]["platform"], "android", "platform 本身有效")
        self.assertIsNone(on_load["routeContext"]["version"], "3.2.0 不屬於 rider_app")
        self.assertFalse(on_load["modalActive"])

    def test_omitting_app_still_resolves_context_against_the_default_app(self) -> None:
        """降級只針對『要求了但無效』；完全不帶 ``app`` 的連結行為必須維持不變。"""
        steps = self._run(self.plain_bundle, "#releases?platform=android&version=3.2.0")
        on_load = steps["onLoad"]
        self.assertEqual(on_load["app"], self.plain_bundle["default_app"])
        self.assertEqual(on_load["routeContext"]["platform"], "android")
        self.assertEqual(
            on_load["routeContext"]["version"], "3.2.0", "沒有 app 參數時仍用目前/預設 app 解析"
        )
        self.assertTrue(on_load["modalActive"])
        self.assertEqual(on_load["toast"], "", "沒有任何 context 被丟棄，不該出現 toast")

    # ── back/forward：route 移除 context 時，UI 必須跟著清掉 ───────────────

    def test_forward_to_a_route_without_version_closes_the_stale_release_detail(self) -> None:
        """套用 route 是取代而非疊加：URL 說沒有 version，詳情就不能還開著。"""
        link = nav.build_deep_link_fragment(
            "releases", app="shop_app", platform="android", version="3.2.0"
        )
        steps = self._run(self.catalog_bundle, link, steps=STEPS_VERSION_CONTEXT_REMOVAL)
        self.assertTrue(steps["onLoad"]["modalActive"], "前提：deep link 有把詳情打開")
        self.assertFalse(steps["afterClose"]["modalActive"])
        self.assertEqual(
            steps["afterClose"]["hash"],
            "#releases?app=shop_app&platform=android",
            "關閉詳情要寫出一個沒有 version 的 entry",
        )
        self.assertTrue(steps["afterBack"]["modalActive"], "back 回到帶 version 的 entry 要重開詳情")
        self.assertEqual(steps["afterBack"]["routeContext"]["version"], "3.2.0")
        self.assertFalse(
            steps["afterForward"]["modalActive"],
            "forward 到沒有 version 的 entry 必須把詳情關掉",
        )
        self.assertIsNone(steps["afterForward"]["routeContext"]["version"])

    def test_route_without_platform_resets_the_platform_select_to_neutral(self) -> None:
        """route 移除 platform 時，殘留的 select 值會與 URL 矛盾，必須回到中性 ALL。"""
        link = nav.build_deep_link_fragment("releases", app="shop_app", platform="android")
        steps = self._run(self.plain_bundle, link, steps=STEPS_PLATFORM_CONTEXT_REMOVAL)
        self.assertEqual(steps["onLoad"]["releasePlatformSelect"], "android", "前提：deep link 套用了 platform")
        self.assertEqual(
            steps["afterNoPlatformRoute"]["releasePlatformSelect"],
            nav.ROUTE_PLATFORM_ALL,
            "沒有 platform 的 route 必須把 select 重設為中性值",
        )
        self.assertEqual(steps["afterBack"]["releasePlatformSelect"], "android", "back 要把 platform 帶回來")
        self.assertEqual(
            steps["afterForward"]["releasePlatformSelect"],
            nav.ROUTE_PLATFORM_ALL,
            "forward 回到沒有 platform 的 entry 必須再次清空",
        )
        self.assertIsNone(steps["afterForward"]["routeContext"]["platform"])

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


class TestCanonicalEncoderParityWithNode(unittest.TestCase):
    """Python builder 與 client builder 必須逐位元一致。

    #78 的對外承諾是「同一個 release 產生同一條 canonical link」：Google Chat 貼出的
    連結（Python 產生）與使用者在畫面上互動後 URL 欄的內容（client 產生）必須是同一個
    字串，否則兩者無法互相比對、也不能當成同一個分享單位。這裡逐字元把關兩個標準編碼器
    的分歧集合（``encodeURIComponent`` 放行、``quote(safe="")`` 不放行的那幾個字元）。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.node_bin = shutil.which("node")

    def _js_results(self, cases: list[dict]) -> list[list[str]]:
        if not self.node_bin:
            self.skipTest("Node.js runtime is not available in environment")
        with tempfile.TemporaryDirectory() as tmp:
            js_path = Path(tmp) / "routing.js"
            js_path.write_text(nav.get_navigation_js(), encoding="utf-8")
            cases_path = Path(tmp) / "cases.json"
            cases_path.write_text(json.dumps(cases), encoding="utf-8")
            runner_path = Path(tmp) / "runner.js"
            runner_path.write_text(ENCODER_PARITY_RUNNER, encoding="utf-8")
            res = subprocess.run(
                [self.node_bin, str(runner_path), str(js_path), str(cases_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
        self.assertEqual(res.returncode, 0, f"Node failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")
        marker = [line for line in res.stdout.splitlines() if line.startswith("__REPORT__")]
        self.assertTrue(marker, f"runner produced no report:\nSTDERR:\n{res.stderr}")
        return json.loads(marker[-1][len("__REPORT__") :])

    def test_divergent_characters_are_encoded_identically_on_both_sides(self) -> None:
        """``!'()*``：``encodeURIComponent`` 留字面、Python ``quote(safe='')`` 會編碼。"""
        cases = [
            {"view": "overview", "app": f"a{ch}b", "platform": "android", "version": f"1.0.0-rc{ch}1"}
            for ch in nav.ROUTE_EXTRA_ESCAPE_CHARS
        ]
        results = self._js_results(cases)
        self.assertEqual(len(results), len(cases))
        for case, (js_hash, naive_app) in zip(cases, results, strict=True):
            with self.subTest(char=case["app"][1]):
                expected = nav.build_deep_link_fragment(
                    case["view"],
                    app=case["app"],
                    platform=case["platform"],
                    version=case["version"],
                )
                self.assertEqual(js_hash, expected)
                # 反向證明此字元真的是分歧點：裸的 encodeURIComponent 與 canonical 不同。
                self.assertNotEqual(
                    naive_app,
                    nav.encode_route_value(case["app"]),
                    f"{case['app'][1]!r} 不是兩個編碼器的分歧字元，這個 case 沒有鑑別力",
                )

    def test_arbitrary_values_are_encoded_identically_on_both_sides(self) -> None:
        """一般會出現在 app id / version 字串裡的字元（含會破壞 query 結構的那些）。"""
        cases = [
            {"view": "overview", "app": "shop_app", "platform": "android", "version": "3.2.0"},
            {"view": "releases", "app": "a b/c", "platform": "ios", "version": "1.0 (build 7)"},
            {"view": "issues", "app": "a&b=c#d%e", "platform": "android", "version": "v~1.0-x_2.3"},
            {"view": "releases", "app": "漢字 app", "platform": "ios", "version": "版本 1.0*"},
            {"view": "issues", "app": "a+b", "platform": "android", "version": "1.0?x"},
        ]
        results = self._js_results(cases)
        for case, (js_hash, _naive) in zip(cases, results, strict=True):
            with self.subTest(app=case["app"], version=case["version"]):
                self.assertEqual(
                    js_hash,
                    nav.build_deep_link_fragment(
                        case["view"],
                        app=case["app"],
                        platform=case["platform"],
                        version=case["version"],
                    ),
                )

    def test_canonical_link_survives_a_round_trip_through_the_client_parser(self) -> None:
        """編碼一致還不夠：client 必須能把 canonical link 原樣解回同一組 context。"""
        app, platform, version = "a!'()*b", "android", "1.0 (rc*1)"
        link = nav.build_deep_link_fragment("releases", app=app, platform=platform, version=version)
        route = nav.parse_deep_link(link)
        self.assertEqual((route.app, route.platform, route.version), (app, platform, version))
        results = self._js_results([{"view": "releases", "app": app, "platform": platform, "version": version}])
        self.assertEqual(results[0][0], link)


if __name__ == "__main__":
    unittest.main()
