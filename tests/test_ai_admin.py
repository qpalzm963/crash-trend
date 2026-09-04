"""Tests for AI Policy Admin Controls & Configuration Service (Dashboard V2.5 - Issue #41)."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from crash_trend.ai_config_service import (
    get_effective_ai_policy,
    reset_app_ai_policy,
    update_ai_policy,
    validate_ai_policy_update,
)
from crash_trend.ai_provider import DEFAULT_GEMINI_MODEL

SAMPLE_CONFIG = {
    "credentials": {"bq_service_account": "/tmp/fake_sa.json"},
    "ai": {
        "mode": "auto",
        "primary": {"provider": "gemini", "model": DEFAULT_GEMINI_MODEL, "api_key": "AIzaSecretGlobalKey"},
        "lightweight": {"provider": "openrouter", "model": "openrouter/free", "api_key": "sk-or-secret-global"},
        "allow_paid_models": False,
        "privacy": {"include_source_snippet": True},
        "fallback": {"enabled": False},
    },
    "apps": {
        "shop_app": {
            "display_name": "Shop App",
            "firebase_project": "proj-shop",
            "data_sources": {"crashlytics_bigquery": True, "sessions": False},
        },
        "custom_app": {
            "display_name": "Custom App",
            "firebase_project": "proj-custom",
            "ai": {
                "mode": "gemini_only",
                "primary": {"model": "gemini-2.5-pro", "api_key": "AIzaSecretAppKey"},
            },
        },
    },
}


class TestAIAdmin(unittest.TestCase):
    """Test suite for Issue #41 AI Policy Admin Controls."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp_dir.name) / "apps.yaml"
        self.cfg_path.write_text(yaml.dump(copy.deepcopy(SAMPLE_CONFIG), sort_keys=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_1_get_effective_ai_policy_redacts_secrets(self) -> None:
        """Test 1: get_effective_ai_policy resolves configuration without leaking API keys."""
        with open(self.cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        # Global policy
        g_policy = get_effective_ai_policy(None, cfg)
        self.assertEqual(g_policy["mode"], "auto")
        self.assertEqual(g_policy["primary_model"], DEFAULT_GEMINI_MODEL)
        self.assertEqual(g_policy["lightweight_model"], "openrouter/free")
        self.assertFalse(g_policy["allow_paid_models"])
        self.assertFalse(g_policy["has_per_app_override"])
        # Verify secret keys are completely absent from payload
        raw_str = json.dumps(g_policy)
        self.assertNotIn("AIzaSecretGlobalKey", raw_str)
        self.assertNotIn("sk-or-secret-global", raw_str)

        # App with override
        app_policy = get_effective_ai_policy("custom_app", cfg)
        self.assertEqual(app_policy["mode"], "gemini_only")
        self.assertEqual(app_policy["primary_model"], "gemini-2.5-pro")
        self.assertTrue(app_policy["has_per_app_override"])
        raw_app_str = json.dumps(app_policy)
        self.assertNotIn("AIzaSecretAppKey", raw_app_str)

    def test_2_update_ai_policy_global_mode_switch(self) -> None:
        """Test 2: Switching global mode updates apps.yaml and retains other configuration."""
        updated = update_ai_policy(
            app_name=None,
            updates={"mode": "openrouter_only", "fallback_enabled": True},
            config_path=self.cfg_path,
        )
        self.assertEqual(updated["mode"], "openrouter_only")
        self.assertTrue(updated["fallback_enabled"])

        # Verify disk persistence and credentials intact
        disk_cfg = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(disk_cfg["ai"]["mode"], "openrouter_only")
        self.assertTrue(disk_cfg["ai"]["fallback"]["enabled"])
        self.assertEqual(disk_cfg["credentials"]["bq_service_account"], "/tmp/fake_sa.json")

    def test_3_cost_guard_blocks_paid_models_by_default(self) -> None:
        """Test 3: Free Guard strictly rejects paid models when allow_paid_models is false."""
        with self.assertRaises(ValueError) as ctx:
            update_ai_policy(
                app_name="shop_app",
                updates={"lightweight_model": "anthropic/claude-3.5-sonnet"},
                config_path=self.cfg_path,
            )
        self.assertIn("Cost Guard Violation", str(ctx.exception))

    def test_4_paid_model_opt_in_requires_explicit_confirmation(self) -> None:
        """Test 4: Enabling paid models requires explicit user action (confirm_paid_opt_in)."""
        # Attempting without explicit_paid_opt_in -> Rejected!
        with self.assertRaises(ValueError) as ctx:
            update_ai_policy(
                app_name="shop_app",
                updates={"allow_paid_models": True},
                config_path=self.cfg_path,
                explicit_paid_opt_in=False,
            )
        self.assertIn("Cost Guard Security", str(ctx.exception))

        # With explicit opt-in -> Allowed!
        policy = update_ai_policy(
            app_name="shop_app",
            updates={"allow_paid_models": True, "lightweight_model": "anthropic/claude-3.5-sonnet"},
            config_path=self.cfg_path,
            explicit_paid_opt_in=True,
        )
        self.assertTrue(policy["allow_paid_models"])
        self.assertEqual(policy["lightweight_model"], "anthropic/claude-3.5-sonnet")

    def test_5_reset_app_ai_policy_reverts_to_global(self) -> None:
        """Test 5: Resetting per-app override deletes app ai section and inherits global."""
        res = reset_app_ai_policy("custom_app", config_path=self.cfg_path)
        self.assertFalse(res["has_per_app_override"])
        self.assertEqual(res["mode"], "auto")  # inherited from global
        self.assertEqual(res["primary_model"], DEFAULT_GEMINI_MODEL)  # inherited from global

        disk_cfg = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        self.assertNotIn("ai", disk_cfg["apps"]["custom_app"])
        self.assertEqual(disk_cfg["apps"]["custom_app"]["display_name"], "Custom App")

    def test_6_http_admin_api_endpoints(self) -> None:
        """Test 6: AIConfigHTTPHandler serves GET /api/ai_policy and POST /api/ai_policy with token auth and origin protection."""
        import io
        from unittest.mock import MagicMock
        from crash_trend.ai_config_service import AIConfigHTTPHandler

        AIConfigHTTPHandler.config_path = self.cfg_path
        test_token = "test-token-12345"
        AIConfigHTTPHandler.admin_token = test_token

        # 1. Test GET /api/ai_policy?app=shop_app
        handler = AIConfigHTTPHandler.__new__(AIConfigHTTPHandler)
        handler.path = "/api/ai_policy?app=shop_app"
        handler.headers = {"Origin": "http://127.0.0.1:8080"}
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_GET()
        handler.send_response.assert_called_with(200)
        res_data = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(res_data["mode"], "auto")

        # 2. Test untrusted Origin rejection (CORS protection against external sites)
        handler_bad_origin = AIConfigHTTPHandler.__new__(AIConfigHTTPHandler)
        handler_bad_origin.path = "/api/ai_policy"
        handler_bad_origin.headers = {"Origin": "https://malicious-website.com"}
        handler_bad_origin.wfile = io.BytesIO()
        handler_bad_origin.send_response = MagicMock()
        handler_bad_origin.send_header = MagicMock()
        handler_bad_origin.end_headers = MagicMock()

        handler_bad_origin.do_OPTIONS()
        handler_bad_origin.send_response.assert_called_with(403)

        # 3. Test POST without token -> 401 Unauthorized
        post_body = json.dumps({
            "app_name": "shop_app",
            "updates": {"mode": "gemini_only"},
        }).encode("utf-8")

        handler_unauth = AIConfigHTTPHandler.__new__(AIConfigHTTPHandler)
        handler_unauth.path = "/api/ai_policy"
        handler_unauth.headers = {"Content-Length": str(len(post_body)), "Origin": "http://localhost:8080"}
        handler_unauth.rfile = io.BytesIO(post_body)
        handler_unauth.wfile = io.BytesIO()
        handler_unauth.send_response = MagicMock()
        handler_unauth.send_header = MagicMock()
        handler_unauth.end_headers = MagicMock()

        handler_unauth.do_POST()
        handler_unauth.send_response.assert_called_with(401)

        # 4. Test POST with valid token update
        handler_auth = AIConfigHTTPHandler.__new__(AIConfigHTTPHandler)
        handler_auth.path = "/api/ai_policy"
        handler_auth.headers = {
            "Content-Length": str(len(post_body)),
            "Origin": "http://127.0.0.1:8080",
            "X-Admin-Token": test_token,
        }
        handler_auth.rfile = io.BytesIO(post_body)
        handler_auth.wfile = io.BytesIO()
        handler_auth.send_response = MagicMock()
        handler_auth.send_header = MagicMock()
        handler_auth.end_headers = MagicMock()

        handler_auth.do_POST()
        handler_auth.send_response.assert_called_with(200)
        post_res = json.loads(handler_auth.wfile.getvalue().decode("utf-8"))
        self.assertEqual(post_res["mode"], "gemini_only")
        self.assertTrue(post_res["has_per_app_override"])

        # 5. Test POST /api/ai_policy/reset with valid token
        reset_body = json.dumps({"app_name": "shop_app"}).encode("utf-8")
        handler_reset = AIConfigHTTPHandler.__new__(AIConfigHTTPHandler)
        handler_reset.path = "/api/ai_policy/reset"
        handler_reset.headers = {
            "Content-Length": str(len(reset_body)),
            "Origin": "null",
            "X-Admin-Token": test_token,
        }
        handler_reset.rfile = io.BytesIO(reset_body)
        handler_reset.wfile = io.BytesIO()
        handler_reset.send_response = MagicMock()
        handler_reset.send_header = MagicMock()
        handler_reset.end_headers = MagicMock()

        handler_reset.do_POST()
        handler_reset.send_response.assert_called_with(200)
        reset_res = json.loads(handler_reset.wfile.getvalue().decode("utf-8"))
        self.assertFalse(reset_res["has_per_app_override"])

    def test_7_update_providers_via_service(self) -> None:
        """Test 7: Updating primary_provider and lightweight_provider persists correctly."""
        policy = update_ai_policy(
            app_name="shop_app",
            updates={"primary_provider": "gemini", "lightweight_provider": "openrouter"},
            config_path=self.cfg_path,
        )
        self.assertEqual(policy["primary_provider"], "gemini")
        self.assertEqual(policy["lightweight_provider"], "openrouter")

    def test_8_gemini_cost_guard_blocks_pro_and_unknown_models(self) -> None:
        """Test 8: Cost Guard strictly blocks Gemini Pro, paid Flash-image, and unknown models unless allow_paid_models is explicitly opted in."""
        from crash_trend.ai_router import is_free_gemini_model

        # 1. Exact allowlist verification
        self.assertTrue(is_free_gemini_model("gemini-3.8-flash"))
        self.assertTrue(is_free_gemini_model("gemini-2.5-flash"))
        self.assertTrue(is_free_gemini_model("models/gemini-3.8-flash"))
        # Crucial regression: paid-only Flash image models must be False
        self.assertFalse(is_free_gemini_model("gemini-2.5-flash-image"))
        self.assertFalse(is_free_gemini_model("gemini-3.1-flash-image"))
        self.assertFalse(is_free_gemini_model("gemini-3.1-pro-preview"))
        self.assertFalse(is_free_gemini_model("gemini-1.5-pro"))
        self.assertFalse(is_free_gemini_model("gemini-unknown-enterprise"))

        # 2. Reject gemini-3.1-pro-preview when allow_paid_models is false
        with self.assertRaises(ValueError) as ctx1:
            update_ai_policy(
                app_name="shop_app",
                updates={"primary_provider": "gemini", "primary_model": "gemini-3.1-pro-preview"},
                config_path=self.cfg_path,
                explicit_paid_opt_in=False,
            )
        self.assertIn("Cost Guard Violation", str(ctx1.exception))
        self.assertIn("gemini-3.1-pro-preview", str(ctx1.exception))

        # 3. Reject gemini-2.5-flash-image (paid flash image) when allow_paid_models is false
        with self.assertRaises(ValueError) as ctx_img:
            update_ai_policy(
                app_name="shop_app",
                updates={"primary_provider": "gemini", "primary_model": "gemini-2.5-flash-image"},
                config_path=self.cfg_path,
                explicit_paid_opt_in=False,
            )
        self.assertIn("Cost Guard Violation", str(ctx_img.exception))
        self.assertIn("gemini-2.5-flash-image", str(ctx_img.exception))

        # 4. Reject gemini-1.5-pro for lightweight when allow_paid_models is false
        with self.assertRaises(ValueError) as ctx2:
            update_ai_policy(
                app_name="shop_app",
                updates={"lightweight_provider": "gemini", "lightweight_model": "gemini-1.5-pro"},
                config_path=self.cfg_path,
                explicit_paid_opt_in=False,
            )
        self.assertIn("Cost Guard Violation", str(ctx2.exception))
        self.assertIn("gemini-1.5-pro", str(ctx2.exception))

        # 5. Allow when explicit_paid_opt_in=True and allow_paid_models=True
        policy = update_ai_policy(
            app_name="shop_app",
            updates={"primary_provider": "gemini", "primary_model": "gemini-2.5-flash-image", "allow_paid_models": True},
            config_path=self.cfg_path,
            explicit_paid_opt_in=True,
        )
        self.assertEqual(policy["primary_model"], "gemini-2.5-flash-image")
        self.assertTrue(policy["allow_paid_models"])

        # 6. Provider-aware lightweight_is_free verification
        pol_paid_gemini = update_ai_policy(
            app_name="shop_app",
            updates={"lightweight_provider": "gemini", "lightweight_model": "gemini-2.5-flash-image", "allow_paid_models": True},
            config_path=self.cfg_path,
            explicit_paid_opt_in=True,
        )
        self.assertFalse(pol_paid_gemini["lightweight_is_free"])

        pol_free_gemini = update_ai_policy(
            app_name="shop_app",
            updates={"lightweight_provider": "gemini", "lightweight_model": "gemini-3.8-flash", "allow_paid_models": True},
            config_path=self.cfg_path,
            explicit_paid_opt_in=True,
        )
        self.assertTrue(pol_free_gemini["lightweight_is_free"])

    def test_9_http_post_origin_early_rejection_prevents_side_effects(self) -> None:
        """Test 9: Untrusted Origin is rejected at line 1 of do_POST with 403, preventing any disk write."""
        import io
        from unittest.mock import MagicMock
        from crash_trend.ai_config_service import AIConfigHTTPHandler

        AIConfigHTTPHandler.config_path = self.cfg_path
        test_token = "valid-token-for-attack"
        AIConfigHTTPHandler.admin_token = test_token

        # Initial config state for shop_app
        initial_disk = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        initial_mode = initial_disk.get("apps", {}).get("shop_app", {}).get("ai", {}).get("mode")

        post_body = json.dumps({
            "app_name": "shop_app",
            "updates": {"mode": "gemini_only"},
        }).encode("utf-8")

        handler = AIConfigHTTPHandler.__new__(AIConfigHTTPHandler)
        handler.path = "/api/ai_policy"
        # Attacker supplies valid token but requests from untrusted Origin
        handler.headers = {
            "Content-Length": str(len(post_body)),
            "Origin": "https://evil-cross-origin.com",
            "X-Admin-Token": test_token,
        }
        handler.rfile = io.BytesIO(post_body)
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler.do_POST()
        handler.send_response.assert_called_with(403)
        err_res = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertIn("Forbidden", err_res["error"])

        # Crucial: Disk configuration MUST NOT have been modified!
        post_disk = yaml.safe_load(self.cfg_path.read_text(encoding="utf-8"))
        post_mode = post_disk.get("apps", {}).get("shop_app", {}).get("ai", {}).get("mode")
        self.assertEqual(initial_mode, post_mode)


if __name__ == "__main__":
    unittest.main()
