"""Gate 門檻建議 (Issue #76，原 #66 項目 5).

這個工具**推薦**門檻、不套用門檻，因此本檔要釘住的第一件事就是那條界線：它不寫設定檔、
不參與 `threshold_source` 契約、也不會被 gate 在評估時呼叫。

其餘三件：

1. **反事實必須等於 gate 真的會怎麼判。** 「這 7 次發布在現行門檻下有 2 次 fail」是這份
   建議唯一可被審查的部分；若它是用自己寫的比較算出來的，那它就不是證據。因此有一條
   測試直接跑 `evaluate_release()`，逐一比對兩邊的判定。
2. **正負號只有一套。** `GatePolicy` 的門檻已經在「正值代表退化」的 frame 裡，而
   `vs_previous` 的變化量不是（下降型指標的退化是負值）。開發過程中這裡真的寫錯過一次：
   對 policy 門檻多做一次方向換算，導致無崩潰用戶率的反事實變成 4/4 fail。
3. **寧可不給數字。** 樣本不足、從未觀測到退化、分佈過於集中三種情況一律拒絕給建議，
   並說明原因。
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.gate.evaluator import evaluate_release
from crash_trend.gate.metric_rules import (
    COMPARISON_METRIC_SPECS,
    classify_threshold_breach,
    oriented_change,
)
from crash_trend.gate.policy import GatePolicy, ThresholdRule, load_gate_policy
from crash_trend.gate.threshold_recommendation import (
    MIN_OBSERVATIONS,
    collect_observations,
    nearest_rank_percentile,
    recommend_for_metric,
    recommend_thresholds,
)
from crash_trend.recommend_gate_thresholds import format_report
from crash_trend.schema_v2 import COMPARISON_THRESHOLD_SOURCE
from tests.test_overview_comparison import BASE_RECENT_HEALTH

FIXTURE = ROOT / "tests" / "fixtures" / "dashboard_v2_release_decision.json"

CRASH_SPEC = next(s for s in COMPARISON_METRIC_SPECS if s.metric_name == "crash_rate_change_pct")
CFU_SPEC = next(s for s in COMPARISON_METRIC_SPECS if s.metric_name == "crash_free_users_diff")


def catalog_item(
    version: str,
    vs_previous: dict[str, Any] | None,
) -> dict[str, Any]:
    """一個 gate 會真的走完 rule 1~4 的 catalog item（樣本充足，不會早退）。"""
    item: dict[str, Any] = {
        "version": version,
        "platform": "android",
        "status": "active",
        "recent_health": copy.deepcopy(BASE_RECENT_HEALTH),
        "issue_lifecycle": {
            "introduced_count": 0,
            "persistent_count": 0,
            "regressed_count": 0,
            "resolved_count": 0,
        },
    }
    if vs_previous is not None:
        item["vs_previous"] = {
            "previous_version": "prev",
            "previous_sample_sufficient": True,
            "previous_sessions_total": 50000,
            **vs_previous,
        }
    return item


#: 一個 gate 會在 sample sufficiency guard 就擋下來的 recent_health：採用率、工作階段、
#: 事件數三條都不達 `GatePolicy()` 的預設門檻。刻意不寫 `sample_sufficient` 旗標——
#: 那是硬否決，會讓「充足與否由 policy 決定」這件事測不出來。
STARVED_RECENT_HEALTH: dict[str, Any] = {
    "30d": {
        "crash_events": 2,
        "affected_users": 1,
        "sessions_total": 40,
        "adoption_rate": 0.001,
    }
}


def make_catalog(
    crash_changes: list[float | None],
    cfu_diffs: list[float] | None = None,
) -> list[dict[str, Any]]:
    cfu = cfu_diffs if cfu_diffs is not None else [0.0] * len(crash_changes)
    return [
        catalog_item(
            f"1.0.{idx}",
            {
                "crash_rate_change_pct": change,
                "fatal_rate_change_pct": 0.0,
                "anr_rate_change_pct": 0.0,
                "crash_free_users_diff": diff,
            },
        )
        for idx, (change, diff) in enumerate(zip(crash_changes, cfu, strict=True))
    ]


class TestPercentileIsDeterministicAndObserved(unittest.TestCase):
    def test_nearest_rank_returns_an_observed_value(self) -> None:
        """不插值：門檻要能對應到「哪幾次發布會被擋下來」。"""
        values = [0.0, 0.1, 0.2, 0.3, 0.4]
        for p in (0.1, 0.5, 0.8, 0.95, 1.0):
            with self.subTest(p=p):
                self.assertIn(nearest_rank_percentile(values, p), values)

    def test_known_ranks(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        self.assertEqual(nearest_rank_percentile(values, 0.2), 1.0)
        self.assertEqual(nearest_rank_percentile(values, 0.5), 3.0)
        self.assertEqual(nearest_rank_percentile(values, 0.8), 4.0)
        self.assertEqual(nearest_rank_percentile(values, 0.95), 5.0)

    def test_order_does_not_matter(self) -> None:
        self.assertEqual(
            nearest_rank_percentile([5.0, 1.0, 3.0, 2.0, 4.0], 0.8),
            nearest_rank_percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.8),
        )

    def test_an_empty_set_raises_instead_of_inventing_a_value(self) -> None:
        with self.assertRaises(ValueError):
            nearest_rank_percentile([], 0.8)


class TestObservationsAreOrientedAndFiltered(unittest.TestCase):
    def test_a_drop_in_crash_free_users_is_a_positive_observation(self) -> None:
        """下降型指標：退化在 `vs_previous` 裡是負值，觀測值必須翻正。"""
        catalog = make_catalog([0.0] * 3, cfu_diffs=[-0.01, 0.0, 0.02])
        obs = collect_observations(catalog, CFU_SPEC, GatePolicy())
        self.assertEqual(obs.values, (0.01, -0.0, -0.02))

    def test_zero_baseline_releases_are_excluded_and_counted(self) -> None:
        """零基準的變化率是 ±1.0 的哨兵值，算進分佈只會把門檻拉到 100%。"""
        catalog = make_catalog([0.05, 1.0, 0.08])
        catalog[1]["vs_previous"]["zero_baseline_crash"] = True
        obs = collect_observations(catalog, CRASH_SPEC, GatePolicy())
        self.assertEqual(obs.values, (0.05, 0.08))
        self.assertEqual(obs.zero_baseline, 1)

    def test_an_insufficient_previous_sample_is_excluded_and_counted(self) -> None:
        catalog = make_catalog([0.05, 0.9, 0.08])
        catalog[1]["vs_previous"]["previous_sample_sufficient"] = False
        obs = collect_observations(catalog, CRASH_SPEC, GatePolicy())
        self.assertEqual(obs.values, (0.05, 0.08))
        self.assertEqual(obs.insufficient_previous, 1)

    def test_a_release_whose_own_sample_is_insufficient_is_excluded_and_counted(self) -> None:
        """本版樣本不足時 gate 在 guard 就回 `insufficient_data`，rule 1~4 根本不會跑。

        那筆變化量從來不會被判定，算進分佈只會讓門檻被一個 gate 不看的數值拉高。
        """
        catalog = make_catalog([0.05, 0.9, 0.08])
        catalog[1]["recent_health"] = copy.deepcopy(STARVED_RECENT_HEALTH)
        self.assertEqual(
            evaluate_release(catalog[1], GatePolicy())["gate_status"],
            "insufficient_data",
            "前提：這一版 gate 真的會判 insufficient_data",
        )
        obs = collect_observations(catalog, CRASH_SPEC, GatePolicy())
        self.assertEqual(obs.values, (0.05, 0.08))
        self.assertEqual(obs.insufficient_current, 1)

    def test_sample_sufficiency_follows_the_policy_the_gate_will_use(self) -> None:
        """門檻放寬後 gate 會判定這一版，推薦器就必須把它收進分佈。"""
        catalog = make_catalog([0.05, 0.9, 0.08])
        catalog[1]["recent_health"] = copy.deepcopy(STARVED_RECENT_HEALTH)
        lenient = GatePolicy(min_sessions=10, min_adoption_rate=0.0005, min_version_events=1)
        self.assertNotEqual(
            evaluate_release(catalog[1], lenient)["gate_status"], "insufficient_data"
        )
        obs = collect_observations(catalog, CRASH_SPEC, lenient)
        self.assertEqual(obs.values, (0.05, 0.9, 0.08))
        self.assertEqual(obs.insufficient_current, 0)

    def test_a_missing_metric_is_counted_not_treated_as_zero(self) -> None:
        catalog = make_catalog([0.05, None, 0.08])
        obs = collect_observations(catalog, CRASH_SPEC, GatePolicy())
        self.assertEqual(obs.values, (0.05, 0.08))
        self.assertEqual(obs.missing, 1)

    def test_a_release_without_a_previous_version_is_not_an_observation(self) -> None:
        catalog = make_catalog([0.05, 0.08])
        catalog.append(catalog_item("2.0.0", None))
        obs = collect_observations(catalog, CRASH_SPEC, GatePolicy())
        self.assertEqual(obs.sample_size, 2)
        self.assertEqual((obs.missing, obs.zero_baseline, obs.insufficient_previous), (0, 0, 0))


class TestTheCounterfactualEqualsWhatTheGateWouldSay(unittest.TestCase):
    """這份建議唯一可被審查的部分，必須真的等於 gate 的判定。"""

    CHANGES = [0.02, 0.31, -0.05, 0.12, 0.08, 0.45, 0.10, 0.25]

    def _gate_counts(self, catalog: list[dict[str, Any]], policy: GatePolicy, metric: str) -> dict[str, int]:
        counts = {"pass": 0, "warn": 0, "fail": 0}
        for item in catalog:
            result = evaluate_release(item, policy)
            for rule in result["rule_results"]:
                if rule["metric_name"] == metric:
                    counts[str(rule["status"])] += 1
        return counts

    def test_current_counts_match_a_real_gate_evaluation(self) -> None:
        catalog = make_catalog(list(self.CHANGES))
        policy = GatePolicy()
        rec = recommend_for_metric(catalog, CRASH_SPEC, policy)
        self.assertEqual(
            rec.current_counts,
            self._gate_counts(catalog, policy, CRASH_SPEC.metric_name),
            "反事實與 evaluate_release() 的判定不一致",
        )
        self.assertGreater(rec.current_counts["fail"], 0, "前提：這組資料在現行門檻下有 fail")

    def test_recommended_counts_match_a_gate_run_with_the_recommended_policy(self) -> None:
        """把建議門檻真的寫進 policy 再跑一次 gate，次數必須相同。"""
        catalog = make_catalog(list(self.CHANGES))
        rec = recommend_for_metric(catalog, CRASH_SPEC, GatePolicy())
        self.assertIsNotNone(rec.recommended)
        assert rec.recommended is not None
        adopted = GatePolicy(crash_rate_change_pct=rec.recommended)
        self.assertEqual(
            rec.recommended_counts,
            self._gate_counts(catalog, adopted, CRASH_SPEC.metric_name),
        )

    def test_a_release_the_gate_never_judges_is_absent_from_the_counterfactual(self) -> None:
        """本版樣本不足的版本不得出現在分佈或反事實裡。

        `_gate_counts()` 只數 gate 真的對這個指標做出的判定；那一版 gate 回的是
        `sample_sufficiency` / `insufficient_data`，因此兩邊相等就代表推薦器排掉了它。
        """
        catalog = make_catalog([*self.CHANGES, 0.9])
        catalog[-1]["recent_health"] = copy.deepcopy(STARVED_RECENT_HEALTH)
        policy = GatePolicy()
        rec = recommend_for_metric(catalog, CRASH_SPEC, policy)
        self.assertEqual(rec.observations.insufficient_current, 1)
        self.assertNotIn(0.9, rec.observations.values)
        self.assertEqual(
            rec.current_counts,
            self._gate_counts(catalog, policy, CRASH_SPEC.metric_name),
        )

    def test_the_decrease_metric_counterfactual_is_not_all_fail(self) -> None:
        """回歸測試：對 policy 門檻多做一次方向換算，會讓每一筆都變成 fail。"""
        catalog = make_catalog([0.0] * 6, cfu_diffs=[0.0002, -0.0009, -0.0026, 0.0, 0.0001, -0.001])
        rec = recommend_for_metric(catalog, CFU_SPEC, GatePolicy())
        self.assertEqual(rec.current_counts["fail"], 0, "門檻方向被轉錯了")
        self.assertEqual(rec.current_counts["pass"], 6)
        # 與原語逐筆核對。
        expected = {"pass": 0, "warn": 0, "fail": 0}
        for value in rec.observations.values:
            expected[classify_threshold_breach(value, GatePolicy().crash_free_users_drop)] += 1
        self.assertEqual(rec.current_counts, expected)


class TestRecommendationStaysInThePolicyFrame(unittest.TestCase):
    def test_a_decrease_metric_recommendation_is_positive_like_the_policy(self) -> None:
        """`crash_free_users_drop` 在 apps.yaml 裡存正值（下降幅度）。

        建議值若是負數，貼進設定檔就是完全不同的門檻。
        """
        catalog = make_catalog([0.0] * 7, cfu_diffs=[-0.001, -0.004, 0.0, -0.009, 0.0002, -0.002, 0.0])
        rec = recommend_for_metric(catalog, CFU_SPEC, GatePolicy())
        assert rec.recommended is not None
        self.assertGreater(rec.recommended.warn, 0)
        self.assertGreater(rec.recommended.fail, rec.recommended.warn)

    def test_a_recommendation_survives_load_gate_policy_unchanged(self) -> None:
        """建議值貼進 apps.yaml 後必須原封不動地回來。

        `load_gate_policy()` 會把負門檻退回預設值、`ThresholdRule` 會把 warn 夾到 fail：
        只要建議值踩到任何一條，報表裡的反事實就不是採用後真正會生效的 gate。
        """
        catalogs = (
            make_catalog([-0.5, -0.4, -0.3, -0.2, 0.6]),
            make_catalog([0.02, 0.31, -0.05, 0.12, 0.08, 0.45, 0.10, 0.25]),
            make_catalog(
                [0.0] * 7,
                cfu_diffs=[-0.001, -0.004, 0.0, -0.009, 0.0002, -0.002, 0.0],
            ),
        )
        for idx, catalog in enumerate(catalogs):
            for rec in recommend_thresholds(catalog, GatePolicy()):
                if rec.recommended is None:
                    continue
                with self.subTest(catalog=idx, metric=rec.metric_name):
                    loaded = load_gate_policy(
                        {
                            "release_gate": {
                                "thresholds": {
                                    rec.policy_field: {
                                        "warn": rec.recommended.warn,
                                        "fail": rec.recommended.fail,
                                    }
                                }
                            }
                        }
                    )
                    self.assertEqual(
                        getattr(loaded, rec.policy_field),
                        rec.recommended,
                        "建議門檻被 loader 換掉了，反事實不等於採用後的 gate",
                    )

    def test_recommendation_uses_the_same_frame_as_current(self) -> None:
        for spec in COMPARISON_METRIC_SPECS:
            with self.subTest(metric=spec.metric_name):
                catalog = make_catalog(
                    [0.02, 0.31, -0.05, 0.12, 0.08, 0.45, 0.10],
                    cfu_diffs=[-0.001, -0.004, 0.0, -0.009, 0.0002, -0.002, 0.0],
                )
                rec = recommend_for_metric(catalog, spec, GatePolicy())
                if rec.recommended is None:
                    continue
                self.assertGreater(rec.current.warn, 0, "前提：policy 門檻是正值 frame")
                self.assertGreater(rec.recommended.warn, 0)


class TestItRefusesRatherThanGuessing(unittest.TestCase):
    def test_too_few_observations(self) -> None:
        catalog = make_catalog([0.1] * (MIN_OBSERVATIONS - 1))
        rec = recommend_for_metric(catalog, CRASH_SPEC, GatePolicy())
        self.assertIsNone(rec.recommended)
        self.assertIn("觀測值只有", rec.reason)
        self.assertEqual(rec.percentiles, {}, "拒絕時不該附上看起來可用的百分位數")

    def test_no_regression_ever_observed(self) -> None:
        """全部 ≤ 0 時任何百分位門檻都會讓每次發布觸發。"""
        catalog = make_catalog([-0.1, -0.2, 0.0, -0.05, -0.3, 0.0])
        rec = recommend_for_metric(catalog, CRASH_SPEC, GatePolicy())
        self.assertIsNone(rec.recommended)
        self.assertIn("未觀測到任何退化", rec.reason)

    def test_a_negative_warn_threshold_is_refused(self) -> None:
        """多數版本其實在改善時 p80 會是負數。

        負門檻貼進 `apps.yaml` 會被 `load_gate_policy()` 退回預設值，因此那份反事實
        描述的不是採用後真正會生效的 gate。
        """
        catalog = make_catalog([-0.5, -0.4, -0.3, -0.2, 0.6])
        rec = recommend_for_metric(catalog, CRASH_SPEC, GatePolicy())
        self.assertIsNone(rec.recommended)
        self.assertIn("不大於 0", rec.reason)
        # 為什麼不能給：loader 會把它換掉。
        loaded = load_gate_policy(
            {"release_gate": {"thresholds": {"crash_rate_change_pct": {"warn": -0.2, "fail": 0.6}}}}
        )
        self.assertEqual(loaded.crash_rate_change_pct.warn, GatePolicy().crash_rate_change_pct.warn)

    def test_a_zero_warn_threshold_is_refused(self) -> None:
        """判定是 `value >= warn`，warn = 0 會讓「完全沒有變化」也觸發 WARN。"""
        catalog = make_catalog([0.0, 0.0, 0.0, 0.0, 0.3])
        rec = recommend_for_metric(catalog, CRASH_SPEC, GatePolicy())
        self.assertIsNone(rec.recommended)
        self.assertIn("不大於 0", rec.reason)
        # 為什麼不能給：沒有退化的 0 會被判成 warn。
        self.assertEqual(
            classify_threshold_breach(0.0, ThresholdRule(warn=0.0, fail=0.3)), "warn"
        )

    def test_a_distribution_too_concentrated_to_separate_warn_from_fail(self) -> None:
        catalog = make_catalog([0.2] * 8)
        rec = recommend_for_metric(catalog, CRASH_SPEC, GatePolicy())
        self.assertIsNone(rec.recommended)
        self.assertIn("分佈過於集中", rec.reason)

    def test_a_refusal_still_reports_the_current_counterfactual(self) -> None:
        """就算不給建議，「現行門檻在歷史上會判幾次」仍然是有用的資訊。"""
        catalog = make_catalog([0.3] * (MIN_OBSERVATIONS - 1))
        rec = recommend_for_metric(catalog, CRASH_SPEC, GatePolicy())
        self.assertIsNone(rec.recommended)
        self.assertEqual(sum(rec.current_counts.values()), MIN_OBSERVATIONS - 1)

    def test_the_real_fixture_refuses_because_it_is_small(self) -> None:
        """#90 fixture 只有幾個版本——不得從那麼少的資料推出門檻。"""
        bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
        catalog = bundle["apps"]["shop_app"]["release_catalog"]
        recs = recommend_thresholds(catalog, GatePolicy())
        self.assertTrue(recs)
        for rec in recs:
            with self.subTest(metric=rec.metric_name):
                self.assertLess(rec.observations.sample_size, MIN_OBSERVATIONS)
                self.assertIsNone(rec.recommended)


class TestTheReportIsPasteReady(unittest.TestCase):
    CATALOG = make_catalog(
        [0.02, 0.31, -0.05, 0.12, 0.08, 0.45, 0.01],
        cfu_diffs=[0.0001, -0.004, 0.0002, -0.001, 0.0, -0.009, 0.0003],
    )

    def setUp(self) -> None:
        self.recs = recommend_thresholds(self.CATALOG, GatePolicy())
        self.report = format_report("demo_app", self.recs)

    def test_the_snippet_uses_the_apps_yaml_key_not_the_metric_name(self) -> None:
        """`crash_free_users_diff`（契約欄位）與 `crash_free_users_drop`（設定鍵）不同名。"""
        self.assertIn("crash_free_users_drop", self.report)
        self.assertNotIn("crash_free_users_diff:", self.report)

    def test_the_snippet_round_trips_through_load_gate_policy(self) -> None:
        """最強的「可貼上」證明：真的用 yaml 解析，再餵給 load_gate_policy。"""
        start = self.report.index("    apps:")
        end = self.report.index("=====", start)
        snippet = "\n".join(
            line[4:] if line.startswith("    ") else line
            for line in self.report[start:end].splitlines()
            if line.strip()
        )
        parsed = yaml.safe_load(snippet)
        policy = load_gate_policy(parsed["apps"]["demo_app"])
        for rec in self.recs:
            if rec.recommended is None:
                continue
            with self.subTest(metric=rec.metric_name):
                self.assertEqual(getattr(policy, rec.policy_field), rec.recommended)

    def test_the_report_states_it_changes_nothing(self) -> None:
        self.assertIn("不會修改任何設定檔", self.report)

    def test_the_report_warns_that_the_standard_is_relative(self) -> None:
        """照抄百分位門檻會讓 gate 永遠對最差的那幾 % 喊 fail，這件事必須寫在輸出裡。"""
        self.assertIn("不尋常", self.report)
        self.assertIn("反事實", self.report)

    def test_both_counterfactuals_are_shown_for_an_actionable_metric(self) -> None:
        self.assertIn("現行 pass", self.report)
        self.assertIn("建議 pass", self.report)


class TestItDoesNotCrossIntoRuntimeThresholds(unittest.TestCase):
    """#76 的界線：推薦門檻，不成為第二個門檻來源。"""

    MODULES = (
        ROOT / "crash_trend" / "gate" / "threshold_recommendation.py",
        ROOT / "crash_trend" / "recommend_gate_thresholds.py",
    )

    def test_no_module_writes_configuration(self) -> None:
        """工具不得改 apps.yaml——那會讓門檻的來源從「人寫的設定」變成「工具寫的」。"""
        for path in self.MODULES:
            source = path.read_text(encoding="utf-8")
            code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
            with self.subTest(module=path.name):
                for forbidden in ("write_text(", "yaml.safe_dump", "yaml.dump", 'open('):
                    self.assertNotIn(forbidden, code, f"{path.name} 出現寫入設定的痕跡")

    def test_the_recommendation_is_not_a_threshold_source(self) -> None:
        """`threshold_source` 仍然只有一個合法值，本工具不參與那個契約。

        斷言的是**程式碼**不碰那個欄位：docstring 解釋「為什麼不碰」是必要的，
        但模組不得產生 `threshold_source` 這個鍵，也不需要 import bundle 契約。
        """
        self.assertEqual(COMPARISON_THRESHOLD_SOURCE, "release_gate_policy")
        for path in self.MODULES:
            source = path.read_text(encoding="utf-8")
            with self.subTest(module=path.name):
                self.assertNotIn('"threshold_source"', source, "模組產生了 threshold_source 欄位")
                self.assertNotIn(
                    "from crash_trend.schema_v2 import",
                    source,
                    "推薦器不該需要 bundle 契約",
                )

    def test_the_gate_evaluator_does_not_import_the_recommender(self) -> None:
        """gate 在評估時不得呼叫推薦器——那就是動態 baseline（V3 定案不做）。"""
        for name in ("evaluator.py", "policy.py", "metric_rules.py"):
            source = (ROOT / "crash_trend" / "gate" / name).read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertNotIn("threshold_recommendation", source)


class TestRecommendationCoversEveryGateMetric(unittest.TestCase):
    def test_one_recommendation_per_spec_in_order(self) -> None:
        recs = recommend_thresholds(make_catalog([0.1] * 6), GatePolicy())
        self.assertEqual(
            [r.metric_name for r in recs],
            [s.metric_name for s in COMPARISON_METRIC_SPECS],
        )

    def test_every_recommendation_names_a_real_policy_field(self) -> None:
        policy = GatePolicy()
        for rec in recommend_thresholds(make_catalog([0.1] * 6), policy):
            with self.subTest(metric=rec.metric_name):
                self.assertIsInstance(getattr(policy, rec.policy_field), ThresholdRule)

    def test_observations_are_oriented_with_the_shared_helper(self) -> None:
        """方向換算共用 metric_rules，不在本模組重寫。"""
        catalog = make_catalog([0.0] * 3, cfu_diffs=[-0.01, 0.005, 0.0])
        obs = collect_observations(catalog, CFU_SPEC, GatePolicy())
        expected = tuple(
            oriented_change(CFU_SPEC, v) for v in (-0.01, 0.005, 0.0)
        )
        self.assertEqual(obs.values, expected)


if __name__ == "__main__":
    unittest.main()
