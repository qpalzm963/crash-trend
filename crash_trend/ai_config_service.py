"""AI Policy Admin Controls & Configuration Service (Dashboard V2.5 - Issue #41).

Provides single-source administration and safe writeback for AI routing policies:
- Effective policy inspection across Global, per-app, and environment configurations
- Safe mode switching: auto / gemini_only / openrouter_only
- Strict Cost Guard: blocks paid models unless explicitly opted in
- Privacy & Fallback policy adjustments
- Per-app override reset back to global defaults
- Zero-leak credential policy: API keys and secrets are never exported or logged
"""

from __future__ import annotations

import argparse
import copy
import http.server
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import yaml

from .ai_provider import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    SUPPORTED_PROVIDERS,
)
from .ai_router import (
    DEFAULT_LIGHTWEIGHT_MODEL,
    SUPPORTED_ROUTING_MODES,
    AIRouterConfig,
    is_free_openrouter_model,
    resolve_router_config,
)
from .config import APPS_YAML, ROOT, load_config
from .pipeline_health import sanitize_error_message


def get_effective_ai_policy(
    app_name: Optional[str] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolves and returns the effective AI policy for an app or global defaults.

    Ensures that NO API keys, secret URLs, or private tokens are returned in the payload.
    """
    if cfg is None:
        cfg = load_config()

    global_ai = copy.deepcopy(cfg.get("ai") or {})
    app_cfg = (cfg.get("apps") or {}).get(app_name) if app_name else None
    app_ai = copy.deepcopy(app_cfg.get("ai") if app_cfg else {})

    has_per_app_override = bool(app_name and "ai" in (app_cfg or {}))

    # Provenance tracking
    mode_source = "default"
    if app_ai and "mode" in app_ai:
        mode_source = f"app ({app_name})"
    elif global_ai and "mode" in global_ai:
        mode_source = "global"
    elif os.environ.get("AI_ROUTING_MODE"):
        mode_source = "env (AI_ROUTING_MODE)"

    router_cfg = resolve_router_config(app_cfg=app_cfg, global_cfg=cfg)

    # Cost guard status
    paid_source = "default (false)"
    if app_ai and "allow_paid_models" in app_ai:
        paid_source = f"app ({app_name})"
    elif global_ai and "allow_paid_models" in global_ai:
        paid_source = "global"
    elif os.environ.get("AI_ALLOW_PAID_MODELS"):
        paid_source = "env (AI_ALLOW_PAID_MODELS)"

    return {
        "app_name": app_name,
        "mode": router_cfg.mode,
        "mode_source": mode_source,
        "primary_provider": router_cfg.primary_provider,
        "primary_model": router_cfg.primary_model,
        "lightweight_provider": router_cfg.lightweight_provider,
        "lightweight_model": router_cfg.lightweight_model,
        "lightweight_is_free": is_free_openrouter_model(router_cfg.lightweight_model)
        if router_cfg.lightweight_provider == "openrouter"
        else True,
        "allow_paid_models": router_cfg.allow_paid_models,
        "allow_paid_models_source": paid_source,
        "include_source_snippet": router_cfg.include_source_snippet,
        "fallback_enabled": router_cfg.fallback_enabled,
        "has_per_app_override": has_per_app_override,
    }


def validate_ai_policy_update(
    updates: Dict[str, Any],
    current_policy: Dict[str, Any],
    explicit_paid_opt_in: bool = False,
) -> None:
    """Validates proposed AI policy changes against Cost Guard and enum rules."""
    if not isinstance(updates, dict):
        raise ValueError("Updates must be a dictionary")

    # 1. Mode validation
    if "mode" in updates:
        mode = str(updates["mode"]).strip().lower()
        if mode not in SUPPORTED_ROUTING_MODES:
            raise ValueError(
                f"Invalid routing mode '{updates['mode']}'. Supported: {sorted(SUPPORTED_ROUTING_MODES)}"
            )

    # 2. Provider validation
    for prov_key in ("primary_provider", "lightweight_provider"):
        if prov_key in updates:
            prov = str(updates[prov_key]).strip().lower()
            if prov not in SUPPORTED_PROVIDERS:
                raise ValueError(
                    f"Invalid provider for {prov_key}: '{updates[prov_key]}'. Supported: {sorted(SUPPORTED_PROVIDERS)}"
                )

    # 3. Cost Guard & Paid Model Opt-in
    allow_paid = updates.get("allow_paid_models")
    if allow_paid is None:
        allow_paid = current_policy.get("allow_paid_models", False)
    else:
        allow_paid = bool(allow_paid)
        if allow_paid and not explicit_paid_opt_in:
            raise ValueError(
                "Cost Guard Security: Enabling paid models requires explicit confirmation (explicit_paid_opt_in=True)"
            )

    # Check models against free guard
    light_model = updates.get("lightweight_model") or current_policy.get("lightweight_model")
    light_prov = updates.get("lightweight_provider") or current_policy.get("lightweight_provider")
    if light_prov == "openrouter" and not allow_paid:
        if light_model and not is_free_openrouter_model(light_model):
            raise ValueError(
                f"Cost Guard Violation: Paid model '{light_model}' is not allowed when allow_paid_models is false. "
                f"Use 'openrouter/free' or a ':free' suffix model, or explicitly opt in to paid models."
            )

    primary_model = updates.get("primary_model") or current_policy.get("primary_model")
    primary_prov = updates.get("primary_provider") or current_policy.get("primary_provider")
    if primary_prov == "openrouter" and not allow_paid:
        if primary_model and not is_free_openrouter_model(primary_model):
            raise ValueError(
                f"Cost Guard Violation: Paid model '{primary_model}' is not allowed when allow_paid_models is false."
            )


def update_ai_policy(
    app_name: Optional[str] = None,
    updates: Optional[Dict[str, Any]] = None,
    config_path: Optional[Path] = None,
    explicit_paid_opt_in: bool = False,
) -> Dict[str, Any]:
    """Validates and writes AI policy updates to apps.yaml cleanly.

    Returns the new effective policy.
    """
    target_path = Path(config_path) if config_path else APPS_YAML
    if not target_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {target_path}")

    with open(target_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    current_policy = get_effective_ai_policy(app_name, cfg)
    clean_updates = dict(updates or {})

    # Validate updates
    validate_ai_policy_update(
        clean_updates,
        current_policy,
        explicit_paid_opt_in=explicit_paid_opt_in,
    )

    # Prepare ai dictionary block
    if app_name:
        apps = cfg.setdefault("apps", {})
        if app_name not in apps:
            raise KeyError(f"App '{app_name}' does not exist in configuration")
        target_ai = apps[app_name].setdefault("ai", {})
    else:
        target_ai = cfg.setdefault("ai", {})

    # Apply valid fields
    if "mode" in clean_updates:
        target_ai["mode"] = str(clean_updates["mode"]).strip().lower()

    if "primary_model" in clean_updates or "primary_provider" in clean_updates:
        primary_sec = target_ai.setdefault("primary", {})
        if "primary_provider" in clean_updates:
            primary_sec["provider"] = str(clean_updates["primary_provider"]).strip().lower()
        if "primary_model" in clean_updates:
            primary_sec["model"] = str(clean_updates["primary_model"]).strip()

    if "lightweight_model" in clean_updates or "lightweight_provider" in clean_updates:
        light_sec = target_ai.setdefault("lightweight", {})
        if "lightweight_provider" in clean_updates:
            light_sec["provider"] = str(clean_updates["lightweight_provider"]).strip().lower()
        if "lightweight_model" in clean_updates:
            light_sec["model"] = str(clean_updates["lightweight_model"]).strip()

    if "allow_paid_models" in clean_updates:
        target_ai["allow_paid_models"] = bool(clean_updates["allow_paid_models"])

    if "include_source_snippet" in clean_updates:
        priv_sec = target_ai.setdefault("privacy", {})
        priv_sec["include_source_snippet"] = bool(clean_updates["include_source_snippet"])

    if "fallback_enabled" in clean_updates:
        fb_sec = target_ai.setdefault("fallback", {})
        fb_sec["enabled"] = bool(clean_updates["fallback_enabled"])

    # Write back YAML cleanly
    with open(target_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)

    # Return refreshed effective policy
    return get_effective_ai_policy(app_name, cfg)


def reset_app_ai_policy(
    app_name: str,
    config_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Removes per-app AI policy override in apps.yaml, reverting to global policy."""
    target_path = Path(config_path) if config_path else APPS_YAML
    if not target_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {target_path}")

    with open(target_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    apps = cfg.get("apps") or {}
    if app_name not in apps:
        raise KeyError(f"App '{app_name}' does not exist in configuration")

    if "ai" in apps[app_name]:
        del apps[app_name]["ai"]
        with open(target_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)

    return get_effective_ai_policy(app_name, cfg)


class AIConfigHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Lightweight HTTP Handler serving the AI Policy Admin REST API."""
    config_path: Optional[Path] = None

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy standard output in background server mode
        pass

    def _set_cors_headers(self, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_OPTIONS(self) -> None:
        self._set_cors_headers(204)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/ai_policy":
            params = parse_qs(parsed.query)
            app_name = params.get("app", [None])[0]
            try:
                policy = get_effective_ai_policy(app_name=app_name)
                self._set_cors_headers(200)
                self.wfile.write(json.dumps(policy, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self._set_cors_headers(400)
                self.wfile.write(json.dumps({"error": sanitize_error_message(str(e))}).encode("utf-8"))
        else:
            self._set_cors_headers(404)
            self.wfile.write(json.dumps({"error": "Not Found"}).encode("utf-8"))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len) if content_len > 0 else b"{}"
        try:
            payload = json.loads(post_body.decode("utf-8"))
        except Exception:
            self._set_cors_headers(400)
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode("utf-8"))
            return

        if parsed.path == "/api/ai_policy":
            app_name = payload.get("app_name")
            updates = payload.get("updates", {})
            explicit_opt_in = bool(payload.get("explicit_paid_opt_in", False))
            try:
                updated = update_ai_policy(
                    app_name=app_name,
                    updates=updates,
                    config_path=self.config_path,
                    explicit_paid_opt_in=explicit_opt_in,
                )
                self._set_cors_headers(200)
                self.wfile.write(json.dumps(updated, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self._set_cors_headers(400)
                self.wfile.write(json.dumps({"error": sanitize_error_message(str(e))}).encode("utf-8"))
        elif parsed.path == "/api/ai_policy/reset":
            app_name = payload.get("app_name")
            if not app_name:
                self._set_cors_headers(400)
                self.wfile.write(json.dumps({"error": "app_name is required for reset"}).encode("utf-8"))
                return
            try:
                res = reset_app_ai_policy(app_name, config_path=self.config_path)
                self._set_cors_headers(200)
                self.wfile.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self._set_cors_headers(400)
                self.wfile.write(json.dumps({"error": sanitize_error_message(str(e))}).encode("utf-8"))
        else:
            self._set_cors_headers(404)
            self.wfile.write(json.dumps({"error": "Not Found"}).encode("utf-8"))


def serve_admin_api(port: int = 8080, host: str = "127.0.0.1", config_path: Optional[Path] = None) -> None:
    """Runs a local administrative HTTP API server for Dashboard AI Policy controls."""
    AIConfigHTTPHandler.config_path = config_path
    server = http.server.HTTPServer((host, port), AIConfigHTTPHandler)
    print(f"✓ AI Policy Admin API server running at http://{host}:{port}/api/ai_policy")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAdmin API server stopped.")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="AI Policy Admin Controls CLI (Dashboard V2.5)")
    parser.add_argument("--app", help="Target app name (omit for global policy)")
    parser.add_argument("--show", action="store_true", help="Display current effective policy")
    parser.add_argument("--mode", choices=["auto", "gemini_only", "openrouter_only"], help="Set routing mode")
    parser.add_argument("--primary-model", help="Set primary model name")
    parser.add_argument("--lightweight-model", help="Set lightweight model name")
    parser.add_argument("--allow-paid-models", choices=["true", "false"], help="Set paid model policy")
    parser.add_argument("--confirm-paid-opt-in", action="store_true", help="Explicit confirmation for paid models")
    parser.add_argument("--privacy-source-snippet", choices=["true", "false"], help="Include local source snippet")
    parser.add_argument("--fallback", choices=["true", "false"], help="Enable or disable transient fallback")
    parser.add_argument("--reset", action="store_true", help="Reset per-app override to global policy")
    parser.add_argument("--serve", nargs="?", const=8080, type=int, help="Start local admin HTTP API server (default port 8080)")

    args = parser.parse_args()

    if args.serve:
        serve_admin_api(port=args.serve)
        return

    if args.reset:
        if not args.app:
            sys.exit("[錯誤] --reset 需要指定 --app <name>")
        res = reset_app_ai_policy(args.app)
        print(f"✓ App「{args.app}」AI 設定已重置回 Global Policy：\n{json.dumps(res, indent=2, ensure_ascii=False)}")
        return

    updates: Dict[str, Any] = {}
    if args.mode:
        updates["mode"] = args.mode
    if args.primary_model:
        updates["primary_model"] = args.primary_model
    if args.lightweight_model:
        updates["lightweight_model"] = args.lightweight_model
    if args.allow_paid_models:
        updates["allow_paid_models"] = args.allow_paid_models.lower() == "true"
    if args.privacy_source_snippet:
        updates["include_source_snippet"] = args.privacy_source_snippet.lower() == "true"
    if args.fallback:
        updates["fallback_enabled"] = args.fallback.lower() == "true"

    if updates:
        try:
            res = update_ai_policy(
                app_name=args.app,
                updates=updates,
                explicit_paid_opt_in=args.confirm_paid_opt_in,
            )
            target_label = f"App「{args.app}」" if args.app else "Global"
            print(f"✓ 已成功更新 {target_label} AI Policy：\n{json.dumps(res, indent=2, ensure_ascii=False)}")
        except Exception as e:
            sys.exit(f"[錯誤] 更新 AI Policy 失敗：{e}")
    else:
        policy = get_effective_ai_policy(args.app)
        target_label = f"App「{args.app}」" if args.app else "Global"
        print(f"=== {target_label} Effective AI Policy ===\n{json.dumps(policy, indent=2, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
