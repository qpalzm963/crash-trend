"""CLI：依歷史 release 分佈推薦 Gate 門檻（Issue #76）。

    python -m crash_trend.recommend_gate_thresholds --app shop_app [--json]

**本工具只輸出建議，不會修改任何設定檔。** 門檻要不要調整、調成多少，由人看完反事實
（同一批歷史版本在現行／建議門檻下各會被判幾次 warn / fail）之後自己寫進 `apps.yaml`。
因此執行期的門檻仍然只有 `GatePolicy` 一個來源，`threshold_source` 契約（#74）不變。

資料源與 gate 完全相同：`release_gate.load_app_release_catalog()`，也就是 gate 評估時
讀的那一份 `release_catalog`。判定一律呼叫 `classify_threshold_breach()`。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from crash_trend.config import get_app, load_config
from crash_trend.gate.policy import GatePolicy, load_gate_policy
from crash_trend.gate.threshold_recommendation import (
    FAIL_PERCENTILE,
    MIN_OBSERVATIONS,
    WARN_PERCENTILE,
    ThresholdRecommendation,
    recommend_thresholds,
)
from crash_trend.release_gate import load_app_release_catalog


def format_report(app_name: str, recs: list[ThresholdRecommendation]) -> str:
    """組出人讀的報表。"""
    lines: list[str] = [
        "",
        f"================ [Gate 門檻建議：{app_name}] ================",
        f"  百分位數：warn = p{int(WARN_PERCENTILE * 100)}、fail = p{int(FAIL_PERCENTILE * 100)}"
        f"（最少 {MIN_OBSERVATIONS} 筆觀測才給建議）",
        "  本工具不會修改任何設定檔；以下是給人審查後自行採用的建議值。",
        "  注意：百分位門檻是「對這個 App 而言不尋常」，不是「品質可接受」——照抄會讓",
        "  gate 永遠對最差的那幾 % 發布喊 fail，無論絕對品質如何。請以反事實為判斷依據。",
        "",
    ]

    for rec in recs:
        obs = rec.observations
        lines.append(f"  ── {rec.label}（{rec.metric_name}）")
        excluded = (
            f"缺值 {obs.missing} / 零基準 {obs.zero_baseline}"
            f" / 本版樣本不足 {obs.insufficient_current}"
            f" / 前版樣本不足 {obs.insufficient_previous}"
        )
        lines.append(f"     觀測 {obs.sample_size} 筆（排除：{excluded}）")
        if rec.percentiles:
            pct = " · ".join(f"{k}={v:+.4f}" for k, v in rec.percentiles.items())
            lines.append(f"     分佈（正值=退化）：{pct}")
        lines.append(f"     現行門檻：warn {rec.current.warn} / fail {rec.current.fail}")
        if rec.recommended is None:
            lines.append(f"     建議：不調整 —— {rec.reason}")
        else:
            lines.append(
                f"     建議門檻：warn {rec.recommended.warn} / fail {rec.recommended.fail}"
                f" —— {rec.reason}"
            )
        if obs.sample_size:
            cur = rec.current_counts
            lines.append(
                f"     反事實（這 {obs.sample_size} 次發布）："
                f"現行 pass {cur['pass']} / warn {cur['warn']} / fail {cur['fail']}"
            )
            if rec.recommended is not None:
                new = rec.recommended_counts
                lines.append(
                    f"                              "
                    f"建議 pass {new['pass']} / warn {new['warn']} / fail {new['fail']}"
                )
        lines.append("")

    actionable = [r for r in recs if r.recommended is not None]
    if actionable:
        lines.append("  可貼進 apps.yaml 的片段（請自行確認後再採用）：")
        lines.append("")
        lines.append("    apps:")
        lines.append(f"      {app_name}:")
        lines.append("        release_gate:")
        lines.append("          thresholds:")
        for rec in actionable:
            assert rec.recommended is not None
            lines.append(
                f"            {rec.policy_field}: {{ warn: {rec.recommended.warn},"
                f" fail: {rec.recommended.fail} }}"
            )
        lines.append("")
    else:
        lines.append("  沒有任何指標達到給建議的條件，維持現行門檻即可。")
        lines.append("")
    lines.append("=============================================================")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="依歷史 release 分佈推薦 Release Gate 門檻（只輸出建議，不修改設定）"
    )
    parser.add_argument("--app", required=True, help="apps.yaml 中的 app 名稱")
    parser.add_argument("--json", action="store_true", help="以 JSON 輸出")
    args = parser.parse_args()

    cfg = load_config()
    app_cfg = get_app(args.app, cfg)
    policy: GatePolicy = load_gate_policy(app_cfg)

    try:
        catalog = load_app_release_catalog(args.app)
    except FileNotFoundError as exc:
        sys.exit(f"[錯誤] {exc}")

    recs = recommend_thresholds(catalog, policy)

    if args.json:
        payload: dict[str, Any] = {
            "app_id": args.app,
            "policy_version": policy.policy_version,
            "warn_percentile": WARN_PERCENTILE,
            "fail_percentile": FAIL_PERCENTILE,
            "min_observations": MIN_OBSERVATIONS,
            "recommendations": [r.to_dict() for r in recs],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print(format_report(args.app, recs))


if __name__ == "__main__":
    main()
