"""Gate threshold 判定原語與 comparison metric 規格表 (Issue #74)。

本模組存在的唯一理由是**避免第二套判定引擎**。

V3.4 要在 Dashboard 首屏呈現「與上一版相比哪些指標變差」，而那個
`normal / warn / fail` 的判定必須與 Release Regression Gate 完全一致——否則會出現
「Gate 判 PASS，但 comparison 說 ANR 異常」這種兩邊都自稱權威的矛盾。做法不是在
comparison 端再寫一次 `if value >= threshold`，而是把**判定本身**抽成一個原語
(`classify_threshold_breach`)，由 `gate/evaluator.py` 與
`catalog/comparison.py` 共用：

* 同一個 `ThresholdRule`
* 同一個 `zero_baseline` 升級規則（前版基準為 0 事件 → 直接 fail）
* 同一組 `metric -> policy 欄位` 對應（`COMPARISON_METRIC_SPECS`）

刻意**不**共用的是 reason 文案：判定分歧會導致誤判，文案分歧不會，而 evaluator
的文案已被既有測試逐字鎖定，硬要統一只會擴大改動面（見 #74 PR 說明）。

threshold 一律來自 `crash_trend.gate.policy.GatePolicy`（`load_gate_policy` 的產物），
本模組不定義任何 threshold 數值，也不提供第二個設定面。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from crash_trend.gate.policy import GatePolicy, ThresholdRule
from crash_trend.schema_v2 import (
    COMPARISON_THRESHOLD_SOURCE,
    ComparisonMetricEvaluation,
)

#: 與 `gate.artifact.RuleStatus` 同一套詞彙。刻意不自創 `normal` / `abnormal`：
#: 一旦有第二套詞彙就必須有一張映射表，而那張表就是第二套判定的入口。
MetricClassification = Literal["pass", "warn", "fail", "skip"]


def classify_threshold_breach(
    value: float,
    rule: ThresholdRule,
    zero_baseline: bool = False,
) -> MetricClassification:
    """把「已朝著『越大越差』方向正規化的變化量」對 threshold 判定成 gate status。

    `value` 必須已經轉成「正值代表退化」：升高型指標（crash / fatal / ANR 率變化）
    直接傳變化率，下降型指標（無崩潰用戶率）傳 `-diff`（也就是下降幅度）。方向轉換
    由呼叫端負責，因為那屬於指標語意，不屬於 threshold 判定。

    `zero_baseline=True` 代表前版基準為 0 事件、本版出現事件；此時變化率在數學上
    沒有意義（分母為 0），gate 的既有語意是直接判 fail。
    """
    if zero_baseline:
        return "fail"
    if value >= rule.fail:
        return "fail"
    if value >= rule.warn:
        return "warn"
    return "pass"


@dataclass(frozen=True)
class ComparisonMetricSpec:
    """一個 comparison metric 與其 gate rule / policy threshold 的綁定關係。

    這張綁定關係是「Dashboard 與 Gate 對同一個指標套用同一個 threshold」的唯一定義處。
    """

    #: `vs_previous` 中承載變化量的欄位名，同時也是 `RuleEvaluationResult.metric_name`。
    metric_name: str
    #: 對應的 gate rule 名（`RuleEvaluationResult.rule_name`），供追溯。
    rule_name: str
    #: `GatePolicy` 上的 threshold 欄位名。
    policy_field: str
    #: 顯示名（繁體中文），與 evaluator 的 reason 用詞一致。
    label: str
    #: `increase` = 數值越大越差；`decrease` = 數值越小越差（無崩潰用戶率）。
    direction: Literal["increase", "decrease"]
    #: `vs_previous` 中對應的 zero-baseline 旗標欄位；`None` 代表此指標沒有零基準概念。
    zero_baseline_field: str | None


#: Crash / Fatal / ANR / Crash-free（#74 驗收條件要求的四項）。
#: 四者與 evaluator 的 rule 1~4 一對一；rule 5/6（issue 計數）不是 current vs previous
#: 的指標比較，因此不在此表內。
COMPARISON_METRIC_SPECS: tuple[ComparisonMetricSpec, ...] = (
    ComparisonMetricSpec(
        metric_name="crash_rate_change_pct",
        rule_name="crash_rate_regression",
        policy_field="crash_rate_change_pct",
        label="崩潰率",
        direction="increase",
        zero_baseline_field="zero_baseline_crash",
    ),
    ComparisonMetricSpec(
        metric_name="fatal_rate_change_pct",
        rule_name="fatal_rate_regression",
        policy_field="fatal_rate_change_pct",
        label="Fatal 崩潰率",
        direction="increase",
        zero_baseline_field="zero_baseline_fatal",
    ),
    ComparisonMetricSpec(
        metric_name="anr_rate_change_pct",
        rule_name="anr_rate_regression",
        policy_field="anr_rate_change_pct",
        label="ANR 率",
        direction="increase",
        zero_baseline_field="zero_baseline_anr",
    ),
    ComparisonMetricSpec(
        metric_name="crash_free_users_diff",
        rule_name="crash_free_users_drop",
        policy_field="crash_free_users_drop",
        label="無崩潰用戶率",
        direction="decrease",
        zero_baseline_field=None,
    ),
)


#: `metric_name` -> spec。canonical 的 `metric_name -> rule_name -> direction` 配對
#: 只有這一份；contract validation 也讀它，因此「某個指標自稱來自 release_gate_policy
#: 卻不是 gate 評估的指標」在 artifact 邊界就會被拒絕。
_SPECS_BY_METRIC: dict[str, ComparisonMetricSpec] = {
    spec.metric_name: spec for spec in COMPARISON_METRIC_SPECS
}


def spec_for_metric(metric_name: Any) -> ComparisonMetricSpec | None:
    """查 canonical spec；不是 canonical comparison metric 時回 `None`。"""
    if not isinstance(metric_name, str):
        return None
    return _SPECS_BY_METRIC.get(metric_name)


def classification_implied_by_payload(
    direction: Any,
    change: float | int | None,
    warn_threshold: float | int,
    fail_threshold: float | int,
    zero_baseline: bool,
) -> MetricClassification:
    """由一筆 evaluation **自己攜帶的欄位**反推 classification。

    這是給 contract validation 用的：一筆 `ComparisonMetricEvaluation` 同時帶著
    `change` / `direction` / `warn_threshold` / `fail_threshold` / `zero_baseline`
    與 `classification`，因此那個 classification 是可被驗證的，不必信任產生它的人。
    Dashboard 刻意只信任 `classification`、不在前端重算，所以這道驗證必須發生在
    artifact 邊界——否則型別合法但語意矛盾的 bundle 會被畫成綠色「正常」。

    仍舊呼叫同一個 `classify_threshold_breach()`：validator 不長出第二套判定引擎，
    它只是把 payload 的欄位餵回同一個原語。`policy` 不參與——bundle 可能帶著
    app 自訂 policy 的門檻，因此可驗證的是**自我一致性**，而非門檻等於預設值。
    """
    if change is None:
        return "skip"
    sign = 1 if direction == "increase" else -1
    rule = ThresholdRule(warn=sign * warn_threshold, fail=sign * fail_threshold)
    return classify_threshold_breach(sign * change, rule, zero_baseline=zero_baseline)


def threshold_rule_for(spec: ComparisonMetricSpec, policy: GatePolicy) -> ThresholdRule:
    """取出 spec 對應的 policy threshold。找不到欄位就是 spec 表寫錯，直接炸。"""
    rule = getattr(policy, spec.policy_field)
    if not isinstance(rule, ThresholdRule):
        raise TypeError(f"GatePolicy.{spec.policy_field} is not a ThresholdRule")
    return rule


def _oriented(spec: ComparisonMetricSpec, change: float) -> float:
    """把契約上的變化量轉成「正值代表退化」。"""
    return change if spec.direction == "increase" else -change


def _signed_threshold(spec: ComparisonMetricSpec, value: float | int) -> float | int:
    """把 threshold 轉回與 `change` 同一個方向。

    與 evaluator 對 `crash_free_users_drop` 的既有慣例一致（該 rule 的
    `warn_threshold` / `fail_threshold` 也是存負值），因此 consumer 看到的
    threshold 與 `change` 可以直接比大小，不需要自己知道方向。
    """
    return value if spec.direction == "increase" else -value


def format_threshold_display(spec: ComparisonMetricSpec, rule: ThresholdRule) -> str:
    """產生 threshold 的顯示字串。

    threshold 的**格式化也留在 Python**：只要前端需要自己把 0.10 排版成 `+10%`，
    那個 `* 100` 與小數位數就會變成前端對 threshold 語意的第二份假設。前端只負責
    把這個字串印出來。
    """
    warn = _signed_threshold(spec, rule.warn) * 100
    fail = _signed_threshold(spec, rule.fail) * 100
    return f"警告 {warn:+.2f}% / 失敗 {fail:+.2f}%"


def _reason(
    spec: ComparisonMetricSpec,
    classification: MetricClassification,
    change: float | None,
    rule: ThresholdRule,
    zero_baseline: bool,
) -> str:
    if classification == "skip":
        return f"未取得{spec.label}比較資料，無法判定"
    if zero_baseline:
        return f"前版基準{spec.label}為 0 事件，本版出現事件，判定為零基準退化"
    assert change is not None
    pct = change * 100
    if classification == "pass":
        return f"{spec.label}變動 {pct:+.2f}% 於正常範圍（{format_threshold_display(spec, rule)}）"
    threshold = _signed_threshold(spec, rule.fail if classification == "fail" else rule.warn) * 100
    level = "失敗" if classification == "fail" else "警告"
    return f"{spec.label}變動 {pct:+.2f}%，達到{level}門檻 ({threshold:+.2f}%)"


def evaluate_comparison_metric(
    spec: ComparisonMetricSpec,
    vs_previous: dict[str, Any],
    policy: GatePolicy,
) -> ComparisonMetricEvaluation:
    """對單一 metric 產出帶 classification 與 threshold 來源的評估結果。"""
    rule = threshold_rule_for(spec, policy)
    raw = vs_previous.get(spec.metric_name)
    zero_baseline = bool(
        spec.zero_baseline_field is not None and vs_previous.get(spec.zero_baseline_field)
    )

    change: float | None = None if raw is None else float(raw)
    if change is None:
        classification: MetricClassification = "skip"
    else:
        classification = classify_threshold_breach(
            _oriented(spec, change), rule, zero_baseline=zero_baseline
        )

    return {
        "metric_name": spec.metric_name,
        "rule_name": spec.rule_name,
        "label": spec.label,
        "direction": spec.direction,
        "change": change,
        "classification": classification,
        "warn_threshold": _signed_threshold(spec, rule.warn),
        "fail_threshold": _signed_threshold(spec, rule.fail),
        "threshold_display": format_threshold_display(spec, rule),
        "threshold_source": COMPARISON_THRESHOLD_SOURCE,
        "policy_version": policy.policy_version,
        "zero_baseline": zero_baseline,
        "reason": _reason(spec, classification, change, rule, zero_baseline),
    }


def build_comparison_metric_evaluations(
    vs_previous: dict[str, Any],
    policy: GatePolicy,
) -> list[ComparisonMetricEvaluation]:
    """對 `COMPARISON_METRIC_SPECS` 全表產出評估結果（順序即呈現順序）。

    刻意**不**省略資料缺失的指標：省略會讓前端無法區分「這個指標正常」與
    「這個指標沒有資料」，而後者不得被畫成綠燈。缺資料一律是 `skip`。
    """
    return [evaluate_comparison_metric(spec, vs_previous, policy) for spec in COMPARISON_METRIC_SPECS]
