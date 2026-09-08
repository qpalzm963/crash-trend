"""Google Chat Incoming Webhook alert provider (Issue #59).

Implements:
- Secret injection strictly from environment
- Finite bounded retries with exponential backoff on transient errors (429, 5xx, timeouts)
- Fast failure on permanent 4xx errors (400, 401, 403, 404)
- Thread key support and Space thread fallback
- Redaction of webhook tokens and full URLs in logs/exceptions
"""

from __future__ import annotations

import datetime as dt
import os
import re
import time
from collections.abc import Callable
from typing import Any

import requests

from crash_trend.alerts.models import AlertMessage, DeliveryResult


def redact_url(text: str | None, raw_url: str | None = None) -> str:
    """Sanitizes Google Chat webhook URLs and credentials in strings."""
    if not text:
        return ""
    cleaned = str(text)
    if raw_url:
        cleaned = cleaned.replace(raw_url, "https://chat.googleapis.com/...<redacted>")
    # Match any Google Chat webhook URL pattern
    cleaned = re.sub(
        r"https://chat\.googleapis\.com/[^\s'\"<>]+",
        "https://chat.googleapis.com/...<redacted>",
        cleaned,
    )
    # Scrub common secret / token / key query params
    cleaned = re.sub(r"([?&](?:key|token|access_token|secret)=)[^&\s'\"]+", r"\1<redacted>", cleaned)
    return cleaned


class GoogleChatWebhookProvider:
    """Delivers AlertMessage to Google Chat Space via Incoming Webhook."""

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
        self._explicit_url = webhook_url
        self.webhook_env = webhook_env
        self.use_threads = use_threads
        self.max_attempts = max(1, max_attempts)
        self.timeout = timeout
        self.backoff_sec = max(0.0, backoff_sec)
        self.sleep_fn = sleep_fn
        self._session = session

    def resolve_webhook_url(self) -> str | None:
        """Resolves webhook URL from explicit parameter or environment."""
        if self._explicit_url:
            return self._explicit_url.strip()
        val = os.environ.get(self.webhook_env)
        return val.strip() if val else None

    def build_payload(self, alert: AlertMessage, include_thread: bool = True) -> dict[str, Any]:
        """Formats the JSON body for the Google Chat webhook."""
        payload: dict[str, Any] = {"text": alert.text}
        if include_thread and self.use_threads and alert.thread_key:
            payload["thread"] = {"threadKey": alert.thread_key}
        return payload

    def send(self, alert: AlertMessage) -> DeliveryResult:
        """Sends alert message to Google Chat Space with bounded retries and backoff."""
        url = self.resolve_webhook_url()
        if not url:
            return DeliveryResult(
                status="failed",
                attempt_count=0,
                error_code="MISSING_WEBHOOK_URL",
                error_message=f"Environment variable '{self.webhook_env}' is not set or empty.",
            )

        session = self._session or requests.Session()

        include_thread = bool(self.use_threads and alert.thread_key)
        payload = self.build_payload(alert, include_thread=include_thread)
        params: dict[str, str] | None = None
        if include_thread and alert.thread_key:
            params = {
                "threadKey": alert.thread_key,
                "messageReplyOption": "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
            }

        last_error_code: str | None = None
        last_error_msg: str | None = None
        last_status_code: int | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                res = session.post(
                    url,
                    json=payload,
                    params=params,
                    timeout=self.timeout,
                )
                last_status_code = res.status_code

                if res.status_code in (200, 201):
                    now_iso = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                    msg_name: str | None = None
                    try:
                        data = res.json()
                        msg_name = data.get("name")
                    except Exception:
                        pass
                    return DeliveryResult(
                        status="sent",
                        attempt_count=attempt,
                        delivered_at=now_iso,
                        http_status=res.status_code,
                        thread_key=alert.thread_key if include_thread else None,
                        message_name=msg_name,
                    )

                # Thread fallback on 400 Bad Request
                if res.status_code == 400 and include_thread:
                    try:
                        fb_res = session.post(
                            url,
                            json={"text": alert.text},
                            timeout=self.timeout,
                        )
                        if fb_res.status_code in (200, 201):
                            now_iso = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                            msg_name = None
                            try:
                                msg_name = fb_res.json().get("name")
                            except Exception:
                                pass
                            return DeliveryResult(
                                status="sent",
                                attempt_count=attempt + 1,
                                delivered_at=now_iso,
                                http_status=fb_res.status_code,
                                thread_key=None,
                                message_name=msg_name,
                            )
                    except Exception:
                        pass

                # Non-transient 4xx permanent error -> fast fail
                if 400 <= res.status_code < 500 and res.status_code != 429:
                    return DeliveryResult(
                        status="failed",
                        attempt_count=attempt,
                        http_status=res.status_code,
                        error_code=f"HTTP_{res.status_code}",
                        error_message=redact_url(res.text[:300], raw_url=url),
                    )

                # Transient 429 or 5xx
                last_error_code = f"HTTP_{res.status_code}"
                last_error_msg = redact_url(res.text[:300], raw_url=url)

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.RequestException) as exc:
                last_error_code = "NETWORK_ERROR"
                last_error_msg = redact_url(str(exc), raw_url=url)
            except Exception as exc:
                return DeliveryResult(
                    status="failed",
                    attempt_count=attempt,
                    error_code="UNEXPECTED_ERROR",
                    error_message=redact_url(str(exc), raw_url=url),
                )

            # Retry transient failures with exponential backoff
            if attempt < self.max_attempts:
                backoff = self.backoff_sec * (2 ** (attempt - 1))
                if backoff > 0:
                    self.sleep_fn(backoff)

        return DeliveryResult(
            status="failed",
            attempt_count=self.max_attempts,
            http_status=last_status_code,
            error_code=last_error_code or "RETRY_EXHAUSTED",
            error_message=last_error_msg or "Max delivery retry attempts reached.",
        )
