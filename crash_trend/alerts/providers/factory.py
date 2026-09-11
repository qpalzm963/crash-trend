"""依 AlertPolicy 建構 provider（Issue #66 項目 4）。

dispatcher 原本寫死 `if policy.provider == "google_chat"`，於是「支援哪些通道」散在
條件式裡。現在通道清單只有 `registry.PROVIDER_CLASSES` 一份，dispatcher 不需要知道
任何 provider 的名字。

未知的 provider 名回 `None` 而不是拋例外——#59 的既有設計是「投遞失敗不得影響品質
閘門的判定」，因此設定打錯字要走稽核紀錄（帶著支援清單的錯誤訊息），而不是讓整支
pipeline 掛掉。
"""

from __future__ import annotations

import requests

from crash_trend.alerts.policy import AlertPolicy
from crash_trend.alerts.providers.google_chat import GoogleChatWebhookProvider
from crash_trend.alerts.providers.registry import provider_class
from crash_trend.alerts.providers.webhook import WebhookDeliveryProvider


def build_provider(
    policy: AlertPolicy,
    session: requests.Session | None = None,
) -> WebhookDeliveryProvider | None:
    """依 policy 建立 provider；名稱不認識時回 `None`。

    `webhook_env` 只有在與該 provider 的預設值不同時才傳進去——否則把 Google Chat 的
    環境變數名帶到 Slack 上，會在「設定看起來沒錯」的情況下永遠讀不到 URL。
    """
    cls = provider_class(policy.provider)
    if cls is None:
        return None
    kwargs: dict[str, object] = {"session": session}
    if policy.webhook_env and policy.webhook_env != cls.default_webhook_env:
        kwargs["webhook_env"] = policy.webhook_env
    if cls is GoogleChatWebhookProvider:
        kwargs["use_threads"] = policy.use_threads
    return cls(**kwargs)  # type: ignore[arg-type]
