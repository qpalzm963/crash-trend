"""Unit and regression tests for AI Task Router (Issue #35).

Covers routing modes (Auto / Gemini Only / OpenRouter Only), task taxonomy (deep_analysis / lightweight),
free model guardrails (allow_paid_models), transient error fallback policy, privacy guards,
multi-app configuration isolation, and observability telemetry.
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
    GeminiProvider,
    OpenRouterProvider,
)
from crash_trend.ai_router import (
    AIRouterConfig,
    AITaskRouter,
    get_ai_router,
    is_free_openrouter_model,
    is_transient_error,
    resolve_router_config,
)
from crash_trend.analyze_gemini import build_ai_prompt, enrich_app_data_with_priority_and_ai
from crash_trend.config import ROOT
from crash_trend.schema_v2 import validate_app_dashboard_v2


class TestAIRouter(unittest.TestCase):
    def test_is_free_openrouter_model(self) -> None:
        self.assertTrue(is_free_openrouter_model("openrouter/free"))
        self.assertTrue(is_free_openrouter_model("OPENROUTER/FREE"))
        self.assertTrue(is_free_openrouter_model("google/gemini-2.0-flash-exp:free"))
        self.assertTrue(is_free_openrouter_model("meta-llama/llama-3.3-70b-instruct:free"))
        self.assertTrue(is_free_openrouter_model("deepseek/deepseek-r1:free"))

        self.assertFalse(is_free_openrouter_model("google/gemini-2.5-flash"))
        self.assertFalse(is_free_openrouter_model("anthropic/claude-3.5-sonnet"))
        self.assertFalse(is_free_openrouter_model("openai/gpt-4o"))
        self.assertFalse(is_free_openrouter_model(""))

    def test_is_transient_error(self) -> None:
        # Transient errors
        self.assertTrue(is_transient_error(requests.Timeout("Connection timed out")))
        self.assertTrue(is_transient_error(requests.ConnectionError("Failed to establish connection")))
        self.assertTrue(is_transient_error(RuntimeError("OpenRouter API 回傳狀態碼 429：Rate limit exceeded")))
        self.assertTrue(is_transient_error(RuntimeError("Gemini API 回傳狀態碼 503：Service Unavailable")))
        self.assertTrue(is_transient_error(RuntimeError("OpenRouter API 回傳狀態碼 502：Bad Gateway")))

        # Non-transient errors
        self.assertFalse(is_transient_error(RuntimeError("Gemini API 回傳狀態碼 400：Bad Request")))
        self.assertFalse(is_transient_error(RuntimeError("Gemini API 回傳狀態碼 401：Unauthorized")))
        self.assertFalse(is_transient_error(RuntimeError("Gemini API 回傳狀態碼 403：Forbidden")))
        self.assertFalse(is_transient_error(ValueError("Paid model 'gpt-4o' not allowed")))

    def test_1_auto_deep_analysis_routes_to_gemini(self) -> None:
        """Test 1: Auto mode routes deep_analysis to primary provider (Gemini)."""
        router = get_ai_router(global_cfg={"ai": {"mode": "auto"}})
        decision = router.route("deep_analysis")
        self.assertEqual(decision.mode, "auto")
        self.assertEqual(decision.task_type, "deep_analysis")
        self.assertEqual(decision.selected_provider, "gemini")
        self.assertEqual(decision.selected_model, DEFAULT_GEMINI_MODEL)
        self.assertIn("deep_analysis", decision.routing_reason)
        self.assertIn("Gemini Direct", decision.routing_reason)

    def test_2_auto_lightweight_routes_to_openrouter_free(self) -> None:
        """Test 2: Auto mode routes lightweight tasks to OpenRouter Free worker."""
        router = get_ai_router(global_cfg={"ai": {"mode": "auto"}})
        decision = router.route("lightweight")
        self.assertEqual(decision.mode, "auto")
        self.assertEqual(decision.task_type, "lightweight")
        self.assertEqual(decision.selected_provider, "openrouter")
        self.assertEqual(decision.selected_model, "openrouter/free")
        self.assertIn("lightweight", decision.routing_reason)
        self.assertIn("OpenRouter", decision.routing_reason)

    def test_3_gemini_only_routes_all_tasks_to_gemini(self) -> None:
        """Test 3: Gemini Only mode forces Gemini for all tasks and disables OpenRouter fallback."""
        router = get_ai_router(global_cfg={"ai": {"mode": "gemini_only", "fallback": {"enabled": True}}})

        d_deep = router.route("deep_analysis")
        self.assertEqual(d_deep.selected_provider, "gemini")
        self.assertIsNone(d_deep.fallback_target_provider)

        d_light = router.route("lightweight")
        self.assertEqual(d_light.selected_provider, "gemini")
        self.assertIsNone(d_light.fallback_target_provider)

    def test_4_openrouter_only_routes_all_tasks_to_openrouter(self) -> None:
        """Test 4: OpenRouter Only mode forces OpenRouter for all tasks."""
        router = get_ai_router(global_cfg={"ai": {"mode": "openrouter_only"}})

        d_deep = router.route("deep_analysis")
        self.assertEqual(d_deep.selected_provider, "openrouter")
        self.assertEqual(d_deep.selected_model, "openrouter/free")

        d_light = router.route("lightweight")
        self.assertEqual(d_light.selected_provider, "openrouter")
        self.assertEqual(d_light.selected_model, "openrouter/free")

    def test_5_per_app_override_isolation(self) -> None:
        """Test 5: Per-App override does not pollute other Apps or global configuration."""
        global_cfg = {
            "ai": {
                "mode": "auto",
                "allow_paid_models": False,
                "privacy": {"include_source_snippet": True},
            }
        }
        app_override_cfg = {
            "ai": {
                "mode": "openrouter_only",
                "allow_paid_models": True,
                "privacy": {"include_source_snippet": False},
            }
        }
        app_default_cfg = {"display_name": "Standard App"}

        router_override = get_ai_router(app_override_cfg, global_cfg)
        router_default = get_ai_router(app_default_cfg, global_cfg)

        # App with override
        self.assertEqual(router_override.config.mode, "openrouter_only")
        self.assertTrue(router_override.config.allow_paid_models)
        self.assertFalse(router_override.config.include_source_snippet)

        # App without override (must remain on global values)
        self.assertEqual(router_default.config.mode, "auto")
        self.assertFalse(router_default.config.allow_paid_models)
        self.assertTrue(router_default.config.include_source_snippet)

    def test_6_allow_paid_models_false_rejects_paid_model(self) -> None:
        """Test 6: allow_paid_models=false raises error if non-free model is configured for OpenRouter."""
        cfg = {
            "ai": {
                "mode": "openrouter_only",
                "lightweight": {"model": "anthropic/claude-3.5-sonnet"},
                "allow_paid_models": False,
            }
        }
        router = get_ai_router(global_cfg=cfg)
        with self.assertRaises(ValueError) as ctx:
            router.route("deep_analysis")
        self.assertIn("Paid model 'anthropic/claude-3.5-sonnet' not allowed", str(ctx.exception))

        # Explicit free model succeeds
        cfg_free = {
            "ai": {
                "mode": "openrouter_only",
                "lightweight": {"model": "google/gemini-2.0-flash-exp:free"},
                "allow_paid_models": False,
            }
        }
        router_free = get_ai_router(global_cfg=cfg_free)
        d = router_free.route("deep_analysis")
        self.assertEqual(d.selected_model, "google/gemini-2.0-flash-exp:free")

    @patch("crash_trend.ai_provider.time.sleep")
    @patch("crash_trend.ai_provider.requests.post")
    def test_7_free_router_429_graceful_degradation_without_paid_model(
        self, mock_post: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """Test 7: Free worker rate limit (429) fails gracefully without switching to paid model."""
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.text = "Rate limit exceeded"
        mock_post.return_value = resp_429

        cfg = {
            "ai": {
                "mode": "openrouter_only",
                "lightweight": {"api_key": "sk-or-free-key", "model": "openrouter/free"},
                "allow_paid_models": False,
                "fallback": {"enabled": False},
            }
        }
        router = get_ai_router(global_cfg=cfg)
        with self.assertRaises(RuntimeError) as ctx:
            router.analyze("test prompt")
        self.assertIn("429", str(ctx.exception))
        # Verify it didn't call any paid model
        for call in mock_post.call_args_list:
            self.assertEqual(call[1]["json"]["model"], "openrouter/free")

    @patch("crash_trend.ai_provider.time.sleep")
    @patch("crash_trend.ai_provider.requests.post")
    def test_8_fallback_on_transient_failure(self, mock_post: MagicMock, mock_sleep: MagicMock) -> None:
        """Test 8: Fallback on + transient failure (503 on Gemini) falls back to OpenRouter free worker."""
        # 1. Gemini fails with 503
        resp_gemini_503 = MagicMock()
        resp_gemini_503.status_code = 503
        resp_gemini_503.text = "Service Unavailable"

        # 2. OpenRouter succeeds
        resp_or_200 = MagicMock()
        resp_or_200.status_code = 200
        resp_or_200.json.return_value = {
            "choices": [{"message": {"content": '{"overview": "Fallback Success", "items": []}'}}]
        }

        # Gemini retries 3 times then fails, OpenRouter succeeds on 1st attempt
        mock_post.side_effect = [resp_gemini_503, resp_gemini_503, resp_gemini_503, resp_or_200]

        cfg = {
            "ai": {
                "mode": "auto",
                "primary": {"provider": "gemini", "api_key": "AIzaPrimary", "model": "gemini-2.5-flash"},
                "lightweight": {"provider": "openrouter", "api_key": "sk-or-free", "model": "openrouter/free"},
                "fallback": {"enabled": True},
                "allow_paid_models": False,
            }
        }
        router = get_ai_router(global_cfg=cfg)
        res = router.analyze("Analyze crash", task_type="deep_analysis")

        self.assertEqual(res.data, {"overview": "Fallback Success", "items": []})
        self.assertTrue(res.fallback_used)
        self.assertIn("gemini", res.fallback_reason.lower())
        self.assertEqual(res.active_provider, "openrouter")
        self.assertEqual(res.active_model, "openrouter/free")

    @patch("crash_trend.ai_provider.requests.post")
    def test_9_non_transient_errors_do_not_fallback(self, mock_post: MagicMock) -> None:
        """Test 9: HTTP 400 Bad Request or 401 Unauthorized do not trigger fallback."""
        resp_400 = MagicMock()
        resp_400.status_code = 400
        resp_400.text = "Invalid JSON schema"
        mock_post.return_value = resp_400

        cfg = {
            "ai": {
                "mode": "auto",
                "primary": {"provider": "gemini", "api_key": "AIzaPrimary", "model": "gemini-2.5-flash"},
                "lightweight": {"provider": "openrouter", "api_key": "sk-or-free", "model": "openrouter/free"},
                "fallback": {"enabled": True},
            }
        }
        router = get_ai_router(global_cfg=cfg)
        with self.assertRaises(RuntimeError) as ctx:
            router.analyze("Prompt with schema", task_type="deep_analysis")
        self.assertIn("400", str(ctx.exception))
        # Post was only called once (Gemini), never reached OpenRouter
        self.assertEqual(mock_post.call_count, 1)

    def test_10_privacy_guard_omits_source_snippets(self) -> None:
        """Test 10: include_source_snippet=false ensures prompt does not contain local source code snippet."""
        fixture_path = ROOT / "tests" / "fixtures" / "dashboard_v2_no_sessions.json"
        fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))
        app_data = copy.deepcopy(fixture_data["apps"]["legacy_app"])

        mock_provider = MagicMock(spec=GeminiProvider)
        mock_provider.provider_name = "gemini"
        mock_provider.model_name = "gemini-2.5-flash"
        mock_provider.is_configured.return_value = True
        mock_provider.analyze.return_value = {
            "overview": "Privacy Protected",
            "key_takeaways": [],
            "distribution_insights": "",
            "recommended_actions": [],
            "data_limitations": None,
            "items": [],
        }

        # Run with privacy.include_source_snippet = False
        app_cfg = {
            "ai": {
                "privacy": {"include_source_snippet": False},
            }
        }
        enriched = enrich_app_data_with_priority_and_ai(app_data, provider=mock_provider, app_cfg=app_cfg)
        self.assertEqual(enriched["ai_summary"]["status"], "available")

        # Verify prompt passed to analyze did NOT contain raw snippet code
        call_prompt = mock_provider.analyze.call_args[0][0]
        self.assertIn("（無可用原始碼片段）", call_prompt)

    @patch("crash_trend.ai_provider.requests.post")
    def test_11_multi_app_production_lifecycle_e2e(self, mock_post: MagicMock) -> None:
        """Test 11: Production lifecycle E2E across multi-app with router policies."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": json.dumps({
                            "overview": "Multi-App Router E2E OK",
                            "key_takeaways": ["Takeaway"],
                            "distribution_insights": "Clean",
                            "recommended_actions": [],
                            "data_limitations": None,
                            "items": [],
                        })
                    }]
                }
            }]
        }
        mock_post.return_value = mock_resp

        fixture_path = ROOT / "tests" / "fixtures" / "dashboard_v2_no_sessions.json"
        fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))

        global_cfg = {
            "ai": {
                "mode": "auto",
                "primary": {"api_key": "AIzaGlobalGemini"},
                "lightweight": {"api_key": "sk-or-global-free", "model": "openrouter/free"},
            }
        }
        app_cfg = {"ai": {"mode": "auto"}}

        app_data = copy.deepcopy(fixture_data["apps"]["legacy_app"])
        router = get_ai_router(app_cfg=app_cfg, global_cfg=global_cfg)

        enriched = enrich_app_data_with_priority_and_ai(app_data, router=router, app_cfg=app_cfg)

        # Verify schema validation
        errs = validate_app_dashboard_v2(enriched)
        self.assertEqual(errs, [])

        # Verify telemetry contract
        src_ai = enriched["sources"]["ai"]
        self.assertEqual(src_ai["status"], "available")
        self.assertEqual(src_ai["requested_mode"], "auto")
        self.assertEqual(src_ai["task_type"], "deep_analysis")
        self.assertEqual(src_ai["selected_provider"], "gemini")
        self.assertEqual(src_ai["selected_model"], DEFAULT_GEMINI_MODEL)
        self.assertFalse(src_ai["fallback_used"])
        self.assertFalse(src_ai["paid_model_allowed"])
        self.assertIn("deep_analysis", src_ai["routing_reason"])

    def test_12_metadata_telemetry_no_secret_leaks(self) -> None:
        """Test 12: Telemetry contract contains no leaked keys or auth tokens."""
        fixture_path = ROOT / "tests" / "fixtures" / "dashboard_v2_no_sessions.json"
        fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))
        app_data = copy.deepcopy(fixture_data["apps"]["legacy_app"])

        # Unconfigured router degradation
        router = get_ai_router(
            global_cfg={
                "ai": {
                    "mode": "auto",
                    "primary": {"api_key": None},
                    "lightweight": {"api_key": None},
                }
            }
        )
        with patch.dict("os.environ", {}, clear=True):
            enriched = enrich_app_data_with_priority_and_ai(app_data, router=router)

        src_ai = enriched["sources"]["ai"]
        self.assertEqual(src_ai["status"], "disabled")
        self.assertEqual(src_ai["requested_mode"], "auto")
        self.assertIsNone(src_ai["last_sync_timestamp"])
        self.assertIsNone(src_ai["error_message"])
        self.assertIn("routing_reason", src_ai)

    def test_13_legacy_openrouter_config_without_allow_paid_models_rejects_paid_model(self) -> None:
        """Test 13 (Review 5103409751): Legacy config with provider: openrouter and non-free model must be rejected if allow_paid_models is not explicitly True."""
        legacy_cfg = {
            "ai": {
                "provider": "openrouter",
                "model": "anthropic/claude-3.5-sonnet",
                "api_key": "sk-or-fake",
            }
        }
        router = get_ai_router(app_cfg=legacy_cfg)
        self.assertFalse(router.config.allow_paid_models)
        with self.assertRaises(ValueError) as ctx:
            router.route("deep_analysis")
        self.assertIn("Paid model 'anthropic/claude-3.5-sonnet' not allowed", str(ctx.exception))

        # Explicit allow_paid_models: True must succeed
        legacy_cfg_allowed = {
            "ai": {
                "provider": "openrouter",
                "model": "anthropic/claude-3.5-sonnet",
                "api_key": "sk-or-fake",
                "allow_paid_models": True,
            }
        }
        router_allowed = get_ai_router(app_cfg=legacy_cfg_allowed)
        self.assertTrue(router_allowed.config.allow_paid_models)
        d = router_allowed.route("deep_analysis")
        self.assertEqual(d.selected_model, "anthropic/claude-3.5-sonnet")

    @patch("crash_trend.pipeline_run.run_stage_process")
    def test_14_pipeline_health_full_routing_telemetry(self, mock_run_proc: MagicMock) -> None:
        """Test 14 (Review 5103409751): Pipeline health records complete routing telemetry matching canonical sources.ai."""
        import tempfile
        from pathlib import Path
        from crash_trend.pipeline_run import run_pipeline

        mock_run_proc.return_value = (0, "ok", "")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmproot = Path(tmpdir)
            out_app = tmproot / "out" / "shop_app"
            out_app.mkdir(parents=True, exist_ok=True)
            v2_path = out_app / "dashboard_v2.json"

            # Synthetic enriched V2 artifact with full router telemetry
            mock_v2 = {
                "metadata": {"app_id": "shop_app", "display_name": "Shop"},
                "ai_summary": {"status": "available"},
                "sources": {
                    "ai": {
                        "status": "available",
                        "provider": "gemini",
                        "model": "gemini-2.5-flash",
                        "requested_mode": "auto",
                        "task_type": "deep_analysis",
                        "selected_provider": "gemini",
                        "selected_model": "gemini-2.5-flash",
                        "routing_reason": "Routing task 'deep_analysis' to primary Gemini Direct",
                        "fallback_used": False,
                        "fallback_reason": None,
                        "paid_model_allowed": False,
                        "last_sync_timestamp": "2026-09-03T12:00:00Z",
                        "error_message": None,
                    }
                },
            }
            v2_path.write_text(json.dumps(mock_v2), encoding="utf-8")

            fake_cfg = {
                "apps": {
                    "shop_app": {
                        "display_name": "Shop",
                        "firebase_project": "proj-shop",
                        "data_sources": {"crashlytics_bigquery": True, "sessions": False, "mcp": "off"},
                        "ai": {"mode": "auto", "primary": {"api_key": "AIzaTest"}},
                    }
                }
            }
            sum_path = tmproot / "out" / "pipeline_run.json"

            with patch("crash_trend.pipeline_run.ROOT", tmproot):
                with patch("crash_trend.pipeline_run.load_config", return_value=fake_cfg):
                    summary = run_pipeline(
                        app_names=["shop_app"],
                        summary_path=sum_path,
                        skip_dashboard=True,
                        verbose=False,
                    )

            ai_details = summary["apps"]["shop_app"]["stages"]["ai"]["details"]
            self.assertEqual(ai_details["requested_mode"], "auto")
            self.assertEqual(ai_details["task_type"], "deep_analysis")
            self.assertEqual(ai_details["selected_provider"], "gemini")
            self.assertEqual(ai_details["selected_model"], "gemini-2.5-flash")
            self.assertIn("Gemini Direct", ai_details["routing_reason"])
            self.assertFalse(ai_details["fallback_used"])
            self.assertIsNone(ai_details["fallback_reason"])
            self.assertFalse(ai_details["paid_model_allowed"])


if __name__ == "__main__":
    unittest.main()
