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

import datetime as dt
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
from crash_trend.gate.history import compute_evaluation_key
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


class TestTemporalConsistency(unittest.TestCase):
    """時間一致性：fixture 描述的世界必須是 pipeline 真的能產生的世界。

    這裡刻意把時間戳分成兩類，因為它們的約束不同 —— 用一條「任何時間戳都不得晚於
    ``generated_at``」的通則會同時誤判 base fixture，那代表通則本身錯了：

    * **觀測值**（``release_catalog[].first_seen`` / ``last_seen``、
      ``top_issues[].first_seen_timestamp`` / ``last_seen_timestamp``、
      breadcrumb / log 的 ``timestamp``）是「在觀測窗內看到了什麼」，
      因此**不得晚於 ``period.end_time``**。反方向則不成立：release_catalog 與
      issue 都是跨期保留的（#47），``first_seen`` 早於 ``period.start_time``
      是正常的（base fixture 自己就有 2026-07-05 的 ``first_seen_timestamp``），
      所以這裡**不**斷言 ``first_seen >= period.start_time``。
    * **派生值**（``ai_summary.generated_at``、``sources[].last_sync_timestamp``、
      ``release_gate.evaluated_at``）是「收完資料之後才算出來的」，本來就會晚於
      ``generated_at``。base fixture 已經定義了這條慣例：14:00 收 BQ / sessions、
      14:05 MCP 補件、14:10 AI 分析，本 fixture 的 gate 評估接在 14:15。
      因此約束是「不早於 ``generated_at``、且落在一個有界的處理窗內」。

    ``DERIVED_MARGIN`` 是那個有界窗。它不是為了寬鬆而寬鬆：一旦某個派生時間戳跨到
    隔天（本單 review 抓到的正是 ``evaluated_at`` 晚了 12 小時），就不再是「處理延遲」
    而是資料錯亂，必須被擋下來。
    """

    # 派生資料允許落後 generated_at 的上限；base fixture 用 +5 / +10 分鐘，gate 用 +15
    DERIVED_MARGIN = dt.timedelta(minutes=30)

    ALL_FIXTURES = (FIXTURE, *LEGACY_FIXTURES)

    @staticmethod
    def _ts(value: str) -> dt.datetime:
        return dt.datetime.fromisoformat(value)

    @staticmethod
    def _observations(app: dict[str, Any]) -> list[tuple[str, str]]:
        """回傳 (label, timestamp)：所有「被觀測到」的時間戳。"""
        out: list[tuple[str, str]] = []
        for item in app.get("release_catalog") or []:
            label = f"release {item['version']} ({item['platform']})"
            for field in ("first_seen", "last_seen"):
                if isinstance(item.get(field), str):
                    out.append((f"{label}.{field}", item[field]))
        for issue in app.get("top_issues") or []:
            label = f"issue {issue.get('issue_id')}"
            for field in ("first_seen_timestamp", "last_seen_timestamp"):
                if isinstance(issue.get(field), str):
                    out.append((f"{label}.{field}", issue[field]))
            detail = issue.get("detail") or {}
            for bucket in ("breadcrumbs", "logs"):
                for idx, entry in enumerate(detail.get(bucket) or []):
                    if isinstance(entry, dict) and isinstance(entry.get("timestamp"), str):
                        out.append((f"{label}.detail.{bucket}[{idx}].timestamp", entry["timestamp"]))
        return out

    @staticmethod
    def _derived(app: dict[str, Any]) -> list[tuple[str, str]]:
        """回傳 (label, timestamp)：所有「收完資料後才算出來」的時間戳。"""
        out: list[tuple[str, str]] = []
        summary_generated = (app.get("ai_summary") or {}).get("generated_at")
        if isinstance(summary_generated, str):
            out.append(("ai_summary.generated_at", summary_generated))
        for name, source in (app.get("sources") or {}).items():
            if isinstance(source, dict) and isinstance(source.get("last_sync_timestamp"), str):
                out.append((f"sources.{name}.last_sync_timestamp", source["last_sync_timestamp"]))
        for item in app.get("release_catalog") or []:
            gate = item.get("release_gate")
            if isinstance(gate, dict) and isinstance(gate.get("evaluated_at"), str):
                out.append(
                    (
                        f"release {item['version']} ({item['platform']}).release_gate.evaluated_at",
                        gate["evaluated_at"],
                    )
                )
        return out

    def test_no_observation_postdates_the_reporting_window(self) -> None:
        """觀測值不得晚於觀測窗結束——不可能「看到」窗關掉之後才發生的事。

        這是 review 抓到的核心錯誤：``last_seen`` 落在 9/3 凌晨，但這份 bundle 的觀測窗
        9/2 14:00 就結束了。consumer 若照此開發，會以為「最新版還在持續回報」這個狀態
        可以由 bundle 內的資料證明，實際上不行。
        """
        for path in self.ALL_FIXTURES:
            bundle = json.loads(path.read_text(encoding="utf-8"))
            for app_id, app in bundle.get("apps", {}).items():
                end_time = app["period"]["end_time"]
                for label, value in self._observations(app):
                    with self.subTest(fixture=path.name, app=app_id, field=label):
                        self.assertLessEqual(
                            self._ts(value),
                            self._ts(end_time),
                            f"{path.name}:{app_id} {label}={value} is observed after the "
                            f"reporting window closed at {end_time}",
                        )

    def test_first_seen_never_follows_last_seen(self) -> None:
        """同一個實體不可能「最後一次出現」早於「第一次出現」。"""
        for path in self.ALL_FIXTURES:
            bundle = json.loads(path.read_text(encoding="utf-8"))
            for app_id, app in bundle.get("apps", {}).items():
                pairs: list[tuple[str, str | None, str | None]] = [
                    (f"release {i['version']} ({i['platform']})", i.get("first_seen"), i.get("last_seen"))
                    for i in app.get("release_catalog") or []
                ]
                pairs += [
                    (
                        f"issue {i.get('issue_id')}",
                        i.get("first_seen_timestamp"),
                        i.get("last_seen_timestamp"),
                    )
                    for i in app.get("top_issues") or []
                ]
                for label, first_seen, last_seen in pairs:
                    if not isinstance(first_seen, str) or not isinstance(last_seen, str):
                        continue
                    with self.subTest(fixture=path.name, app=app_id, entity=label):
                        self.assertLessEqual(
                            self._ts(first_seen),
                            self._ts(last_seen),
                            f"{path.name}:{app_id} {label} first_seen={first_seen} "
                            f"follows last_seen={last_seen}",
                        )

    def test_derived_timestamps_sit_in_a_bounded_processing_window(self) -> None:
        """派生值可以晚於 ``generated_at``，但只能晚一小段處理時間。

        上界存在的理由就是本單 review 的那筆錯誤：``evaluated_at`` 晚 12 小時、跨到隔天，
        已經不是處理延遲而是資料錯亂。下界則擋掉「用還沒收到的資料算出結論」。
        """
        for path in self.ALL_FIXTURES:
            bundle = json.loads(path.read_text(encoding="utf-8"))
            generated_at = self._ts(bundle["generated_at"])
            deadline = generated_at + self.DERIVED_MARGIN
            for app_id, app in bundle.get("apps", {}).items():
                for label, value in self._derived(app):
                    with self.subTest(fixture=path.name, app=app_id, field=label):
                        self.assertGreaterEqual(
                            self._ts(value),
                            generated_at,
                            f"{path.name}:{app_id} {label}={value} precedes the bundle's "
                            f"generated_at={bundle['generated_at']}",
                        )
                        self.assertLessEqual(
                            self._ts(value),
                            deadline,
                            f"{path.name}:{app_id} {label}={value} is more than "
                            f"{self.DERIVED_MARGIN} after generated_at="
                            f"{bundle['generated_at']}; that is data corruption, "
                            "not processing latency",
                        )

    def test_gate_is_never_evaluated_before_the_data_it_judges(self) -> None:
        """gate 不可能在最後一次觀測之前就評估完那批觀測。"""
        bundle = load_fixture()
        for app_id, item in all_releases(bundle):
            gate = item.get("release_gate")
            if not isinstance(gate, dict) or not isinstance(gate.get("evaluated_at"), str):
                continue
            with self.subTest(app=app_id, version=item["version"], platform=item["platform"]):
                self.assertGreaterEqual(
                    self._ts(gate["evaluated_at"]),
                    self._ts(item["last_seen"]),
                    f"{app_id} {item['platform']} {item['version']} gate evaluated at "
                    f"{gate['evaluated_at']} but its data was still arriving until "
                    f"{item['last_seen']}",
                )

    def test_gate_history_is_strictly_chronological(self) -> None:
        """timeline 要看得出狀態轉移，前提是每筆評估的時間嚴格遞增且互異。

        ``transition.previous_status`` 是照這個順序算出來的；順序一亂，整條
        pass -> warn -> fail 的敘事就跟資料脫鉤了。
        """
        for app_id, item in all_releases(load_fixture()):
            history = item.get("gate_history") or []
            if not history:
                continue
            stamps = [p["evaluated_at"] for p in history]
            with self.subTest(app=app_id, version=item["version"], platform=item["platform"]):
                self.assertEqual(
                    stamps,
                    sorted(stamps),
                    f"{app_id} {item['platform']} {item['version']} gate_history is out of order: {stamps}",
                )
                self.assertEqual(
                    len(set(stamps)),
                    len(stamps),
                    f"{app_id} {item['platform']} {item['version']} gate_history has "
                    f"duplicate evaluated_at: {stamps}",
                )

    def test_latest_history_point_is_the_current_gate_evaluation(self) -> None:
        """history 的最後一筆就是當前 gate，兩者若不同步，首屏顯示的狀態會和 timeline 尾端矛盾。"""
        for app_id, item in all_releases(load_fixture()):
            history = item.get("gate_history") or []
            gate = item.get("release_gate")
            if not history or not isinstance(gate, dict) or "evaluated_at" not in gate:
                continue
            with self.subTest(app=app_id, version=item["version"], platform=item["platform"]):
                self.assertEqual(
                    history[-1]["evaluated_at"],
                    gate["evaluated_at"],
                    f"{app_id} {item['platform']} {item['version']} timeline ends at "
                    f"{history[-1]['evaluated_at']} but the gate claims {gate['evaluated_at']}",
                )
                self.assertEqual(
                    history[-1]["gate_status"],
                    gate["status"],
                    f"{app_id} {item['platform']} {item['version']} timeline ends in "
                    f"{history[-1]['gate_status']} but the gate reports {gate['status']}",
                )

    def test_history_evaluation_keys_match_their_timestamps(self) -> None:
        """``evaluation_key`` 是 (app, platform, version, evaluated_at, policy) 的雜湊。

        所以搬動 ``evaluated_at`` 時它必須一起重算，不能留著舊 key —— 舊 key 會讓
        history store 的 idempotency 判斷指向一筆不存在的評估。這條斷言就是為了讓
        「只改時間戳、忘了重算 key」這種修法必然失敗。
        """
        checked = 0
        for app_id, item in all_releases(load_fixture()):
            for point in item.get("gate_history") or []:
                expected = compute_evaluation_key(
                    app_id,
                    item["platform"],
                    item["version"],
                    point["evaluated_at"],
                    point["policy_version"],
                    point["policy_identity"],
                )
                self.assertEqual(
                    point["evaluation_key"],
                    expected,
                    f"{app_id} {item['platform']} {item['version']} history point "
                    f"{point['evaluated_at']} carries a stale evaluation_key",
                )
                checked += 1
        self.assertGreater(checked, 0, "no gate_history points found to verify")

    def test_catalog_keeps_a_release_predating_the_reporting_window(self) -> None:
        """release_catalog 是跨期保留的（#47），因此必須留著一筆 ``first_seen`` 早於
        ``period.start_time`` 的 release。

        這筆資料同時是上面那條「觀測值只有上界、沒有下界」的理由：若哪天有人把
        ``first_seen >= period.start_time`` 也加進 invariant，這個案例會立刻告訴他
        那條規則會誤殺長期存在的 release。
        """
        bundle = load_fixture()
        predating = [
            (app_id, item["version"], item["platform"])
            for app_id, app in bundle.get("apps", {}).items()
            for item in app.get("release_catalog") or []
            if item["first_seen"] < app["period"]["start_time"]
        ]
        self.assertTrue(
            predating,
            "fixture must keep at least one release first seen before the reporting window, "
            "otherwise the persistent-catalog case (#47) loses its coverage",
        )


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
