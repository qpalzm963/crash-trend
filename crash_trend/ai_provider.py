"""AI Provider Abstraction Layer (Dashboard V2.3 - Issue #26).

Provides a provider-neutral interface for Crash Intelligence analysis, with support
for Google Gemini and OpenRouter (multi-model routing).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import requests

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_OPENROUTER_MODEL = "google/gemini-2.5-flash"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_INTERACTIONS_API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
GEMINI_API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SUPPORTED_PROVIDERS = {"gemini", "openrouter"}

# Canonical provider-neutral standard JSON Schema (strict, lowercase types for OpenRouter)
CANONICAL_AI_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "key_takeaways": {
            "type": "array",
            "items": {"type": "string"},
        },
        "distribution_insights": {"type": "string"},
        "recommended_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                    "issue_id": {"type": "string"},
                    "action": {"type": "string"},
                    "effort": {"type": "string", "enum": ["S", "M", "L"]},
                },
                "required": ["priority", "issue_id", "action", "effort"],
                "additionalProperties": False,
            },
        },
        "data_limitations": {"type": ["string", "null"]},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string"},
                    "root_cause": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                    "effort": {"type": "string", "enum": ["S", "M", "L"]},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low", "needs_manual_review"],
                    },
                    "reasoning_sources": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["issue_id", "root_cause", "suggested_fix", "effort", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "overview",
        "key_takeaways",
        "distribution_insights",
        "recommended_actions",
        "data_limitations",
        "items",
    ],
    "additionalProperties": False,
}

# Canonical JSON Schema for Lightweight Issue Triage, Classification, and Tagging (Dashboard V2.5 - Issue #40)
CANONICAL_LIGHTWEIGHT_TRIAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string"},
                    "short_summary": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": [
                            "UI_CRASH",
                            "NETWORK",
                            "NULL_POINTER",
                            "STORAGE_IO",
                            "LIFECYCLE_ANR",
                            "AUTH",
                            "DATABASE",
                            "CONCURRENCY",
                            "RESOURCE_EXHAUSTION",
                            "THIRD_PARTY_SDK",
                            "OTHER",
                        ],
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "warrants_deep_analysis": {"type": "boolean"},
                    "triage_reason": {"type": "string"},
                },
                "required": [
                    "issue_id",
                    "short_summary",
                    "category",
                    "tags",
                    "warrants_deep_analysis",
                    "triage_reason",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}


def to_gemini_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """Adapts canonical JSON Schema to legacy Gemini GenerativeLanguage API schema format (OpenAPI 3.0 subset).

    Maintained as a backward-compatibility fallback for OpenAPI 3.0 responseSchema.
    New Gemini runtime calls directly leverage native JSON Schema via `responseJsonSchema`
    (Dashboard V2.5 - Issue #39).
    """
    if not isinstance(schema, dict):
        return schema

    adapted: Dict[str, Any] = {}
    for k, v in schema.items():
        if k == "additionalProperties":
            continue
        elif k == "type":
            if isinstance(v, list):
                # e.g. ["string", "null"] -> "STRING", nullable=True
                types = [t for t in v if t != "null"]
                adapted["type"] = types[0].upper() if types else "STRING"
                if "null" in v:
                    adapted["nullable"] = True
            elif isinstance(v, str):
                adapted["type"] = v.upper()
            else:
                adapted["type"] = v
        elif k == "properties" and isinstance(v, dict):
            adapted["properties"] = {pk: to_gemini_schema(pv) for pk, pv in v.items()}
        elif k == "items" and isinstance(v, dict):
            adapted["items"] = to_gemini_schema(v)
        else:
            adapted[k] = v

    return adapted


def extract_json_block(text: str) -> str:
    """Extracts JSON substring from raw text or markdown code fence."""
    if not text:
        return ""
    text = text.strip()
    # Match ```json ... ``` or ``` ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Or find first { and last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1].strip()
    return text


def resolve_gemini_key(raise_on_missing: bool = False) -> Optional[str]:
    """Resolves Gemini API Key from GEMINI_API_KEY or GEMINI_KEY_URL."""
    key = os.environ.get("GEMINI_API_KEY")
    if key and key.strip():
        return key.strip()

    key_url = os.environ.get("GEMINI_KEY_URL")
    if key_url:
        try:
            r = requests.get(
                key_url,
                headers={"x-internal-token": os.environ.get("INTERNAL_API_TOKEN", "")},
                timeout=15,
            )
            if r.status_code == 200:
                k = r.json().get("api_key")
                if k and k.strip():
                    return k.strip()
        except Exception as e:
            if raise_on_missing:
                sys.exit(f"[錯誤] 向後台取 Gemini key 失敗：{e}")

    if raise_on_missing:
        sys.exit("[錯誤] 未設定 GEMINI_API_KEY，也未設定 GEMINI_KEY_URL")
    return None


class AIProvider(ABC):
    """Abstract Base Class for AI Providers."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Returns the canonical provider name, e.g. 'gemini' or 'openrouter'."""
        pass

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Returns the model name used for inference."""
        pass

    @abstractmethod
    def is_configured(self) -> bool:
        """Returns True if the provider has valid credentials / configuration."""
        pass

    @abstractmethod
    def get_api_key(self) -> str:
        """Returns the resolved API key or raises RuntimeError if missing."""
        pass

    @abstractmethod
    def analyze(
        self,
        prompt: str,
        schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Executes structured analysis against the model and returns a dictionary."""
        pass


class GeminiProvider(AIProvider):
    """Google Gemini AI Provider implementation."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        use_legacy_schema: bool = False,
        api_type: str = "interactions",
    ) -> None:
        self._explicit_key = api_key
        self._model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
        self._temperature = temperature
        self._use_legacy_schema = use_legacy_schema
        self._api_type = api_type

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return self._model

    def _is_gemini_3_x(self, model: str) -> bool:
        """Checks if model belongs to Gemini 3.x or modern reasoning generations (e.g. 2.5/3.x)."""
        m = (model or "").lower()
        return "gemini-3" in m or "gemini-2.5" in m or "gemini-exp" in m

    def is_configured(self) -> bool:
        if self._explicit_key:
            return True
        try:
            import crash_trend.analyze_gemini as ag
            if hasattr(ag, "resolve_api_key") and ag.resolve_api_key(raise_on_missing=False):
                return True
        except ImportError:
            pass
        return bool(resolve_gemini_key(raise_on_missing=False))

    def get_api_key(self) -> str:
        if self._explicit_key:
            return self._explicit_key
        try:
            import crash_trend.analyze_gemini as ag
            if hasattr(ag, "resolve_api_key"):
                k = ag.resolve_api_key(raise_on_missing=False)
                if k:
                    return k
        except ImportError:
            pass
        key = resolve_gemini_key(raise_on_missing=True)
        if not key:
            raise RuntimeError("Gemini API key is not configured")
        return key

    def analyze(
        self,
        prompt: str,
        schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        key = self.get_api_key()
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": key,
        }

        effective_schema = schema or CANONICAL_AI_RESPONSE_SCHEMA

        if self._api_type == "interactions":
            url = GEMINI_INTERACTIONS_API_URL
            body: Dict[str, Any] = {
                "model": self._model,
                "input": prompt,
            }
            if effective_schema:
                body["response_format"] = {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": effective_schema,
                }
            if self._temperature is not None:
                body["generation_config"] = {"temperature": self._temperature}
            elif not self._is_gemini_3_x(self._model):
                body["generation_config"] = {"temperature": 0.2}
        else:
            # Legacy generateContent endpoint fallback (OpenAPI 3.0 or legacy responseJsonSchema)
            url = GEMINI_API_URL_TEMPLATE.format(model=self._model)
            generation_config: Dict[str, Any] = {
                "responseMimeType": "application/json",
            }
            if self._temperature is not None:
                generation_config["temperature"] = self._temperature
            elif not self._is_gemini_3_x(self._model):
                generation_config["temperature"] = 0.2

            if effective_schema:
                if self._use_legacy_schema:
                    generation_config["responseSchema"] = to_gemini_schema(effective_schema)
                else:
                    generation_config["responseJsonSchema"] = effective_schema

            body = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": generation_config,
            }

        last_err: Optional[Exception] = None
        for attempt in (1, 2, 3):
            try:
                r = requests.post(
                    url,
                    headers=headers,
                    json=body,
                    timeout=300,
                )
                if r.status_code in (429, 500, 503) and attempt < 3:
                    time.sleep(2 * attempt)
                    continue
                if r.status_code != 200:
                    raise RuntimeError(f"Gemini API 回傳狀態碼 {r.status_code}：{r.text[:300]}")

                data = r.json()

                # Extract tokens from usage_metadata (Interactions API) or usageMetadata (legacy)
                usage = data.get("usage_metadata") or data.get("usageMetadata")
                if usage and isinstance(usage, dict):
                    self.last_tokens = {
                        "prompt_tokens": usage.get("prompt_token_count") or usage.get("promptTokenCount"),
                        "completion_tokens": usage.get("candidates_token_count") or usage.get("candidatesTokenCount"),
                        "total_tokens": usage.get("total_token_count") or usage.get("totalTokenCount"),
                    }
                else:
                    self.last_tokens = None

                # Extract text output from Interactions API steps or legacy candidates
                text = None
                steps = data.get("steps")
                if steps and isinstance(steps, list):
                    for step in steps:
                        if isinstance(step, dict) and step.get("type") == "model_output":
                            content_parts = step.get("content") or []
                            for part in content_parts:
                                if isinstance(part, dict) and part.get("text"):
                                    text = part["text"]
                                    break
                        if text:
                            break

                # Backward-compatibility fallback for legacy generateContent candidates structure or output_text
                if not text and "candidates" in data and data["candidates"]:
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                elif not text and "output_text" in data:
                    text = data["output_text"]

                if not text:
                    raise ValueError(f"No text content found in Gemini response: {data}")

                cleaned_text = extract_json_block(text)
                return json.loads(cleaned_text)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = e
                if attempt < 3:
                    time.sleep(2 * attempt)
                    continue
                raise RuntimeError(f"Gemini API 連線逾時（已重試 3 次）：{e}") from e
            except json.JSONDecodeError as e:
                last_err = e
                if attempt < 3:
                    time.sleep(2)
                    continue
                raise RuntimeError(f"Gemini 回傳非合法 JSON：{e}") from e

        raise last_err or RuntimeError("Gemini API 調用失敗")


class OpenRouterProvider(AIProvider):
    """OpenRouter Provider implementation supporting multi-model routing with strict schema enforcement."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        api_url: Optional[str] = None,
        zdr: bool = True,
    ) -> None:
        self._explicit_key = api_key
        self._model = model or os.environ.get("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL
        self._api_url = api_url or os.environ.get("OPENROUTER_API_URL") or OPENROUTER_API_URL
        self._zdr = zdr

    @property
    def provider_name(self) -> str:
        return "openrouter"

    @property
    def model_name(self) -> str:
        return self._model

    def is_configured(self) -> bool:
        key = self._explicit_key or os.environ.get("OPENROUTER_API_KEY")
        return bool(key and key.strip())

    def get_api_key(self) -> str:
        key = self._explicit_key or os.environ.get("OPENROUTER_API_KEY")
        if not key or not key.strip():
            raise RuntimeError("OpenRouter API key is not configured (missing OPENROUTER_API_KEY)")
        return key.strip()

    def analyze(
        self,
        prompt: str,
        schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        key = self.get_api_key()
        effective_schema = schema or CANONICAL_AI_RESPONSE_SCHEMA

        headers = {
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "https://github.com/qpalzm963/crash-trend",
            "X-Title": "crash-trend",
            "Content-Type": "application/json",
        }

        body: Dict[str, Any] = {
            "model": self._model,
            "max_tokens": 4096,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a stability intelligence assistant. "
                        "You must strictly output a valid JSON object matching the requested schema."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "crash_intelligence_response",
                    "strict": True,
                    "schema": effective_schema,
                },
            },
            "provider": {
                "data_collection": "deny",
                "zdr": self._zdr,
                "require_parameters": True,
            },
            "temperature": 0.2,
        }

        last_err: Optional[Exception] = None
        for attempt in (1, 2, 3):
            try:
                r = requests.post(
                    self._api_url,
                    headers=headers,
                    json=body,
                    timeout=300,
                )
                if r.status_code in (429, 500, 502, 503) and attempt < 3:
                    time.sleep(2 * attempt)
                    continue
                if r.status_code != 200:
                    raise RuntimeError(f"OpenRouter API 回傳狀態碼 {r.status_code}：{r.text[:300]}")

                data = r.json()
                usage = data.get("usage")
                if usage and isinstance(usage, dict):
                    self.last_tokens = {
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                        "total_tokens": usage.get("total_tokens"),
                    }
                else:
                    self.last_tokens = None

                choices = data.get("choices") or []
                if not choices:
                    raise RuntimeError(f"OpenRouter 回傳空白 choices：{data}")

                content = choices[0].get("message", {}).get("content", "")
                cleaned_text = extract_json_block(content)
                return json.loads(cleaned_text)
            except (requests.Timeout, requests.ConnectionError) as e:
                last_err = e
                if attempt < 3:
                    time.sleep(2 * attempt)
                    continue
                raise RuntimeError(f"OpenRouter API 連線逾時（已重試 3 次）：{e}") from e
            except json.JSONDecodeError as e:
                last_err = e
                if attempt < 3:
                    time.sleep(2)
                    continue
                raise RuntimeError(f"OpenRouter 回傳非合法 JSON：{e}") from e

        raise last_err or RuntimeError("OpenRouter API 調用失敗")


def get_ai_provider(
    app_cfg: Optional[Dict[str, Any]] = None,
    global_cfg: Optional[Dict[str, Any]] = None,
) -> AIProvider:
    """Factory function resolving the configured AIProvider instance.

    Resolution rules (Issue #26 / Review #5099212339):
      1. Fail-fast on unknown provider (must be in SUPPORTED_PROVIDERS).
      2. Provider scoping for model:
         - App model used only if app provider matches, or app did not change provider.
         - Global model used only if global provider matches.
         - Otherwise defaults to provider default (never cross-inherits another provider's model).
      3. Provider scoping for credentials:
         - Gemini credentials only passed to GeminiProvider.
         - OpenRouter credentials only passed to OpenRouterProvider.
    """
    app_ai = (app_cfg.get("ai") or {}) if app_cfg else {}
    global_ai = (global_cfg.get("ai") or {}) if global_cfg else {}

    raw_provider = app_ai.get("provider") or global_ai.get("provider") or os.environ.get("AI_PROVIDER")

    if raw_provider:
        provider_name = str(raw_provider).strip().lower()
        if provider_name not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"Unknown AI provider: '{raw_provider}'. Supported providers: {sorted(SUPPORTED_PROVIDERS)}"
            )
    else:
        # Auto-detect from environment if not explicitly configured in yaml
        has_gemini = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GEMINI_KEY_URL"))
        has_openrouter = bool(os.environ.get("OPENROUTER_API_KEY"))

        if has_openrouter and not has_gemini:
            provider_name = "openrouter"
        else:
            provider_name = "gemini"

    # Provider-scoped model resolution
    model: Optional[str] = None
    app_has_provider = bool(app_ai.get("provider"))
    app_provider_normalized = str(app_ai.get("provider", "")).strip().lower()

    if app_has_provider and app_provider_normalized == provider_name and app_ai.get("model"):
        model = app_ai["model"]
    elif not app_has_provider and app_ai.get("model"):
        # App did not change provider, but specified model for inherited provider
        model = app_ai["model"]
    elif str(global_ai.get("provider", "gemini")).strip().lower() == provider_name and global_ai.get("model"):
        model = global_ai["model"]
    elif os.environ.get("AI_MODEL"):
        model = os.environ["AI_MODEL"]
    else:
        model = DEFAULT_OPENROUTER_MODEL if provider_name == "openrouter" else DEFAULT_GEMINI_MODEL

    # Provider-scoped credential resolution
    if provider_name == "openrouter":
        api_key = None
        if app_has_provider and app_provider_normalized == "openrouter" and app_ai.get("api_key"):
            api_key = app_ai["api_key"]
        elif str(global_ai.get("provider", "")).strip().lower() == "openrouter" and global_ai.get("api_key"):
            api_key = global_ai["api_key"]

        zdr = app_ai.get("zdr", global_ai.get("zdr", True))
        return OpenRouterProvider(
            api_key=api_key,
            model=model,
            zdr=bool(zdr),
        )

    # Gemini provider
    api_key = None
    if (not app_has_provider or app_provider_normalized == "gemini") and app_ai.get("api_key"):
        api_key = app_ai["api_key"]
    elif str(global_ai.get("provider", "gemini")).strip().lower() == "gemini" and global_ai.get("api_key"):
        api_key = global_ai["api_key"]

    temp = app_ai.get("temperature", global_ai.get("temperature"))
    return GeminiProvider(
        api_key=api_key,
        model=model,
        temperature=temp,
    )


def get_ai_router(
    app_cfg: Optional[Dict[str, Any]] = None,
    global_cfg: Optional[Dict[str, Any]] = None,
):
    """Lazy imports and invokes get_ai_router from ai_router."""
    try:
        from .ai_router import get_ai_router as _get_ai_router
    except ImportError:
        from crash_trend.ai_router import get_ai_router as _get_ai_router
    return _get_ai_router(app_cfg=app_cfg, global_cfg=global_cfg)

