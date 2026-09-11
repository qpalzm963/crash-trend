"""Custom HTTP webhook alert provider (Issue #66 項目 4).

給「不是 IM、而是自家服務」的接收端：body 不是排版好的文字，而是
`AlertMessage` 的**完整結構化欄位**，讓接收端可以自己判斷要怎麼處理
（轉派、開單、寫進自家儀表板）。

envelope 直接由 `AlertMessage` 的 dataclass 欄位產生，因此不存在「新增欄位忘了同步
envelope」的漂移——有一條測試反向確認兩邊的欄位集合相同。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from crash_trend.alerts.models import AlertMessage
from crash_trend.alerts.providers.webhook import WebhookDeliveryProvider


class GenericWebhookProvider(WebhookDeliveryProvider):
    """Posts the full structured alert to an arbitrary HTTPS endpoint."""

    provider_name = "webhook"
    default_webhook_env = "ALERT_WEBHOOK_URL"
    def build_payload(self, alert: AlertMessage) -> dict[str, Any]:
        return dataclasses.asdict(alert)
