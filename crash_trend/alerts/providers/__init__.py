"""Alert provider protocol interface and implementations (Issue #59)."""

from __future__ import annotations

from typing import Protocol

from crash_trend.alerts.models import AlertMessage, DeliveryResult


class AlertProvider(Protocol):
    """Protocol for downstream alert delivery providers."""

    def send(self, alert: AlertMessage) -> DeliveryResult:
        """Dispatches the alert message to the destination service."""
        ...
