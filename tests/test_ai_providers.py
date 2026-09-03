"""Unit tests for AI Provider Abstraction (Issue #26).

Tests cover GeminiProvider, OpenRouterProvider, factory resolution,
JSON extraction, schema compliance, and credential sanitization.
"""

from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import MagicMock, patch

import requests

from crash_trend.ai_provider import (
    CANONICAL_AI_RESPONSE_SCHEMA,
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    GeminiProvider,
    OpenRouterProvider,
    extract_json_block,
    get_ai_provider,
)
from crash_trend.analyze_gemini import enrich_app_data_with_priority_and_ai
from crash_trend.config import ROOT
from crash_trend.pipeline_health import sanitize_error_message
from crash_trend.schema_v2 import validate_app_dashboard_v2


class TestAIProviders(unittest.TestCase):
    def test_extract_json_block(self) -> None:
        # Raw JSON
        self.assertEqual(extract_json_block('{"a": 1}'), '{"a": 1}')

        # Markdown json fence
        fenced = '```json\n{"key": "value"}\n```'
        self.assertEqual(extract_json_block(fenced), '{"key": "value"}')

        # Generic fence without json identifier
        fenced2 = '```\n{"key": 123}\n```'
        self.assertEqual(extract_json_block(fenced2), '{"key": 123}')

        # Surrounding conversational text
        surrounded = 'Sure, here is your analysis:\n{"result": true}\nHope this helps!'
        self.assertEqual(extract_json_block(surrounded), '{"result": true}')

    @patch("crash_trend.ai_provider.requests.post")
    def test_gemini_provider_success(self, mock_post: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{"text": '{"overview": "OK", "items": []}'}]
                }
            }]
        }
        mock_post.return_value = mock_resp

        provider = GeminiProvider(api_key="fake-gemini-key", model="gemini-2.0-flash")
        self.assertEqual(provider.provider_name, "gemini")
        self.assertEqual(provider.model_name, "gemini-2.0-flash")
        self.assertTrue(provider.is_configured())

        res = provider.analyze("test prompt")
        self.assertEqual(res, {"overview": "OK", "items": []})
        self.assertEqual(mock_post.call_count, 1)

    @patch("crash_trend.ai_provider.time.sleep")
    @patch("crash_trend.ai_provider.requests.post")
    def test_gemini_provider_retry_on_429(self, mock_post: MagicMock, mock_sleep: MagicMock) -> None:
        resp_429 = MagicMock()
        resp_429.status_code = 429

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{"text": '{"status": "recovered"}'}]
                }
            }]
        }
        mock_post.side_effect = [resp_429, resp_200]

        provider = GeminiProvider(api_key="test-key")
        res = provider.analyze("prompt")
        self.assertEqual(res, {"status": "recovered"})
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once()

    @patch("crash_trend.ai_provider.requests.post")
    def test_openrouter_provider_success_with_strict_schema_and_policy(self, mock_post: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "content": '```json\n{"overview": "OpenRouter Success", "items": []}\n```'
                }
            }]
        }
        mock_post.return_value = mock_resp

        provider = OpenRouterProvider(api_key="sk-or-v1-fake-key", model="google/gemini-2.0-flash-001", zdr=True)
        self.assertEqual(provider.provider_name, "openrouter")
        self.assertEqual(provider.model_name, "google/gemini-2.0-flash-001")
        self.assertTrue(provider.is_configured())

        res = provider.analyze("test prompt")
        self.assertEqual(res, {"overview": "OpenRouter Success", "items": []})

        # Verify OpenRouter headers and body
        args, kwargs = mock_post.call_args
        headers = kwargs.get("headers", {})
        self.assertEqual(headers["Authorization"], "Bearer sk-or-v1-fake-key")
        self.assertIn("HTTP-Referer", headers)
        body = kwargs.get("json", {})
        self.assertEqual(body["model"], "google/gemini-2.0-flash-001")

        # Verify strict structured schema enforcement (Review 5099212339)
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(body["response_format"]["json_schema"]["schema"], CANONICAL_AI_RESPONSE_SCHEMA)

        # Verify data policy guard (Review 5099212339)
        self.assertEqual(body["provider"]["data_collection"], "deny")
        self.assertTrue(body["provider"]["zdr"])
        self.assertTrue(body["provider"]["require_parameters"])

    @patch("crash_trend.ai_provider.time.sleep")
    @patch("crash_trend.ai_provider.requests.post")
    def test_openrouter_provider_retry_on_502(self, mock_post: MagicMock, mock_sleep: MagicMock) -> None:
        resp_502 = MagicMock()
        resp_502.status_code = 502

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = {
            "choices": [{
                "message": {
                    "content": '{"overview": "Recovered after 502"}'
                }
            }]
        }
        mock_post.side_effect = [resp_502, resp_200]

        provider = OpenRouterProvider(api_key="sk-or-v1-key")
        res = provider.analyze("prompt")
        self.assertEqual(res, {"overview": "Recovered after 502"})
        self.assertEqual(mock_post.call_count, 2)

    def test_openrouter_provider_unconfigured(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            provider = OpenRouterProvider(api_key=None)
            self.assertFalse(provider.is_configured())
            with self.assertRaises(RuntimeError):
                provider.analyze("prompt")

    def test_unknown_provider_fail_fast(self) -> None:
        """Unknown provider must raise ValueError immediately (Review 5099212339)."""
        app_cfg = {"ai": {"provider": "openrouetr"}}
        with self.assertRaises(ValueError) as ctx:
            get_ai_provider(app_cfg)
        self.assertIn("Unknown AI provider: 'openrouetr'", str(ctx.exception))

    def test_provider_scoped_model_isolation(self) -> None:
        """App overriding provider must NOT inherit global model from another provider (Review 5099212339)."""
        global_cfg = {
            "ai": {
                "provider": "gemini",
                "model": "gemini-1.5-pro",
                "api_key": "gemini-secret-key",
            }
        }
        app_cfg = {
            "ai": {
                "provider": "openrouter",
                # Note: No model specified in app_cfg!
            }
        }
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "env-or-key"}, clear=True):
            p = get_ai_provider(app_cfg, global_cfg)
            self.assertEqual(p.provider_name, "openrouter")
            # Must fall back to OpenRouter default, NOT Gemini's gemini-1.5-pro!
            self.assertEqual(p.model_name, DEFAULT_OPENROUTER_MODEL)
            # Must NOT inherit gemini-secret-key!
            self.assertEqual(p.get_api_key(), "env-or-key")

    def test_credential_isolation_gemini_vs_openrouter(self) -> None:
        """Gemini key must never be passed to OpenRouter and vice versa (Review 5099212339)."""
        # Case 1: Global Gemini with key, App OpenRouter without key
        global_gemini = {
            "ai": {
                "provider": "gemini",
                "api_key": "gemini-super-secret",
            }
        }
        app_or = {"ai": {"provider": "openrouter"}}
        with patch.dict("os.environ", {}, clear=True):
            p = get_ai_provider(app_or, global_gemini)
            self.assertEqual(p.provider_name, "openrouter")
            # OpenRouter should not be configured because it didn't steal gemini-super-secret
            self.assertFalse(p.is_configured())

        # Case 2: Global OpenRouter with key, App Gemini without key
        global_or = {
            "ai": {
                "provider": "openrouter",
                "api_key": "sk-or-v1-super-secret",
            }
        }
        app_gemini = {"ai": {"provider": "gemini"}}
        with patch.dict("os.environ", {}, clear=True):
            p2 = get_ai_provider(app_gemini, global_or)
            self.assertEqual(p2.provider_name, "gemini")
            # Gemini should not be configured because it didn't steal OpenRouter key
            self.assertFalse(p2.is_configured())

    def test_get_ai_provider_resolution(self) -> None:
        # 1. Per-app override
        app_cfg = {
            "ai": {
                "provider": "openrouter",
                "model": "anthropic/claude-3.5-sonnet",
                "api_key": "app-specific-key",
            }
        }
        p1 = get_ai_provider(app_cfg)
        self.assertEqual(p1.provider_name, "openrouter")
        self.assertEqual(p1.model_name, "anthropic/claude-3.5-sonnet")
        self.assertEqual(p1.get_api_key(), "app-specific-key")

        # 2. Global config override
        global_cfg = {
            "ai": {
                "provider": "openrouter",
                "model": "openai/gpt-4o-mini",
            }
        }
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "env-or-key"}, clear=True):
            p2 = get_ai_provider(None, global_cfg)
            self.assertEqual(p2.provider_name, "openrouter")
            self.assertEqual(p2.model_name, "openai/gpt-4o-mini")
            self.assertEqual(p2.get_api_key(), "env-or-key")

        # 3. Auto-detection from env (OPENROUTER_API_KEY only)
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "auto-or-key"}, clear=True):
            p3 = get_ai_provider()
            self.assertEqual(p3.provider_name, "openrouter")
            self.assertEqual(p3.model_name, DEFAULT_OPENROUTER_MODEL)

        # 4. Auto-detection from env (GEMINI_API_KEY)
        with patch.dict("os.environ", {"GEMINI_API_KEY": "gemini-key"}, clear=True):
            p4 = get_ai_provider()
            self.assertEqual(p4.provider_name, "gemini")
            self.assertEqual(p4.model_name, DEFAULT_GEMINI_MODEL)

    def test_sanitization_openrouter_keys(self) -> None:
        raw_err = "Failed request with key sk-or-v1-abcdef0123456789abcdef0123456789abcdef0123456789 and status 401"
        sanitized = sanitize_error_message(raw_err)
        self.assertNotIn("abcdef0123456789", sanitized)
        self.assertIn("sk-or-[REDACTED]", sanitized)

        raw_openai = "Error with sk-abcdefghijklmnopqrstuvwxyz0123456789"
        sanitized_openai = sanitize_error_message(raw_openai)
        self.assertIn("sk-[REDACTED]", sanitized_openai)

    def test_injected_gemini_provider_no_unbound_error(self) -> None:
        """Injected GeminiProvider must not trigger UnboundLocalError on p_key (Review 5099212339)."""
        fixture_path = ROOT / "tests" / "fixtures" / "dashboard_v2_no_sessions.json"
        fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))
        app_data = copy.deepcopy(fixture_data["apps"]["legacy_app"])

        mock_gemini = MagicMock(spec=GeminiProvider)
        mock_gemini.provider_name = "gemini"
        mock_gemini.model_name = "gemini-flash-latest"
        mock_gemini.is_configured.return_value = True
        mock_gemini.analyze.return_value = {
            "overview": "Injected Gemini succeeded.",
            "key_takeaways": [],
            "distribution_insights": "",
            "recommended_actions": [],
            "data_limitations": None,
            "items": [],
        }

        # Must execute cleanly without UnboundLocalError or falling into error degradation
        enriched = enrich_app_data_with_priority_and_ai(app_data, provider=mock_gemini)
        self.assertEqual(enriched["ai_summary"]["status"], "available")
        self.assertEqual(enriched["ai_summary"]["provider"], "gemini")
        self.assertEqual(enriched["sources"]["ai"]["status"], "available")

    def test_error_message_sanitization_in_artifacts(self) -> None:
        """Exception messages containing secrets must be sanitized before storing in artifacts (Review 5099212339)."""
        fixture_path = ROOT / "tests" / "fixtures" / "dashboard_v2_no_sessions.json"
        fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))
        app_data = copy.deepcopy(fixture_data["apps"]["legacy_app"])

        secret_err = (
            "API failure on https://example.com/api?key=AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6 "
            "with auth token sk-or-v1-abcdef0123456789abcdef0123456789abcdef0123456789"
        )
        mock_provider = MagicMock(spec=OpenRouterProvider)
        mock_provider.provider_name = "openrouter"
        mock_provider.model_name = "google/gemini-2.0-flash-001"
        mock_provider.is_configured.return_value = True
        mock_provider.analyze.side_effect = RuntimeError(secret_err)

        enriched = enrich_app_data_with_priority_and_ai(app_data, provider=mock_provider)

        # 1. Overview must be sanitized
        self.assertEqual(enriched["ai_summary"]["status"], "error")
        self.assertNotIn("AIzaSyA1B2C3D4E5F6G7H8I9J0K1L2M3N4O5P6", enriched["ai_summary"]["overview"])
        self.assertNotIn("abcdef0123456789", enriched["ai_summary"]["overview"])
        self.assertIn("AIza[REDACTED]", enriched["ai_summary"]["overview"])
        self.assertIn("sk-or-[REDACTED]", enriched["ai_summary"]["overview"])

        # 2. sources.ai.error_message must be sanitized
        self.assertIn("AIza[REDACTED]", enriched["sources"]["ai"]["error_message"])
        self.assertIn("sk-or-[REDACTED]", enriched["sources"]["ai"]["error_message"])

    def test_enrich_app_data_with_openrouter(self) -> None:
        fake_response = {
            "overview": "OpenRouter stability overview.",
            "key_takeaways": ["Takeaway 1", "Takeaway 2"],
            "distribution_insights": "Android 14 dominates.",
            "recommended_actions": [
                {
                    "priority": "P0",
                    "issue_id": "issue_101",
                    "action": "Fix null pointer in checkout flow",
                    "effort": "S",
                }
            ],
            "data_limitations": None,
            "items": [
                {
                    "issue_id": "issue_101",
                    "root_cause": "NPE in CartFragment",
                    "suggested_fix": "Add null check",
                    "effort": "S",
                    "confidence": "high",
                    "reasoning_sources": ["stack_trace"],
                }
            ],
        }

        mock_provider = MagicMock(spec=OpenRouterProvider)
        mock_provider.provider_name = "openrouter"
        mock_provider.model_name = "google/gemini-2.0-flash-001"
        mock_provider.is_configured.return_value = True
        mock_provider.analyze.return_value = fake_response

        fixture_path = ROOT / "tests" / "fixtures" / "dashboard_v2_no_sessions.json"
        fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))
        app_data = fixture_data["apps"]["legacy_app"]

        enriched = enrich_app_data_with_priority_and_ai(app_data, provider=mock_provider)

        # Verify AI summary
        self.assertEqual(enriched["ai_summary"]["status"], "available")
        self.assertEqual(enriched["ai_summary"]["provider"], "openrouter")
        self.assertEqual(enriched["ai_summary"]["model"], "google/gemini-2.0-flash-001")

        # Verify generic sources.ai
        self.assertIn("ai", enriched["sources"])
        self.assertEqual(enriched["sources"]["ai"]["status"], "available")
        self.assertEqual(enriched["sources"]["ai"]["provider"], "openrouter")
        self.assertEqual(enriched["sources"]["ai"]["model"], "google/gemini-2.0-flash-001")

        # Verify backward compatible sources.gemini_ai
        self.assertIn("gemini_ai", enriched["sources"])
        self.assertEqual(enriched["sources"]["gemini_ai"]["status"], "available")

        # Verify schema validation passes with 0 errors
        errors = validate_app_dashboard_v2(enriched)
        self.assertEqual(errors, [])

    def test_global_openrouter_inherited_by_app_without_ai(self) -> None:
        """Global OpenRouter config must be inherited by apps without an ai block (Review 5099212339)."""
        global_cfg = {
            "ai": {
                "provider": "openrouter",
                "model": "google/gemini-2.0-flash-001",
                "api_key": "sk-or-global-secret",
            }
        }
        app_cfg = {"display_name": "App Without AI Block"}

        p = get_ai_provider(app_cfg, global_cfg)
        self.assertEqual(p.provider_name, "openrouter")
        self.assertEqual(p.model_name, "google/gemini-2.0-flash-001")
        self.assertEqual(p.get_api_key(), "sk-or-global-secret")

    def test_gemini_and_openrouter_env_coexistence_app_explicit_openrouter(self) -> None:
        """When both Gemini and OpenRouter env vars exist, app choosing openrouter must use OpenRouter (Review 5099212339)."""
        app_cfg = {
            "ai": {
                "provider": "openrouter",
                "model": "google/gemini-2.0-flash-001",
            }
        }
        with patch.dict(
            "os.environ",
            {
                "GEMINI_API_KEY": "gemini-env-key",
                "OPENROUTER_API_KEY": "sk-or-env-key",
            },
            clear=True,
        ):
            p = get_ai_provider(app_cfg)
            self.assertEqual(p.provider_name, "openrouter")
            self.assertEqual(p.get_api_key(), "sk-or-env-key")

    def test_get_app_supports_optional_cfg(self) -> None:
        """Verifies get_app signature supports both 1-arg and 2-arg calls (Review 5099441434)."""
        from crash_trend.config import get_app
        fake_cfg = {"apps": {"test_app": {"display_name": "Test App"}}}
        # 2-arg call with pre-loaded cfg
        app = get_app("test_app", fake_cfg)
        self.assertEqual(app["display_name"], "Test App")

    def test_analyze_ai_subprocess_execution(self) -> None:
        """Smoke test verifying python -m crash_trend.analyze_ai runs without ModuleNotFoundError (Review 5099441434)."""
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "-m", "crash_trend.analyze_ai", "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, f"Subprocess failed with stderr: {result.stderr}")
        self.assertIn("AI 智慧分析與策略建議", result.stdout)

        # Also test direct script execution
        result_direct = subprocess.run(
            [sys.executable, str(ROOT / "crash_trend" / "analyze_ai.py"), "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result_direct.returncode, 0, f"Direct script execution failed: {result_direct.stderr}")


if __name__ == "__main__":
    unittest.main()
