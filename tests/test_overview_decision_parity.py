"""Dashboard ↔ Google Chat 的 Release Decision parity test（Issue #73）。

#72 只交付了 **negative** drift test（「程式碼裡不存在第二套 status → 文案映射」），
因為當時還沒有 Dashboard consumer 可以比對。本單補上真正的 parity：同一個
release，Dashboard 首屏渲染出來的建議／行動，必須與 Google Chat alert 送出的
建議／行動**逐字元相同**。

為什麼是這個形狀：

- **比欄位，不比訊息文字。** `AlertMessage` 的 `decision_action` / `recommendation`
  是 #72 為此預留的接縫。去 parse Chat 的訊息本文會把「排版改了」誤判成
  「建議漂移了」，反而讓這個測試變成噪音而被關掉。
- **兩邊都對照 fixture 的欄位。** 只斷言「兩邊相等」的話，兩邊一起錯（例如都
  改成從 status 重算）仍會通過。因此第三個斷言把兩邊各自釘到
  `release_gate.decision` 上。
- **presentation 改動不得造成分歧。** 對 alert 端只影響外觀的輸入（severity 圖示、
  是否用 thread、有沒有 dashboard 連結、前版版本號）逐一變動，兩邊的
  建議／行動必須完全不動。這才是「渲染層改不動決策」的實質保證。

**保證邊界（刻意寫下來，避免被當成比實際更強的保證）：**

1. 只保證 `recommendation` 與 `action` 這兩個欄位，不保證 reasons 的排版、
   標題文案、或 Chat 卡片的視覺結構。
2. 只覆蓋 fixture 中**帶有 canonical decision** 的 release。gate 未評估
   （無 `decision` / `release_gate: null`）時 Dashboard 不產生建議，Chat 也不會
   發出這種 alert，兩邊「都沒有建議」的一致性由
   ``tests/test_overview_decision.py`` 負責。
3. 這是一條**雙 consumer**的 parity test。未來新增第三個 consumer（CLI、GitHub
   Check）不會自動被納入；#72 的靜態 drift test 仍是那一層的防線。
4. 不驗證投遞（webhook、去重、cooldown）——那是 #59 / #63 的範圍。
"""

from __future__ import annotations

import copy
import html as html_mod
import unittest
from typing import Any

from crash_trend.alerts.dispatcher import build_alert_message
from tests.test_overview_decision import _ClientRuntime, _field


def _releases_with_decision(bundle: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """回傳 fixture 中所有帶 canonical decision 的 release。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for app_id, app in bundle["apps"].items():
        for release in app.get("release_catalog") or []:
            gate = release.get("release_gate")
            if gate and isinstance(gate.get("decision"), dict):
                out.append((app_id, release))
    return out


def _alert_for(app_id: str, release: dict[str, Any], **overrides: Any) -> Any:
    """用同一份 `release_gate.decision` 產生 Google Chat 的 AlertMessage。"""
    gate = release["release_gate"]
    vs_prev = release.get("vs_previous") or {}
    kwargs: dict[str, Any] = {
        "app_id": app_id,
        "platform": release["platform"],
        "target_version": release["version"],
        "previous_version": vs_prev.get("previous_version"),
        "gate_status": gate["status"],
        "is_recovery": False,
        "rule_results": gate.get("rule_results") or [],
        "policy_version": "1",
        "evaluated_at": gate.get("evaluated_at") or "",
        "decision": gate["decision"],
        "alert_severity": gate.get("alert_severity", "none"),
    }
    kwargs.update(overrides)
    return build_alert_message(**kwargs)


class TestDashboardAndChatShareOneDecision(_ClientRuntime):
    """同一個 release，兩個 consumer 的建議／行動必須相同。"""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.steps = None

    def setUp(self) -> None:
        if getattr(type(self), "steps", None) is None:
            type(self).steps = self._render()

    def _rendered(self, app_id: str, release: dict[str, Any]) -> tuple[str, str]:
        """Dashboard 首屏那張卡上**看得到**的建議與建議行動。"""
        card = self.steps["cards"][f"{app_id}|{release['platform']}|{release['version']}"]
        recommendation = _field(card, "decision-recommendation")
        action = _field(card, "decision-action")
        self.assertIsNotNone(recommendation, "前提：這個 release 有 canonical decision，卡片必須顯示建議")
        self.assertIsNotNone(action, "前提：這個 release 有 canonical decision，卡片必須顯示建議行動")
        return html_mod.unescape(recommendation or ""), html_mod.unescape(action or "")

    def test_fixture_covers_more_than_one_state(self) -> None:
        """前提：parity 至少要跨越多種 decision 狀態，否則只證明了一個特例。"""
        statuses = {
            r["release_gate"]["decision"]["status"] for _, r in _releases_with_decision(self.bundle)
        }
        self.assertGreaterEqual(len(statuses), 4, f"覆蓋的 decision 狀態太少：{statuses}")

    def test_every_release_reports_the_same_recommendation_and_action_to_both_consumers(self) -> None:
        releases = _releases_with_decision(self.bundle)
        self.assertTrue(releases, "前提：fixture 至少有一個帶 decision 的 release")

        for app_id, release in releases:
            with self.subTest(app=app_id, platform=release["platform"], version=release["version"]):
                dash_rec, dash_action = self._rendered(app_id, release)
                msg = _alert_for(app_id, release)
                decision = release["release_gate"]["decision"]

                # 1) 兩個 consumer 彼此一致。
                self.assertEqual(dash_rec, msg.recommendation)
                self.assertEqual(dash_action, msg.decision_action)
                # 2) 兩者各自釘在 contract 欄位上——「一起錯」也不會通過。
                self.assertEqual(dash_rec, decision["recommendation"])
                self.assertEqual(dash_action, decision["action"])
                self.assertEqual(msg.recommendation, decision["recommendation"])
                self.assertEqual(msg.decision_action, decision["action"])

    def test_a_changed_contract_moves_both_consumers_together(self) -> None:
        """把 contract 欄位換成 sentinel，兩邊必須同時跟著變。

        這一條是 parity 的核心：它排除了「兩邊各自持有一份寫死文案、剛好相同」
        這種假通過。任何一邊有私藏副本，它就會停在 canonical 文案上而被抓到。
        """
        sentinel_rec = "PARITY-SENTINEL-RECOMMENDATION-4c19"
        sentinel_action = "parity_sentinel_action_4c19"

        bundle = copy.deepcopy(self.bundle)
        for _, release in _releases_with_decision(bundle):
            release["release_gate"]["decision"]["recommendation"] = sentinel_rec
            release["release_gate"]["decision"]["action"] = sentinel_action

        steps = self._render(bundle=bundle)
        for app_id, release in _releases_with_decision(bundle):
            with self.subTest(app=app_id, platform=release["platform"], version=release["version"]):
                card = steps["cards"][f"{app_id}|{release['platform']}|{release['version']}"]
                msg = _alert_for(app_id, release)

                self.assertEqual(_field(card, "decision-recommendation"), sentinel_rec)
                self.assertEqual(_field(card, "decision-action"), sentinel_action)
                self.assertEqual(msg.recommendation, sentinel_rec)
                self.assertEqual(msg.decision_action, sentinel_action)

    def test_presentation_only_alert_changes_cannot_cause_divergence(self) -> None:
        """alert 端純外觀的輸入變動，不得動到建議／行動。

        alert 的標題圖示、thread、dashboard 連結、前版版本號都屬於呈現層；
        其中任何一項一旦被拿去參與建議的生成，Dashboard 與 Chat 就會在
        「同一個 release、不同投遞情境」下說出不同的話。
        """
        app_id, release = _releases_with_decision(self.bundle)[0]
        dash_rec, dash_action = self._rendered(app_id, release)

        variants: list[dict[str, Any]] = [
            {},
            {"alert_severity": "critical"},
            {"alert_severity": "none"},
            {"use_threads": False},
            {"dashboard_url": "https://example.invalid/dash.html#overview"},
            {"previous_version": None},
            {"previous_version": "0.0.1"},
        ]
        for overrides in variants:
            with self.subTest(overrides=overrides):
                msg = _alert_for(app_id, release, **overrides)
                self.assertEqual(msg.recommendation, dash_rec)
                self.assertEqual(msg.decision_action, dash_action)


if __name__ == "__main__":
    unittest.main()
