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

    `webhook_env` 只在 policy 真的指定時才傳進去（`None` / 空字串 = 沒指定），其餘情況
    由 provider 套自己的 `default_webhook_env`。

    刻意**不**用「值是否等於某個預設」來推論「有沒有明確指定」：那會把
    `AlertPolicy(provider="slack")`（dataclass 預設）與「使用者真的寫了 Google Chat 的
    變數」混為一談，結果是 Slack provider 讀 Google Chat 的 webhook URL。
    """
    cls = provider_class(policy.provider)
    if cls is None:
        return None
    kwargs: dict[str, object] = {"session": session}
    if policy.webhook_env:
        kwargs["webhook_env"] = policy.webhook_env
    if cls is GoogleChatWebhookProvider:
        kwargs["use_threads"] = policy.use_threads
    return cls(**kwargs)  # type: ignore[arg-type]
