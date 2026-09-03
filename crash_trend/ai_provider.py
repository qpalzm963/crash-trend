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

DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
DEFAULT_OPENROUTER_MODEL = "google/gemini-2.0-flash-001"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


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
    ) -> None:
        self._explicit_key = api_key
        self._model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return self._model

    def is_configured(self) -> bool:
        if self._explicit_key:
            return True
        try:
            from crash_trend.analyze_gemini import resolve_api_key
            if resolve_api_key(raise_on_missing=False):
                return True
        except ImportError:
            pass
        return bool(resolve_gemini_key(raise_on_missing=False))

    def get_api_key(self) -> str:
        key = self._explicit_key or resolve_gemini_key(raise_on_missing=True)
        if not key:
            raise RuntimeError("Gemini API key is not configured")
        return key

    def analyze(
        self,
        prompt: str,
        schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        key = self.get_api_key()
        url = GEMINI_API_URL_TEMPLATE.format(model=self._model)

        generation_config: Dict[str, Any] = {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        }
        if schema:
            generation_config["responseSchema"] = schema

        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }

        last_err: Optional[Exception] = None
        for attempt in (1, 2, 3):
            try:
                r = requests.post(
                    url,
                    params={"key": key},
                    json=body,
                    timeout=300,
                )
                if r.status_code in (429, 500, 503) and attempt < 3:
                    time.sleep(2 * attempt)
                    continue
                if r.status_code != 200:
                    raise RuntimeError(f"Gemini API 回傳狀態碼 {r.status_code}：{r.text[:300]}")

                data = r.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
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
    """OpenRouter Provider implementation supporting multi-model routing."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        api_url: Optional[str] = None,
    ) -> None:
        self._explicit_key = api_key
        self._model = model or os.environ.get("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL
        self._api_url = api_url or os.environ.get("OPENROUTER_API_URL") or OPENROUTER_API_URL

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

        headers = {
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "https://github.com/qpalzm963/crash-trend",
            "X-Title": "crash-trend",
            "Content-Type": "application/json",
        }

        body: Dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
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

    Priority:
      1. app_cfg["ai"] (per-app override)
      2. global_cfg["ai"] (global apps.yaml setting)
      3. Environment variables (OPENROUTER_API_KEY vs GEMINI_API_KEY / GEMINI_KEY_URL)
      4. Default: GeminiProvider
    """
    app_ai = (app_cfg.get("ai") or {}) if app_cfg else {}
    global_ai = (global_cfg.get("ai") or {}) if global_cfg else {}

    # Check provider name
    provider_name = (
        app_ai.get("provider")
        or global_ai.get("provider")
        or os.environ.get("AI_PROVIDER")
    )

    if provider_name:
        provider_name = str(provider_name).strip().lower()
    else:
        # Auto-detect from environment if not explicitly configured in yaml
        has_gemini = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GEMINI_KEY_URL"))
        has_openrouter = bool(os.environ.get("OPENROUTER_API_KEY"))

        if has_openrouter and not has_gemini:
            provider_name = "openrouter"
        else:
            provider_name = "gemini"

    # Check model
    model = (
        app_ai.get("model")
        or global_ai.get("model")
        or os.environ.get("AI_MODEL")
    )

    # Check API key override
    api_key = (
        app_ai.get("api_key")
        or global_ai.get("api_key")
    )

    if provider_name == "openrouter":
        return OpenRouterProvider(
            api_key=api_key,
            model=model,
        )

    return GeminiProvider(
        api_key=api_key,
        model=model,
    )
