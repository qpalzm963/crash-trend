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


if __name__ == "__main__":
    unittest.main()
