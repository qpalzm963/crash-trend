"""Google Chat Quality Alerts Delivery package (Issue #59).

Provides:
- AlertMessage, DeliveryResult, DeliveryRecord, DispatchDecision, AlertDispatchSummary
- AlertPolicy, load_alert_policy, compute_alert_fingerprint
- AlertDeliveryStore
- AlertProvider, GoogleChatWebhookProvider
- AlertDispatcher, dispatch_alerts_for_app, build_alert_message, evaluate_alert_decision
"""

from __future__ import annotations

from crash_trend.alerts.dispatcher import (
    AlertDispatcher,
    build_alert_message,
    dispatch_alerts_for_app,
    evaluate_alert_decision,
)
from crash_trend.alerts.models import (
    AlertDispatchSummary,
    AlertMessage,
    DeliveryRecord,
    DeliveryResult,
    DispatchDecision,
)
from crash_trend.alerts.policy import (
    AlertPolicy,
    compute_alert_fingerprint,
    load_alert_policy,
)
from crash_trend.alerts.providers import AlertProvider
from crash_trend.alerts.providers.google_chat import (
    GoogleChatWebhookProvider,
    redact_url,
)
from crash_trend.alerts.state import (
    AlertDeliveryStore,
    AlertStoreError,
    sanitize_audit_text,
)

__all__ = [
    "AlertDispatchSummary",
    "AlertDispatcher",
    "AlertMessage",
    "AlertPolicy",
    "AlertProvider",
    "AlertStoreError",
    "AlertDeliveryStore",
    "DeliveryRecord",
    "DeliveryResult",
    "DispatchDecision",
    "GoogleChatWebhookProvider",
    "build_alert_message",
    "compute_alert_fingerprint",
    "dispatch_alerts_for_app",
    "evaluate_alert_decision",
    "load_alert_policy",
    "redact_url",
    "sanitize_audit_text",
]
