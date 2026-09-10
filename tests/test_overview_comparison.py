"""Gate 對齊的 Previous Release Comparison 契約與呈現測試 (Issue #74)。

這些測試存在的理由：

1. **「Gate PASS 但 comparison 顯示異常」是本單最嚴重的失敗模式。** 兩邊各自寫一次
   `if value >= threshold` 遲早會分歧，而分歧時兩邊都自稱權威、使用者無從判斷該信誰。
   因此這裡不是「檢查 comparison 算得對」，而是**逐一比對 comparison 的
   classification 與 gate 自己對同一個指標、同一組 threshold 的 rule status**。
2. **「threshold 不由前端硬編數字」是最容易被默默違反的驗收條件。** 一顆寫死的
   `0.25` 在 code review 裡看起來完全無害，直到有人調了 policy 而 Dashboard 沒跟上。
   因此把它做成機械檢查：掃描實際產出的比較面 JS 字面值，並反向證明門檻文字真的隨
   policy 改變。
3. **舊 bundle 沒有 `metric_evaluations` 必須仍能 validate / render。** 這是 V3
   schema 擴充的前提；缺欄位時前端必須說「沒有判定」，而不是拿變化量自行套門檻補一個。
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
from crash_trend.catalog.comparison import compute_previous_release_comparison
from crash_trend.dashboard import navigation as nav
from crash_trend.dashboard import overview
from crash_trend.dashboard.assets import get_dashboard_styles, get_shell_js_bottom
from crash_trend.dashboard.overview import get_release_comparison_js
from crash_trend.gate.evaluator import evaluate_release
from crash_trend.gate.metric_rules import (
    COMPARISON_METRIC_SPECS,
    build_comparison_metric_evaluations,
    threshold_rule_for,
)
from crash_trend.gate.policy import GatePolicy, ThresholdRule
from crash_trend.schema_v2 import (
    COMPARISON_THRESHOLD_SOURCE,
    validate_dashboard_v2,
    validate_release_catalog,
)
from tests.test_dashboard_deep_link import NODE_RUNNER

FIXTURE = ROOT / "tests" / "fixtures" / "dashboard_v2_release_decision.json"
LEGACY_FIXTURE = ROOT / "tests" / "fixtures" / "dashboard_v2.json"

#: #74 驗收條件要求的四項指標。寫成字面常數而非從 spec 表推導，否則有人把 spec
#: 刪掉一項時這裡也會跟著「同意」。
REQUIRED_METRICS = (
    "crash_rate_change_pct",
    "fatal_rate_change_pct",
    "anr_rate_change_pct",
    "crash_free_users_diff",
)

#: 讓 gate 不會在樣本不足 / 無前版就早退的最小 release item 骨架。
BASE_RECENT_HEALTH: dict[str, Any] = {
    "30d": {
        "crash_events": 100,
        "affected_users": 50,
        "sessions_total": 50000,
        "adoption_rate": 0.6,
        "sample_sufficient": True,
    }
}


def make_release_item(vs_previous: dict[str, Any]) -> dict[str, Any]:
    """組出一個 gate 會真的走完六條 rule 的 release item。"""
    return {
        "version": "9.9.9",
        "platform": "android",
        "status": "latest",
        "recent_health": copy.deepcopy(BASE_RECENT_HEALTH),
        "issue_lifecycle": {
            "introduced_count": 0,
            "persistent_count": 0,
            "regressed_count": 0,
            "resolved_count": 0,
        },
        "vs_previous": {
            "previous_version": "9.9.8",
            "previous_sample_sufficient": True,
            "previous_sessions_total": 50000,
            **vs_previous,
        },
    }


#: 讓 `validate_release_catalog()` 除了 metric_evaluations 之外零錯誤的最小 catalog item。
#: 與 `make_release_item()` 分開：那個是給 gate evaluator 走完六條 rule 用的，
#: 這個是給 contract validation 用的，兩者的必填欄位不同。
def make_catalog_item_for_validation(
    evaluations: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "version": "9.9.9",
        "platform": "android",
        "status": "latest",
        "lifetime_crashes": 0,
        "lifetime_issues": 0,
        "lifetime_affected_users": 0,
        "lifetime_fatal": 0,
        "lifetime_anr": 0,
        "recent_health": {
            "30d": {"crash_events": 0, "affected_users": 0, "sample_sufficient": True}
        },
        "vs_previous": {
            "previous_version": "9.9.8",
            "stability": "stable",
            "metric_evaluations": evaluations,
        },
    }


def make_evaluation(metric_name: str, /, **overrides: Any) -> dict[str, Any]:
    """從 production producer 的輸出出發，再覆寫成要測的形狀。

    刻意不手寫整個 payload：negative 測試的基底必須是「真的合法」的那一份，
    否則斷言到的可能是自己手寫錯的欄位，而不是要驗的那個矛盾。
    """
    evals = build_comparison_metric_evaluations(
        {
            "crash_rate_change_pct": 0.0,
            "fatal_rate_change_pct": 0.0,
            "anr_rate_change_pct": 0.0,
            "crash_free_users_diff": 0.0,
        },
        GatePolicy(),
    )
    base = next(dict(ev) for ev in evals if ev["metric_name"] == metric_name)
    # positional-only：`metric_name` 也可以被覆寫掉（用來測「自創指標」）。
    base.update(overrides)
    return base


class TestSpecTableBindsEveryRequiredMetricToAGateRule(unittest.TestCase):
    """spec 表是「Dashboard 與 Gate 對同一指標套同一門檻」的唯一定義處。"""

    def test_every_required_metric_has_a_spec(self) -> None:
        covered = {spec.metric_name for spec in COMPARISON_METRIC_SPECS}
        for metric in REQUIRED_METRICS:
            self.assertIn(metric, covered, f"#74 要求涵蓋 {metric}")

    def test_no_spec_invents_a_metric_the_gate_does_not_evaluate(self) -> None:
        """spec 的 rule_name / metric_name 必須真的出現在 gate 的 rule_results 裡。

        否則 comparison 會拿一個 gate 從未評估的指標宣稱「與 Gate 一致」。
        """
        result = evaluate_release(
            make_release_item({
                "crash_rate_change_pct": 0.0,
                "fatal_rate_change_pct": 0.0,
                "anr_rate_change_pct": 0.0,
                "crash_free_users_diff": 0.0,
            }),
            GatePolicy(),
        )
        gate_rules = {(r["rule_name"], r["metric_name"]) for r in result["rule_results"]}
        for spec in COMPARISON_METRIC_SPECS:
            with self.subTest(metric=spec.metric_name):
                self.assertIn(
                    (spec.rule_name, spec.metric_name),
                    gate_rules,
                    f"{spec.metric_name} 不是 gate 實際評估的 rule",
                )

    def test_every_spec_points_at_a_real_policy_threshold(self) -> None:
        policy = GatePolicy()
        for spec in COMPARISON_METRIC_SPECS:
            with self.subTest(metric=spec.metric_name):
                self.assertIsInstance(threshold_rule_for(spec, policy), ThresholdRule)


class TestThresholdsComeFromTheGatePolicy(unittest.TestCase):
    """threshold 的唯一來源是 GatePolicy；contract 必須讓它可被追溯。"""

    def test_threshold_source_and_policy_version_are_recorded(self) -> None:
        policy = GatePolicy(policy_version="7.7")
        evals = build_comparison_metric_evaluations(
            {"crash_rate_change_pct": 0.0, "fatal_rate_change_pct": 0.0,
             "anr_rate_change_pct": 0.0, "crash_free_users_diff": 0.0},
            policy,
        )
        self.assertEqual(len(evals), len(COMPARISON_METRIC_SPECS))
        for ev in evals:
            with self.subTest(metric=ev["metric_name"]):
                self.assertEqual(ev["threshold_source"], COMPARISON_THRESHOLD_SOURCE)
                self.assertEqual(ev["policy_version"], "7.7")

    def test_custom_policy_thresholds_reach_the_contract_verbatim(self) -> None:
        """反向證明門檻不是常數：換一組 policy，contract 裡的數字必須跟著換。"""
        policy = GatePolicy(
            crash_rate_change_pct=ThresholdRule(warn=0.42, fail=0.77),
            crash_free_users_drop=ThresholdRule(warn=0.031, fail=0.062),
        )
        by_metric = {
            ev["metric_name"]: ev
            for ev in build_comparison_metric_evaluations(
                {"crash_rate_change_pct": 0.5, "crash_free_users_diff": -0.04}, policy
            )
        }
        crash = by_metric["crash_rate_change_pct"]
        self.assertEqual((crash["warn_threshold"], crash["fail_threshold"]), (0.42, 0.77))
        self.assertIn("42.00%", crash["threshold_display"])
        self.assertIn("77.00%", crash["threshold_display"])
        # 0.5 落在 warn(0.42) 與 fail(0.77) 之間 —— 用預設 policy 會是 fail。
        self.assertEqual(crash["classification"], "warn")

        cfu = by_metric["crash_free_users_diff"]
        # 下降型指標的 threshold 與 change 同方向（負值），與 gate rule 慣例一致。
        self.assertEqual((cfu["warn_threshold"], cfu["fail_threshold"]), (-0.031, -0.062))
        self.assertEqual(cfu["classification"], "warn")

    def test_missing_change_is_skip_never_pass(self) -> None:
        """沒有資料不是正常。若這裡回 pass，首屏會把沒觀測到的指標畫成綠燈。"""
        evals = build_comparison_metric_evaluations({}, GatePolicy())
        for ev in evals:
            with self.subTest(metric=ev["metric_name"]):
                self.assertIsNone(ev["change"])
                self.assertEqual(ev["classification"], "skip")

    def test_zero_baseline_is_fail_even_when_the_ratio_looks_mild(self) -> None:
        """零基準退化在 gate 是直接 fail；比例本身在數學上沒有意義（分母為 0）。"""
        evals = {
            ev["metric_name"]: ev
            for ev in build_comparison_metric_evaluations(
                {"anr_rate_change_pct": 0.0, "zero_baseline_anr": True}, GatePolicy()
            )
        }
        anr = evals["anr_rate_change_pct"]
        self.assertTrue(anr["zero_baseline"])
        self.assertEqual(anr["classification"], "fail")


class TestClassificationAgreesWithTheGateVerdict(unittest.TestCase):
    """本檔的核心斷言：同一個指標、同一組 threshold，兩邊的判定必須逐一相等。

    覆蓋面刻意包含每一種 status，並在最後反向斷言「四種 status 都真的出現過」——
    否則一組全部落在 pass 的案例也會讓這個測試通過而毫無鑑別力。
    """

    #: (case 名稱, vs_previous 片段)
    CASES: tuple[tuple[str, dict[str, Any]], ...] = (
        ("all_normal", {
            "crash_rate_change_pct": 0.01, "fatal_rate_change_pct": -0.2,
            "anr_rate_change_pct": 0.0, "crash_free_users_diff": 0.001,
        }),
        ("crash_warn", {
            "crash_rate_change_pct": 0.15, "fatal_rate_change_pct": 0.0,
            "anr_rate_change_pct": 0.0, "crash_free_users_diff": 0.0,
        }),
        ("crash_fail_anr_warn", {
            "crash_rate_change_pct": 0.9, "fatal_rate_change_pct": 0.0,
            "anr_rate_change_pct": 0.12, "crash_free_users_diff": 0.0,
        }),
        ("cfu_drop_fail", {
            "crash_rate_change_pct": 0.0, "fatal_rate_change_pct": 0.0,
            "anr_rate_change_pct": 0.0, "crash_free_users_diff": -0.05,
        }),
        ("cfu_drop_warn", {
            "crash_rate_change_pct": 0.0, "fatal_rate_change_pct": 0.0,
            "anr_rate_change_pct": 0.0, "crash_free_users_diff": -0.008,
        }),
        ("zero_baseline_fatal", {
            "crash_rate_change_pct": 0.0, "fatal_rate_change_pct": 1.0,
            "anr_rate_change_pct": 0.0, "crash_free_users_diff": 0.0,
            "zero_baseline_fatal": True,
        }),
        ("no_metric_data", {}),
        ("exactly_on_the_warn_boundary", {
            "crash_rate_change_pct": 0.10, "fatal_rate_change_pct": 0.0,
            "anr_rate_change_pct": 0.0, "crash_free_users_diff": 0.0,
        }),
        ("exactly_on_the_fail_boundary", {
            "crash_rate_change_pct": 0.25, "fatal_rate_change_pct": 0.0,
            "anr_rate_change_pct": 0.0, "crash_free_users_diff": 0.0,
        }),
    )

    #: 預設 policy 之外再跑一組自訂 policy，證明一致性不是預設值的巧合。
    POLICIES: tuple[tuple[str, GatePolicy], ...] = (
        ("default", GatePolicy()),
        ("custom", GatePolicy(
            policy_version="9.9",
            crash_rate_change_pct=ThresholdRule(warn=0.02, fail=0.04),
            fatal_rate_change_pct=ThresholdRule(warn=0.5, fail=0.9),
            anr_rate_change_pct=ThresholdRule(warn=0.01, fail=0.11),
            crash_free_users_drop=ThresholdRule(warn=0.0005, fail=0.0009),
        )),
    )

    def _compare(self, policy: GatePolicy, vs_previous: dict[str, Any]) -> list[tuple[str, str, str]]:
        """回傳 (metric_name, gate rule status, comparison classification)。"""
        item = make_release_item(vs_previous)
        gate = evaluate_release(item, policy)
        gate_status_by_metric = {r["metric_name"]: r["status"] for r in gate["rule_results"]}
        evals = build_comparison_metric_evaluations(item["vs_previous"], policy)
        out = []
        for ev in evals:
            gate_status = gate_status_by_metric.get(ev["metric_name"])
            if gate_status is None:
                continue
            out.append((ev["metric_name"], gate_status, ev["classification"]))
        return out

    def test_every_metric_classification_equals_the_gate_rule_status(self) -> None:
        seen: set[str] = set()
        checked = 0
        for policy_name, policy in self.POLICIES:
            for case_name, vs_previous in self.CASES:
                for metric, gate_status, classification in self._compare(policy, vs_previous):
                    with self.subTest(policy=policy_name, case=case_name, metric=metric):
                        self.assertEqual(
                            classification,
                            gate_status,
                            f"{metric} 在 {case_name} / {policy_name} 下 comparison 與 gate 分歧："
                            f"gate={gate_status} comparison={classification}",
                        )
                    seen.add(gate_status)
                    checked += 1
        self.assertGreater(checked, 0, "沒有任何指標被比對，這個測試沒有鑑別力")
        # 鑑別力反向斷言：四種 status 都必須真的出現過。
        self.assertEqual(seen, {"pass", "warn", "fail", "skip"}, f"覆蓋面不足：{sorted(seen)}")

    def test_gate_pass_never_coexists_with_an_abnormal_comparison(self) -> None:
        """對齊的直接後果：gate 判某指標 pass 時，comparison 不得說它 warn / fail。"""
        for policy_name, policy in self.POLICIES:
            for case_name, vs_previous in self.CASES:
                for metric, gate_status, classification in self._compare(policy, vs_previous):
                    if gate_status != "pass":
                        continue
                    with self.subTest(policy=policy_name, case=case_name, metric=metric):
                        self.assertNotIn(classification, ("warn", "fail"))

    def test_fixture_classifications_match_its_own_gate_rule_results(self) -> None:
        """真實 fixture 上的同一條斷言：資料本身不得內部矛盾。"""
        bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
        checked = 0
        for app_id, app in bundle.get("apps", {}).items():
            for item in app.get("release_catalog") or []:
                vp = item.get("vs_previous")
                gate = item.get("release_gate")
                if not isinstance(vp, dict) or not isinstance(gate, dict):
                    continue
                gate_status_by_metric = {
                    r["metric_name"]: r["status"] for r in (gate.get("rule_results") or [])
                }
                for ev in vp.get("metric_evaluations") or []:
                    gate_status = gate_status_by_metric.get(ev["metric_name"])
                    if gate_status is None:
                        continue
                    with self.subTest(app=app_id, version=item["version"], metric=ev["metric_name"]):
                        self.assertEqual(ev["classification"], gate_status)
                    checked += 1
        self.assertGreater(checked, 0, "fixture 中沒有可比對的 metric，覆蓋面已消失")


class TestValidationRejectsAContradictoryClassification(unittest.TestCase):
    """classification 的語意保證必須在 **artifact 邊界**成立（#95 review）。

    產生端與 gate 共用判定原語，只保證「我們產出的 bundle」自我一致；但契約的消費端
    看到的是一份檔案，而 Dashboard 刻意只信任 `classification`、不在前端重算。因此
    一筆型別完全合法、語意互相矛盾的 evaluation（`change=0.90` 卻宣稱 `pass`）若能
    通過 validation，就會被畫成綠色「正常」——失敗模式與 #72 修過的
    `gate_status=fail` + `decision.status=pass` 同一類。這個類別把那道邊界釘住。
    """

    def _errors(self, evaluations: list[dict[str, Any]]) -> list[str]:
        errors: list[str] = []
        validate_release_catalog([make_catalog_item_for_validation(evaluations)], errors)
        return errors

    def test_the_validation_harness_itself_reports_nothing_on_valid_data(self) -> None:
        """前提測試：producer 的輸出在這個 harness 上零錯誤。

        沒有這條，下面每一條 negative 斷言都可能只是抓到 harness 自己的雜訊。
        """
        evals = build_comparison_metric_evaluations(
            {
                "crash_rate_change_pct": 0.15,
                "fatal_rate_change_pct": 0.0,
                "anr_rate_change_pct": None,
                "crash_free_users_diff": -0.05,
            },
            GatePolicy(),
        )
        self.assertEqual(self._errors([dict(ev) for ev in evals]), [])

    def test_a_type_valid_but_contradictory_classification_is_rejected(self) -> None:
        """review 提出的那筆 payload：欄位型別全合法，判定卻與自己的門檻矛盾。"""
        errors = self._errors([
            make_evaluation(
                "anr_rate_change_pct",
                change=0.90,
                classification="pass",
                reason="ANR 率變動 +90.00% 於正常範圍",
            )
        ])
        self.assertTrue(errors, "change=0.90 搭 fail_threshold=0.25 卻宣稱 pass 必須被拒絕")
        self.assertTrue(
            any("classification 'pass' contradicts" in e for e in errors),
            f"錯誤訊息未指出 classification 矛盾：{errors}",
        )

    def test_a_present_change_cannot_claim_skip(self) -> None:
        """`skip` 的語意是「沒有比較資料」，不是「不想判定」。"""
        errors = self._errors([
            make_evaluation("crash_rate_change_pct", change=0.5, classification="skip")
        ])
        self.assertTrue(any("skip" in e and "contradicts" in e for e in errors), errors)

    def test_a_null_change_must_be_skip(self) -> None:
        errors = self._errors([
            make_evaluation("crash_rate_change_pct", change=None, classification="pass")
        ])
        self.assertTrue(
            any("must be 'skip' when change is null" in e for e in errors), errors
        )

    def test_zero_baseline_must_be_fail(self) -> None:
        """零基準退化在 gate 是直接 fail；bundle 不得把它降級成 warn。"""
        errors = self._errors([
            make_evaluation(
                "anr_rate_change_pct", change=1.0, zero_baseline=True, classification="warn"
            )
        ])
        self.assertTrue(any("implies 'fail'" in e for e in errors), errors)

    def test_zero_baseline_is_rejected_for_a_metric_that_has_no_such_concept(self) -> None:
        """無崩潰用戶率沒有「前版基準 0 事件」這回事（spec 的 zero_baseline_field 為 None）。"""
        errors = self._errors([
            make_evaluation(
                "crash_free_users_diff",
                change=-0.05,
                zero_baseline=True,
                classification="fail",
            )
        ])
        self.assertTrue(
            any("zero_baseline must be false" in e for e in errors),
            f"crash_free_users_diff 不該能宣稱零基準退化：{errors}",
        )

    def test_a_decrease_metric_contradiction_is_rejected(self) -> None:
        """下降型指標的門檻存負值，方向搞錯就會把 -5% 的下降說成正常。"""
        errors = self._errors([
            make_evaluation(
                "crash_free_users_diff", change=-0.05, classification="pass"
            )
        ])
        self.assertTrue(any("implies 'fail'" in e for e in errors), errors)

    def test_a_decrease_metric_that_is_genuinely_consistent_is_accepted(self) -> None:
        """正向對照：同一個下降型指標判對了就必須放行，validator 不是一律拒絕。"""
        self.assertEqual(
            self._errors([
                make_evaluation(
                    "crash_free_users_diff", change=-0.05, classification="fail"
                )
            ]),
            [],
        )
        self.assertEqual(
            self._errors([
                make_evaluation(
                    "crash_free_users_diff", change=-0.001, classification="pass"
                )
            ]),
            [],
        )

    def test_rule_name_must_match_the_canonical_metric_binding(self) -> None:
        errors = self._errors([
            make_evaluation("anr_rate_change_pct", rule_name="crash_rate_regression")
        ])
        self.assertTrue(
            any("rule_name must be 'anr_rate_regression'" in e for e in errors), errors
        )

    def test_direction_must_match_the_canonical_metric_binding(self) -> None:
        errors = self._errors([
            make_evaluation("anr_rate_change_pct", direction="decrease")
        ])
        self.assertTrue(
            any("direction must be 'increase'" in e for e in errors), errors
        )

    def test_an_unknown_metric_cannot_claim_the_gate_policy_as_its_source(self) -> None:
        """否則任何自創指標都能借用 `release_gate_policy` 的權威。"""
        errors = self._errors([
            make_evaluation("anr_rate_change_pct", metric_name="made_up_metric")
        ])
        self.assertTrue(
            any("canonical comparison metrics" in e for e in errors), errors
        )

    def test_a_contradiction_inside_a_real_bundle_is_rejected(self) -> None:
        """同一條保證要在 `validate_dashboard_v2()` 這一層也成立。"""
        bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(validate_dashboard_v2(bundle), [], "fixture 前提：未被破壞前是乾淨的")

        corrupted = copy.deepcopy(bundle)
        patched = False
        for app in corrupted.get("apps", {}).values():
            for item in app.get("release_catalog") or []:
                vp = item.get("vs_previous")
                if not isinstance(vp, dict):
                    continue
                for ev in vp.get("metric_evaluations") or []:
                    if ev["classification"] in ("warn", "fail"):
                        ev["classification"] = "pass"
                        patched = True
                        break
                if patched:
                    break
            if patched:
                break
        self.assertTrue(patched, "fixture 裡沒有 warn/fail 指標，這個測試已失去覆蓋面")

        errors = validate_dashboard_v2(corrupted)
        self.assertTrue(errors, "被降級成 pass 的指標必須被 validate_dashboard_v2 拒絕")
        self.assertTrue(any("metric_evaluations" in e for e in errors), errors)

    def test_every_producer_output_passes_the_semantic_validator(self) -> None:
        """反向把關：validator 不得拒絕產生端的合法輸出。

        共用 `TestClassificationAgreesWithTheGateVerdict` 的案例與 policy 組合
        （含自訂門檻、邊界值、零基準、完全無資料），因此一旦 validator 的反推與
        producer 的判定漂移成兩套規則，這裡就會紅——它是「不要長出第二套判定引擎」
        這件事的哨兵。
        """
        agreement = TestClassificationAgreesWithTheGateVerdict
        checked = 0
        for policy_name, policy in agreement.POLICIES:
            for case_name, vs_previous in agreement.CASES:
                item = make_release_item(vs_previous)
                evals = build_comparison_metric_evaluations(item["vs_previous"], policy)
                with self.subTest(policy=policy_name, case=case_name):
                    self.assertEqual(self._errors([dict(ev) for ev in evals]), [])
                checked += 1
        self.assertGreater(checked, 0, "沒有任何案例被驗證，這個測試沒有鑑別力")


class TestNoThresholdNumberIsHardcodedInTheEmittedJs(unittest.TestCase):
    """機械檢查：比較面 JS 不得含任何門檻數值，也不得做任何門檻比較。

    掃描的是**實際會被送到瀏覽器的那段字串**，而不是原始檔案，因此連
    「用 f-string 把 policy 值插進 JS」這種做法也會被抓到——那同樣是把門檻凍結在
    build 期，policy 改了畫面不會跟著改。
    """

    def setUp(self) -> None:
        self.js = get_release_comparison_js()

    @staticmethod
    def _forbidden_tokens() -> set[str]:
        """本單分類的四個指標，其 policy 門檻的各種可能字面寫法。"""
        policy = GatePolicy()
        tokens: set[str] = set()
        for spec in COMPARISON_METRIC_SPECS:
            rule = threshold_rule_for(spec, policy)
            for value in (rule.warn, rule.fail):
                for cand in (float(value), float(value) * 100):
                    tokens.add(f"{cand:g}")
                    tokens.add(f"{cand:.1f}")
                    tokens.add(f"{cand:.2f}")
        return tokens

    def test_the_forbidden_token_set_is_not_empty(self) -> None:
        """前提：若門檻集合是空的，下面那條斷言會空跑而永遠通過。"""
        self.assertTrue(self._forbidden_tokens())

    def test_no_policy_threshold_value_appears_as_a_literal(self) -> None:
        literals = set(re.findall(r"(?<![\w.$])-?\d+(?:\.\d+)?", self.js))
        forbidden = self._forbidden_tokens()
        hits = sorted(lit for lit in literals if lit.lstrip("-") in forbidden)
        self.assertEqual(
            hits,
            [],
            f"比較面 JS 出現門檻數值字面值 {hits}；門檻必須來自 contract 欄位",
        )

    def test_no_numeric_comparison_against_a_non_zero_literal(self) -> None:
        """比較面不做任何門檻比較。允許與 0 比（那是正負號排版，不是門檻）。"""
        compared = re.findall(r"(?:>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)", self.js)
        non_zero = sorted({c for c in compared if float(c) != 0.0})
        self.assertEqual(
            non_zero,
            [],
            f"比較面 JS 對數字字面值做了比較 {non_zero}；判定必須讀 classification 欄位",
        )

    def test_rendering_reads_the_contract_fields_instead(self) -> None:
        for field in ("classification", "threshold_display", "threshold_source", "policy_version", "reason"):
            with self.subTest(field=field):
                self.assertIn(field, self.js, f"比較面必須讀 contract 的 {field} 欄位")

    def test_comparison_js_does_not_name_a_policy_threshold_field(self) -> None:
        """前端不得直接引用 policy 的門檻欄位名——那是「自己去查門檻」的第一步。"""
        for spec in COMPARISON_METRIC_SPECS:
            with self.subTest(field=spec.policy_field):
                self.assertNotIn(f"policy.{spec.policy_field}", self.js)


class TestComparisonSurfaceIsWired(unittest.TestCase):
    """容器、renderer 掛載點與 CSS 都必須真的存在，不靠字串巧合維繫。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.overview_html = overview.get_overview_html()
        cls.overview_js = overview.get_overview_js()

    def test_overview_html_carries_the_comparison_container(self) -> None:
        self.assertIn(f'id="{overview.COMPARISON_GRID_ID}"', self.overview_html)
        self.assertIn(f'id="{overview.COMPARISON_SUBTITLE_ID}"', self.overview_html)

    def test_comparison_surface_follows_the_decision_surface(self) -> None:
        """順序是語意的一部分：先「能不能發」，再「哪裡變差」。"""
        decision_at = self.overview_html.index(f'id="{overview.DECISION_GRID_ID}"')
        comparison_at = self.overview_html.index(f'id="{overview.COMPARISON_GRID_ID}"')
        kpi_at = self.overview_html.index('id="kpiCFUsers"')
        self.assertLess(decision_at, comparison_at)
        self.assertLess(comparison_at, kpi_at)

    def test_render_all_invokes_the_comparison_renderer(self) -> None:
        self.assertIn("renderReleaseComparison();", get_shell_js_bottom())

    def test_route_apply_refreshes_the_comparison_surface(self) -> None:
        """deep link 指定版本時，比較面必須跟著換到那一版。"""
        self.assertIn("renderReleaseComparison", nav.get_navigation_js())

    def test_every_classification_tone_class_is_styled(self) -> None:
        css = get_dashboard_styles()
        for tone in ("good", "warn", "bad", "neutral"):
            with self.subTest(tone=tone):
                self.assertIn(f"{overview.COMPARISON_CLASS_PREFIX}{tone}", css)


class OverviewClientRuntime(unittest.TestCase):
    """在 Node 內執行真正產出的 client JS，讀回渲染結果。

    與 #73 一樣重用 ``tests.test_dashboard_deep_link.NODE_RUNNER``：只有真的跑一遍
    才抓得到「欄位缺失讓 render 丟例外」這類問題，Python 端的字串斷言看不到。
    """

    node_bin: str | None

    @classmethod
    def setUpClass(cls) -> None:
        cls.node_bin = shutil.which("node")

    def render(
        self,
        bundle: dict[str, Any],
        steps: str,
        initial_hash: str = "",
    ) -> dict[str, Any]:
        if not self.node_bin:
            self.skipTest("Node.js runtime is not available in environment")
        html = build_html(bundle)
        scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        self.assertGreaterEqual(len(scripts), 2, "HTML must contain at least 2 <script> tags")
        dom_ids = sorted(set(re.findall(r'id="([A-Za-z0-9_-]+)"', html)))

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


#: 逐一 dump 每個 release 的比較卡，以及首屏 grid 的實際內容。
STEPS_DUMP_COMPARISON = r"""
  const APPS = routeAppsData();
  report.steps.cards = {};
  Object.keys(APPS).forEach(aid => {
    (APPS[aid].release_catalog || []).forEach(r => {
      report.steps.cards[aid + '|' + r.platform + '|' + r.version] =
        buildReleaseComparisonCardHtml(r.platform, r.platform, r, {});
    });
  });
  report.steps.cards['__NO_RELEASE__'] = buildReleaseComparisonCardHtml('ios', 'iOS', null, {});
  report.steps.grid = elements['overviewComparisonGrid'].innerHTML;
  report.steps.subtitle = elements['overviewComparisonSubtitle'].textContent;
"""


def classifications(card: str) -> list[tuple[str, str]]:
    """回傳卡片內 (metric_name, classification)，順序即呈現順序。"""
    rows = re.findall(
        r'data-comparison-metric="([^"]+)"(.*?)</div>\s*</div>',
        card,
        re.DOTALL,
    )
    out: list[tuple[str, str]] = []
    for metric, body in rows:
        m = re.search(r'data-comparison-classification="([^"]+)"', body)
        out.append((metric, m.group(1) if m else ""))
    return out


def tones(card: str) -> list[str]:
    return re.findall(r"comparison-class-(\w+)", card)


class TestComparisonRendersWhatTheContractSays(OverviewClientRuntime):
    """呈現面只反映契約，不自行判定。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def setUp(self) -> None:
        self.steps = self.render(self.bundle, STEPS_DUMP_COMPARISON)

    def _card(self, app_id: str, platform: str, version: str) -> str:
        return self.steps["cards"][f"{app_id}|{platform}|{version}"]

    def _fixture_evals(self, app_id: str, platform: str, version: str) -> list[dict[str, Any]]:
        for item in self.bundle["apps"][app_id]["release_catalog"]:
            if item["platform"] == platform and item["version"] == version:
                return list((item.get("vs_previous") or {}).get("metric_evaluations") or [])
        raise AssertionError(f"fixture 缺少 {app_id} {platform} {version}")

    def test_all_four_required_metrics_are_rendered(self) -> None:
        card = self._card("shop_app", "android", "3.2.0")
        rendered = [metric for metric, _ in classifications(card)]
        for metric in REQUIRED_METRICS:
            with self.subTest(metric=metric):
                self.assertIn(metric, rendered)

    def test_rendered_classification_equals_the_contract_field(self) -> None:
        """畫面上的判定必須逐一等於契約欄位，不是前端重算的結果。"""
        checked = 0
        for app_id, app in self.bundle["apps"].items():
            for item in app.get("release_catalog") or []:
                evals = (item.get("vs_previous") or {}).get("metric_evaluations")
                if not evals:
                    continue
                card = self._card(app_id, item["platform"], item["version"])
                self.assertEqual(
                    classifications(card),
                    [(ev["metric_name"], ev["classification"]) for ev in evals],
                    f"{app_id} {item['platform']} {item['version']} 呈現與契約不符",
                )
                checked += 1
        self.assertGreater(checked, 0)

    def test_threshold_text_is_printed_verbatim_from_the_contract(self) -> None:
        card = self._card("shop_app", "android", "3.2.0")
        for ev in self._fixture_evals("shop_app", "android", "3.2.0"):
            with self.subTest(metric=ev["metric_name"]):
                self.assertIn(ev["threshold_display"], card)
                self.assertIn(ev["threshold_source"], card)

    def test_fail_metric_is_not_rendered_in_the_pass_tone(self) -> None:
        card = self._card("shop_app", "android", "3.2.0")
        by_metric = dict(classifications(card))
        self.assertEqual(by_metric["anr_rate_change_pct"], "fail")
        self.assertIn("comparison-class-bad", card)

    def test_zero_baseline_is_surfaced_as_such(self) -> None:
        card = self._card("shop_app", "android", "3.2.0")
        self.assertIn("零基準退化", card)

    def test_both_platforms_appear_in_the_grid(self) -> None:
        grid = self.steps["grid"]
        self.assertIn('data-comparison-platform="android"', grid)
        self.assertIn('data-comparison-platform="ios"', grid)

    def test_subtitle_says_the_cards_are_the_latest_versions(self) -> None:
        self.assertIn("最新版本", self.steps["subtitle"])


class TestComparisonNeverBecomesASecondReleaseVerdict(OverviewClientRuntime):
    """比較面是逐項指標判定，不是發布結論。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def setUp(self) -> None:
        self.steps = self.render(self.bundle, STEPS_DUMP_COMPARISON)

    def _card(self, app_id: str, platform: str, version: str) -> str:
        return self.steps["cards"][f"{app_id}|{platform}|{version}"]

    def test_gate_evaluated_release_points_at_the_decision_surface(self) -> None:
        card = self._card("shop_app", "android", "3.2.0")
        self.assertIn("發布建議請看上方發布決策", card)

    def test_unevaluated_release_says_it_is_not_a_release_verdict(self) -> None:
        """rider 1.8.0 的 gate policy 未啟用（沒有 decision），但它的 ANR 是零基準退化。

        這正是最容易被誤讀成矛盾的組合：畫面必須說清楚「閘門沒判定」，
        而不是讓讀者把 ANR 失敗讀成 gate 的結論。
        """
        card = self._card("rider_app", "android", "1.8.0")
        self.assertIn("品質閘門未對此版本做出判定", card)
        self.assertIn("不構成發布判定", card)

    def test_no_card_emits_a_release_recommendation_or_action(self) -> None:
        """比較面不得長出第二套建議文案／建議行動。"""
        for key, card in self.steps["cards"].items():
            with self.subTest(card=key):
                self.assertNotIn("decision-recommendation", card)
                self.assertNotIn("建議行動", card)


class TestBackwardCompatibilityWithoutClassification(OverviewClientRuntime):
    """舊 bundle 沒有 metric_evaluations 必須仍能 validate / render。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))

    @staticmethod
    def _strip_metric_evaluations(bundle: dict[str, Any]) -> dict[str, Any]:
        stripped = copy.deepcopy(bundle)
        for app in stripped["apps"].values():
            for item in app.get("release_catalog") or []:
                vp = item.get("vs_previous")
                if isinstance(vp, dict):
                    vp.pop("metric_evaluations", None)
        return stripped

    def test_legacy_fixtures_still_have_no_release_catalog(self) -> None:
        """向後相容基準：這兩個 fixture 不得被補上 release_catalog（#90）。"""
        legacy = json.loads(LEGACY_FIXTURE.read_text(encoding="utf-8"))
        for app_id, app in legacy["apps"].items():
            with self.subTest(app=app_id):
                self.assertNotIn("release_catalog", app)

    def test_bundle_without_classification_validates(self) -> None:
        self.assertEqual(validate_dashboard_v2(self._strip_metric_evaluations(self.bundle)), [])

    def test_legacy_bundle_without_release_catalog_validates_and_renders(self) -> None:
        legacy = json.loads(LEGACY_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(validate_dashboard_v2(legacy), [])
        html = build_html(legacy)
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn(f'id="{overview.COMPARISON_GRID_ID}"', html)

    def test_release_without_classification_renders_no_classification_at_all(self) -> None:
        """缺欄位時不得拿變化量自行套門檻補一個判定。"""
        steps = self.render(self._strip_metric_evaluations(self.bundle), STEPS_DUMP_COMPARISON)
        card = steps["cards"]["shop_app|android|3.2.0"]
        self.assertEqual(classifications(card), [])
        self.assertEqual(tones(card), [])
        self.assertIn("不呈現正常／異常判定", card)
        # 版本對照本身仍必須看得到，否則等於整個區塊消失。
        self.assertIn("3.2.0", card)
        self.assertIn("3.1.2", card)

    def test_legacy_bundle_renders_a_neutral_comparison_surface(self) -> None:
        legacy = json.loads(LEGACY_FIXTURE.read_text(encoding="utf-8"))
        steps = self.render(legacy, STEPS_DUMP_COMPARISON)
        self.assertIn("release_catalog", steps["grid"])
        self.assertEqual(steps["subtitle"], "無發佈版本資料")

    def test_platform_without_a_release_renders_no_classification(self) -> None:
        steps = self.render(self.bundle, STEPS_DUMP_COMPARISON)
        card = steps["cards"]["__NO_RELEASE__"]
        self.assertEqual(classifications(card), [])
        self.assertIn("沒有可比較的前版", card)

    def test_baseline_release_says_it_has_no_previous_version(self) -> None:
        """該平台首個版本沒有前版基準；不得畫成「全部正常」。"""
        bundle = copy.deepcopy(self.bundle)
        for item in bundle["apps"]["shop_app"]["release_catalog"]:
            if item["platform"] == "android" and item["version"] == "3.2.0":
                item["vs_previous"] = None
        steps = self.render(bundle, STEPS_DUMP_COMPARISON)
        card = steps["cards"]["shop_app|android|3.2.0"]
        self.assertEqual(classifications(card), [])
        self.assertIn("沒有前版基準", card)


class TestThresholdsFlowFromDataIntoTheDom(OverviewClientRuntime):
    """端到端反向證明：改 policy → 契約改 → 畫面改。前端沒有第二份門檻。"""

    CUSTOM_POLICY = GatePolicy(
        policy_version="42.0",
        crash_rate_change_pct=ThresholdRule(warn=0.9, fail=0.95),
        fatal_rate_change_pct=ThresholdRule(warn=0.9, fail=0.95),
        anr_rate_change_pct=ThresholdRule(warn=0.9, fail=0.95),
        crash_free_users_drop=ThresholdRule(warn=0.9, fail=0.95),
    )

    def test_a_looser_policy_flips_the_rendered_classification(self) -> None:
        bundle = json.loads(FIXTURE.read_text(encoding="utf-8"))
        app = bundle["apps"]["shop_app"]
        by_key = {(i["platform"], i["version"]): i for i in app["release_catalog"]}
        target = by_key[("android", "3.2.0")]
        previous = by_key[("android", target["vs_previous"]["previous_version"])]

        # 用同一支 production 函式重算，只換 policy——不手改任何數字。
        target["vs_previous"] = dict(
            compute_previous_release_comparison(
                v_curr_info={"recent_health": target["recent_health"]},
                v_prev_info={"recent_health": previous["recent_health"]},
                v_prev=previous["version"],
                recent_health=target["recent_health"],
                introduced_count=0,
                prev_introduced_count=0,
                target_window="30",
                policy=self.CUSTOM_POLICY,
            )
        )
        steps = self.render(bundle, STEPS_DUMP_COMPARISON)
        card = steps["cards"]["shop_app|android|3.2.0"]
        by_metric = dict(classifications(card))

        # 崩潰率 +37.57% 在預設 policy 是 fail；門檻放寬到 +90% 之後只是 pass。
        self.assertEqual(by_metric["crash_rate_change_pct"], "pass")
        self.assertIn("90.00%", card)
        self.assertIn("95.00%", card)
        self.assertIn("42.0", card)
        # 零基準退化與門檻無關，必須仍然是 fail。
        self.assertEqual(by_metric["anr_rate_change_pct"], "fail")


if __name__ == "__main__":
    unittest.main()
