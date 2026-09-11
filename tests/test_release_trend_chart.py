"""Release 詳情的指標演進圖 (Issue #66 項目 6).

橫軸是**此版本的歷次品質閘門評估**，兩條線是 gate 已經記錄下來的
`gate_history[].rule_results[].current_value`（崩潰率變化 / 無崩潰用戶率變化）。

三個不變量：

1. **圖上的每個數字都來自資料**，包含兩條門檻線——它們讀同一筆 `rule_results` 的
   `warn_threshold` / `fail_threshold`，與 #74 的「前端不得硬編門檻」同一條規則。
   本檔以反向證明把關：改掉 fixture 裡的門檻值，畫出來的線必須跟著動。
2. **缺觀測值畫成斷線，不補 0**：補 0 會讓「沒量到」看起來像「沒有變化」。
3. **一次評估不是趨勢**：只有一筆評估時不畫圖（單點折線圖會被讀成「很平穩」），
   改為一句說明。

Node runtime 重用 `tests.test_dashboard_deep_link.NODE_RUNNER`，並在 steps 裡換掉
`Chart` 以捕捉真正傳給 Chart.js 的設定——斷言的是「餵給圖表的資料」，而不是
「DOM 裡有一個 canvas」。
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
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.build_dashboard import build_html
from crash_trend.dashboard import releases
from tests.test_dashboard_deep_link import NODE_RUNNER

FIXTURE = ROOT / "tests" / "fixtures" / "dashboard_v2_release_decision.json"

#: fixture 中 shop_app / android / 3.2.0 的三次評估（gate_history 最完整的一筆）。
TARGET = ("shop_app", "android", "3.2.0")
#: 沒有 gate_history 的版本：整個圖表區塊都不該出現。
NO_HISTORY = ("shop_app", "android", "3.1.2")
#: 期望的崩潰率變化（比例 × 100），直接寫死以免從 fixture 推導而跟著壞值一起同意。
EXPECTED_CRASH_PCT = [-3.7, 21.21, 37.57]
EXPECTED_CRASH_WARN_PCT = 10.0
EXPECTED_CFU_PCT = [0.02, -0.09, -0.26]


def steps_for(version: str, platform: str, reopen: bool = False) -> str:
    """在 Node 內開啟 release 詳情，並捕捉傳給 Chart 的設定。"""
    return f"""
  const configs = [];
  const destroyed = [];
  global.Chart = function (ctx, cfg) {{
    configs.push(cfg);
    return {{ destroy: () => destroyed.push(cfg), update: () => {{}} }};
  }};
  openReleaseDetail({json.dumps(version)}, {json.dumps(platform)});
  report.steps.body = elements['releaseModalBody'].innerHTML;
  report.steps.configs = configs.length;
  report.steps.config = configs.length ? configs[configs.length - 1] : null;
  {"openReleaseDetail(" + json.dumps(version) + ", " + json.dumps(platform) + ");" if reopen else ""}
  report.steps.configsAfterReopen = configs.length;
  report.steps.destroyedBeforeClose = destroyed.length;
  closeReleaseDetail();
  report.steps.destroyedAfterClose = destroyed.length;
"""


class TrendChartRuntime(unittest.TestCase):
    """在 Node 內跑真正產出的 client JS。"""

    node_bin: str | None
    bundle: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.node_bin = shutil.which("node")
        cls.bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def render(self, bundle: dict[str, Any], steps: str) -> dict[str, Any]:
        if not self.node_bin:
            self.skipTest("Node.js runtime is not available in environment")
        html = build_html(bundle)
        scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        self.assertGreaterEqual(len(scripts), 2)
        dom_ids = sorted(set(re.findall(r'id="([A-Za-z0-9_-]+)"', html)))
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "client.js").write_text(scripts[1], encoding="utf-8")
            (t / "dom_ids.json").write_text(json.dumps(dom_ids), encoding="utf-8")
            (t / "runner.js").write_text(NODE_RUNNER, encoding="utf-8")
            (t / "steps.js").write_text(steps, encoding="utf-8")
            res = subprocess.run(
                [self.node_bin, str(t / "runner.js"), str(t / "client.js"), "", str(t / "steps.js")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
        self.assertEqual(res.returncode, 0, f"Node failed:\n{res.stdout}\n{res.stderr}")
        marker = [ln for ln in res.stdout.splitlines() if ln.startswith("__REPORT__")]
        self.assertTrue(marker, f"no report:\n{res.stdout}\n{res.stderr}")
        report = json.loads(marker[-1][len("__REPORT__") :])
        self.assertIsNone(report["error"], f"client JS threw: {report['error']}")
        return report["steps"]

    def open_target(
        self,
        bundle: dict[str, Any] | None = None,
        target: tuple[str, str, str] = TARGET,
        reopen: bool = False,
    ) -> dict[str, Any]:
        _, platform, version = target
        return self.render(
            bundle if bundle is not None else copy.deepcopy(self.bundle),
            steps_for(version, platform, reopen=reopen),
        )

    @staticmethod
    def release(bundle: dict[str, Any], target: tuple[str, str, str] = TARGET) -> dict[str, Any]:
        app_id, platform, version = target
        for item in bundle["apps"][app_id]["release_catalog"]:
            if item["platform"] == platform and item["version"] == version:
                return item
        raise AssertionError(f"fixture 沒有 {target}")

    @staticmethod
    def dataset(config: dict[str, Any], label_contains: str) -> dict[str, Any] | None:
        for ds in config["data"]["datasets"]:
            if label_contains in ds["label"]:
                return ds
        return None


class TestTheChartIsFedFromTheData(TrendChartRuntime):
    def setUp(self) -> None:
        self.dump = self.open_target()
        self.assertIsNotNone(self.dump["config"], "前提：這個版本應該畫出圖")

    def test_one_chart_is_created_for_the_release(self) -> None:
        self.assertEqual(self.dump["configs"], 1)
        self.assertEqual(self.dump["config"]["type"], "line")

    def test_x_axis_is_the_evaluation_timeline(self) -> None:
        labels = self.dump["config"]["data"]["labels"]
        self.assertEqual(
            labels, ["2026-08-21 02:00", "2026-08-28 02:00", "2026-09-02 14:15"]
        )

    def test_crash_rate_series_values_come_from_rule_results(self) -> None:
        ds = self.dataset(self.dump["config"], "崩潰率 變化")
        self.assertIsNotNone(ds)
        assert ds is not None
        self.assertEqual([round(v, 2) for v in ds["data"]], EXPECTED_CRASH_PCT)
        self.assertEqual(ds["yAxisID"], "y")

    def test_crash_free_users_series_is_on_the_second_axis(self) -> None:
        ds = self.dataset(self.dump["config"], "無崩潰用戶率 變化")
        self.assertIsNotNone(ds)
        assert ds is not None
        self.assertEqual([round(v, 2) for v in ds["data"]], EXPECTED_CFU_PCT)
        self.assertEqual(ds["yAxisID"], "y1")

    def test_series_labels_reuse_the_gate_vocabulary(self) -> None:
        """同一個指標在 Gate、比較面與這張圖上必須是同一個詞。"""
        labels = {spec.metric_name: spec.label for spec in releases.COMPARISON_METRIC_SPECS}
        for metric_name, _axis in releases.RELEASE_TREND_SERIES:
            with self.subTest(metric=metric_name):
                self.assertIsNotNone(
                    self.dataset(self.dump["config"], labels[metric_name]),
                    f"{metric_name} 的線沒有用 gate spec 表的顯示名",
                )

    def test_both_thresholds_are_drawn_as_flat_reference_lines(self) -> None:
        warn = self.dataset(self.dump["config"], "崩潰率 警告門檻")
        self.assertIsNotNone(warn)
        assert warn is not None
        self.assertEqual(warn["data"], [EXPECTED_CRASH_WARN_PCT] * 3)
        self.assertEqual(warn["pointRadius"], 0, "門檻線不該有資料點")
        self.assertTrue(warn.get("borderDash"), "門檻線必須是虛線，避免被讀成觀測值")
        self.assertIsNotNone(self.dataset(self.dump["config"], "崩潰率 失敗門檻"))


class TestThresholdsFollowTheDataNotTheCode(TrendChartRuntime):
    """反向證明：改掉資料裡的門檻，畫出來的線必須跟著動。

    這比掃描字面值可靠——圖表設定裡本來就有一堆與門檻無關的數字（字級、tension），
    純字面掃描會一直誤報。
    """

    def test_a_different_threshold_in_the_data_moves_the_line(self) -> None:
        bundle = copy.deepcopy(self.bundle)
        release = self.release(bundle)
        for point in release["gate_history"]:
            for rule in point["rule_results"]:
                if rule["metric_name"] == "crash_rate_change_pct":
                    rule["warn_threshold"] = 0.42
                    rule["fail_threshold"] = 0.99
        dump = self.open_target(bundle)
        warn = self.dataset(dump["config"], "崩潰率 警告門檻")
        fail = self.dataset(dump["config"], "崩潰率 失敗門檻")
        assert warn is not None and fail is not None
        self.assertEqual([round(v, 2) for v in warn["data"]], [42.0] * 3)
        self.assertEqual([round(v, 2) for v in fail["data"]], [99.0] * 3)

    def test_the_latest_recorded_threshold_wins(self) -> None:
        """policy 中途調整過時，門檻線要用最新那份，才與畫面其他地方的判定一致。"""
        bundle = copy.deepcopy(self.bundle)
        release = self.release(bundle)
        for rule in release["gate_history"][0]["rule_results"]:
            if rule["metric_name"] == "crash_rate_change_pct":
                rule["warn_threshold"] = 0.01
        for rule in release["gate_history"][-1]["rule_results"]:
            if rule["metric_name"] == "crash_rate_change_pct":
                rule["warn_threshold"] = 0.33
        dump = self.open_target(bundle)
        warn = self.dataset(dump["config"], "崩潰率 警告門檻")
        assert warn is not None
        self.assertEqual([round(v, 2) for v in warn["data"]], [33.0] * 3)

    def test_a_metric_without_thresholds_still_draws_its_series(self) -> None:
        bundle = copy.deepcopy(self.bundle)
        release = self.release(bundle)
        for point in release["gate_history"]:
            for rule in point["rule_results"]:
                if rule["metric_name"] == "crash_rate_change_pct":
                    rule.pop("warn_threshold", None)
                    rule.pop("fail_threshold", None)
        dump = self.open_target(bundle)
        self.assertIsNotNone(self.dataset(dump["config"], "崩潰率 變化"))
        self.assertIsNone(self.dataset(dump["config"], "崩潰率 警告門檻"))


class TestMissingObservationsAreGapsNotZeros(TrendChartRuntime):
    def test_a_null_metric_becomes_a_gap(self) -> None:
        bundle = copy.deepcopy(self.bundle)
        release = self.release(bundle)
        for rule in release["gate_history"][1]["rule_results"]:
            if rule["metric_name"] == "crash_rate_change_pct":
                rule["current_value"] = None
        dump = self.open_target(bundle)
        ds = self.dataset(dump["config"], "崩潰率 變化")
        assert ds is not None
        self.assertIsNone(ds["data"][1], "缺觀測值被補成數字了")
        self.assertNotEqual(ds["data"][1], 0)
        self.assertFalse(ds["spanGaps"], "spanGaps 為真會把斷點連成一條直線")

    def test_a_series_with_no_observations_at_all_is_dropped(self) -> None:
        """整條線都沒有資料時不畫空線，也不畫成一條 0 的水平線。"""
        bundle = copy.deepcopy(self.bundle)
        release = self.release(bundle)
        for point in release["gate_history"]:
            point["rule_results"] = [
                r for r in point["rule_results"] if r["metric_name"] != "crash_free_users_diff"
            ]
        dump = self.open_target(bundle)
        self.assertIsNone(self.dataset(dump["config"], "無崩潰用戶率 變化"))
        self.assertIsNotNone(self.dataset(dump["config"], "崩潰率 變化"))


class TestDegradation(TrendChartRuntime):
    def test_a_single_evaluation_is_not_charted(self) -> None:
        bundle = copy.deepcopy(self.bundle)
        release = self.release(bundle)
        release["gate_history"] = release["gate_history"][:1]
        dump = self.open_target(bundle)
        self.assertEqual(dump["configs"], 0, "一次評估不構成趨勢，不該畫圖")
        self.assertIn("尚無法構成趨勢", dump["body"])

    def test_no_history_renders_no_chart_block(self) -> None:
        dump = self.open_target(target=NO_HISTORY)
        self.assertEqual(dump["configs"], 0)
        self.assertNotIn(releases.RELEASE_TREND_CANVAS_ID, dump["body"])
        self.assertNotIn("尚無法構成趨勢", dump["body"])

    def test_an_empty_history_list_renders_no_chart_block(self) -> None:
        bundle = copy.deepcopy(self.bundle)
        self.release(bundle)["gate_history"] = []
        dump = self.open_target(bundle)
        self.assertEqual(dump["configs"], 0)
        self.assertNotIn(releases.RELEASE_TREND_CANVAS_ID, dump["body"])

    def test_the_chart_survives_a_history_point_without_rule_results(self) -> None:
        """舊快照可能沒有 rule_results；不得讓整個 modal 因此丟例外。"""
        bundle = copy.deepcopy(self.bundle)
        self.release(bundle)["gate_history"][0].pop("rule_results")
        dump = self.open_target(bundle)
        ds = self.dataset(dump["config"], "崩潰率 變化")
        assert ds is not None
        self.assertIsNone(ds["data"][0])
        self.assertEqual(len(ds["data"]), 3)


class TestChartInstanceLifecycle(TrendChartRuntime):
    def test_reopening_destroys_the_previous_instance(self) -> None:
        """不銷毀就重畫會把兩張圖疊在同一個 canvas 上。"""
        dump = self.open_target(reopen=True)
        self.assertEqual(dump["configsAfterReopen"], 2)
        self.assertGreaterEqual(dump["destroyedBeforeClose"], 1)

    def test_closing_the_modal_destroys_the_chart(self) -> None:
        dump = self.open_target()
        self.assertEqual(dump["destroyedBeforeClose"], 0)
        self.assertEqual(dump["destroyedAfterClose"], 1)


class TestSeriesSpecIsBoundToTheGateMetrics(unittest.TestCase):
    """這張圖只能畫 gate 真的評估過的指標。"""

    def test_every_series_metric_exists_in_the_gate_spec_table(self) -> None:
        known = {spec.metric_name for spec in releases.COMPARISON_METRIC_SPECS}
        for metric_name, _axis in releases.RELEASE_TREND_SERIES:
            with self.subTest(metric=metric_name):
                self.assertIn(metric_name, known)

    def test_an_unknown_metric_is_rejected_loudly(self) -> None:
        original = releases.RELEASE_TREND_SERIES
        releases.RELEASE_TREND_SERIES = (("not_a_gate_metric", "y"),)
        try:
            with self.assertRaises(KeyError):
                releases._release_trend_series_js()
        finally:
            releases.RELEASE_TREND_SERIES = original

    def test_the_series_are_the_two_metrics_the_issue_asks_for(self) -> None:
        self.assertEqual(
            [m for m, _ in releases.RELEASE_TREND_SERIES],
            ["crash_rate_change_pct", "crash_free_users_diff"],
        )


if __name__ == "__main__":
    unittest.main()
