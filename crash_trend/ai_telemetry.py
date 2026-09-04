"""AI Usage & Quota Observability Engine (Dashboard V2.5 - Issue #42).

Provides tracking, aggregation, and metrics for Crash Intelligence AI usage:
- Daily and weekly request volumes
- Provider, model, app, and task-type distribution
- Fallback, error, and HTTP 429 rate-limit counts
- Strict token accounting without estimation or hallucination
- Free tier policy verification and Cost Guard audit trail
- Sanitized telemetry output (never exposes secrets, tokens, or raw prompts)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ai_router import is_free_openrouter_model
from .config import ROOT
from .pipeline_health import sanitize_error_message

DEFAULT_HISTORY_PATH = ROOT / "out" / "ai_usage_history.json"


def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_ai_call(
    app_id: str,
    task_type: str,
    provider: str,
    model: str,
    status: str = "success",
    http_status: Optional[int] = None,
    duration_ms: Optional[float] = None,
    paid_model_allowed: bool = False,
    tokens: Optional[Dict[str, Optional[int]]] = None,
    error_message: Optional[str] = None,
    history_path: Optional[Path] = None,
    max_records: int = 10000,
) -> Dict[str, Any]:
    """Records an AI call telemetry event to persistent history.

    All error messages are automatically sanitized before disk write.
    """
    target = Path(history_path) if history_path else DEFAULT_HISTORY_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    # Clean tokens: ensure values are ints or None; do not invent fake numbers
    clean_tokens: Optional[Dict[str, Optional[int]]] = None
    if isinstance(tokens, dict):
        p_tok = tokens.get("prompt_tokens")
        c_tok = tokens.get("completion_tokens")
        t_tok = tokens.get("total_tokens")
        if any(isinstance(v, int) for v in (p_tok, c_tok, t_tok)):
            clean_tokens = {
                "prompt_tokens": int(p_tok) if isinstance(p_tok, int) else None,
                "completion_tokens": int(c_tok) if isinstance(c_tok, int) else None,
                "total_tokens": int(t_tok) if isinstance(t_tok, int) else None,
            }

    event: Dict[str, Any] = {
        "timestamp": now_utc_iso(),
        "app_id": str(app_id or "global"),
        "task_type": str(task_type or "unknown"),
        "provider": str(provider or "unknown"),
        "model": str(model or "unknown"),
        "status": status,
        "http_status": http_status,
        "duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
        "paid_model_allowed": bool(paid_model_allowed),
        "tokens": clean_tokens,
        "error_message": sanitize_error_message(error_message),
    }

    # Load existing history
    records: List[Dict[str, Any]] = []
    if target.is_file():
        try:
            records = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                records = []
        except Exception:
            records = []

    records.append(event)
    if len(records) > max_records:
        records = records[-max_records:]

    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix="ai_usage_", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(records, indent=2, ensure_ascii=False))
        os.replace(tmp_path, str(target))
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    return event


def load_ai_usage_history(
    history_path: Optional[Path] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Loads historical AI telemetry records."""
    target = Path(history_path) if history_path else DEFAULT_HISTORY_PATH
    if not target.is_file():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data[-limit:] if limit else data
    except Exception:
        return []
    return []


def aggregate_ai_usage(
    history_records: Optional[List[Dict[str, Any]]] = None,
    days: int = 7,
    now: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Aggregates AI usage records into Dashboard-ready metrics and distributions."""
    records = history_records if history_records is not None else load_ai_usage_history()

    curr_now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = curr_now - dt.timedelta(days=days)

    filtered: List[Dict[str, Any]] = []
    for r in records:
        ts_str = r.get("timestamp")
        if not ts_str:
            continue
        try:
            clean_ts = ts_str.replace("Z", "+00:00")
            r_dt = dt.datetime.fromisoformat(clean_ts)
            if r_dt.tzinfo is None:
                r_dt = r_dt.replace(tzinfo=dt.timezone.utc)
            if r_dt >= cutoff:
                filtered.append(r)
        except Exception:
            continue

    total_requests = len(filtered)
    status_counts = {"success": 0, "error": 0, "fallback": 0, "rate_limit": 0}
    by_task: Dict[str, int] = defaultdict(int)
    by_provider: Dict[str, int] = defaultdict(int)
    by_model: Dict[str, int] = defaultdict(int)
    by_app: Dict[str, int] = defaultdict(int)
    daily_map: Dict[str, Dict[str, int]] = defaultdict(lambda: {"total": 0, "success": 0, "error": 0, "fallback": 0, "rate_limit": 0})

    paid_models_ever_allowed = False
    has_token_data = False
    total_tokens_sum = 0
    prompt_tokens_sum = 0
    completion_tokens_sum = 0
    free_tier_count = 0

    for r in filtered:
        st = r.get("status", "success")
        if st in status_counts:
            status_counts[st] += 1
        elif r.get("http_status") == 429 or (r.get("error_message") and "429" in r["error_message"]):
            status_counts["rate_limit"] += 1
        else:
            status_counts["error"] += 1

        task = r.get("task_type", "unknown")
        prov = r.get("provider", "unknown")
        model = r.get("model", "unknown")
        app = r.get("app_id", "global")

        by_task[task] += 1
        by_provider[prov] += 1
        by_model[model] += 1
        by_app[app] += 1

        if r.get("paid_model_allowed"):
            paid_models_ever_allowed = True

        # Tier breakdown: OpenRouter Free Worker vs Gemini Direct
        if prov == "openrouter" and is_free_openrouter_model(model):
            free_tier_count += 1
        elif prov == "gemini":
            # Gemini 3.8 Flash is free-tier eligible, but subject to Google AI quota & project billing
            free_tier_count += 1

        # Daily trend
        day_str = r.get("timestamp", "")[:10]
        if day_str:
            daily_map[day_str]["total"] += 1
            if st in daily_map[day_str]:
                daily_map[day_str][st] += 1

        # Token counting
        tok = r.get("tokens")
        if isinstance(tok, dict) and any(isinstance(tok.get(k), int) for k in ("prompt_tokens", "completion_tokens", "total_tokens")):
            has_token_data = True
            if isinstance(tok.get("total_tokens"), int):
                total_tokens_sum += tok["total_tokens"]
            if isinstance(tok.get("prompt_tokens"), int):
                prompt_tokens_sum += tok["prompt_tokens"]
            if isinstance(tok.get("completion_tokens"), int):
                completion_tokens_sum += tok["completion_tokens"]

    free_tier_ratio = round((free_tier_count / total_requests), 4) if total_requests > 0 else 1.0

    daily_trend = [
        {"date": d, **counts}
        for d, counts in sorted(daily_map.items())
    ]

    token_summary = {
        "status": "available" if has_token_data else "unavailable",
        "total_tokens": total_tokens_sum if has_token_data else None,
        "prompt_tokens": prompt_tokens_sum if has_token_data else None,
        "completion_tokens": completion_tokens_sum if has_token_data else None,
    }

    return {
        "period_days": days,
        "total_requests": total_requests,
        "success_count": status_counts["success"],
        "error_count": status_counts["error"],
        "fallback_count": status_counts["fallback"],
        "rate_limit_count": status_counts["rate_limit"],
        "free_tier_ratio": free_tier_ratio,
        "free_tier_count": free_tier_count,
        "cost_guard": {
            "paid_models_ever_allowed": paid_models_ever_allowed,
            "policy": "paid_models_permitted" if paid_models_ever_allowed else "strict_free_guard_active",
            "disclaimer": "OpenRouter 輕量任務鎖定 free worker；Gemini 呼叫適用 Google Free Tier 配額，超出或綁定計費專案可能產生費用，實際以 Google Cloud Console 帳單為準。",
        },
        "by_task_type": dict(by_task),
        "by_provider": dict(by_provider),
        "by_model": dict(by_model),
        "by_app": dict(by_app),
        "daily_trend": daily_trend,
        "tokens": token_summary,
    }
