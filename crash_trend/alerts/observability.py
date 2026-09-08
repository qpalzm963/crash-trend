"""Alert Delivery Observability Projection and Query Layer (Issue #63).

Provides read-only query APIs, data projection models, and deterministic health
summarization over the AlertDeliveryStore. Enforces strict zero-secret and zero-raw-UUID
constraints. Does not mutate deduplication or cooldown states.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path
from typing import Any, Literal

from crash_trend.alerts.models import DeliveryRecord
from crash_trend.alerts.state import AlertDeliveryStore, sanitize_audit_text
from crash_trend.config import out_dir

AlertHealthStatus = Literal["healthy", "degraded", "no_data"]


@dataclasses.dataclass(frozen=True)
class AlertDeliveryItem:
    """Dashboard-safe projected alert delivery record."""

    id: int
    provider: str
    platform: str
    version: str
    status: str  # sent, failed, suppressed, pending
    gate_status: str
    attempted_at: str
    delivered_at: str | None
    attempt_count: int
    http_status: int | None
    error_code: str | None
    error_message: str | None
    suppression_reason: str | None
    thread_key: str | None
    message_name: str | None
    reasons: list[str]
    dry_run: bool
    is_recovery: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "platform": self.platform,
            "version": self.version,
            "status": self.status,
            "gate_status": self.gate_status,
            "attempted_at": self.attempted_at,
            "delivered_at": self.delivered_at,
            "attempt_count": self.attempt_count,
            "http_status": self.http_status,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "suppression_reason": self.suppression_reason,
            "thread_key": self.thread_key,
            "message_name": self.message_name,
            "reasons": list(self.reasons),
            "dry_run": self.dry_run,
            "is_recovery": self.is_recovery,
        }


@dataclasses.dataclass(frozen=True)
class AlertDeliveryHealth:
    """Aggregated health statistics for alert delivery."""

    status: AlertHealthStatus
    provider: str
    sent_24h: int
    failed_24h: int
    suppressed_24h: int
    latest_success_at: str | None
    latest_failure_at: str | None
    unresolved_failures: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider": self.provider,
            "sent_24h": self.sent_24h,
            "failed_24h": self.failed_24h,
            "suppressed_24h": self.suppressed_24h,
            "latest_success_at": self.latest_success_at,
            "latest_failure_at": self.latest_failure_at,
            "unresolved_failures": self.unresolved_failures,
        }


def record_to_delivery_item(record: DeliveryRecord) -> AlertDeliveryItem:
    """Projects a raw SQLite DeliveryRecord into a safe AlertDeliveryItem.

    Sanitizes webhook URLs/tokens defensively and derives recovery/suppression markers.
    """
    clean_err = sanitize_audit_text(record.error_message)
    clean_thread = sanitize_audit_text(record.thread_key)
    st = record.status.strip().lower()

    if st == "suppressed":
        suppression_reason = clean_err
        err_msg = None
    else:
        suppression_reason = None
        err_msg = clean_err

    # Recovery marker: gate status pass or recovery mentioned in message
    is_rec = (record.gate_status.strip().lower() == "pass") or (
        "recover" in (clean_err or "").lower()
    )

    return AlertDeliveryItem(
        id=record.id,
        provider=record.provider,
        platform=record.platform,
        version=record.version,
        status=st,
        gate_status=record.gate_status.strip().lower(),
        attempted_at=record.attempted_at,
        delivered_at=record.delivered_at,
        attempt_count=record.attempt_count,
        http_status=record.http_status,
        error_code=record.error_code,
        error_message=err_msg,
        suppression_reason=suppression_reason,
        thread_key=clean_thread,
        message_name=record.message_name,
        reasons=list(record.reasons),
        dry_run=record.dry_run,
        is_recovery=is_rec,
    )


def _resolve_store(
    app_id: str,
    store: AlertDeliveryStore | None = None,
    custom_path: Path | None = None,
) -> tuple[AlertDeliveryStore | None, bool]:
    """Resolves or opens an AlertDeliveryStore safely.

    Returns:
        (store_instance, should_close_when_done)
    """
    if store is not None:
        return store, False

    db_path = custom_path or (out_dir(app_id) / "alert_delivery.sqlite3")
    if not db_path.is_file():
        return None, False

    try:
        s = AlertDeliveryStore(db_path, app_id=app_id)
        return s, True
    except Exception:
        return None, False


def get_recent_alert_deliveries(
    app_id: str,
    platform: str | None = None,
    version: str | None = None,
    limit: int = 50,
    store: AlertDeliveryStore | None = None,
    custom_path: Path | None = None,
) -> list[AlertDeliveryItem]:
    """Retrieves recent delivery records for an app, ordered latest first."""
    s, should_close = _resolve_store(app_id, store=store, custom_path=custom_path)
    if s is None:
        return []

    try:
        records = s.get_history(
            app_id=app_id,
            platform=platform,
            version=version,
            limit=limit,
        )
        return [record_to_delivery_item(r) for r in records]
    finally:
        if should_close:
            s.close()


def get_release_alert_history(
    app_id: str,
    platform: str,
    version: str,
    limit: int = 20,
    store: AlertDeliveryStore | None = None,
    custom_path: Path | None = None,
) -> list[AlertDeliveryItem]:
    """Retrieves delivery records for a specific release version and platform."""
    s, should_close = _resolve_store(app_id, store=store, custom_path=custom_path)
    if s is None:
        return []

    try:
        records = s.get_history(
            app_id=app_id,
            platform=platform,
            version=version,
            limit=limit,
        )
        return [record_to_delivery_item(r) for r in records]
    finally:
        if should_close:
            s.close()


def get_latest_release_delivery(
    app_id: str,
    platform: str,
    version: str,
    store: AlertDeliveryStore | None = None,
    custom_path: Path | None = None,
) -> AlertDeliveryItem | None:
    """Returns the most recent delivery record for a release."""
    history = get_release_alert_history(
        app_id=app_id,
        platform=platform,
        version=version,
        limit=1,
        store=store,
        custom_path=custom_path,
    )
    return history[0] if history else None


def summarize_alert_delivery_health(
    app_id: str,
    provider: str = "google_chat",
    store: AlertDeliveryStore | None = None,
    now: dt.datetime | None = None,
    custom_path: Path | None = None,
) -> AlertDeliveryHealth:
    """Computes transparent, deterministic delivery health metrics.

    Health Rules:
    - dry_run records are strictly excluded from all counts and health state.
    - If no non-dry-run delivery attempts ('sent' or 'failed') exist -> 'no_data'.
    - If the latest non-dry-run attempt succeeded ('sent') -> 'healthy'.
    - If the latest non-dry-run attempt failed ('failed') -> 'degraded'.
    - 'suppressed' is a normal gate decision and never degrades health.
    """
    s, should_close = _resolve_store(app_id, store=store, custom_path=custom_path)
    if s is None:
        return AlertDeliveryHealth(
            status="no_data",
            provider=provider,
            sent_24h=0,
            failed_24h=0,
            suppressed_24h=0,
            latest_success_at=None,
            latest_failure_at=None,
            unresolved_failures=0,
        )

    try:
        all_records = s.get_history(app_id=app_id, limit=500)
    finally:
        if should_close:
            s.close()

    # Exclude dry-run records
    real_records = [r for r in all_records if not r.dry_run]
    if not real_records:
        return AlertDeliveryHealth(
            status="no_data",
            provider=provider,
            sent_24h=0,
            failed_24h=0,
            suppressed_24h=0,
            latest_success_at=None,
            latest_failure_at=None,
            unresolved_failures=0,
        )

    now_dt = now or dt.datetime.now(dt.UTC)
    cutoff_24h_iso = (now_dt - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 24-hour counts
    sent_24h = 0
    failed_24h = 0
    suppressed_24h = 0
    for r in real_records:
        if r.attempted_at >= cutoff_24h_iso:
            st = r.status.strip().lower()
            if st == "sent":
                sent_24h += 1
            elif st == "failed":
                failed_24h += 1
            elif st == "suppressed":
                suppressed_24h += 1

    # Find latest success and latest failure across all records
    latest_success_at: str | None = None
    latest_failure_at: str | None = None
    for r in real_records:
        st = r.status.strip().lower()
        if st == "sent" and latest_success_at is None:
            latest_success_at = r.delivered_at or r.attempted_at
        elif st == "failed" and latest_failure_at is None:
            latest_failure_at = r.attempted_at
        if latest_success_at and latest_failure_at:
            break

    # Determine status among real delivery attempts ('sent' or 'failed')
    real_attempts = [r for r in real_records if r.status.strip().lower() in ("sent", "failed")]
    if not real_attempts:
        health_status: AlertHealthStatus = "no_data"
        unresolved_failures = 0
    else:
        latest_attempt = real_attempts[0]
        if latest_attempt.status.strip().lower() == "sent":
            health_status = "healthy"
            unresolved_failures = 0
        else:
            health_status = "degraded"
            # Count consecutive failures since latest success
            unresolved_failures = 0
            for r in real_attempts:
                if r.status.strip().lower() == "failed":
                    unresolved_failures += 1
                else:
                    break

    return AlertDeliveryHealth(
        status=health_status,
        provider=provider,
        sent_24h=sent_24h,
        failed_24h=failed_24h,
        suppressed_24h=suppressed_24h,
        latest_success_at=latest_success_at,
        latest_failure_at=latest_failure_at,
        unresolved_failures=unresolved_failures,
    )


def get_alert_observability_bundle(
    app_id: str,
    provider: str = "google_chat",
    store: AlertDeliveryStore | None = None,
    now: dt.datetime | None = None,
    custom_path: Path | None = None,
) -> dict[str, Any]:
    """Assembles a complete Dashboard-safe alert delivery observability payload."""
    health = summarize_alert_delivery_health(
        app_id=app_id,
        provider=provider,
        store=store,
        now=now,
        custom_path=custom_path,
    )
    recent = get_recent_alert_deliveries(
        app_id=app_id,
        limit=50,
        store=store,
        custom_path=custom_path,
    )

    return {
        "provider": provider,
        "health": health.to_dict(),
        "recent": [item.to_dict() for item in recent],
    }
