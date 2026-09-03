"""Pipeline Health: Run Summary and Stage Status Tracking (Dashboard V2.2 - Issue #22).

This module defines machine-readable pipeline run summary contracts, error message
sanitization to prevent credential/token leaks, and run tracker utilities that record
per-app, per-stage execution status.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TypedDict

try:
    from typing import NotRequired
except ImportError:
    from typing_extensions import NotRequired  # type: ignore

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUN_SUMMARY_PATH = ROOT / "out" / "pipeline_run.json"
RUN_SUMMARY_SCHEMA_VERSION = "1.0"

StageStatus = Literal["success", "failed", "skipped", "disabled", "degraded"]
OverallStatus = Literal["success", "degraded", "failed"]

# Set of stages considered non-negotiable / core. If a core stage fails,
# the app and overall pipeline run must be marked 'failed'.
CORE_STAGES = {"crashlytics_bigquery", "build_dashboard"}


class StageResult(TypedDict):
    status: StageStatus
    started_at: str
    finished_at: str
    duration_sec: float
    error_message: Optional[str]
    details: NotRequired[Optional[Dict[str, Any]]]


class AppPipelineSummary(TypedDict):
    status: OverallStatus
    stages: Dict[str, StageResult]


class PipelineRunSummary(TypedDict):
    schema_version: str
    started_at: str
    finished_at: str
    duration_sec: float
    status: OverallStatus
    apps: Dict[str, AppPipelineSummary]
    build_dashboard: NotRequired[Optional[StageResult]]


# ---------------------------------------------------------------------------
# Sensitive Credential Sanitization
# ---------------------------------------------------------------------------

_PATTERNS_TO_SANITIZE = [
    # Google API Key (e.g. AIzaSy...)
    (re.compile(r"AIza[0-9A-Za-z-_]{25,45}"), "AIza[REDACTED]"),
    # Bearer tokens
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.]+"), "Bearer [REDACTED]"),
    # Private keys (RSA / EC / OpenSSH)
    (
        re.compile(r"-----BEGIN[A-Z\s]+PRIVATE KEY-----[^-]+-----END[A-Z\s]+PRIVATE KEY-----", re.DOTALL),
        "[PRIVATE KEY REDACTED]",
    ),
    # OAuth refresh tokens
    (re.compile(r"1//[0-9A-Za-z_\-]+"), "[REFRESH_TOKEN_REDACTED]"),
    # Common auth query params or key assignments
    (
        re.compile(r"(?i)(api[_-]?key|token|secret|password|credential|client_secret)[\s:=]+['\"]?([A-Za-z0-9_\-\.\~]{8,})['\"]?"),
        r"\1=[REDACTED]",
    ),
    # GCP SA private_key / client_email in json snippets
    (re.compile(r'"private_key":\s*"[^"]+"'), '"private_key": "[REDACTED]"'),
    (re.compile(r'"client_secret":\s*"[^"]+"'), '"client_secret": "[REDACTED]"'),
]


def sanitize_error_message(msg: Optional[Any], max_len: int = 500) -> Optional[str]:
    """Sanitizes an error message by stripping sensitive tokens and bounding length."""
    if msg is None:
        return None
    text = str(msg).strip()
    if not text:
        return ""

    for pattern, replacement in _PATTERNS_TO_SANITIZE:
        text = pattern.sub(replacement, text)

    if len(text) > max_len:
        text = text[:max_len] + " ... (truncated)"
    return text


def now_utc_iso() -> str:
    """Returns current UTC timestamp in ISO 8601 format."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str) -> dt.datetime:
    """Parses ISO timestamp string into UTC datetime object."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return dt.datetime.fromisoformat(ts)


# ---------------------------------------------------------------------------
# Pipeline Run Tracker
# ---------------------------------------------------------------------------

class PipelineRunTracker:
    """Tracks and records execution health, stage timings, and outcomes across apps."""

    def __init__(self, started_at: Optional[str] = None) -> None:
        self.started_at: str = started_at or now_utc_iso()
        self.finished_at: Optional[str] = None
        self.apps: Dict[str, Dict[str, StageResult]] = {}
        self.build_dashboard_result: Optional[StageResult] = None

    def ensure_app(self, app_name: str) -> None:
        if app_name not in self.apps:
            self.apps[app_name] = {}

    def record_stage(
        self,
        app_name: Optional[str],
        stage_name: str,
        status: StageStatus,
        started_at: str,
        finished_at: str,
        error_message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> StageResult:
        """Records the result of a pipeline stage."""
        try:
            t0 = parse_iso(started_at)
            t1 = parse_iso(finished_at)
            dur = max(0.0, round((t1 - t0).total_seconds(), 3))
        except Exception:
            dur = 0.0

        sanitized_err = sanitize_error_message(error_message)

        res: StageResult = {
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_sec": dur,
            "error_message": sanitized_err,
        }
        if details:
            res["details"] = details

        if app_name:
            self.ensure_app(app_name)
            self.apps[app_name][stage_name] = res
        elif stage_name == "build_dashboard":
            self.build_dashboard_result = res

        return res

    def compute_app_status(self, app_stages: Dict[str, StageResult]) -> OverallStatus:
        """Determines the overall status for a single app."""
        # Check core stages first
        for core_s in CORE_STAGES:
            if core_s in app_stages and app_stages[core_s]["status"] == "failed":
                return "failed"

        # Check optional stages
        has_failed_optional = any(
            s_res["status"] == "failed"
            for s_name, s_res in app_stages.items()
            if s_name not in CORE_STAGES
        )
        if has_failed_optional:
            return "degraded"

        return "success"

    def compute_overall_status(self) -> OverallStatus:
        """Determines overall pipeline run status."""
        # Top-level build_dashboard failure makes the entire run failed
        if self.build_dashboard_result and self.build_dashboard_result["status"] == "failed":
            return "failed"

        any_app_failed = False
        any_app_degraded = False

        for stages in self.apps.values():
            app_st = self.compute_app_status(stages)
            if app_st == "failed":
                any_app_failed = True
            elif app_st == "degraded":
                any_app_degraded = True

        if any_app_failed:
            return "failed"
        if any_app_degraded:
            return "degraded"
        return "success"

    def reset_finish(self) -> None:
        """Resets finished_at so that a subsequent finalized summary can be recorded."""
        self.finished_at = None

    def build_summary(
        self,
        finished_at: Optional[str] = None,
        finalize: bool = True,
    ) -> PipelineRunSummary:
        """Compiles tracked metrics into a validated PipelineRunSummary dictionary.

        If finalize is False (e.g. provisional save before build_dashboard),
        self.finished_at is not locked, allowing the final summary to capture
        subsequent stages and full execution duration.
        """
        if finalize:
            fin = finished_at or self.finished_at or now_utc_iso()
            self.finished_at = fin
        else:
            fin = finished_at or now_utc_iso()

        try:
            t0 = parse_iso(self.started_at)
            t1 = parse_iso(fin)
            dur = max(0.0, round((t1 - t0).total_seconds(), 3))
        except Exception:
            dur = 0.0

        apps_summary: Dict[str, AppPipelineSummary] = {}
        for app_name, stages in self.apps.items():
            apps_summary[app_name] = {
                "status": self.compute_app_status(stages),
                "stages": dict(stages),
            }

        summary: PipelineRunSummary = {
            "schema_version": RUN_SUMMARY_SCHEMA_VERSION,
            "started_at": self.started_at,
            "finished_at": fin,
            "duration_sec": dur,
            "status": self.compute_overall_status(),
            "apps": apps_summary,
        }

        if self.build_dashboard_result:
            summary["build_dashboard"] = self.build_dashboard_result

        return summary

    def save_summary(
        self,
        target_path: Optional[Path] = None,
        finished_at: Optional[str] = None,
        finalize: bool = True,
    ) -> Path:
        """Atomically writes pipeline_run.json to disk."""
        target = Path(target_path) if target_path else DEFAULT_RUN_SUMMARY_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        summary = self.build_summary(finished_at=finished_at, finalize=finalize)

        content = json.dumps(summary, indent=2, ensure_ascii=False)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix="run_summary_", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, str(target))
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

        return target


def load_run_summary(path: Optional[Path] = None) -> Optional[PipelineRunSummary]:
    """Loads a previously written pipeline_run.json, returning None if absent or invalid."""
    target = Path(path) if path else DEFAULT_RUN_SUMMARY_PATH
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "schema_version" in data and "apps" in data:
            return data  # type: ignore
    except Exception:
        return None
    return None
