"""Alert policy configuration and deterministic fingerprinting (Issue #59).

Supports:
- Hierarchical configuration (app-level overriding global release_alerts)
- Safe default values (fail, warn notification; recovery notification; 360m cooldown)
- Deterministic SHA-256 fingerprinting based on (app, platform, version, status, reasons, policy_version)
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass
from typing import Any

from crash_trend.alerts.providers.registry import default_webhook_env_for


@dataclass(frozen=True)
class AlertPolicy:
    """Configurable policy for quality alert notifications."""

    enabled: bool = True
    provider: str = "google_chat"
    notify_on: tuple[str, ...] = ("fail", "warn")
    cooldown_minutes: int = 360
    resend_on_status_change: bool = True
    resend_on_new_reason: bool = True
    notify_recovery: bool = True
    webhook_env: str = "GOOGLE_CHAT_WEBHOOK_URL"
    use_threads: bool = True
    policy_version: str = "1.0"
    dashboard_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serializes policy to dict for diagnostics and testing."""
        return asdict(self)


def compute_alert_fingerprint(
    app_id: str,
    platform: str,
    version: str,
    gate_status: str,
    triggered_reasons: list[str],
    policy_version: str = "1.0",
) -> str:
    """Computes a deterministic SHA-256 fingerprint for deduplication.

    Fingerprint components:
      app_id + platform + version + gate_status + sorted(triggered_reasons) + policy_version
    """
    sorted_reasons = ",".join(sorted(str(r).strip() for r in triggered_reasons))
    raw = f"{app_id.strip()}:{platform.strip()}:{version.strip()}:{gate_status.strip()}:{sorted_reasons}:{policy_version.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_alert_policy(
    app_cfg: dict[str, Any] | None = None,
    global_cfg: dict[str, Any] | None = None,
) -> AlertPolicy:
    """Loads and resolves AlertPolicy from app configuration and global configuration.

    Priority:
      1. app_cfg["release_alerts"]
      2. global_cfg["release_alerts"]
      3. Disabled default if release_alerts is omitted from both
    """
    raw_cfg: dict[str, Any] | None = None
    if isinstance(app_cfg, dict) and "release_alerts" in app_cfg:
        val = app_cfg["release_alerts"]
        if isinstance(val, dict):
            raw_cfg = val
    elif isinstance(global_cfg, dict) and "release_alerts" in global_cfg:
        val = global_cfg["release_alerts"]
        if isinstance(val, dict):
            raw_cfg = val

    if raw_cfg is None:
        # Not configured anywhere -> cleanly disabled
        return AlertPolicy(enabled=False)

    enabled = bool(raw_cfg.get("enabled", True))
    provider = str(raw_cfg.get("provider", "google_chat")).strip().lower()

    # notify_on
    raw_notify = raw_cfg.get("notify_on", ["fail", "warn"])
    if isinstance(raw_notify, (list, tuple)):
        notify_on = tuple(str(x).strip().lower() for x in raw_notify if str(x).strip())
    else:
        notify_on = ("fail", "warn")

    # cooldown_minutes
    try:
        cooldown_minutes = max(0, int(raw_cfg.get("cooldown_minutes", 360)))
    except (TypeError, ValueError):
        cooldown_minutes = 360

    resend_on_status_change = bool(raw_cfg.get("resend_on_status_change", True))
    resend_on_new_reason = bool(raw_cfg.get("resend_on_new_reason", True))
    notify_recovery = bool(raw_cfg.get("notify_recovery", True))

    # Provider 專屬設定。原本只認 `google_chat:` 這個子區塊；現在任何 provider 都能用
    # 同名子區塊（`slack:` / `microsoft_teams:` / `webhook:`），語意與原本一致。
    raw_provider_cfg = raw_cfg.get(provider)
    provider_dict: dict[str, Any] = raw_provider_cfg if isinstance(raw_provider_cfg, dict) else {}

    # 未指定時用**該 provider 自己的**預設環境變數，而不是一律 Google Chat 的那一個：
    # 把 GOOGLE_CHAT_WEBHOOK_URL 帶到 Slack 上，會在「設定看起來沒錯」的情況下永遠
    # 讀不到 URL。provider 名不認識時退回 Google Chat 的預設值（維持既有行為）。
    fallback_env = default_webhook_env_for(provider) or "GOOGLE_CHAT_WEBHOOK_URL"
    webhook_env = str(
        provider_dict.get("webhook_env", raw_cfg.get("webhook_env", fallback_env))
    ).strip()
    use_threads = bool(provider_dict.get("use_threads", raw_cfg.get("use_threads", True)))

    policy_version = str(raw_cfg.get("policy_version", "1.0")).strip()

    # Dashboard URL resolution
    dash_url = raw_cfg.get("dashboard_url")
    if not dash_url and isinstance(app_cfg, dict):
        dash_url = app_cfg.get("dashboard_url")
    if not dash_url:
        dash_url = os.environ.get("DASHBOARD_URL")
    if dash_url:
        dash_url = str(dash_url).strip()
        # Append #<app_id> if not already containing hash anchor
        if isinstance(app_cfg, dict) and "display_name" in app_cfg and "#" not in dash_url:
            # Let caller handle or retain as provided
            pass

    return AlertPolicy(
        enabled=enabled,
        provider=provider,
        notify_on=notify_on,
        cooldown_minutes=cooldown_minutes,
        resend_on_status_change=resend_on_status_change,
        resend_on_new_reason=resend_on_new_reason,
        notify_recovery=notify_recovery,
        webhook_env=webhook_env or fallback_env,
        use_threads=use_threads,
        policy_version=policy_version or "1.0",
        dashboard_url=dash_url or None,
    )
