"""Microsoft Teams webhook alert provider (Issue #66 項目 4).

同時支援兩種 Teams 端點，因為它們的成功狀態碼不同：

- 傳統 Office 365 connector（`*.webhook.office.com`）：回 200，body 為 `1`
- Power Automate 的 "When a Teams webhook request is received"
  （`*.logic.azure.com`）：回 202 Accepted

兩者都接受 `{"text": ...}`（connector 會把它當 MessageCard 的 text 欄位），因此 body
沿用基底類別的預設值——刻意不送 Adaptive Card：卡片的 schema 版本在兩種端點上不同，
而通知的內容已經是 Python 端排好的純文字（`AlertMessage.text`），沒有理由再引入一份
只有 Teams 看得懂的排版。
"""

from __future__ import annotations

from crash_trend.alerts.providers.webhook import WebhookDeliveryProvider

TEAMS_HOST_PATTERN = r"https://[^\s'\"<>]*(?:webhook\.office\.com|logic\.azure\.com)[^\s'\"<>]*"


class MicrosoftTeamsWebhookProvider(WebhookDeliveryProvider):
    """Delivers AlertMessage to a Microsoft Teams channel via webhook."""

    provider_name = "microsoft_teams"
    default_webhook_env = "MS_TEAMS_WEBHOOK_URL"
    host_pattern = TEAMS_HOST_PATTERN
