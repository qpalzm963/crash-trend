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
from crash_trend.alerts.observability import (
    AlertDeliveryHealth,
    AlertDeliveryItem,
    AlertHealthStatus,
    get_alert_observability_bundle,
    get_latest_release_delivery,
    get_recent_alert_deliveries,
    get_release_alert_history,
    record_to_delivery_item,
    summarize_alert_delivery_health,
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
    "AlertDeliveryHealth",
    "AlertDeliveryItem",
    "AlertDeliveryStore",
    "AlertDispatchSummary",
    "AlertDispatcher",
    "AlertHealthStatus",
    "AlertMessage",
    "AlertPolicy",
    "AlertProvider",
    "AlertStoreError",
    "DeliveryRecord",
    "DeliveryResult",
    "DispatchDecision",
    "GoogleChatWebhookProvider",
    "build_alert_message",
    "compute_alert_fingerprint",
    "dispatch_alerts_for_app",
    "evaluate_alert_decision",
    "get_alert_observability_bundle",
    "get_latest_release_delivery",
    "get_recent_alert_deliveries",
    "get_release_alert_history",
    "load_alert_policy",
    "record_to_delivery_item",
    "redact_url",
    "sanitize_audit_text",
    "summarize_alert_delivery_health",
]

