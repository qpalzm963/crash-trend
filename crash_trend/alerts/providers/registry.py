"""provider 名稱 → 實作的唯一對應表（Issue #66 項目 4）。

刻意與 `factory.py` 分開：`factory` 需要 `AlertPolicy`，而 `policy` 需要「這個
provider 的預設環境變數是什麼」。把對應表放在不 import policy 的模組裡，兩邊就都能
讀同一份，而不需要各自再寫一張表——第二張表就是兩份預設值開始漂移的起點。
"""

from __future__ import annotations

from crash_trend.alerts.providers.generic import GenericWebhookProvider
from crash_trend.alerts.providers.google_chat import GoogleChatWebhookProvider
from crash_trend.alerts.providers.microsoft_teams import MicrosoftTeamsWebhookProvider
from crash_trend.alerts.providers.slack import SlackWebhookProvider
from crash_trend.alerts.providers.webhook import WebhookDeliveryProvider

#: 設定檔 `release_alerts.provider` 的合法值 → provider 類別。
PROVIDER_CLASSES: dict[str, type[WebhookDeliveryProvider]] = {
    GoogleChatWebhookProvider.provider_name: GoogleChatWebhookProvider,
    SlackWebhookProvider.provider_name: SlackWebhookProvider,
    MicrosoftTeamsWebhookProvider.provider_name: MicrosoftTeamsWebhookProvider,
    GenericWebhookProvider.provider_name: GenericWebhookProvider,
}

#: 別名：少一個底線或寫成常見簡稱時，不該靜默失敗。
PROVIDER_ALIASES: dict[str, str] = {
    "teams": MicrosoftTeamsWebhookProvider.provider_name,
    "msteams": MicrosoftTeamsWebhookProvider.provider_name,
    "ms_teams": MicrosoftTeamsWebhookProvider.provider_name,
    "http": GenericWebhookProvider.provider_name,
    "http_webhook": GenericWebhookProvider.provider_name,
    "custom": GenericWebhookProvider.provider_name,
    "googlechat": GoogleChatWebhookProvider.provider_name,
}


def supported_providers() -> tuple[str, ...]:
    """回傳可用的 provider 名（排序後，供錯誤訊息與文件使用）。"""
    return tuple(sorted(PROVIDER_CLASSES))


def normalize_provider_name(name: str | None) -> str:
    raw = str(name or "").strip().lower()
    return PROVIDER_ALIASES.get(raw, raw)


def provider_class(name: str | None) -> type[WebhookDeliveryProvider] | None:
    return PROVIDER_CLASSES.get(normalize_provider_name(name))


def provider_supports_threads(name: str | None) -> bool:
    """該 provider 支不支援 thread。名稱不認識時回 `False`。

    認不出來的通道等於沒有投遞路徑，宣稱用了 thread 只會在稽核裡留下假紀錄。
    """
    cls = provider_class(name)
    return bool(cls.supports_threads) if cls is not None else False


def default_webhook_env_for(name: str | None) -> str | None:
    """該 provider 未指定 `webhook_env` 時要讀的環境變數名。"""
    cls = provider_class(name)
    return cls.default_webhook_env if cls is not None else None
