"""多通道告警 provider (Issue #66 項目 4).

原本只有 Google Chat，而它那 100 行重試 / 退避 / 錯誤分類的邏輯是整支告警管線裡最
不該被複製的東西：四個 provider 各自抄一份，就等於四套「什麼算暫時性失敗」的判斷，
而其中三套永遠不會被人讀到。

因此本檔的主軸不是「新增了三個類別」，而是**投遞語意對每一個 provider 都相同**：
以參數化測試把同一組情境（成功 / 429 重試 / 5xx 耗盡 / 4xx 快速失敗 / 缺 URL /
機密遮蔽）套在全部 provider 上。少了這一組，新增第五個通道時就只能靠 review 記得
「這些行為也要照做一次」。

另外釘住三件事：

* **機密遮蔽**：webhook URL 本身就是機密（Slack / Teams / Google Chat 的權杖都在
  URL 裡），任何 provider 的錯誤訊息都不得把它寫進稽核紀錄。
* **不支援 thread 的服務不得宣稱用了 thread**：Slack incoming webhook 沒有 thread，
  在稽核裡記一個沒用到的 thread key 就是假紀錄。
* **generic webhook 的 envelope 不會漂移**：欄位集合直接由 `AlertMessage` 產生，
  有測試反向確認兩邊相同。
"""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.alerts.models import AlertMessage
from crash_trend.alerts.policy import AlertPolicy, load_alert_policy
from crash_trend.alerts.providers.factory import build_provider
from crash_trend.alerts.providers.generic import GenericWebhookProvider
from crash_trend.alerts.providers.google_chat import GoogleChatWebhookProvider
from crash_trend.alerts.providers.microsoft_teams import MicrosoftTeamsWebhookProvider
from crash_trend.alerts.providers.registry import (
    PROVIDER_CLASSES,
    default_webhook_env_for,
    normalize_provider_name,
    provider_class,
    supported_providers,
)
from crash_trend.alerts.providers.slack import SlackWebhookProvider

#: 每個 provider 一組「帶機密的 webhook URL」，用來驗遮蔽。
SECRET_URLS = {
    "google_chat": "https://chat.googleapis.com/v1/spaces/AAA/messages?key=SECRETKEY&token=SECRETTOKEN",
    "slack": "https://hooks.slack.com/services/T00000000/B00000000/SuperSecretPath",
    "microsoft_teams": "https://acme.webhook.office.com/webhookb2/deadbeef@tenant/IncomingWebhook/SECRET/abc",
    "webhook": "https://ops.internal.example.com/hooks/alerts?token=SECRETTOKEN",
}

#: 各 provider URL 裡不得外流的那一段。
SECRET_FRAGMENTS = {
    "google_chat": ["SECRETKEY", "SECRETTOKEN"],
    "slack": ["SuperSecretPath", "B00000000"],
    "microsoft_teams": ["IncomingWebhook/SECRET", "deadbeef"],
    "webhook": ["SECRETTOKEN"],
}


def make_alert(thread_key: str | None = "shop_app:android:3.2.0") -> AlertMessage:
    return AlertMessage(
        app_id="shop_app",
        platform="android",
        target_version="3.2.0",
        previous_version="3.1.2",
        gate_status="fail",
        is_recovery=False,
        title="版本 3.2.0 品質閘門失敗",
        summary="崩潰率變動 +37.57%",
        regression_reasons=["crash_rate_regression"],
        policy_version="1.0",
        evaluated_at="2026-09-11T02:00:00Z",
        dashboard_url="https://dash.example.com/#shop_app",
        thread_key=thread_key,
        text="[FAIL] 版本 3.2.0 (android) 品質閘門失敗",
        decision_action="阻止發布",
        recommendation="立即回退或修復後重新評估",
    )


class FakeResponse:
    def __init__(self, status_code: int, text: str = "", payload: Any = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """記下每一次 POST，並依序回傳預先安排的回應。"""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, json: Any = None, params: Any = None, timeout: Any = None) -> FakeResponse:
        self.calls.append({"url": url, "json": json, "params": params})
        return self.responses.pop(0) if self.responses else FakeResponse(500, "no more")


def build(name: str, responses: list[FakeResponse], **kwargs: Any) -> tuple[Any, FakeSession]:
    """用帶機密的假 URL 與假 session 建出指定 provider。"""
    session = FakeSession(responses)
    cls = PROVIDER_CLASSES[name]
    provider = cls(
        webhook_url=SECRET_URLS[name],
        session=session,
        sleep_fn=lambda _s: None,
        backoff_sec=0.0,
        **kwargs,
    )
    return provider, session


class TestRegistryAndFactory(unittest.TestCase):
    def test_all_four_channels_the_issue_asks_for_are_registered(self) -> None:
        self.assertEqual(
            supported_providers(),
            ("google_chat", "microsoft_teams", "slack", "webhook"),
        )

    def test_each_channel_has_its_own_default_env_var(self) -> None:
        envs = {name: default_webhook_env_for(name) for name in supported_providers()}
        self.assertEqual(
            envs,
            {
                "google_chat": "GOOGLE_CHAT_WEBHOOK_URL",
                "slack": "SLACK_WEBHOOK_URL",
                "microsoft_teams": "MS_TEAMS_WEBHOOK_URL",
                "webhook": "ALERT_WEBHOOK_URL",
            },
        )
        self.assertEqual(len(set(envs.values())), len(envs), "兩個通道共用同一個環境變數")

    def test_aliases_resolve_to_canonical_names(self) -> None:
        for alias, expected in (
            ("teams", "microsoft_teams"),
            ("MSTeams", "microsoft_teams"),
            ("  ms_teams ", "microsoft_teams"),
            ("http", "webhook"),
            ("custom", "webhook"),
            ("googlechat", "google_chat"),
        ):
            with self.subTest(alias=alias):
                self.assertEqual(normalize_provider_name(alias), expected)

    def test_factory_builds_the_class_the_name_points_at(self) -> None:
        for name, cls in PROVIDER_CLASSES.items():
            with self.subTest(provider=name):
                built = build_provider(AlertPolicy(provider=name, webhook_env=""))
                self.assertIsInstance(built, cls)

    def test_an_unknown_provider_yields_none_instead_of_raising(self) -> None:
        """#59 的設計：投遞問題不得讓整支 pipeline 掛掉，要走稽核紀錄。"""
        self.assertIsNone(build_provider(AlertPolicy(provider="carrier_pigeon")))
        self.assertIsNone(provider_class("carrier_pigeon"))

    def test_an_explicit_webhook_env_overrides_the_default(self) -> None:
        built = build_provider(AlertPolicy(provider="slack", webhook_env="MY_OWN_VAR"))
        assert built is not None
        self.assertEqual(built.webhook_env, "MY_OWN_VAR")

    def test_google_chat_still_receives_its_thread_setting(self) -> None:
        built = build_provider(AlertPolicy(provider="google_chat", use_threads=False))
        assert isinstance(built, GoogleChatWebhookProvider)
        self.assertFalse(built.use_threads)


class TestPolicyResolvesPerProviderSettings(unittest.TestCase):
    def test_each_provider_gets_its_own_default_env(self) -> None:
        for provider, expected in (
            ("google_chat", "GOOGLE_CHAT_WEBHOOK_URL"),
            ("slack", "SLACK_WEBHOOK_URL"),
            ("microsoft_teams", "MS_TEAMS_WEBHOOK_URL"),
            ("webhook", "ALERT_WEBHOOK_URL"),
        ):
            with self.subTest(provider=provider):
                policy = load_alert_policy({"release_alerts": {"provider": provider}})
                self.assertEqual(policy.webhook_env, expected)

    def test_a_provider_sub_block_configures_that_provider(self) -> None:
        """`google_chat:` 這個既有慣例現在對每個 provider 都成立。"""
        policy = load_alert_policy(
            {"release_alerts": {"provider": "slack", "slack": {"webhook_env": "TEAM_SLACK_HOOK"}}}
        )
        self.assertEqual(policy.webhook_env, "TEAM_SLACK_HOOK")

    def test_a_top_level_webhook_env_still_works(self) -> None:
        """向後相容：舊設定把 webhook_env 寫在最上層。"""
        policy = load_alert_policy(
            {"release_alerts": {"provider": "google_chat", "webhook_env": "LEGACY_VAR"}}
        )
        self.assertEqual(policy.webhook_env, "LEGACY_VAR")

    def test_google_chat_config_is_unchanged(self) -> None:
        policy = load_alert_policy(
            {
                "release_alerts": {
                    "google_chat": {"webhook_env": "GC_VAR", "use_threads": False},
                }
            }
        )
        self.assertEqual(policy.provider, "google_chat")
        self.assertEqual(policy.webhook_env, "GC_VAR")
        self.assertFalse(policy.use_threads)

    def test_an_unknown_provider_falls_back_to_the_legacy_default_env(self) -> None:
        policy = load_alert_policy({"release_alerts": {"provider": "carrier_pigeon"}})
        self.assertEqual(policy.webhook_env, "GOOGLE_CHAT_WEBHOOK_URL")


class TestDeliverySemanticsAreIdenticalAcrossProviders(unittest.TestCase):
    """同一組投遞情境套在每一個 provider 上。"""

    def test_a_2xx_is_delivered(self) -> None:
        for name in supported_providers():
            for code in (200, 201, 202, 204):
                with self.subTest(provider=name, code=code):
                    provider, session = build(name, [FakeResponse(code, "ok")])
                    res = provider.send(make_alert())
                    self.assertEqual(res.status, "sent", f"{name} 把 {code} 當成失敗")
                    self.assertEqual(res.attempt_count, 1)
                    self.assertEqual(res.http_status, code)
                    self.assertEqual(len(session.calls), 1, "成功卻重送了")

    def test_429_is_retried_then_succeeds(self) -> None:
        for name in supported_providers():
            with self.subTest(provider=name):
                provider, session = build(
                    name, [FakeResponse(429, "slow down"), FakeResponse(200, "ok")]
                )
                res = provider.send(make_alert())
                self.assertEqual(res.status, "sent")
                self.assertEqual(res.attempt_count, 2)
                self.assertEqual(len(session.calls), 2)

    def test_5xx_retries_until_exhausted(self) -> None:
        for name in supported_providers():
            with self.subTest(provider=name):
                provider, session = build(
                    name, [FakeResponse(500, "boom")] * 3, max_attempts=3
                )
                res = provider.send(make_alert())
                self.assertEqual(res.status, "failed")
                self.assertEqual(res.attempt_count, 3)
                self.assertEqual(res.error_code, "HTTP_500")
                self.assertEqual(len(session.calls), 3)

    def test_a_permanent_4xx_fails_fast(self) -> None:
        for name in supported_providers():
            with self.subTest(provider=name):
                provider, session = build(name, [FakeResponse(401, "unauthorized")])
                res = provider.send(make_alert())
                self.assertEqual(res.status, "failed")
                self.assertEqual(res.attempt_count, 1, "永久性錯誤不該重試")
                self.assertEqual(res.error_code, "HTTP_401")
                self.assertEqual(len(session.calls), 1)

    def test_a_missing_url_names_the_environment_variable(self) -> None:
        for name in supported_providers():
            with self.subTest(provider=name):
                provider = PROVIDER_CLASSES[name](webhook_env="DEFINITELY_NOT_SET_XYZ")
                res = provider.send(make_alert())
                self.assertEqual(res.status, "failed")
                self.assertEqual(res.error_code, "MISSING_WEBHOOK_URL")
                self.assertIn("DEFINITELY_NOT_SET_XYZ", res.error_message or "")

    def test_an_unexpected_2xx_is_never_retried(self) -> None:
        """對方已經收下的通知不得被重送——那會變成重複告警。"""
        for name in supported_providers():
            with self.subTest(provider=name):
                provider, session = build(name, [FakeResponse(207, "multi-status")])
                res = provider.send(make_alert())
                self.assertEqual(res.status, "sent")
                self.assertEqual(len(session.calls), 1)


class TestSecretsNeverReachTheAuditTrail(unittest.TestCase):
    """webhook URL 本身就是機密，錯誤訊息會被寫進 SQLite 稽核表。"""

    def test_no_provider_leaks_its_url_in_an_error_message(self) -> None:
        for name in supported_providers():
            with self.subTest(provider=name):
                echoed = f"upstream rejected request to {SECRET_URLS[name]}"
                provider, _ = build(name, [FakeResponse(403, echoed)])
                res = provider.send(make_alert())
                message = res.error_message or ""
                self.assertTrue(message, "錯誤訊息不該是空的")
                self.assertNotIn(SECRET_URLS[name], message)
                for fragment in SECRET_FRAGMENTS[name]:
                    self.assertNotIn(fragment, message, f"{name} 外洩了 {fragment}")
                self.assertIn("redacted", message)

    def test_network_exception_text_is_redacted_too(self) -> None:
        import requests

        class ExplodingSession(FakeSession):
            def post(self, url: str, **kwargs: Any) -> FakeResponse:
                raise requests.exceptions.ConnectionError(f"failed connecting to {url}")

        for name in supported_providers():
            with self.subTest(provider=name):
                provider = PROVIDER_CLASSES[name](
                    webhook_url=SECRET_URLS[name],
                    session=ExplodingSession([]),
                    sleep_fn=lambda _s: None,
                    backoff_sec=0.0,
                    max_attempts=1,
                )
                res = provider.send(make_alert())
                self.assertEqual(res.error_code, "NETWORK_ERROR")
                for fragment in SECRET_FRAGMENTS[name]:
                    self.assertNotIn(fragment, res.error_message or "")


class TestChannelSpecificBehaviour(unittest.TestCase):
    def test_slack_and_teams_do_not_claim_a_thread_they_cannot_use(self) -> None:
        """在稽核裡記一個沒用到的 thread key 就是假紀錄。"""
        for cls in (SlackWebhookProvider, MicrosoftTeamsWebhookProvider, GenericWebhookProvider):
            with self.subTest(provider=cls.provider_name):
                session = FakeSession([FakeResponse(200, "ok")])
                provider = cls(webhook_url=SECRET_URLS[cls.provider_name], session=session)
                res = provider.send(make_alert(thread_key="shop_app:android:3.2.0"))
                self.assertEqual(res.status, "sent")
                self.assertIsNone(res.thread_key)
                self.assertIsNone(session.calls[0]["params"], "不支援 thread 卻送了 thread 參數")

    def test_the_im_payloads_carry_no_thread_instruction(self) -> None:
        """Slack / Teams 的 body 只有純文字；generic 的 envelope 另有一條測試。"""
        for cls in (SlackWebhookProvider, MicrosoftTeamsWebhookProvider):
            with self.subTest(provider=cls.provider_name):
                session = FakeSession([FakeResponse(200, "ok")])
                cls(webhook_url=SECRET_URLS[cls.provider_name], session=session).send(make_alert())
                self.assertNotIn("thread", json.dumps(session.calls[0]["json"]).lower())

    def test_google_chat_keeps_its_thread_and_fallback(self) -> None:
        session = FakeSession([FakeResponse(400, "bad thread"), FakeResponse(200, "ok")])
        provider = GoogleChatWebhookProvider(
            webhook_url=SECRET_URLS["google_chat"], session=session
        )
        res = provider.send(make_alert())
        self.assertEqual(res.status, "sent")
        self.assertIsNotNone(session.calls[0]["params"])
        self.assertIn("thread", session.calls[0]["json"])
        self.assertNotIn("thread", session.calls[1]["json"], "fallback 仍帶著 thread")
        self.assertIsNone(res.thread_key, "fallback 沒有用到 thread，不該記進稽核")

    def test_only_google_chat_defines_a_400_fallback(self) -> None:
        """fallback 是 Google Chat thread 失效的補救，不是通用行為。"""
        alert = make_alert()
        for name, cls in PROVIDER_CLASSES.items():
            with self.subTest(provider=name):
                provider = cls(webhook_url=SECRET_URLS[name])
                expected_none = cls is not GoogleChatWebhookProvider
                self.assertEqual(provider.fallback_payload(alert) is None, expected_none)

    def test_slack_and_teams_send_the_prerendered_text(self) -> None:
        for cls in (SlackWebhookProvider, MicrosoftTeamsWebhookProvider):
            with self.subTest(provider=cls.provider_name):
                session = FakeSession([FakeResponse(200, "ok")])
                cls(webhook_url=SECRET_URLS[cls.provider_name], session=session).send(make_alert())
                self.assertEqual(session.calls[0]["json"], {"text": make_alert().text})


class TestGenericWebhookEnvelope(unittest.TestCase):
    def test_the_envelope_carries_every_alert_field(self) -> None:
        """欄位集合由 AlertMessage 產生，因此不會出現「新增欄位忘了同步」。"""
        alert = make_alert()
        session = FakeSession([FakeResponse(200, "ok")])
        GenericWebhookProvider(webhook_url=SECRET_URLS["webhook"], session=session).send(alert)
        body = session.calls[0]["json"]
        self.assertEqual(set(body), {f.name for f in dataclasses.fields(AlertMessage)})

    def test_the_envelope_is_json_serialisable_and_keeps_the_decision_fields(self) -> None:
        alert = make_alert()
        session = FakeSession([FakeResponse(200, "ok")])
        GenericWebhookProvider(webhook_url=SECRET_URLS["webhook"], session=session).send(alert)
        body = session.calls[0]["json"]
        json.dumps(body)  # 不可序列化就直接炸在這裡
        self.assertEqual(body["decision_action"], alert.decision_action)
        self.assertEqual(body["recommendation"], alert.recommendation)
        self.assertEqual(body["regression_reasons"], alert.regression_reasons)

    def test_it_is_not_just_a_text_field(self) -> None:
        """自家接收端要能自己判斷；只給一段排好版的文字等於什麼都沒給。"""
        session = FakeSession([FakeResponse(200, "ok")])
        GenericWebhookProvider(webhook_url=SECRET_URLS["webhook"], session=session).send(make_alert())
        self.assertNotEqual(set(session.calls[0]["json"]), {"text"})


class TestRetryLogicExistsOnlyOnce(unittest.TestCase):
    def test_no_provider_module_implements_its_own_retry_loop(self) -> None:
        """機械掃描：退避重試只能出現在共用核心裡。

        沒有這條，新增第五個通道時只要照著 Google Chat 複製一份，就會多出一套
        「什麼算暫時性失敗」的判斷——而那種分歧只在對方真的回 429 的那天才會現形。
        """
        providers_dir = ROOT / "crash_trend" / "alerts" / "providers"
        offenders: list[str] = []
        for path in sorted(providers_dir.glob("*.py")):
            if path.name == "webhook.py":
                continue
            for idx, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                line = raw.split("#", 1)[0]
                if "sleep_fn(" in line or "backoff_sec *" in line or "range(1, self.max_attempts" in line:
                    offenders.append(f"{path.name}:{idx}: {raw.strip()}")
        self.assertEqual(offenders, [], "以下 provider 自己實作了重試：\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
