"""Slack Incoming Webhook alert provider (Issue #66 項目 4).

與 Google Chat 的差別只有三件事，其餘（重試、退避、錯誤分類）共用
`providers/webhook.py`：

- body 是 `{"text": ...}`，沒有 thread（Slack incoming webhook 不支援回覆到既有討論串；
  要 thread 必須改用 `chat.postMessage` + bot token，那是另一種授權模型，不在本單）
- 成功只有 200（回應 body 是純文字 `ok`，不是 JSON，因此沒有可追溯的訊息 id）
- 機密在 URL path（`/services/T…/B…/…`），遮蔽時只能保留 host
"""

from __future__ import annotations

from crash_trend.alerts.providers.webhook import WebhookDeliveryProvider

SLACK_HOST_PATTERN = r"https://hooks\.slack\.com/[^\s'\"<>]+"


class SlackWebhookProvider(WebhookDeliveryProvider):
    """Delivers AlertMessage to a Slack channel via Incoming Webhook."""

    provider_name = "slack"
    default_webhook_env = "SLACK_WEBHOOK_URL"
    host_pattern = SLACK_HOST_PATTERN
