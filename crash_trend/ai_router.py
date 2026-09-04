"""AI Task Router Engine (Dashboard V2.4 - Issue #35).

Provides intelligent task-based routing across Gemini Direct and OpenRouter Free Worker,
supporting manual overrides (Auto / Gemini Only / OpenRouter Only), per-app configurations,
strict free model protection (paid model guards), transient fallback policies,
privacy guards, and complete observability telemetry.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

import requests

from .ai_provider import (
    AIProvider,
    CANONICAL_AI_RESPONSE_SCHEMA,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    GeminiProvider,
    OpenRouterProvider,
    resolve_gemini_key,
)
from .pipeline_health import sanitize_error_message

SUPPORTED_ROUTING_MODES = {"auto", "gemini_only", "openrouter_only"}
DEFAULT_LIGHTWEIGHT_MODEL = "openrouter/free"

# AI Task Taxonomy (Dashboard V2.5 - Issue #40)
TASK_DEEP_ANALYSIS = "deep_analysis"
TASK_LIGHTWEIGHT = "lightweight"
TASK_ISSUE_TRIAGE = "issue_triage"
TASK_ISSUE_SUMMARY = "issue_summary"
TASK_ISSUE_CLASSIFICATION = "issue_classification"
TASK_ISSUE_TAGGING = "issue_tagging"

LIGHTWEIGHT_TASKS = {
    TASK_LIGHTWEIGHT,
    TASK_ISSUE_TRIAGE,
    TASK_ISSUE_SUMMARY,
    TASK_ISSUE_CLASSIFICATION,
    TASK_ISSUE_TAGGING,
}


def is_free_openrouter_model(model: str) -> bool:
    """Returns True if the OpenRouter model is explicitly recognized as a free model."""
    if not model or not isinstance(model, str):
        return False
    m = model.strip().lower()
    return m == "openrouter/free" or m.endswith(":free")


# Gemini models verified to offer Standard Free Tier on Google AI Studio
FREE_TIER_GEMINI_MODELS = {
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-001",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash-lite-preview-02-05",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
}


def is_free_gemini_model(model: str) -> bool:
    """Returns True if the Gemini model is explicitly recognized as Standard Free Tier eligible.

    Unknown models, Pro models (e.g. gemini-3.1-pro-preview, gemini-1.5-pro), and paid-only Flash
    models (e.g. gemini-2.5-flash-image, gemini-3.1-flash-image) strictly return False and require
    allow_paid_models=True.
    """
    if not model or not isinstance(model, str):
        return False
    m = model.strip().lower()
    if m.startswith("models/"):
        m = m[len("models/"):]
    # Strict allowlist matching: no fuzzy fallback
    return m in FREE_TIER_GEMINI_MODELS


def is_transient_error(e: Exception) -> bool:
    """Returns True only for transient errors that may recover via fallback.

    Eligible errors:
      - HTTP 429 (Rate Limit / Quota)
      - HTTP 500, 502, 503, 504 (Server Errors)
      - Connection timeout / network connection error

    Non-transient errors (MUST NOT FALLBACK):
      - HTTP 400 (Bad request / schema error)
      - HTTP 401, 403 (Authentication / authorization)
      - ValueError / configuration error / paid model disallowed
    """
    if isinstance(e, (requests.Timeout, requests.ConnectionError)):
        return True

    err_str = str(e).lower()

    # Explicit non-transient checks
    if any(code in err_str for code in ("400", "401", "403", "bad request", "unauthorized", "forbidden")):
        return False
    if "paid model" in err_str or "not allowed" in err_str:
        return False

    # Check for transient HTTP status codes
    transient_indicators = ("429", "500", "502", "503", "504", "timeout", "timed out", "connection", "rate limit")
    return any(ind in err_str for ind in transient_indicators)


@dataclass
class AIRouterConfig:
    mode: str = "auto"
    primary_provider: str = "gemini"
    primary_model: str = DEFAULT_GEMINI_MODEL
    primary_api_key: Optional[str] = None
    primary_temperature: Optional[float] = None
    lightweight_provider: str = "openrouter"
    lightweight_model: str = DEFAULT_LIGHTWEIGHT_MODEL
    lightweight_api_key: Optional[str] = None
    lightweight_zdr: bool = True
    allow_paid_models: bool = False
    include_source_snippet: bool = True
    fallback_enabled: bool = False


@dataclass
class AIRoutingDecision:
    mode: str
    task_type: str
    selected_provider: str
    selected_model: str
    routing_reason: str
    paid_model_allowed: bool
    fallback_target_provider: Optional[str] = None
    fallback_target_model: Optional[str] = None


@dataclass
class AIRouterResult:
    data: Dict[str, Any]
    decision: AIRoutingDecision
    fallback_used: bool = False
    fallback_reason: Optional[str] = None
    active_provider: str = ""
    active_model: str = ""
    tokens: Optional[Dict[str, Optional[int]]] = None


def resolve_router_config(
    app_cfg: Optional[Dict[str, Any]] = None,
    global_cfg: Optional[Dict[str, Any]] = None,
) -> AIRouterConfig:
    """Resolves AIRouterConfig by merging global and per-app configuration dictionaries."""
    app_ai = copy.deepcopy((app_cfg.get("ai") or {}) if app_cfg else {})
    global_ai = copy.deepcopy((global_cfg.get("ai") or {}) if global_cfg else {})

    # Validate provider if specified
    raw_provider = app_ai.get("provider") or global_ai.get("provider")
    if raw_provider:
        prov_name = str(raw_provider).strip().lower()
        if prov_name not in {"gemini", "openrouter"}:
            raise ValueError(
                f"Unknown AI provider: '{raw_provider}'. Supported providers: ['gemini', 'openrouter']"
            )

    # 1. Routing mode resolution
    raw_mode = (
        app_ai.get("mode")
        or global_ai.get("mode")
        or os.environ.get("AI_ROUTING_MODE")
    )
    if raw_mode:
        mode = str(raw_mode).strip().lower()
        if mode not in SUPPORTED_ROUTING_MODES:
            raise ValueError(
                f"Unknown AI routing mode: '{raw_mode}'. Supported modes: {sorted(SUPPORTED_ROUTING_MODES)}"
            )
    else:
        # Legacy backward-compatibility resolution
        app_provider = str(app_ai.get("provider", "")).strip().lower()
        global_provider = str(global_ai.get("provider", "")).strip().lower()
        if app_provider == "openrouter" or (not app_provider and global_provider == "openrouter"):
            mode = "openrouter_only"
        elif app_provider == "gemini" or (not app_provider and global_provider == "gemini"):
            mode = "gemini_only"
        else:
            mode = "auto"

    # 2. Allow paid models resolution (default False; strictly opt-in)
    allow_paid = False
    if "allow_paid_models" in app_ai:
        allow_paid = bool(app_ai["allow_paid_models"])
    elif "allow_paid_models" in global_ai:
        allow_paid = bool(global_ai["allow_paid_models"])
    elif os.environ.get("AI_ALLOW_PAID_MODELS"):
        allow_paid = os.environ.get("AI_ALLOW_PAID_MODELS", "").strip().lower() in ("true", "1", "yes")

    # 3. Privacy settings resolution (default True)
    app_privacy = app_ai.get("privacy") or {}
    global_privacy = global_ai.get("privacy") or {}
    include_source_snippet = True
    if "include_source_snippet" in app_privacy:
        include_source_snippet = bool(app_privacy["include_source_snippet"])
    elif "include_source_snippet" in global_privacy:
        include_source_snippet = bool(global_privacy["include_source_snippet"])

    # 4. Fallback settings resolution (default False)
    app_fallback = app_ai.get("fallback") or {}
    global_fallback = global_ai.get("fallback") or {}
    fallback_enabled = False
    if "enabled" in app_fallback:
        fallback_enabled = bool(app_fallback["enabled"])
    elif "enabled" in global_fallback:
        fallback_enabled = bool(global_fallback["enabled"])

    # 5. Primary provider & model resolution
    app_primary = app_ai.get("primary") or {}
    global_primary = global_ai.get("primary") or {}

    # Check for flat legacy structure in app_ai or global_ai
    legacy_gemini_model = None
    if app_ai.get("provider") == "gemini" and app_ai.get("model"):
        legacy_gemini_model = app_ai["model"]
    elif global_ai.get("provider") == "gemini" and global_ai.get("model"):
        legacy_gemini_model = global_ai["model"]

    primary_provider = app_primary.get("provider") or global_primary.get("provider") or "gemini"
    primary_model = (
        app_primary.get("model")
        or global_primary.get("model")
        or legacy_gemini_model
        or os.environ.get("GEMINI_MODEL")
        or DEFAULT_GEMINI_MODEL
    )
    primary_api_key = app_primary.get("api_key") or global_primary.get("api_key")
    if not primary_api_key and (app_ai.get("provider") == "gemini" or not app_ai.get("provider")):
        primary_api_key = app_ai.get("api_key") or global_ai.get("api_key")
    primary_temperature = app_primary.get("temperature", global_primary.get("temperature"))

    # 6. Lightweight provider & model resolution
    app_lightweight = app_ai.get("lightweight") or {}
    global_lightweight = global_ai.get("lightweight") or {}

    legacy_openrouter_model = None
    if app_ai.get("provider") == "openrouter" and app_ai.get("model"):
        legacy_openrouter_model = app_ai["model"]
    elif global_ai.get("provider") == "openrouter" and global_ai.get("model"):
        legacy_openrouter_model = global_ai["model"]

    lightweight_provider = app_lightweight.get("provider") or global_lightweight.get("provider") or "openrouter"
    lightweight_model = (
        app_lightweight.get("model")
        or global_lightweight.get("model")
        or legacy_openrouter_model
        or os.environ.get("OPENROUTER_MODEL")
        or DEFAULT_LIGHTWEIGHT_MODEL
    )
    lightweight_api_key = app_lightweight.get("api_key") or global_lightweight.get("api_key")
    if not lightweight_api_key and app_ai.get("provider") == "openrouter":
        lightweight_api_key = app_ai.get("api_key") or global_ai.get("api_key")
    lightweight_zdr = bool(app_lightweight.get("zdr", global_lightweight.get("zdr", True)))

    return AIRouterConfig(
        mode=mode,
        primary_provider=primary_provider,
        primary_model=primary_model,
        primary_api_key=primary_api_key,
        primary_temperature=primary_temperature,
        lightweight_provider=lightweight_provider,
        lightweight_model=lightweight_model,
        lightweight_api_key=lightweight_api_key,
        lightweight_zdr=lightweight_zdr,
        allow_paid_models=allow_paid,
        include_source_snippet=include_source_snippet,
        fallback_enabled=fallback_enabled,
    )


class AITaskRouter:
    """Orchestrates AI analysis requests according to routing policy and guardrails."""

    def __init__(self, config: AIRouterConfig) -> None:
        self.config = config

    def route(self, task_type: str = "deep_analysis") -> AIRoutingDecision:
        """Determines the provider, model, and fallback policy for a given task type."""
        mode = self.config.mode

        if mode == "gemini_only":
            selected_provider = "gemini"
            selected_model = self.config.primary_model
            routing_reason = "Gemini only mode: all tasks routed to Gemini Direct"
            fallback_target_provider = None
            fallback_target_model = None

        elif mode == "openrouter_only":
            selected_provider = "openrouter"
            selected_model = self.config.lightweight_model
            routing_reason = "OpenRouter only mode: all tasks routed to OpenRouter worker"
            fallback_target_provider = None
            fallback_target_model = None

        else:  # auto mode
            if task_type == "deep_analysis":
                selected_provider = self.config.primary_provider
                selected_model = self.config.primary_model
                routing_reason = "Auto mode: deep_analysis routed to primary provider (Gemini Direct)"
                if self.config.fallback_enabled:
                    fallback_target_provider = self.config.lightweight_provider
                    fallback_target_model = self.config.lightweight_model
                else:
                    fallback_target_provider = None
                    fallback_target_model = None
            else:
                selected_provider = self.config.lightweight_provider
                selected_model = self.config.lightweight_model
                routing_reason = f"Auto mode: {task_type} routed to lightweight worker (OpenRouter)"
                if self.config.fallback_enabled:
                    fallback_target_provider = self.config.primary_provider
                    fallback_target_model = self.config.primary_model
                else:
                    fallback_target_provider = None
                    fallback_target_model = None

        # Enforce Free Guard on selected model
        if not self.config.allow_paid_models:
            if selected_provider == "openrouter" and not is_free_openrouter_model(selected_model):
                raise ValueError(
                    f"Paid model '{selected_model}' not allowed when allow_paid_models is false"
                )
            elif selected_provider == "gemini" and not is_free_gemini_model(selected_model):
                raise ValueError(
                    f"Gemini model '{selected_model}' is not recognized as Free Tier eligible when allow_paid_models is false"
                )

        # Enforce Free Guard on fallback target model
        if not self.config.allow_paid_models:
            if fallback_target_provider == "openrouter" and fallback_target_model and not is_free_openrouter_model(fallback_target_model):
                # Cannot fallback to a paid model when allow_paid_models is false
                fallback_target_provider = None
                fallback_target_model = None
            elif fallback_target_provider == "gemini" and fallback_target_model and not is_free_gemini_model(fallback_target_model):
                fallback_target_provider = None
                fallback_target_model = None

        return AIRoutingDecision(
            mode=mode,
            task_type=task_type,
            selected_provider=selected_provider,
            selected_model=selected_model,
            routing_reason=routing_reason,
            paid_model_allowed=self.config.allow_paid_models,
            fallback_target_provider=fallback_target_provider,
            fallback_target_model=fallback_target_model,
        )

    def build_provider(self, provider_name: str, model_name: str) -> AIProvider:
        """Instantiates an AIProvider instance with appropriate credentials."""
        p_name = provider_name.lower().strip()
        if p_name == "openrouter":
            if not self.config.allow_paid_models and not is_free_openrouter_model(model_name):
                raise ValueError(
                    f"Paid model '{model_name}' not allowed when allow_paid_models is false"
                )
            key = self.config.lightweight_api_key or os.environ.get("OPENROUTER_API_KEY")
            return OpenRouterProvider(
                api_key=key,
                model=model_name,
                zdr=self.config.lightweight_zdr,
            )
        elif p_name == "gemini":
            if not self.config.allow_paid_models and not is_free_gemini_model(model_name):
                raise ValueError(
                    f"Gemini model '{model_name}' is not recognized as Free Tier eligible when allow_paid_models is false"
                )
            key = self.config.primary_api_key or resolve_gemini_key(raise_on_missing=False)
            return GeminiProvider(
                api_key=key,
                model=model_name,
                temperature=self.config.primary_temperature,
            )
        else:
            raise ValueError(f"Unsupported provider in router: '{provider_name}'")

    def is_configured(self, task_type: str = "deep_analysis") -> bool:
        """Returns True if the designated provider for task_type has credentials."""
        try:
            decision = self.route(task_type)
            provider = self.build_provider(decision.selected_provider, decision.selected_model)
            return provider.is_configured()
        except Exception:
            return False

    def analyze(
        self,
        prompt: str,
        schema: Optional[Dict[str, Any]] = None,
        task_type: str = "deep_analysis",
    ) -> AIRouterResult:
        """Executes analysis through routed provider, with transient fallback if enabled."""
        decision = self.route(task_type)
        primary_provider = self.build_provider(decision.selected_provider, decision.selected_model)

        if not primary_provider.is_configured():
            raise RuntimeError(
                f"{decision.selected_provider.upper()} API key is not configured for {decision.mode} mode"
            )

        try:
            raw_res = primary_provider.analyze(prompt, schema=schema or CANONICAL_AI_RESPONSE_SCHEMA)
            return AIRouterResult(
                data=raw_res,
                decision=decision,
                fallback_used=False,
                fallback_reason=None,
                active_provider=decision.selected_provider,
                active_model=decision.selected_model,
                tokens=getattr(primary_provider, "last_tokens", None),
            )
        except Exception as e:
            # Check fallback eligibility
            can_fallback = (
                self.config.fallback_enabled
                and decision.fallback_target_provider is not None
                and decision.fallback_target_model is not None
                and is_transient_error(e)
            )

            if not can_fallback:
                raise

            safe_reason = sanitize_error_message(str(e))
            fallback_provider_name = decision.fallback_target_provider
            fallback_model_name = decision.fallback_target_model

            try:
                fallback_provider = self.build_provider(fallback_provider_name, fallback_model_name)
                if not fallback_provider.is_configured():
                    # Fallback provider has no credentials; re-raise original error
                    raise e

                raw_res = fallback_provider.analyze(prompt, schema=schema or CANONICAL_AI_RESPONSE_SCHEMA)
                return AIRouterResult(
                    data=raw_res,
                    decision=decision,
                    fallback_used=True,
                    fallback_reason=f"Transient failure on {decision.selected_provider}: {safe_reason}",
                    active_provider=fallback_provider_name,
                    active_model=fallback_model_name,
                    tokens=getattr(fallback_provider, "last_tokens", None),
                )
            except Exception as fb_err:
                # Fallback also failed or non-transient; re-raise fallback error (or original)
                raise fb_err from e


def get_ai_router(
    app_cfg: Optional[Dict[str, Any]] = None,
    global_cfg: Optional[Dict[str, Any]] = None,
) -> AITaskRouter:
    """Factory creating an AITaskRouter instance resolved from app and global configuration."""
    cfg = resolve_router_config(app_cfg, global_cfg)
    return AITaskRouter(cfg)
