"""Release Decision fixture contract tests (Issue #90).

``tests/fixtures/dashboard_v2_release_decision.json`` is the only fixture that
carries ``release_catalog[]``, and therefore the only test data able to feed the
V3 Release Decision 首屏（#73）與其後續單子。它的內容不是任意樣本資料，而是
**下游單子的驗收面**：

1. **形狀正確性由 production code 決定，不由手寫決定。** fixture 的 ``vs_previous``
   由 ``compute_previous_release_comparison()`` 算出、``release_gate.rule_results``
   與 ``decision`` 由 ``evaluate_release()`` / ``derive_decision()` 產出。本檔重新
   推導一次並比對，確保 ``decision.reasons[]`` 永遠是同一筆 ``rule_results`` 的證據，
   而不是被人手改成好看的散文。
2. **三種特例覆蓋必須存活。** ``insufficient_data``、``baseline``、以及「gate 未評估
   因此完全沒有 ``decision`` 欄位」三者是 #73 五種狀態 UX 的核心風險面：前兩者絕不可
   被呈現為 PASS，後者絕不可被憑空補成綠燈建議。若日後有人「整理」fixture 時把這三種
   案例刪掉，consumer 的測試會全部變成 happy path 而沒有任何人會發現。
3. **既有兩個 fixture 必須維持沒有 ``release_catalog``。** 它們是「舊 bundle 沒有
   ``decision`` 仍可 validate / render」的向後相容基準（#72 / #73）。一旦有人好意
   把 ``release_catalog`` 補進去，這條基準就消失了。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.build_dashboard import build_html
from crash_trend.catalog.comparison import compute_previous_release_comparison
from crash_trend.gate.decision import derive_decision
from crash_trend.gate.policy import GatePolicy
from crash_trend.schema_v2 import (
    CANONICAL_DECISION_ACTIONS,
    validate_dashboard_v2,
)

FIXTURES = ROOT / "tests" / "fixtures"
FIXTURE = FIXTURES / "dashboard_v2_release_decision.json"
LEGACY_FIXTURES = (
    FIXTURES / "dashboard_v2.json",
    FIXTURES / "dashboard_v2_no_sessions.json",
)


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def all_releases(bundle: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Flattens every app's release_catalog into (app_id, item) pairs."""
    out: list[tuple[str, dict[str, Any]]] = []
    for app_id, app in bundle.get("apps", {}).items():
        for item in app.get("release_catalog") or []:
            out.append((app_id, item))
    return out


def gate_statuses(bundle: dict[str, Any]) -> list[str]:
    return [
        item["release_gate"]["status"]
        for _, item in all_releases(bundle)
        if isinstance(item.get("release_gate"), dict)
    ]


class TestFixtureIsValidAndRenderable(unittest.TestCase):
    def test_fixture_exists(self) -> None:
        self.assertTrue(FIXTURE.is_file(), f"Missing fixture: {FIXTURE}")

    def test_fixture_passes_schema_validation_with_zero_errors(self) -> None:
        """整份 bundle 必須真的通過 validation，而不是靠省略欄位繞過。

        ``validate_release_decision()`` 會檢查 ``(status, action)`` 是 canonical pair、
        且 ``decision.status`` 與外層 gate status 一致；fixture 若靠手寫拼湊，這裡就會炸。
        """
        errors = validate_dashboard_v2(load_fixture())
        self.assertEqual(errors, [], f"fixture must validate cleanly, got: {errors}")

    def test_fixture_renders_through_build_html(self) -> None:
        """帶著 release_catalog 的 bundle 必須能完整 render，否則 #73 無從開工。"""
        html = build_html(load_fixture())
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn("</html>", html)


class TestReleaseCatalogCoverage(unittest.TestCase):
    """涵蓋面斷言：這些案例是下游單子的依賴，不得在整理 fixture 時被消失。"""

    def setUp(self) -> None:
        self.bundle = load_fixture()
        self.releases = all_releases(self.bundle)

    def test_has_release_catalog_entries(self) -> None:
        self.assertTrue(self.releases, "fixture must carry release_catalog entries")

    def test_covers_both_platforms(self) -> None:
        """Android / iOS 並列比較是首屏的核心版面，缺一邊就測不到平台對照。"""
        platforms = {item["platform"] for _, item in self.releases}
        self.assertEqual(platforms, {"android", "ios"})

    def test_covers_insufficient_data_gate_state(self) -> None:
        """樣本不足是「還不能判定」，首屏不得呈現為 PASS——沒有這筆資料就測不到。"""
        self.assertIn("insufficient_data", gate_statuses(self.bundle))

    def test_covers_baseline_gate_state(self) -> None:
        """首版沒有前版可比，同樣不是 PASS；這是平台首個版本的真實輸出。"""
        self.assertIn("baseline", gate_statuses(self.bundle))

    def test_covers_gate_without_decision_field(self) -> None:
        """policy ``enabled: false`` 的 gate 完全沒有評估品質，因此 evaluator 刻意
        不附上 ``decision``（#72 review）。consumer 必須能證明自己不會憑空生出建議，
        所以 fixture 必須保留「有 release_gate、但沒有 decision 欄位」這個形狀。
        """
        without_decision = [
            (app_id, item["version"], item["platform"])
            for app_id, item in self.releases
            if isinstance(item.get("release_gate"), dict) and "decision" not in item["release_gate"]
        ]
        self.assertTrue(
            without_decision,
            "fixture must keep at least one evaluated-nothing gate carrying no decision field",
        )
        for app_id, version, platform in without_decision:
            item = next(
                i
                for a, i in self.releases
                if a == app_id and i["version"] == version and i["platform"] == platform
            )
            self.assertEqual(
                item["release_gate"].get("rule_results"),
                [],
                "a gate with no decision must also carry no rule evidence",
            )

    def test_covers_zero_baseline_comparison(self) -> None:
        """零基準退化（前版該指標為 0）是 comparison 的特例分支，需要真實樣本。"""
        zero_baseline = [
            item
            for _, item in self.releases
            if isinstance(item.get("vs_previous"), dict)
            and any(
                item["vs_previous"].get(k)
                for k in ("zero_baseline_crash", "zero_baseline_fatal", "zero_baseline_anr")
            )
        ]
        self.assertTrue(zero_baseline, "fixture must cover at least one zero_baseline_* case")

    def test_covers_issue_lifecycle_buckets(self) -> None:
        """introduced / persistent / regressed / resolved 四種 bucket 都要有非零樣本。"""
        totals = {k: 0 for k in ("introduced_count", "persistent_count", "regressed_count", "resolved_count")}
        for _, item in self.releases:
            lc = item.get("issue_lifecycle") or {}
            for key in totals:
                totals[key] += int(lc.get(key, 0))
        for key, total in totals.items():
            self.assertGreater(total, 0, f"issue_lifecycle.{key} has no coverage")

    def test_covers_gate_history(self) -> None:
        """gate timeline 需要同一個 release 的多筆歷史評估才看得出狀態轉移。"""
        with_history = [item for _, item in self.releases if item.get("gate_history")]
        self.assertTrue(with_history, "fixture must carry gate_history for the gate timeline")
        self.assertTrue(
            any(len(item["gate_history"]) >= 2 for item in with_history),
            "at least one release needs multiple history points to show a transition",
        )
        statuses = {p["gate_status"] for item in with_history for p in item["gate_history"]}
        self.assertGreater(len(statuses), 1, "gate_history must contain a status change")


class TestDecisionMatchesRuleEvidence(unittest.TestCase):
    """decision 必須是 rule 證據的推導結果，不是另寫一份文案。"""

    def setUp(self) -> None:
        self.releases = all_releases(load_fixture())

    def test_every_decision_equals_canonical_derivation(self) -> None:
        """對每一筆 gate 重新跑 ``derive_decision()``，結果必須逐欄相同。

        這樣 ``reasons[]`` 就不可能與 ``rule_results`` 脫鉤：任何人手動潤飾 fixture 的
        建議文案或理由，都會在這裡被抓到。
        """
        checked = 0
        for app_id, item in self.releases:
            gate = item.get("release_gate")
            if not isinstance(gate, dict) or "decision" not in gate:
                continue
            expected = derive_decision(
                gate["status"],
                gate.get("rule_results") or [],
                bool(gate.get("sample_sufficient", True)),
            )
            self.assertEqual(
                gate["decision"],
                dict(expected),
                f"{app_id} {item['platform']} {item['version']} decision drifted from rule evidence",
            )
            checked += 1
        self.assertGreater(checked, 0, "no decision blocks found to verify")

    def test_decision_status_action_pairs_are_canonical(self) -> None:
        for app_id, item in self.releases:
            gate = item.get("release_gate")
            if not isinstance(gate, dict) or "decision" not in gate:
                continue
            decision = gate["decision"]
            self.assertEqual(
                decision["action"],
                CANONICAL_DECISION_ACTIONS[decision["status"]],
                f"{app_id} {item['version']} carries a non-canonical (status, action) pair",
            )


class TestComparisonMatchesObservations(unittest.TestCase):
    """vs_previous 必須是同一份 recent_health 觀測值算出來的，不是另填的數字。"""

    def test_vs_previous_matches_comparison_derivation(self) -> None:
        """對每一組 (release, 前版) 重新跑 ``compute_previous_release_comparison()``。

        沒有這條斷言，fixture 很容易出現「3.2.0 說前版 ANR 為 0，但 3.1.2 自己的
        recent_health 明明有 ANR」這種內部矛盾——資料看起來合法，卻描述了一個
        production pipeline 不可能產生的世界，consumer 依此開發就會誤判。
        """
        policy = GatePolicy()
        bundle = load_fixture()
        checked = 0
        for app_id, app in bundle.get("apps", {}).items():
            catalog = app.get("release_catalog") or []
            by_key = {(i["platform"], i["version"]): i for i in catalog}
            for item in catalog:
                vs_previous = item.get("vs_previous")
                if not isinstance(vs_previous, dict):
                    continue
                previous = by_key.get((item["platform"], vs_previous.get("previous_version")))
                if previous is None:
                    # 前版已不在 catalog 保留範圍內（例如 3.0.5），無從重算。
                    continue
                expected = compute_previous_release_comparison(
                    v_curr_info={"recent_health": item["recent_health"]},
                    v_prev_info={"recent_health": previous["recent_health"]},
                    v_prev=previous["version"],
                    recent_health=item["recent_health"],
                    introduced_count=int((item.get("issue_lifecycle") or {}).get("introduced_count", 0)),
                    prev_introduced_count=int(
                        (previous.get("issue_lifecycle") or {}).get("introduced_count", 0)
                    ),
                    target_window="30",
                    min_adoption_rate=policy.min_adoption_rate,
                    min_sessions=policy.min_sessions,
                    min_version_events=policy.min_version_events,
                )
                self.assertEqual(
                    vs_previous,
                    dict(expected),
                    f"{app_id} {item['platform']} {item['version']} vs_previous "
                    "disagrees with the recent_health it claims to compare",
                )
                checked += 1
        self.assertGreater(checked, 0, "no comparable release pairs found to verify")


class TestLegacyFixturesKeepBackwardCompatibilityBaseline(unittest.TestCase):
    """既有 fixture 是「舊 bundle 無 decision 仍可 validate / render」的基準。"""

    def test_existing_fixtures_carry_no_release_catalog(self) -> None:
        for path in LEGACY_FIXTURES:
            with self.subTest(fixture=path.name):
                bundle = json.loads(path.read_text(encoding="utf-8"))
                for app_id, app in bundle.get("apps", {}).items():
                    self.assertNotIn(
                        "release_catalog",
                        app,
                        f"{path.name}:{app_id} must stay release_catalog-free; "
                        "release decision data belongs in dashboard_v2_release_decision.json",
                    )


if __name__ == "__main__":
    unittest.main()
