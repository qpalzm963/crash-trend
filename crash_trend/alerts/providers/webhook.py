"""Incoming-webhook 投遞的共用核心（重試、退避、錯誤分類、機密遮蔽）。

各家 IM 的 incoming webhook 差別其實只有三件事：URL 從哪個環境變數讀、JSON body
長什麼樣、以及機密長在 URL 的哪一段。**成功判定、重試與錯誤分類完全相同**，因此
那段邏輯只能有一份：四個 provider 各自抄一份 retry loop，等於四套「什麼算暫時性
失敗」的判斷，而其中三套永遠不會被人讀。

共用的投遞語意（原本只實作在 Google Chat provider 裡，現在是所有 provider 的合約）：

* 暫時性失敗（429 / 5xx / 連線與逾時錯誤）→ 有限次數的指數退避重試。
* 永久性失敗（其餘 4xx）→ 立刻失敗，不浪費重試次數。
* 缺 webhook URL → `MISSING_WEBHOOK_URL`，並指名該設哪個環境變數。
* 任何錯誤訊息與例外字串在寫進 DeliveryResult 之前都會先過遮蔽——webhook URL
  本身就是機密（Slack / Teams / Google Chat 的權杖都在 URL 裡）。
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

#: 常見的機密 query 參數名（Google Chat 的 key/token 就在 query 裡）。
_SECRET_QUERY_RE = re.compile(r"([?&](?:key|token|access_token|secret|sig|signature)=)[^&\s'\"]+")


def redact_secrets(text: str | None, raw_url: str | None = None, host_pattern: str | None = None) -> str:
    """遮蔽 webhook URL 與常見機密 query 參數。

    `raw_url` 是本次實際使用的 URL（逐字替換）；`host_pattern` 讓各 provider 再補一條
    自家 host 的正則，攔下「錯誤訊息裡回吐了一個我們沒傳過的 URL」這種情況。
    """
    if not text:
        return ""
    cleaned = str(text)
    if raw_url:
        cleaned = cleaned.replace(raw_url, _redacted_form(raw_url))
    if host_pattern:
        cleaned = re.sub(host_pattern, lambda m: _redacted_form(m.group(0)), cleaned)
    return _SECRET_QUERY_RE.sub(r"\1<redacted>", cleaned)


def _is_success(status_code: int) -> bool:
    """任何 2xx 都算投遞成功。

    incoming webhook 能給的唯一訊號就是「端點收下了這個請求」：Google Chat 回 200、
    Slack 回 200、Teams 的傳統 connector 回 200 而 Power Automate 回 202、自家接收端
    常回 204。維護一份 per-provider 的成功碼清單除了會漏（新端點換了狀態碼就變成
    「失敗」），還有一個更糟的後果：**清單外的 2xx 會掉進重試分支**，於是一則對方
    已經收下的通知被重送。因此這裡只判斷區間。
    """
    return 200 <= status_code < 300


def _redacted_form(url: str) -> str:
    """回傳只保留 scheme + host 的遮蔽字串（機密通常在 path 或 query 裡）。"""
    m = re.match(r"(https?://[^/?#\s]+)", url)
    return f"{m.group(1)}/...<redacted>" if m else "<redacted>"


class WebhookDeliveryProvider:
    """Incoming webhook provider 的共用骨架。

    子類別只覆寫「這家服務哪裡不一樣」的幾個 hook；`send()` 只有這一份實作。
    """

    #: 子類別必須覆寫：寫進稽核紀錄的 provider 名（與設定檔的 `provider` 值相同）。
    provider_name = "webhook"
    #: 未指定 `webhook_env` 時的預設環境變數名。
    default_webhook_env = "ALERT_WEBHOOK_URL"
    #: 遮蔽用的自家 host 正則（`None` 表示只靠 raw_url 與 query 參數遮蔽）。
    host_pattern: str | None = None
    #: 這個通道支不支援把通知收攏到討論串。
    #:
    #: 這是 thread 能力的**唯一事實來源**：`AlertMessage.thread_key` 與稽核紀錄都要依它
    #: 決定，而不是只依設定值 `policy.use_threads`。incoming webhook 多數不支援 thread，
    #: 在稽核列裡寫一個沒用到的 thread key 就是假紀錄。
    supports_threads = False

    def __init__(
        self,
        webhook_url: str | None = None,
        webhook_env: str | None = None,
        max_attempts: int = 3,
        timeout: tuple[float, float] | float = (5.0, 10.0),
        backoff_sec: float = 1.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        session: requests.Session | None = None,
    ) -> None:
        self._explicit_url = webhook_url
        self.webhook_env = webhook_env or self.default_webhook_env
        self.max_attempts = max(1, max_attempts)
        self.timeout = timeout
        self.backoff_sec = max(0.0, backoff_sec)
        self.sleep_fn = sleep_fn
        self._session = session

    # ── 子類別可覆寫的 hook ────────────────────────────────────────

    def build_payload(self, alert: AlertMessage) -> dict[str, Any]:
        """這家服務的 JSON body。預設是最通用的 `{"text": ...}`。"""
        return {"text": alert.text}

    def request_params(self, alert: AlertMessage) -> dict[str, str] | None:
        """query 參數（Google Chat 的 threadKey 走這裡）。"""
        return None

    def fallback_payload(self, alert: AlertMessage) -> dict[str, Any] | None:
        """收到 400 時要不要用另一種 body 再試一次（`None` = 不要）。

        存在的唯一理由是 Google Chat 的 thread fallback：threadKey 失效時，寧可送出
        一則沒有 thread 的訊息，也不要讓一次 FAIL 通知整個消失。
        """
        return None

    def delivered_thread_key(self, alert: AlertMessage) -> str | None:
        """成功投遞後要記進稽核的 thread key（不支援 thread 的服務回 `None`）。"""
        return None

    def extract_message_name(self, response: requests.Response) -> str | None:
        """從回應中取出可追溯的訊息 id（沒有就回 `None`）。"""
        return None

    def redact(self, text: str | None, raw_url: str | None = None) -> str:
        return redact_secrets(text, raw_url=raw_url, host_pattern=self.host_pattern)

    # ── 共用實作 ───────────────────────────────────────────────────

    def resolve_webhook_url(self) -> str | None:
        """從建構參數或環境變數解析 webhook URL。機密永遠不進設定檔。"""
        if self._explicit_url:
            return self._explicit_url.strip()
        val = os.environ.get(self.webhook_env)
        return val.strip() if val else None

    def _sent(
        self,
        response: requests.Response,
        attempt: int,
        alert: AlertMessage,
        thread_key: str | None,
    ) -> DeliveryResult:
        return DeliveryResult(
            status="sent",
            attempt_count=attempt,
            delivered_at=dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            http_status=response.status_code,
            thread_key=thread_key,
            message_name=self._safe_message_name(response),
        )

    def _safe_message_name(self, response: requests.Response) -> str | None:
        try:
            return self.extract_message_name(response)
        except Exception:
            return None

    def send(self, alert: AlertMessage) -> DeliveryResult:
        """投遞訊息：暫時性錯誤有限重試，永久性錯誤立刻失敗。"""
        url = self.resolve_webhook_url()
        if not url:
            return DeliveryResult(
                status="failed",
                attempt_count=0,
                error_code="MISSING_WEBHOOK_URL",
                error_message=f"Environment variable '{self.webhook_env}' is not set or empty.",
            )

        session = self._session or requests.Session()
        payload = self.build_payload(alert)
        params = self.request_params(alert)
        thread_key = self.delivered_thread_key(alert)

        last_error_code: str | None = None
        last_error_msg: str | None = None
        last_status_code: int | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                res = session.post(url, json=payload, params=params, timeout=self.timeout)
                last_status_code = res.status_code

                if _is_success(res.status_code):
                    return self._sent(res, attempt, alert, thread_key)

                if res.status_code == 400:
                    fallback = self.fallback_payload(alert)
                    if fallback is not None:
                        try:
                            fb = session.post(url, json=fallback, timeout=self.timeout)
                            if _is_success(fb.status_code):
                                return self._sent(fb, attempt + 1, alert, None)
                        except Exception:
                            pass

                # 其餘 4xx（429 除外）是設定或權限問題，重試只會重複失敗。
                if 400 <= res.status_code < 500 and res.status_code != 429:
                    return DeliveryResult(
                        status="failed",
                        attempt_count=attempt,
                        http_status=res.status_code,
                        error_code=f"HTTP_{res.status_code}",
                        error_message=self.redact(res.text[:300], raw_url=url),
                    )

                # 429 與 5xx：暫時性，重試。
                last_error_code = f"HTTP_{res.status_code}"
                last_error_msg = self.redact(res.text[:300], raw_url=url)

            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.RequestException,
            ) as exc:
                last_error_code = "NETWORK_ERROR"
                last_error_msg = self.redact(str(exc), raw_url=url)
            except Exception as exc:
                return DeliveryResult(
                    status="failed",
                    attempt_count=attempt,
                    error_code="UNEXPECTED_ERROR",
                    error_message=self.redact(str(exc), raw_url=url),
                )

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
