"""Google Chat Incoming Webhook alert provider (Issue #59).

重試、退避、錯誤分類與機密遮蔽已收斂到 `providers/webhook.py`（#66 項目 4）；本檔
只保留 Google Chat 真正不一樣的部分：

- thread 支援（`threadKey` 走 query 參數，並在 400 時退回「不帶 thread」重送一次）
- 從回應取出 `name` 作為可追溯的訊息 id
- `chat.googleapis.com` 的 URL 遮蔽
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

import requests

from crash_trend.alerts.models import AlertMessage
from crash_trend.alerts.providers.webhook import WebhookDeliveryProvider, redact_secrets

#: Google Chat webhook 的 host 正則（錯誤訊息裡回吐 URL 時也要遮）。
GOOGLE_CHAT_HOST_PATTERN = r"https://chat\.googleapis\.com/[^\s'\"<>]+"


def redact_url(text: str | None, raw_url: str | None = None) -> str:
    """Sanitizes Google Chat webhook URLs and credentials in strings."""
    if not text:
        return ""
    cleaned = str(text)
    if raw_url:
        cleaned = cleaned.replace(raw_url, "https://chat.googleapis.com/...<redacted>")
    cleaned = re.sub(
        GOOGLE_CHAT_HOST_PATTERN,
        "https://chat.googleapis.com/...<redacted>",
        cleaned,
    )
    return redact_secrets(cleaned)


class GoogleChatWebhookProvider(WebhookDeliveryProvider):
    """Delivers AlertMessage to Google Chat Space via Incoming Webhook."""

    provider_name = "google_chat"
    default_webhook_env = "GOOGLE_CHAT_WEBHOOK_URL"
    host_pattern = GOOGLE_CHAT_HOST_PATTERN
    supports_threads = True

    def __init__(
        self,
        webhook_url: str | None = None,
        webhook_env: str = "GOOGLE_CHAT_WEBHOOK_URL",
        use_threads: bool = True,
        max_attempts: int = 3,
        timeout: tuple[float, float] | float = (5.0, 10.0),
        backoff_sec: float = 1.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        session: requests.Session | None = None,
    ) -> None:
        super().__init__(
            webhook_url=webhook_url,
            webhook_env=webhook_env,
            max_attempts=max_attempts,
            timeout=timeout,
            backoff_sec=backoff_sec,
            sleep_fn=sleep_fn,
            session=session,
        )
        self.use_threads = use_threads

    def _include_thread(self, alert: AlertMessage) -> bool:
        return bool(self.use_threads and alert.thread_key)

    def build_payload(self, alert: AlertMessage, include_thread: bool = True) -> dict[str, Any]:
        """Formats the JSON body for the Google Chat webhook."""
        payload: dict[str, Any] = {"text": alert.text}
        if include_thread and self.use_threads and alert.thread_key:
            payload["thread"] = {"threadKey": alert.thread_key}
        return payload

    def request_params(self, alert: AlertMessage) -> dict[str, str] | None:
        if not self._include_thread(alert):
            return None
        return {
            "threadKey": alert.thread_key or "",
            "messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
        }

    def fallback_payload(self, alert: AlertMessage) -> dict[str, Any] | None:
        """threadKey 失效時退回不帶 thread 的訊息。

        寧可送出一則沒有 thread 的通知，也不要讓一次 FAIL 通知整個消失。
        """
        if not self._include_thread(alert):
            return None
        return {"text": alert.text}

    def delivered_thread_key(self, alert: AlertMessage) -> str | None:
        return alert.thread_key if self._include_thread(alert) else None

    def extract_message_name(self, response: requests.Response) -> str | None:
        return response.json().get("name")

    def redact(self, text: str | None, raw_url: str | None = None) -> str:
        return redact_url(text, raw_url=raw_url)
