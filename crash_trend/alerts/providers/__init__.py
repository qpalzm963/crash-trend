"""Alert provider protocol interface and implementations (Issue #59, #66).

具體實作見 `webhook.py`（共用投遞核心）、`google_chat.py` / `slack.py` /
`microsoft_teams.py` / `generic.py`（各通道的差異），以及 `registry.py`（名稱對應表）
與 `factory.py`（依 policy 建構）。
"""

from __future__ import annotations

from typing import Protocol

from crash_trend.alerts.models import AlertMessage, DeliveryResult


class AlertProvider(Protocol):
    """Protocol for downstream alert delivery providers."""

    def send(self, alert: AlertMessage) -> DeliveryResult:
        """Dispatches the alert message to the destination service."""
        ...
