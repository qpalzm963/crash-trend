"""Unit tests for AI Provider Abstraction (Issue #26).

Tests cover GeminiProvider, OpenRouterProvider, factory resolution,
JSON extraction, schema compliance, and credential sanitization.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import requests

from crash_trend.ai_provider import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    GeminiProvider,
    OpenRouterProvider,
    extract_json_block,
    get_ai_provider,
)
from crash_trend.analyze_gemini import enrich_app_data_with_priority_and_ai
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
    def test_openrouter_provider_success_with_fenced_json(self, mock_post: MagicMock) -> None:
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

        provider = OpenRouterProvider(api_key="sk-or-v1-fake-key", model="google/gemini-2.0-flash-001")
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
        self.assertEqual(body["response_format"], {"type": "json_object"})

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

        from crash_trend.config import ROOT
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


if __name__ == "__main__":
    unittest.main()
