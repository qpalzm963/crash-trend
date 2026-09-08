"""Domain models for Release Quality Alerts Delivery (Issue #59).

Defines strongly-typed messages, delivery outcomes, audit records, and dispatch summaries.
Conforms strictly to zero-raw-identifiers and zero-secret policies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

DeliveryStatus = Literal["sent", "failed", "suppressed"]
DecisionType = Literal["send", "suppressed"]


@dataclass(frozen=True)
class AlertMessage:
    """Pre-rendered, structured alert message ready for delivery to a provider."""

    app_id: str
    platform: str
    target_version: str
    previous_version: str | None
    gate_status: str
    is_recovery: bool
    title: str
    summary: str
    regression_reasons: list[str]
    policy_version: str
    evaluated_at: str
    dashboard_url: str | None
    thread_key: str | None
    text: str


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of an alert delivery attempt by a provider."""

    status: DeliveryStatus
    attempt_count: int
    delivered_at: str | None = None
    http_status: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    thread_key: str | None = None
    message_name: str | None = None


@dataclass(frozen=True)
class DeliveryRecord:
    """Audit record stored in SQLite alert_deliveries table."""

    id: int
    app_id: str
    platform: str
    version: str
    provider: str
    alert_fingerprint: str
    gate_status: str
    attempted_at: str
    delivered_at: str | None
    status: str
    attempt_count: int
    http_status: int | None
    error_code: str | None
    error_message: str | None
    thread_key: str | None
    message_name: str | None
    reasons: list[str] = field(default_factory=list)
    dry_run: bool = False


@dataclass(frozen=True)
class DispatchDecision:
    """Decision on whether to send an alert for a specific platform evaluation."""

    platform: str
    decision: DecisionType
    reason: str
    is_recovery: bool = False
    fingerprint: str = ""


@dataclass(frozen=True)
class AlertDispatchSummary:
    """Aggregate result of alert dispatching for an app."""

    app_id: str
    results: dict[str, DeliveryResult]
    decisions: dict[str, DispatchDecision]
    total_sent: int
    total_suppressed: int
    total_failed: int
