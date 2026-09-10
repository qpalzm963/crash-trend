"""Alert Dispatcher coordinating decision evaluation, delivery, and audit logging (Issue #59).

Features:
- Single delivery ownership (only dispatcher sends alerts)
- Deterministic deduplication, status-change bypass, new-reason bypass, cooldown
- Automatic recovery notifications (fail/warn -> pass)
- Safe dry-run previews without polluting sent state
- ThreadKey support per platform release
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from crash_trend.alerts.models import (
    AlertDispatchSummary,
    AlertMessage,
    DeliveryResult,
    DispatchDecision,
)
from crash_trend.alerts.policy import (
    AlertPolicy,
    compute_alert_fingerprint,
    load_alert_policy,
)
from crash_trend.alerts.providers import AlertProvider
from crash_trend.alerts.providers.google_chat import GoogleChatWebhookProvider
from crash_trend.alerts.state import AlertDeliveryStore
from crash_trend.config import get_app, load_config, out_dir
from crash_trend.gate.artifact import ReleaseGateArtifact, load_release_gate_artifact
from crash_trend.gate.decision import decision_from_gate_result, derive_decision
from crash_trend.schema_v2 import ReleaseDecision

# Presentation-only icon keyed on the gate's canonical alert_severity. This is
# deliberately NOT a gate_status -> wording/action mapping: recommendation,
# action and reasons come exclusively from the Release Decision contract.
_SEVERITY_ICONS: dict[str, str] = {"critical": "🚨", "warning": "⚠️", "none": "ℹ️"}


def build_alert_message(
    app_id: str,
    platform: str,
    target_version: str,
    previous_version: str | None,
    gate_status: str,
    is_recovery: bool,
    rule_results: Sequence[Mapping[str, Any]],
    policy_version: str,
    evaluated_at: str,
    dashboard_url: str | None = None,
    use_threads: bool = True,
    decision: ReleaseDecision | Mapping[str, Any] | None = None,
    alert_severity: str = "none",
) -> AlertMessage:
    """Formats a human-readable AlertMessage from the canonical Release Decision.

    `decision` is the canonical contract carried by the gate artifact (Issue #72).
    When absent (pre-V3.2 artifacts or direct callers) it is derived through the
    same `crash_trend.gate.decision` domain function, never re-implemented here.
    """
    status_lower = gate_status.strip().lower()
    pf_disp = "Android" if platform.lower() == "android" else ("iOS" if platform.lower() == "ios" else platform.capitalize())

    eff_decision: ReleaseDecision | Mapping[str, Any] = (
        decision if decision else derive_decision(status_lower, rule_results)
    )
    recommendation = str(eff_decision.get("recommendation", ""))
    action = str(eff_decision.get("action", ""))
    decision_status = str(eff_decision.get("status", status_lower))

    # Deterministic threadKey: crash-trend:{app_id}:{platform}:{version}
    thread_key = f"crash-trend:{app_id}:{platform.lower()}:{target_version}" if use_threads else None

    if is_recovery:
        title = "✅ Release Gate Recovered"
        summary = f"版本 {target_version} ({pf_disp}) 品質閘門已復原，指標回到安全範圍"
        regression_reasons: list[str] = []
    else:
        icon = _SEVERITY_ICONS.get(str(alert_severity).strip().lower(), "ℹ️")
        title = f"{icon} Release Gate {decision_status.upper()}"
        summary = f"版本 {target_version} ({pf_disp}) 品質閘門 {decision_status.upper()}：{recommendation}"
        regression_reasons = [str(r) for r in (eff_decision.get("reasons") or [])]

    # Build text lines
    lines: list[str] = [
        title,
        "",
        f"App: {app_id}",
        f"Platform: {pf_disp}",
        f"Version: {target_version}",
        f"Previous: {previous_version or 'None'}",
    ]

    if is_recovery:
        lines.extend([
            "",
            "Status: All quality metrics have returned to safe thresholds.",
        ])
    elif regression_reasons:
        lines.extend([
            "",
            "Regression:",
        ])
        for r in regression_reasons:
            lines.append(f"• {r}")

    lines.extend([
        "",
        f"Action: {action}",
        f"Recommendation: {recommendation}",
        "",
        f"Policy: release-gate-v{policy_version}",
        f"Evaluated: {evaluated_at}",
    ])

    if dashboard_url:
        lines.extend([
            "",
            f"Dashboard: {dashboard_url}",
        ])

    text = "\n".join(lines)

    return AlertMessage(
        app_id=app_id,
        platform=platform.lower(),
        target_version=target_version,
        previous_version=previous_version,
        gate_status=status_lower,
        is_recovery=is_recovery,
        title=title,
        summary=summary,
        regression_reasons=regression_reasons,
        policy_version=policy_version,
        evaluated_at=evaluated_at,
        dashboard_url=dashboard_url,
        thread_key=thread_key,
        text=text,
        decision_action=action,
        recommendation=recommendation,
    )


def evaluate_alert_decision(
    app_id: str,
    platform: str,
    target_version: str,
    gate_status: str,
    triggered_reasons: list[str],
    policy: AlertPolicy,
    store: AlertDeliveryStore,
    now: dt.datetime | None = None,
    force: bool = False,
    history_store: Any | None = None,
) -> DispatchDecision:
    """Evaluates whether to send an alert based on notify_on, recovery, dedupe, and cooldown."""
    st = gate_status.strip().lower()
    pf = platform.strip().lower()
    fp = compute_alert_fingerprint(app_id, pf, target_version, st, triggered_reasons, policy.policy_version)

    if not policy.enabled:
        return DispatchDecision(
            platform=pf,
            decision="suppressed",
            reason="Release alerts disabled in configuration (enabled: false)",
            fingerprint=fp,
        )

    last_sent = store.get_last_sent_delivery(app_id, pf, target_version)

    # Gate History Authority recovery check (Issue #61)
    # Tri-state history authority: "known_recovery" | "known_non_recovery" | "unavailable"
    history_authority_state: Literal["known_recovery", "known_non_recovery", "unavailable"] = "unavailable"
    if history_store is not None:
        try:
            history = history_store.get_release_gate_history(app_id, pf, target_version)
            if history:
                if history[-1].gate_status.lower() == st:
                    if len(history) >= 2:
                        prev_snap = history[-2]
                        if prev_snap.gate_status.lower() in ("fail", "warn"):
                            history_authority_state = "known_recovery"
                        else:
                            history_authority_state = "known_non_recovery"
                    else:
                        history_authority_state = "unavailable"
                else:
                    if history[-1].gate_status.lower() in ("fail", "warn"):
                        history_authority_state = "known_recovery"
                    else:
                        history_authority_state = "known_non_recovery"
        except Exception:
            history_authority_state = "unavailable"

    # Force bypasses cooldown and deduplication
    if force:
        if st == "pass" and policy.notify_recovery:
            if history_authority_state == "known_recovery":
                is_rec = True
            elif history_authority_state == "known_non_recovery":
                is_rec = False
            else:
                is_rec = last_sent is not None and last_sent.gate_status in ("fail", "warn")
            if is_rec:
                return DispatchDecision(
                    platform=pf,
                    decision="send",
                    reason="Forced recovery delivery via --force",
                    is_recovery=True,
                    fingerprint=fp,
                )
        if st in policy.notify_on:
            return DispatchDecision(
                platform=pf,
                decision="send",
                reason="Forced delivery via --force",
                is_recovery=False,
                fingerprint=fp,
            )
        return DispatchDecision(
            platform=pf,
            decision="suppressed",
            reason=f"Status '{st}' not configured in notify_on and not a recovery",
            fingerprint=fp,
        )

    # Recovery transition check (warn/fail -> pass aligned with Gate history authority)
    if st == "pass" and policy.notify_recovery:
        if history_authority_state == "known_recovery":
            is_rec_transition = True
        elif history_authority_state == "known_non_recovery":
            is_rec_transition = False
        else:
            # Fallback to delivery store legacy semantics when history store is unavailable, empty, or single-entry
            is_rec_transition = last_sent is not None and last_sent.gate_status in ("fail", "warn")

        if is_rec_transition:
            if last_sent is not None and last_sent.gate_status == "pass":
                return DispatchDecision(
                    platform=pf,
                    decision="suppressed",
                    reason="Recovery alert already delivered for this release",
                    is_recovery=True,
                    fingerprint=fp,
                )

            if last_sent is not None and last_sent.gate_status in ("fail", "warn"):
                align_note = " (aligned with Gate history authority)" if history_authority_state == "known_recovery" else ""
                return DispatchDecision(
                    platform=pf,
                    decision="send",
                    reason=f"Quality recovered to pass after previous {last_sent.gate_status.upper()} alert{align_note}",
                    is_recovery=True,
                    fingerprint=fp,
                )
            # If no alert was ever sent for this release, suppress recovery notice
            return DispatchDecision(
                platform=pf,
                decision="suppressed",
                reason="Recovery transition detected by Gate authority but no preceding failure alert was delivered",
                is_recovery=True,
                fingerprint=fp,
            )

    # Standard Notification Status check
    if st not in policy.notify_on:
        return DispatchDecision(
            platform=pf,
            decision="suppressed",
            reason=f"Status '{st}' is not in configured notify_on list {list(policy.notify_on)}",
            is_recovery=False,
            fingerprint=fp,
        )


    # First alert for this version
    if last_sent is None:
        return DispatchDecision(
            platform=pf,
            decision="send",
            reason=f"Initial alert for release {target_version} ({st.upper()})",
            is_recovery=False,
            fingerprint=fp,
        )

    # 1. Status change bypasses cooldown
    if policy.resend_on_status_change and st != last_sent.gate_status:
        return DispatchDecision(
            platform=pf,
            decision="send",
            reason=f"Gate status changed from {last_sent.gate_status.upper()} to {st.upper()}",
            is_recovery=False,
            fingerprint=fp,
        )

    # 2. New regression reasons bypass cooldown
    prev_reasons = set(last_sent.reasons)
    curr_reasons = set(triggered_reasons)
    new_reasons = curr_reasons - prev_reasons
    if policy.resend_on_new_reason and new_reasons:
        return DispatchDecision(
            platform=pf,
            decision="send",
            reason=f"New regression reasons detected: {', '.join(sorted(new_reasons))}",
            is_recovery=False,
            fingerprint=fp,
        )

    # 3. Cooldown check
    curr_time = now or dt.datetime.now(dt.UTC)
    try:
        del_at_str = last_sent.delivered_at or ""
        cleaned = del_at_str.replace("Z", "+00:00")
        prev_dt = dt.datetime.fromisoformat(cleaned)
        if prev_dt.tzinfo is None:
            prev_dt = prev_dt.replace(tzinfo=dt.UTC)
        elapsed_minutes = max(0.0, (curr_time - prev_dt).total_seconds() / 60.0)
    except Exception:
        elapsed_minutes = 999999.0

    if elapsed_minutes < policy.cooldown_minutes:
        return DispatchDecision(
            platform=pf,
            decision="suppressed",
            reason=f"Suppressed due to active cooldown ({elapsed_minutes:.1f}m < {policy.cooldown_minutes}m)",
            is_recovery=False,
            fingerprint=fp,
        )

    # Cooldown expired -> resend
    return DispatchDecision(
        platform=pf,
        decision="send",
        reason=f"Cooldown expired ({elapsed_minutes:.1f}m >= {policy.cooldown_minutes}m), resending alert",
        is_recovery=False,
        fingerprint=fp,
    )


class AlertDispatcher:
    """Coordinates alert evaluation, deduplication state, and provider delivery."""

    def __init__(
        self,
        store: AlertDeliveryStore,
        provider: AlertProvider | None = None,
        history_store: Any | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.history_store = history_store

    def dispatch(
        self,
        app_id: str,
        artifact: ReleaseGateArtifact,
        policy: AlertPolicy,
        dry_run: bool = False,
        force: bool = False,
        now: dt.datetime | None = None,
    ) -> AlertDispatchSummary:
        """Dispatches alerts for all platforms present in the ReleaseGateArtifact."""
        results: dict[str, DeliveryResult] = {}
        decisions: dict[str, DispatchDecision] = {}
        total_sent = 0
        total_suppressed = 0
        total_failed = 0

        now_dt = now or dt.datetime.now(dt.UTC)
        attempt_time_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        eval_at = artifact.get("generated_at") or attempt_time_iso

        for pf_name, pf_res in artifact.get("platforms", {}).items():
            target_version = str(pf_res.get("target_version", "")).strip()
            prev_version = pf_res.get("previous_version")
            gate_status = str(pf_res.get("gate_status", "")).strip().lower()
            rule_results = pf_res.get("rule_results", [])
            triggered_reasons = sorted([
                str(r["rule_name"]).strip()
                for r in rule_results
                if r.get("status") in ("fail", "warn")
            ])

            decision = evaluate_alert_decision(
                app_id=app_id,
                platform=pf_name,
                target_version=target_version,
                gate_status=gate_status,
                triggered_reasons=triggered_reasons,
                policy=policy,
                store=self.store,
                now=now,
                force=force,
                history_store=self.history_store,
            )
            decisions[pf_name] = decision


            if decision.decision == "suppressed":
                total_suppressed += 1
                if not dry_run:
                    self.store.record_suppressed(
                        app_id=app_id,
                        platform=pf_name,
                        version=target_version,
                        provider=policy.provider,
                        alert_fingerprint=decision.fingerprint,
                        gate_status=gate_status,
                        reason_text=decision.reason,
                        attempted_at=attempt_time_iso,
                        reasons=triggered_reasons,
                        thread_key=f"crash-trend:{app_id}:{pf_name}:{target_version}" if policy.use_threads else None,
                        dry_run=False,
                        is_recovery=decision.is_recovery,
                    )
                results[pf_name] = DeliveryResult(
                    status="suppressed",
                    attempt_count=0,
                    error_code="SUPPRESSED",
                    error_message=decision.reason,
                )
                continue

            # decision.decision == "send"
            msg = build_alert_message(
                app_id=app_id,
                platform=pf_name,
                target_version=target_version,
                previous_version=prev_version,
                gate_status=gate_status,
                is_recovery=decision.is_recovery,
                rule_results=rule_results,
                policy_version=policy.policy_version,
                evaluated_at=eval_at,
                dashboard_url=policy.dashboard_url,
                use_threads=policy.use_threads,
                decision=pf_res.get("decision") or decision_from_gate_result(pf_res),
                alert_severity=str((pf_res.get("alert") or {}).get("alert_severity", "none")),
            )

            if dry_run:
                total_sent += 1
                # In dry-run, do not make HTTP requests and do not write sent state
                results[pf_name] = DeliveryResult(
                    status="sent",
                    attempt_count=0,
                    delivered_at=attempt_time_iso,
                    thread_key=msg.thread_key,
                    error_message="[DRY-RUN] Alert preview generated successfully (no network requests made).",
                )
                continue

            # Non-dry-run delivery
            rec_id = self.store.record_attempt(
                app_id=app_id,
                platform=pf_name,
                version=target_version,
                provider=policy.provider,
                alert_fingerprint=decision.fingerprint,
                gate_status=gate_status,
                attempted_at=attempt_time_iso,
                reasons=triggered_reasons,
                thread_key=msg.thread_key,
                dry_run=False,
                is_recovery=decision.is_recovery,
            )

            if not self.provider:
                res = DeliveryResult(
                    status="failed",
                    attempt_count=0,
                    error_code="NO_PROVIDER",
                    error_message=f"No delivery provider configured for '{policy.provider}'.",
                )
            else:
                res = self.provider.send(msg)

            if res.status == "sent":
                total_sent += 1
            else:
                total_failed += 1

            self.store.update_result(
                record_id=rec_id,
                status=res.status,
                delivered_at=res.delivered_at,
                attempt_count=res.attempt_count,
                http_status=res.http_status,
                error_code=res.error_code,
                error_message=res.error_message,
                message_name=res.message_name,
            )
            results[pf_name] = res

        return AlertDispatchSummary(
            app_id=app_id,
            results=results,
            decisions=decisions,
            total_sent=total_sent,
            total_suppressed=total_suppressed,
            total_failed=total_failed,
        )


def dispatch_alerts_for_app(
    app_name: str,
    artifact: ReleaseGateArtifact | None = None,
    policy: AlertPolicy | None = None,
    store: AlertDeliveryStore | None = None,
    history_store: Any | None = None,
    provider: AlertProvider | None = None,
    dry_run: bool = False,
    force: bool = False,
    gate_path: Path | None = None,
    verbose: bool = True,
) -> AlertDispatchSummary:
    """Dispatches quality alerts for an app. Used by pipeline and standalone CLI."""
    cfg = load_config()
    app_cfg = get_app(app_name, cfg)

    # 1. Resolve Policy
    effective_policy = policy if policy is not None else load_alert_policy(app_cfg, cfg)

    # Dashboard URL formatting
    dash_url = effective_policy.dashboard_url
    if dash_url and "#" not in dash_url:
        dash_url = f"{dash_url}#{app_name}"
        effective_policy = AlertPolicy(
            enabled=effective_policy.enabled,
            provider=effective_policy.provider,
            notify_on=effective_policy.notify_on,
            cooldown_minutes=effective_policy.cooldown_minutes,
            resend_on_status_change=effective_policy.resend_on_status_change,
            resend_on_new_reason=effective_policy.resend_on_new_reason,
            notify_recovery=effective_policy.notify_recovery,
            webhook_env=effective_policy.webhook_env,
            use_threads=effective_policy.use_threads,
            policy_version=effective_policy.policy_version,
            dashboard_url=dash_url,
        )

    # 2. Resolve Artifact
    effective_artifact = artifact
    if effective_artifact is None:
        art_path = gate_path or (out_dir(app_name) / "release_gate.json")
        loaded = load_release_gate_artifact(art_path)
        if loaded is None:
            raise FileNotFoundError(
                f"找不到 App「{app_name}」之 release_gate.json 產物（{art_path}）；請先執行 release_gate 階段。"
            )
        effective_artifact = loaded

    # 3. Resolve Store & Provider
    own_store = store is None
    effective_store = store or AlertDeliveryStore(
        db_path=out_dir(app_name) / "alert_delivery.sqlite3",
        app_id=app_name,
    )

    own_hist_store = history_store is None
    effective_hist_store = history_store
    if effective_hist_store is None:
        try:
            from crash_trend.gate.history import get_gate_history_store
            effective_hist_store = get_gate_history_store(app_name)
        except Exception:
            effective_hist_store = None

    effective_provider = provider
    if effective_provider is None and effective_policy.provider == "google_chat":
        effective_provider = GoogleChatWebhookProvider(
            webhook_env=effective_policy.webhook_env,
            use_threads=effective_policy.use_threads,
        )

    dispatcher = AlertDispatcher(
        store=effective_store,
        provider=effective_provider,
        history_store=effective_hist_store,
    )

    try:
        summary = dispatcher.dispatch(
            app_id=app_name,
            artifact=effective_artifact,
            policy=effective_policy,
            dry_run=dry_run,
            force=force,
        )
    finally:
        if own_store:
            effective_store.close()
        if own_hist_store and effective_hist_store is not None:
            try:
                effective_hist_store.close()
            except Exception:
                pass


    if verbose:
        mode_str = " (DRY-RUN)" if dry_run else ""
        print(f"\n================ [Release Quality Alerts: {app_name}{mode_str}] ================")
        print(f"  Policy Enabled:  {effective_policy.enabled}")
        print(f"  Provider:        {effective_policy.provider}")
        print(f"  Total Sent:      {summary.total_sent}")
        print(f"  Total Suppressed:{summary.total_suppressed}")
        print(f"  Total Failed:    {summary.total_failed}")

        for pf, res in summary.results.items():
            dec = summary.decisions.get(pf)
            dec_reason = dec.reason if dec else ""
            status_icon = "✓" if res.status == "sent" else ("—" if res.status == "suppressed" else "✕")
            print(f"  - Platform [{pf.upper()}]: [{res.status.upper()}] {status_icon}")
            print(f"      Decision Reason: {dec_reason}")
            if res.error_message:
                print(f"      Message/Detail:  {res.error_message}")
            if res.thread_key:
                print(f"      ThreadKey:       {res.thread_key}")

            # If dry-run, display preview of message text
            if dry_run and dec and dec.decision == "send":
                platforms_dict: dict[str, Any] = effective_artifact.get("platforms") or {}
                pf_data: dict[str, Any] = platforms_dict.get(pf) or {}
                preview_msg = build_alert_message(
                    app_id=app_name,
                    platform=pf,
                    target_version=str(pf_data.get("target_version", "")),
                    previous_version=pf_data.get("previous_version"),
                    gate_status=str(pf_data.get("gate_status", "")),
                    is_recovery=dec.is_recovery,
                    rule_results=pf_data.get("rule_results") or [],
                    policy_version=effective_policy.policy_version,
                    evaluated_at=str(effective_artifact.get("generated_at", "")),
                    dashboard_url=effective_policy.dashboard_url,
                    use_threads=effective_policy.use_threads,
                    decision=pf_data.get("decision") or decision_from_gate_result(pf_data),
                    alert_severity=str((pf_data.get("alert") or {}).get("alert_severity", "none")),
                )
                print("\n      --- [Payload Preview] ---")
                for line in preview_msg.text.splitlines():
                    print(f"      | {line}")
                print("      -------------------------\n")
        print("=======================================================================\n")

    return summary
