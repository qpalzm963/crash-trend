"""從歷史 release 分佈推薦 Gate 門檻（Issue #76 / 原 #66 項目 5）。

**這個模組推薦門檻，不套用門檻。** 輸出是一份給人看過再寫進 `apps.yaml` 的建議值，
因此執行期的門檻仍然完全來自 `GatePolicy`，`metric_evaluations.threshold_source` 也
只有 `release_gate_policy` 一個合法值（#74 契約不變）。V3（#70）定案不建立動態的統計
baseline，本模組刻意不越過那條線：它不會寫入任何設定檔，也不會被 gate 在評估時呼叫。

## 觀測值從哪來

`release_catalog[].vs_previous` 的四個變化量——**正是 gate rule 1~4 實際評估的那些值**
（evaluator 讀的就是 `vs_previous`）。因此「歷史上這個指標的分佈」與「門檻要比較的量」
是同一個尺度，不需要任何轉換假設。

觀測值的方向換算共用 `metric_rules.oriented_change()`：正負號的處理只能有一份。

**門檻本身不需要換算**：`GatePolicy` 存的門檻已經在「正值代表退化」的 frame 裡
（`crash_free_users_drop: 0.005` 表示「下降 0.5 個百分點」），evaluator 也是直接把它
交給判定原語。只有 `metric_evaluations.warn_threshold`（#74 契約，與 `change` 同方向）
才需要 `signed_threshold()`，而本模組不產生那個欄位。

## 推薦怎麼算

對每個指標取「已朝著『越大越差』正規化」的觀測值，用 **nearest-rank 百分位數**
（`sorted[ceil(p*n)-1]`，完全確定、不插值）：

* `warn` = 第 80 百分位 —— 約當「最差的兩成版本會預警」
* `fail` = 第 95 百分位 —— 約當「最差的一成不到會被阻擋」

真正讓這份建議可被審查的不是那兩個數字，而是**反事實**：同一批歷史版本在「現行門檻」
與「建議門檻」下各會有幾次 warn / fail。那個計算一律呼叫 gate 的同一個判定原語
`classify_threshold_breach()`，因此不存在第二套判定。

## 這份建議是「相對」標準，不是「品質可接受」

百分位門檻回答的是「對這個 App 而言什麼樣的變化算不尋常」，不是「什麼樣的崩潰率是可以
接受的」。直接照抄的後果是 gate 大約永遠會對最差的那 5% 發布喊 fail，無論那 5% 的絕對
品質如何——一個很穩定的 App 會因此被自己的雜訊擋下來，一個長期很糟的 App 則會把糟的
水準正規化成新常態。因此輸出裡最重要的不是建議值，而是反事實；採用與否是人的決定。

## 什麼時候拒絕給建議

寧可不給數字，也不要給一個從三筆資料推出來的門檻：

* 可用觀測值少於 `MIN_OBSERVATIONS`；
* 歷史上從未觀測到退化（所有觀測值 ≤ 0）——此時任何百分位數都 ≤ 0，套上去會讓每一次
  發布都觸發；
* 四捨五入後 `warn <= 0`（多數版本其實在改善，只有少數退化）——負門檻貼進 `apps.yaml`
  會被 `load_gate_policy()` 退回預設值，`warn = 0` 則會讓「完全沒有變化」也判 WARN。
  兩者都會讓反事實與採用後的 gate 不一致，因此不給；
* 四捨五入後 `warn >= fail`（分佈過於集中）——那會讓 warn 這一級永遠不可能出現。

被排除的觀測值（本版樣本不足、零基準、前版樣本不足、缺值）一律計數並回報，不靜默丟棄。

## gate 停用時：明載前提的 hypothetical 建議

`release_gate.enabled: false` 時 `evaluate_release()` 會在第一行就 return，rule 1~4
完全不跑、一個 metric 判定都不產生。因此「現行門檻在歷史上會判幾次 warn / fail」這個
問題在停用狀態下**沒有答案**——直接照算會產生一份與真實 gate 無關的反事實。

這個工具最常被用在「新接一個 App、gate 還沒啟用，門檻該定在哪」，所以停用時不是拒絕
輸出，而是把前提明載出來：`gate_enabled=False` 一路帶到報表與 JSON（報表講明這些數字
是「假設 gate 啟用」的推算），可貼的片段也會一併帶上 `enabled: true`——否則貼進去的
門檻仍然不會被評估，反事實就又不成立。

**計算本身不需要為停用狀態做任何特例**：`enabled` 只決定 gate 要不要跑，不影響它怎麼
判（門檻查表與 `classify_threshold_breach()` 都不讀那個欄位）。因此這裡不建
`enabled=True` 的 clone——那會是一個永遠改不了任何輸出的無效動作。parity 改由測試負責：
測試拿 `replace(policy, enabled=True)` 去跑 `evaluate_release()`，核對這裡算出的次數，
另有一條先釘住「停用時 `rule_results` 真的是空的」這個前提。

## 只收 gate 真的會評估的歷史點

`evaluate_release()` 在跑 rule 1~4 之前有兩道 guard：本版樣本不足、前版樣本不足，任一
成立就回 `insufficient_data`，那些變化量根本不會被判定。因此推薦器也必須排除它們——
否則分佈與反事實會納入 gate 從來不看的數值。本版樣本是否充足一律問
`evaluator.is_sample_sufficient()`（guard 用的同一個判斷），不自己重寫一份。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from crash_trend.gate.evaluator import is_sample_sufficient
from crash_trend.gate.metric_rules import (
    COMPARISON_METRIC_SPECS,
    ComparisonMetricSpec,
    classify_threshold_breach,
    oriented_change,
    threshold_rule_for,
)
from crash_trend.gate.policy import GatePolicy, ThresholdRule

#: 低於此觀測數一律不給建議。四筆資料的第 95 百分位數就是「最大值」，那不是統計。
MIN_OBSERVATIONS = 5

#: warn / fail 對應的百分位數。
WARN_PERCENTILE = 0.80
FAIL_PERCENTILE = 0.95

#: 建議值的小數位數（比例）。四捨五入後才寫得進 `apps.yaml`。
ROUND_DIGITS = 3


@dataclass(frozen=True)
class MetricObservations:
    """單一指標的歷史觀測，以及被排除的筆數。"""

    spec: ComparisonMetricSpec
    #: 已正規化為「正值代表退化」的觀測值，依版本順序。
    values: tuple[float, ...]
    #: 缺該指標變化量的版本數。
    missing: int
    #: 本版樣本不足、gate 會在 sample sufficiency guard 就回 `insufficient_data` 的版本數。
    insufficient_current: int
    #: 零基準退化（變化率在數學上無意義，是 ±1.0 的哨兵值）而被排除的版本數。
    zero_baseline: int
    #: 前版樣本不足、比較本身不可信而被排除的版本數。
    insufficient_previous: int

    @property
    def sample_size(self) -> int:
        return len(self.values)


@dataclass(frozen=True)
class ThresholdRecommendation:
    """單一指標的門檻建議（`recommended` 為 `None` 代表刻意不給建議）。"""

    metric_name: str
    rule_name: str
    policy_field: str
    label: str
    #: 這個 App 的 `release_gate.enabled`。`False` 代表 gate 目前完全不做 rule 1~4 判定，
    #: 因此以下反事實與建議值都是「假設把 gate 啟用」的推算（見模組 docstring）。
    gate_enabled: bool
    observations: MetricObservations
    current: ThresholdRule
    recommended: ThresholdRule | None
    reason: str
    #: oriented 百分位數（正值代表退化），供報表呈現分佈形狀。
    percentiles: dict[str, float]
    #: 反事實：同一批歷史版本在現行／建議門檻下的 warn / fail 次數。
    current_counts: dict[str, int]
    recommended_counts: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_name": self.metric_name,
            "rule_name": self.rule_name,
            "policy_field": self.policy_field,
            "label": self.label,
            "gate_enabled": self.gate_enabled,
            # `gate_enabled` 的直接推論，但 JSON consumer 要的是「這些數字能不能當真」
            # 這個問句的答案，因此明寫出來，不要求對方自己推。
            "hypothetical": not self.gate_enabled,
            "sample_size": self.observations.sample_size,
            "excluded": {
                "missing": self.observations.missing,
                "zero_baseline": self.observations.zero_baseline,
                "insufficient_current_sample": self.observations.insufficient_current,
                "insufficient_previous_sample": self.observations.insufficient_previous,
            },
            "current": {"warn": self.current.warn, "fail": self.current.fail},
            "recommended": (
                {"warn": self.recommended.warn, "fail": self.recommended.fail}
                if self.recommended is not None
                else None
            ),
            "reason": self.reason,
            "percentiles": self.percentiles,
            "counterfactual": {
                "current": self.current_counts,
                "recommended": self.recommended_counts,
            },
        }


def nearest_rank_percentile(values: tuple[float, ...] | list[float], p: float) -> float:
    """Nearest-rank 百分位數：`sorted[ceil(p*n) - 1]`。

    刻意不插值——插值會產生一個從未被觀測到的值，而門檻要能對應到「哪幾次發布會被
    擋下來」。同一份資料永遠得到同一個答案。
    """
    if not values:
        raise ValueError("empty observation set")
    ordered = sorted(values)
    rank = max(1, math.ceil(p * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def collect_observations(
    catalog: list[dict[str, Any]],
    spec: ComparisonMetricSpec,
    policy: GatePolicy,
) -> MetricObservations:
    """從 release catalog 收集單一指標的歷史觀測值。

    只收**gate 真的會拿去跑 rule 1~4 的版本**：沒有前版基準的版本不是觀測值；本版或
    前版樣本不足的版本，`evaluate_release()` 會在 guard 就回 `insufficient_data`，那些
    變化量從來不會被判定，算進分佈只會讓門檻被不可信的比較拉高。零基準的變化率在數學
    上沒有意義，同樣排除。

    `policy` 決定樣本充足與否（`min_sessions` / `min_adoption_rate` /
    `min_version_events`），因此必須是 gate 實際會用的那一份 policy。
    """
    values: list[float] = []
    missing = 0
    zero_baseline = 0
    insufficient_current = 0
    insufficient_previous = 0

    for item in catalog:
        vs_previous = item.get("vs_previous") if isinstance(item, dict) else None
        if not isinstance(vs_previous, dict) or not vs_previous.get("previous_version"):
            continue
        # guard 的順序與 `evaluate_release()` 一致：本版樣本先於前版樣本。
        sample_ok, _sessions, _reason, _window = is_sample_sufficient(
            item, item.get("recent_health") or {}, policy
        )
        if not sample_ok:
            insufficient_current += 1
            continue
        if vs_previous.get("previous_sample_sufficient") is False:
            insufficient_previous += 1
            continue
        if spec.zero_baseline_field and vs_previous.get(spec.zero_baseline_field):
            zero_baseline += 1
            continue
        raw = vs_previous.get(spec.metric_name)
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            missing += 1
            continue
        values.append(oriented_change(spec, float(raw)))

    return MetricObservations(
        spec=spec,
        values=tuple(values),
        missing=missing,
        insufficient_current=insufficient_current,
        zero_baseline=zero_baseline,
        insufficient_previous=insufficient_previous,
    )


def _counts_under(values: tuple[float, ...], rule: ThresholdRule) -> dict[str, int]:
    """同一批觀測值在給定門檻下的判定分佈。

    一律呼叫 gate 的判定原語：這份反事實若用自己寫的比較算出來，它就不再是「gate 會
    怎麼判」的證據。
    """
    counts = {"pass": 0, "warn": 0, "fail": 0}
    for value in values:
        counts[classify_threshold_breach(value, rule)] += 1
    return counts


def recommend_for_metric(
    catalog: list[dict[str, Any]],
    spec: ComparisonMetricSpec,
    policy: GatePolicy,
) -> ThresholdRecommendation:
    """對單一指標產出門檻建議（或明確的不建議原因）。

    `policy.enabled` 為 `False` 時，`evaluate_release()` 會在第一行就 return，rule 1~4
    一個都不跑——那個狀態下「現行門檻會判幾次」沒有答案。算法不因此改變（`enabled` 不
    影響 gate 怎麼判，只影響它跑不跑），但 `gate_enabled=False` 必須帶進結果，讓報表與
    JSON 明載這份反事實的前提是「假設 gate 啟用」。
    """
    obs = collect_observations(catalog, spec, policy)
    # `GatePolicy` 的門檻本來就存在「正值代表退化」的 frame 裡（`crash_free_users_drop`
    # 存 0.005 表示「下降 0.5 個百分點」），evaluator 也是直接把它交給
    # `classify_threshold_breach()`。因此這裡**不做**任何方向換算——多轉一次會把
    # warn 變成負值並被 ThresholdRule 夾成 fail，讓每一筆觀測都變成 fail。
    current = threshold_rule_for(spec, policy)

    def result(
        recommended: ThresholdRule | None,
        reason: str,
        percentiles: dict[str, float],
        recommended_oriented: ThresholdRule | None = None,
    ) -> ThresholdRecommendation:
        return ThresholdRecommendation(
            metric_name=spec.metric_name,
            rule_name=spec.rule_name,
            policy_field=spec.policy_field,
            label=spec.label,
            gate_enabled=policy.enabled,
            observations=obs,
            current=current,
            recommended=recommended,
            reason=reason,
            percentiles=percentiles,
            current_counts=_counts_under(obs.values, current),
            recommended_counts=(
                _counts_under(obs.values, recommended_oriented)
                if recommended_oriented is not None
                else {"pass": 0, "warn": 0, "fail": 0}
            ),
        )

    if obs.sample_size < MIN_OBSERVATIONS:
        return result(
            None,
            f"可用觀測值只有 {obs.sample_size} 筆（需要至少 {MIN_OBSERVATIONS} 筆）；維持現行門檻",
            {},
        )

    percentiles = {
        "p50": round(nearest_rank_percentile(obs.values, 0.50), 4),
        "p80": round(nearest_rank_percentile(obs.values, WARN_PERCENTILE), 4),
        "p95": round(nearest_rank_percentile(obs.values, FAIL_PERCENTILE), 4),
        "max": round(max(obs.values), 4),
    }

    if max(obs.values) <= 0:
        return result(
            None,
            "歷史上未觀測到任何退化，任何百分位門檻都會讓每次發布觸發；維持現行門檻",
            percentiles,
        )

    warn_oriented = round(nearest_rank_percentile(obs.values, WARN_PERCENTILE), ROUND_DIGITS)
    fail_oriented = round(nearest_rank_percentile(obs.values, FAIL_PERCENTILE), ROUND_DIGITS)

    if warn_oriented <= 0:
        # 負門檻貼進 apps.yaml 會被 `load_gate_policy()` 的 `w_val < 0` 分支退回預設值，
        # `warn = 0` 則讓「零變化」也達到門檻（判定是 `value >= rule.warn`）。兩種情況下
        # 採用後的 gate 都不等於這裡算出的反事實，因此寧可不給。
        return result(
            None,
            (
                f"第 {int(WARN_PERCENTILE * 100)} 百分位數四捨五入後為 {warn_oriented}（不大於 0）："
                "負門檻會被 load_gate_policy() 退回預設值、零門檻會讓沒有退化的發布也判 warn，"
                "兩者都會讓實際 gate 與這份反事實不一致；維持現行門檻"
            ),
            percentiles,
        )

    if warn_oriented >= fail_oriented:
        return result(
            None,
            (
                f"四捨五入後 warn ({warn_oriented}) 不小於 fail ({fail_oriented})："
                "分佈過於集中，warn 這一級會永遠不可能出現；維持現行門檻"
            ),
            percentiles,
        )

    # 建議值與 `current` 使用同一個表示法（policy / apps.yaml 的 frame），否則產出的
    # YAML 片段對下降型指標會是負數，貼進設定檔就是另一個門檻。
    recommended = ThresholdRule(warn=warn_oriented, fail=fail_oriented)
    return result(
        recommended,
        (
            f"依 {obs.sample_size} 筆歷史觀測的第 {int(WARN_PERCENTILE * 100)} / "
            f"{int(FAIL_PERCENTILE * 100)} 百分位數"
        ),
        percentiles,
        recommended_oriented=recommended,
    )


def recommend_thresholds(
    catalog: list[dict[str, Any]],
    policy: GatePolicy | None = None,
) -> list[ThresholdRecommendation]:
    """對 `COMPARISON_METRIC_SPECS` 全表產出建議（順序即報表順序）。

    `policy` 省略時用 `GatePolicy()` 的預設值——與 `load_gate_policy(None)` 同一組數值，
    因此「沒有 app 設定」與「設定裡沒寫 release_gate」不會得到兩套對照基準。
    """
    eff_policy = policy if policy is not None else GatePolicy()
    return [recommend_for_metric(catalog, spec, eff_policy) for spec in COMPARISON_METRIC_SPECS]
