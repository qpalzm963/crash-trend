"""Release Gate machine-readable artifact schema, validation, and serialization (Issue #57).

Conforms to zero-raw-identifiers policy: strictly excludes raw user IDs and installation UUIDs.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict, cast

from crash_trend.schema_v2 import (
    ReleaseDecision,
    RuleEvaluationResult,
    validate_release_decision,
)

GateStatus = Literal["pass", "warn", "fail", "insufficient_data", "baseline"]
RuleStatus = Literal["pass", "warn", "fail", "skip", "insufficient_data"]
AlertSeverity = Literal["none", "warning", "critical"]


class AlertHookPayload(TypedDict):
    """Normalized payload for downstream alerting integrations (Slack, Teams, etc.)."""

    should_alert: bool
    alert_severity: AlertSeverity
    alert_summary: str
    trigger_rules: list[str]


class PlatformGateResult(TypedDict):
    """Evaluation result for a specific platform's target release."""

    platform: Literal["ios", "android"]
    target_version: str
    previous_version: str | None
    gate_status: GateStatus
    sample_sufficient: bool
    rule_results: list[RuleEvaluationResult]
    alert: AlertHookPayload
    evaluated_at: str
    comparison_window: NotRequired[str | None]
    # Canonical Release Decision contract (Issue #72). NotRequired so artifacts
    # produced before V3.2 still validate and can be consumed.
    decision: NotRequired[ReleaseDecision]


class ReleaseGateArtifact(TypedDict):
    """Machine-readable artifact stored at out/<app>/release_gate.json."""

    schema_version: str
    app_id: str
    generated_at: str
    overall_status: GateStatus
    should_alert: bool
    alert_severity: AlertSeverity
    alert_summary: str
    platforms: dict[str, PlatformGateResult]
    policy_version: str
    policy: dict[str, Any]
    policy_identity: NotRequired[str]


def _is_valid_iso8601_utc(val: Any) -> bool:
    """Checks if string is a valid ISO 8601 UTC timestamp ending in Z or +00:00."""
    if not isinstance(val, str) or not val.strip():
        return False
    if not (val.endswith("Z") or val.endswith("+00:00")):
        return False
    norm = val[:-1] + "+00:00" if val.endswith("Z") else val
    try:
        parsed = dt.datetime.fromisoformat(norm)
        return parsed.tzinfo is not None
    except (ValueError, TypeError):
        return False


def validate_release_gate_artifact(data: Any) -> list[str]:
    """Validates a ReleaseGateArtifact dictionary against schema and privacy constraints.

    Returns a list of error messages (empty list if valid).
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["ReleaseGateArtifact must be an object"]

    # Top-level required keys
    for req_key in (
        "schema_version",
        "app_id",
        "generated_at",
        "overall_status",
        "should_alert",
        "alert_severity",
        "alert_summary",
        "platforms",
        "policy_version",
        "policy",
    ):
        if req_key not in data:
            errors.append(f"Missing required field: {req_key}")

    # Schema version
    if "schema_version" in data and str(data["schema_version"]) not in ("1.0", "2.7"):
        errors.append(f"Unsupported schema_version: {data['schema_version']}")

    # Timestamp
    if "generated_at" in data and not _is_valid_iso8601_utc(data["generated_at"]):
        errors.append("generated_at must be an ISO 8601 UTC timestamp (ending in Z or +00:00)")

    # Overall status
    valid_statuses = {"pass", "warn", "fail", "insufficient_data", "baseline"}
    if "overall_status" in data and data["overall_status"] not in valid_statuses:
        errors.append(f"Invalid overall_status: {data['overall_status']}, must be one of {valid_statuses}")

    # Alert severity
    valid_severities = {"none", "warning", "critical"}
    if "alert_severity" in data and data["alert_severity"] not in valid_severities:
        errors.append(f"Invalid alert_severity: {data['alert_severity']}")

    if "should_alert" in data and not isinstance(data["should_alert"], bool):
        errors.append("should_alert must be a boolean")

    if "policy_identity" in data and not isinstance(data["policy_identity"], str):
        errors.append("policy_identity must be a string")

    # Platform checks
    platforms = data.get("platforms")
    if isinstance(platforms, dict):
        for pf_name, pf_res in platforms.items():
            if pf_name not in ("ios", "android"):
                errors.append(f"Platform key must be 'ios' or 'android', got '{pf_name}'")
            if not isinstance(pf_res, dict):
                errors.append(f"platforms['{pf_name}'] must be an object")
                continue
            for pf_req in (
                "platform",
                "target_version",
                "previous_version",
                "gate_status",
                "sample_sufficient",
                "rule_results",
                "alert",
                "evaluated_at",
            ):
                if pf_req not in pf_res:
                    errors.append(f"platforms['{pf_name}'].{pf_req} is required")
            if "gate_status" in pf_res and pf_res["gate_status"] not in valid_statuses:
                errors.append(f"platforms['{pf_name}'].gate_status '{pf_res['gate_status']}' is invalid")
            if "rule_results" in pf_res and not isinstance(pf_res["rule_results"], list):
                errors.append(f"platforms['{pf_name}'].rule_results must be a list")
            if "comparison_window" in pf_res and pf_res["comparison_window"] is not None and not isinstance(pf_res["comparison_window"], str):
                errors.append(f"platforms['{pf_name}'].comparison_window must be a string or null")
            if "decision" in pf_res:
                errors.extend(validate_release_decision(pf_res["decision"], f"platforms['{pf_name}'].decision"))
    elif platforms is not None:
        errors.append("platforms must be a dictionary")

    # Privacy constraint: Zero raw identifiers check
    def _check_forbidden_keys(node: Any, path: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                cur_p = f"{path}.{k}" if path else str(k)
                if k in ("installation_ids", "user_ids", "installation_id", "user_id"):
                    errors.append(f"Forbidden raw identifier key '{cur_p}' detected (zero raw UUIDs policy)")
                _check_forbidden_keys(v, cur_p)
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                _check_forbidden_keys(item, f"{path}[{idx}]")

    _check_forbidden_keys(data)
    return errors


def save_release_gate_artifact(path: Path, artifact: ReleaseGateArtifact) -> None:
    """Validates and writes ReleaseGateArtifact to file."""
    errors = validate_release_gate_artifact(artifact)
    if errors:
        raise ValueError(f"Invalid ReleaseGateArtifact schema: {'; '.join(errors)}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")


def load_release_gate_artifact(path: Path) -> ReleaseGateArtifact | None:
    """Loads and validates ReleaseGateArtifact from file. Returns None if missing."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        errors = validate_release_gate_artifact(data)
        if errors:
            return None
        return cast(ReleaseGateArtifact, data)
    except Exception:
        return None
