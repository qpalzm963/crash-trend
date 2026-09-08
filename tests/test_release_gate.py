"""Unit and integration tests for Release Regression Gate and Quality Alerts (Issue #57).

Tests:
1. GatePolicy configuration, defaults, and app_cfg parsing.
2. Evaluator determinism and status transitions:
   - baseline when no previous version exists
   - insufficient_data when sample is not sufficient (never pass/fail)
   - pass when all normalized metrics within thresholds
   - warn when threshold exceeded
   - fail when threshold exceeded
   - normalized metrics enforcement (never raw crash count)
3. Zero raw UUIDs privacy constraint & ReleaseGateArtifact schema validation.
4. evaluate_app_release_gate multi-platform aggregation.
5. CLI exit codes contract:
   - 0 on pass / warn / baseline / insufficient (or without --fail-on-regression)
   - 2 on fail with --fail-on-regression
   - 1 on runtime / fatal error
6. Pipeline integration:
   - Pipeline records release_gate stage as success even on quality fail
   - Pipeline with --fail-on-regression exits with code 2 after dashboard build
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crash_trend.catalog.release_catalog import build_release_catalog
from crash_trend.gate import (
    GatePolicy,
    ThresholdRule,
    evaluate_app_release_gate,
    evaluate_release,
    load_gate_policy,
    load_release_gate_artifact,
    save_release_gate_artifact,
    validate_release_gate_artifact,
)
from crash_trend.pipeline_run import run_pipeline
from crash_trend.release_gate import run_release_gate_for_app
from crash_trend.schema_v2 import validate_release_catalog


class TestGatePolicy(unittest.TestCase):
    """Tests policy loading, default threshold verification, and clamping."""

    def test_default_policy(self) -> None:
        policy = GatePolicy()
        self.assertEqual(policy.policy_version, "1.0")
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.min_sessions, 1000)

        # Verify default thresholds
        self.assertEqual(policy.crash_rate_change_pct.warn, 0.10)
        self.assertEqual(policy.crash_rate_change_pct.fail, 0.25)
        self.assertEqual(policy.crash_free_users_drop.warn, 0.005)
        self.assertEqual(policy.crash_free_users_drop.fail, 0.015)
        self.assertEqual(policy.fatal_rate_change_pct.warn, 0.10)
        self.assertEqual(policy.fatal_rate_change_pct.fail, 0.25)
        self.assertEqual(policy.anr_rate_change_pct.warn, 0.10)
        self.assertEqual(policy.anr_rate_change_pct.fail, 0.25)
        self.assertEqual(policy.regressed_issues_count.warn, 1)
        self.assertEqual(policy.regressed_issues_count.fail, 3)
        self.assertEqual(policy.introduced_issues_count.warn, 5)
        self.assertEqual(policy.introduced_issues_count.fail, 10)

    def test_threshold_rule_clamps_when_warn_exceeds_fail(self) -> None:
        # If warn is set higher than fail, it clamps warn down to fail
        rule = ThresholdRule(warn=0.50, fail=0.20)
        self.assertEqual(rule.warn, 0.20)
        self.assertEqual(rule.fail, 0.20)

    def test_custom_policy_from_app_cfg(self) -> None:
        app_cfg = {
            "release_gate": {
                "enabled": True,
                "policy_version": "2.0",
                "min_sessions": 5000,
                "thresholds": {
                    "crash_rate_change_pct": {"warn": 0.05, "fail": 0.15},
                    "crash_free_users_drop": {"warn": 0.002, "fail": 0.008},
                    "regressed_issues_count": {"warn": 2, "fail": 5},
                },
            }
        }
        policy = load_gate_policy(app_cfg)
        self.assertEqual(policy.policy_version, "2.0")
        self.assertEqual(policy.min_sessions, 5000)
        self.assertEqual(policy.crash_rate_change_pct.warn, 0.05)
        self.assertEqual(policy.crash_rate_change_pct.fail, 0.15)
        self.assertEqual(policy.crash_free_users_drop.warn, 0.002)
        self.assertEqual(policy.crash_free_users_drop.fail, 0.008)
        self.assertEqual(policy.regressed_issues_count.warn, 2)
        self.assertEqual(policy.regressed_issues_count.fail, 5)
        # Unspecified thresholds retain defaults
        self.assertEqual(policy.fatal_rate_change_pct.warn, 0.10)
        self.assertEqual(policy.introduced_issues_count.fail, 10)

    def test_policy_to_dict(self) -> None:
        policy = GatePolicy()
        d = policy.to_dict()
        self.assertEqual(d["policy_version"], "1.0")
        self.assertIn("crash_rate_change_pct", d)
        self.assertEqual(d["crash_rate_change_pct"]["warn"], 0.10)


class TestGateEvaluator(unittest.TestCase):
    """Tests deterministic gate evaluation rules and status transitions."""

    def setUp(self) -> None:
        self.policy = GatePolicy(min_sessions=1000)

    def test_insufficient_data_when_sample_not_sufficient(self) -> None:
        """Sample insufficient must NEVER be evaluated as pass or fail."""
        item: dict[str, Any] = {
            "version": "2.1.0",
            "platform": "android",
            "status": "latest",
            "recent_health": {
                "30d": {
                    "sessions_total": 5000,
                    "crash_events": 10,
                    "sample_sufficient": False,  # explicitly insufficient
                }
            },
            "vs_previous": {
                "previous_version": "2.0.0",
                "crash_rate_change_pct": 0.50,  # Would fail if sample were sufficient
            },
        }
        res = evaluate_release(item, self.policy)
        self.assertEqual(res["gate_status"], "insufficient_data")
        self.assertFalse(res["sample_sufficient"])
        self.assertFalse(res["alert"]["should_alert"])
        self.assertEqual(res["alert"]["alert_severity"], "none")
        self.assertIn("樣本不足", res["alert"]["alert_summary"])

    def test_insufficient_data_when_sessions_below_minimum(self) -> None:
        """Sessions below min_sessions (e.g. 500 < 1000) triggers insufficient_data."""
        item: dict[str, Any] = {
            "version": "2.1.0",
            "platform": "android",
            "status": "latest",
            "recent_health": {
                "30d": {
                    "sessions_total": 500,  # Below min_sessions 1000
                    "crash_events": 2,
                    "sample_sufficient": True,
                }
            },
            "vs_previous": {
                "previous_version": "2.0.0",
                "crash_rate_change_pct": -0.10,  # Healthy rate, but sample is small
            },
        }
        res = evaluate_release(item, self.policy)
        self.assertEqual(res["gate_status"], "insufficient_data")
        self.assertFalse(res["sample_sufficient"])

    def test_baseline_status_when_no_previous_version(self) -> None:
        """Initial version on platform with no previous release is baseline."""
        item: dict[str, Any] = {
            "version": "1.0.0",
            "platform": "android",
            "status": "latest",
            "recent_health": {
                "30d": {
                    "sessions_total": 15000,
                    "crash_events": 50,
                    "sample_sufficient": True,
                }
            },
            "vs_previous": None,
        }
        res = evaluate_release(item, self.policy)
        self.assertEqual(res["gate_status"], "baseline")
        self.assertTrue(res["sample_sufficient"])
        self.assertFalse(res["alert"]["should_alert"])
        self.assertEqual(res["alert"]["alert_severity"], "none")

    def test_pass_status_when_all_metrics_healthy(self) -> None:
        """All normalized metrics healthy -> PASS."""
        item: dict[str, Any] = {
            "version": "2.1.0",
            "platform": "android",
            "status": "latest",
            "recent_health": {
                "30d": {
                    "sessions_total": 20000,
                    "crash_events": 40,
                    "sample_sufficient": True,
                }
            },
            "vs_previous": {
                "previous_version": "2.0.0",
                "crash_rate_change_pct": -0.05,  # Improved -5%
                "crash_free_users_diff": 0.002,  # CFU improved +0.2%
                "fatal_rate_change_pct": -0.10,
                "anr_rate_change_pct": 0.0,
            },
            "issue_lifecycle": {
                "regressed_count": 0,
                "introduced_count": 2,
            },
        }
        res = evaluate_release(item, self.policy)
        self.assertEqual(res["gate_status"], "pass")
        self.assertFalse(res["alert"]["should_alert"])
        self.assertEqual(res["alert"]["alert_severity"], "none")

    def test_warn_status_when_crash_rate_crosses_warn_threshold(self) -> None:
        """Crash rate increase >= +10% and < +25% -> WARN."""
        item: dict[str, Any] = {
            "version": "2.1.0",
            "platform": "android",
            "status": "latest",
            "recent_health": {
                "30d": {
                    "sessions_total": 20000,
                    "crash_events": 100,
                    "sample_sufficient": True,
                }
            },
            "vs_previous": {
                "previous_version": "2.0.0",
                "crash_rate_change_pct": 0.15,  # +15% >= 10% warn, < 25% fail
                "crash_free_users_diff": 0.001,
            },
            "issue_lifecycle": {
                "regressed_count": 0,
                "introduced_count": 1,
            },
        }
        res = evaluate_release(item, self.policy)
        self.assertEqual(res["gate_status"], "warn")
        self.assertTrue(res["alert"]["should_alert"])
        self.assertEqual(res["alert"]["alert_severity"], "warning")
        self.assertIn("crash_rate_regression", res["alert"]["trigger_rules"])

    def test_fail_status_when_crash_rate_crosses_fail_threshold(self) -> None:
        """Crash rate increase >= +25% -> FAIL."""
        item: dict[str, Any] = {
            "version": "2.1.0",
            "platform": "android",
            "status": "latest",
            "recent_health": {
                "30d": {
                    "sessions_total": 20000,
                    "crash_events": 150,
                    "sample_sufficient": True,
                }
            },
            "vs_previous": {
                "previous_version": "2.0.0",
                "crash_rate_change_pct": 0.30,  # +30% >= 25% fail
                "crash_free_users_diff": 0.0,
            },
            "issue_lifecycle": {
                "regressed_count": 0,
                "introduced_count": 1,
            },
        }
        res = evaluate_release(item, self.policy)
        self.assertEqual(res["gate_status"], "fail")
        self.assertTrue(res["alert"]["should_alert"])
        self.assertEqual(res["alert"]["alert_severity"], "critical")
        self.assertIn("crash_rate_regression", res["alert"]["trigger_rules"])

    def test_cfu_drop_warn_and_fail(self) -> None:
        """CFU diff drop thresholds: warn at 0.5%, fail at 1.5%."""
        # 1. Warn drop (0.8% drop -> diff = -0.008)
        item_warn: dict[str, Any] = {
            "version": "2.1.0",
            "platform": "android",
            "status": "latest",
            "recent_health": {"30d": {"sessions_total": 20000, "sample_sufficient": True}},
            "vs_previous": {"previous_version": "2.0.0", "crash_free_users_diff": -0.008},
        }
        res_warn = evaluate_release(item_warn, self.policy)
        self.assertEqual(res_warn["gate_status"], "warn")

        # 2. Fail drop (2.0% drop -> diff = -0.020)
        item_fail: dict[str, Any] = {
            "version": "2.1.0",
            "platform": "android",
            "status": "latest",
            "recent_health": {"30d": {"sessions_total": 20000, "sample_sufficient": True}},
            "vs_previous": {"previous_version": "2.0.0", "crash_free_users_diff": -0.020},
        }
        res_fail = evaluate_release(item_fail, self.policy)
        self.assertEqual(res_fail["gate_status"], "fail")

    def test_regressed_issues_warn_and_fail(self) -> None:
        """Regressed issue count: warn at 1, fail at 3."""
        base_item = {
            "version": "2.1.0",
            "platform": "ios",
            "status": "latest",
            "recent_health": {"30d": {"sessions_total": 15000, "sample_sufficient": True}},
            "vs_previous": {"previous_version": "2.0.0", "crash_rate_change_pct": 0.0},
        }

        item_warn = dict(base_item, issue_lifecycle={"regressed_count": 1, "introduced_count": 0})
        self.assertEqual(evaluate_release(item_warn, self.policy)["gate_status"], "warn")

        item_fail = dict(base_item, issue_lifecycle={"regressed_count": 3, "introduced_count": 0})
        self.assertEqual(evaluate_release(item_fail, self.policy)["gate_status"], "fail")

    def test_introduced_issues_warn_and_fail(self) -> None:
        """Introduced issue count: warn at 5, fail at 10."""
        base_item = {
            "version": "2.1.0",
            "platform": "ios",
            "status": "latest",
            "recent_health": {"30d": {"sessions_total": 15000, "sample_sufficient": True}},
            "vs_previous": {"previous_version": "2.0.0", "crash_rate_change_pct": 0.0},
        }

        item_warn = dict(base_item, issue_lifecycle={"regressed_count": 0, "introduced_count": 6})
        self.assertEqual(evaluate_release(item_warn, self.policy)["gate_status"], "warn")

        item_fail = dict(base_item, issue_lifecycle={"regressed_count": 0, "introduced_count": 11})
        self.assertEqual(evaluate_release(item_fail, self.policy)["gate_status"], "fail")

    def test_severity_hierarchy_aggregation(self) -> None:
        """FAIL takes precedence over WARN when multiple rules trigger."""
        item: dict[str, Any] = {
            "version": "2.1.0",
            "platform": "android",
            "status": "latest",
            "recent_health": {"30d": {"sessions_total": 10000, "sample_sufficient": True}},
            "vs_previous": {
                "previous_version": "2.0.0",
                "crash_rate_change_pct": 0.12,  # WARN
                "crash_free_users_diff": -0.02,  # FAIL
            },
            "issue_lifecycle": {"regressed_count": 1, "introduced_count": 1},  # WARN
        }
        res = evaluate_release(item, self.policy)
        self.assertEqual(res["gate_status"], "fail")
        self.assertEqual(res["alert"]["alert_severity"], "critical")
        self.assertIn("crash_free_users_drop", res["alert"]["trigger_rules"])

    def test_normalized_metrics_enforcement_high_raw_crashes_healthy_rate(self) -> None:
        """Test normalized metrics rule: High raw crash count with low crash rate PASSES.

        Example: 10x traffic expansion increases raw crashes from 100 to 700,
        but crash rate per session dropped from 0.001 to 0.0007 (-30% improvement).
        Raw crash count increased 7x, but release is healthier -> Gate PASSES.
        """
        item: dict[str, Any] = {
            "version": "2.1.0",
            "platform": "android",
            "status": "latest",
            "lifetime_crashes": 700,  # High raw crash count
            "recent_health": {
                "30d": {
                    "sessions_total": 1000000,
                    "crash_events": 700,
                    "sample_sufficient": True,
                }
            },
            "vs_previous": {
                "previous_version": "2.0.0",
                "crash_rate_change_pct": -0.30,  # Normalized rate dropped 30%!
                "crash_free_users_diff": 0.005,
            },
            "issue_lifecycle": {"regressed_count": 0, "introduced_count": 1},
        }
        res = evaluate_release(item, self.policy)
        self.assertEqual(res["gate_status"], "pass")
        self.assertFalse(res["alert"]["should_alert"])

    def test_normalized_metrics_enforcement_low_raw_crashes_spiking_rate(self) -> None:
        """Test normalized metrics rule: Low raw crash count with high crash rate FAILS.

        Example: Traffic drops 10x, raw crashes drop from 100 to 20 (-80%),
        but crash rate per session doubled (+100% degradation).
        Gate FAILS despite lower raw crashes.
        """
        item: dict[str, Any] = {
            "version": "2.1.0",
            "platform": "android",
            "status": "latest",
            "lifetime_crashes": 20,  # Low raw crashes
            "recent_health": {
                "30d": {
                    "sessions_total": 10000,
                    "crash_events": 20,
                    "sample_sufficient": True,
                }
            },
            "vs_previous": {
                "previous_version": "2.0.0",
                "crash_rate_change_pct": 1.0,  # +100% rate surge!
                "crash_free_users_diff": -0.010,
            },
            "issue_lifecycle": {"regressed_count": 0, "introduced_count": 1},
        }
        res = evaluate_release(item, self.policy)
        self.assertEqual(res["gate_status"], "fail")


class TestReleaseGateArtifact(unittest.TestCase):
    """Tests machine-readable artifact creation, validation, and privacy constraints."""

    def test_valid_artifact_schema_passes(self) -> None:
        policy = GatePolicy()
        catalog_items = [
            {
                "version": "2.0.0",
                "platform": "android",
                "status": "latest",
                "recent_health": {"30d": {"sessions_total": 5000, "sample_sufficient": True}},
                "vs_previous": {"previous_version": "1.9.0", "crash_rate_change_pct": 0.02},
            }
        ]
        artifact = evaluate_app_release_gate("demo_app", catalog_items, policy, ["android"])
        errors = validate_release_gate_artifact(artifact)
        self.assertEqual(errors, [])
        self.assertEqual(artifact["app_id"], "demo_app")
        self.assertEqual(artifact["overall_status"], "pass")
        self.assertIn("android", artifact["platforms"])

    def test_zero_raw_uuids_policy_enforcement(self) -> None:
        """Artifact validation strictly rejects any raw user_id or installation_id."""
        policy = GatePolicy()
        catalog_items = [
            {
                "version": "2.0.0",
                "platform": "android",
                "status": "latest",
                "recent_health": {"30d": {"sessions_total": 5000, "sample_sufficient": True}},
                "vs_previous": None,
            }
        ]
        artifact = evaluate_app_release_gate("demo_app", catalog_items, policy, ["android"])

        # Inject forbidden raw identifier keys
        artifact["platforms"]["android"]["user_id"] = "user_12345"  # type: ignore[typeddict-item]
        errors = validate_release_gate_artifact(artifact)
        self.assertTrue(any("zero raw UUIDs policy" in err for err in errors))

        del artifact["platforms"]["android"]["user_id"]  # type: ignore[typeddict-item]
        artifact["installation_ids"] = ["inst-abc-def"]  # type: ignore[typeddict-item]
        errors2 = validate_release_gate_artifact(artifact)
        self.assertTrue(any("zero raw UUIDs policy" in err for err in errors2))

    def test_artifact_persistence_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "release_gate.json"
            policy = GatePolicy()
            catalog_items = [
                {
                    "version": "2.0.0",
                    "platform": "android",
                    "status": "latest",
                    "recent_health": {"30d": {"sessions_total": 5000, "sample_sufficient": True}},
                    "vs_previous": {"previous_version": "1.9.0", "crash_rate_change_pct": -0.05},
                }
            ]
            artifact = evaluate_app_release_gate("demo_app", catalog_items, policy, ["android"])
            save_release_gate_artifact(path, artifact)

            loaded = load_release_gate_artifact(path)
            self.assertIsNotNone(loaded)
            if loaded:
                self.assertEqual(loaded["app_id"], "demo_app")
                self.assertEqual(loaded["overall_status"], "pass")


class TestCatalogAndDashboardIntegration(unittest.TestCase):
    """Tests embedding of release_gate into ReleaseCatalogItem and schema validation."""

    def test_build_release_catalog_embeds_release_gate(self) -> None:
        class DummyCatalog:
            issues: dict[str, Any] = {}
            app_versions: dict[str, Any] = {
                "android": {
                    "2.0.0": {"version": "2.0.0", "status": "latest", "crash_events": 20},
                    "1.9.0": {"version": "1.9.0", "status": "active", "crash_events": 30},
                }
            }

            def get_known_app_versions(self, platform: str | None = None) -> list[str]:
                return ["1.9.0", "2.0.0"]

        app_data = {
            "periods": {
                "30": {
                    "version_health": [
                        {"version": "2.0.0", "platform": "android", "sessions_total": 5000, "sample_sufficient": True},
                        {"version": "1.9.0", "platform": "android", "sessions_total": 5000, "sample_sufficient": True},
                    ]
                }
            }
        }
        items = build_release_catalog(DummyCatalog(), app_data=app_data, platform="android")
        self.assertTrue(len(items) >= 2)
        latest = next(i for i in items if i["version"] == "2.0.0")
        self.assertIn("release_gate", latest)
        rg = latest["release_gate"]
        self.assertIsNotNone(rg)
        if rg:
            self.assertIn(rg["status"], {"pass", "warn", "fail", "insufficient_data", "baseline"})

        # Validate against schema_v2
        errors: list[str] = []
        validate_release_catalog(items, errors)
        self.assertEqual(errors, [])


class TestReleaseGateCLIAndPipeline(unittest.TestCase):
    """Tests CLI invocation, exit codes, and pipeline integration."""

    @patch("crash_trend.release_gate.load_config")
    @patch("crash_trend.release_gate.get_app")
    @patch("crash_trend.release_gate.load_app_release_catalog")
    def test_cli_runner_pass_and_fail_artifacts(
        self,
        mock_load_catalog: Any,
        mock_get_app: Any,
        mock_load_cfg: Any,
    ) -> None:
        mock_load_cfg.return_value = {"apps": {"test_app": {}}}
        mock_get_app.return_value = {"platforms": ["android"]}

        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = Path(tmpdir) / "release_gate.json"

            # Case 1: Healthy release -> PASS
            mock_load_catalog.return_value = [
                {
                    "version": "2.0.0",
                    "platform": "android",
                    "status": "latest",
                    "recent_health": {"30d": {"sessions_total": 5000, "sample_sufficient": True}},
                    "vs_previous": {"previous_version": "1.9.0", "crash_rate_change_pct": 0.01},
                }
            ]
            art1 = run_release_gate_for_app("test_app", out_path=out_file, verbose=False)
            self.assertEqual(art1["overall_status"], "pass")
            self.assertTrue(out_file.is_file())

            # Case 2: Degraded release -> FAIL
            mock_load_catalog.return_value = [
                {
                    "version": "2.0.0",
                    "platform": "android",
                    "status": "latest",
                    "recent_health": {"30d": {"sessions_total": 5000, "sample_sufficient": True}},
                    "vs_previous": {"previous_version": "1.9.0", "crash_rate_change_pct": 0.45},
                }
            ]
            art2 = run_release_gate_for_app("test_app", out_path=out_file, verbose=False)
            self.assertEqual(art2["overall_status"], "fail")

    @patch("crash_trend.pipeline_run.run_stage_process")
    @patch("crash_trend.pipeline_run.load_config")
    def test_pipeline_records_release_gate_stage_success_even_on_quality_fail(
        self,
        mock_load_cfg: Any,
        mock_stage_proc: Any,
    ) -> None:
        """Decouples quality regression from pipeline execution status:

        Even if gate returns FAIL, pipeline stage is recorded as SUCCESS,
        preventing operational false alarms unless --fail-on-regression is requested.
        """
        mock_load_cfg.return_value = {
            "apps": {
                "test_app": {
                    "firebase_project": "test-p",
                    "platforms": ["android"],
                    "data_sources": {"sessions": False, "mcp": "off"},
                }
            }
        }
        # Simulate all stage subprocesses succeeding
        mock_stage_proc.return_value = (0, "Stage output", "")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            app_dir = tmp_root / "out" / "test_app"
            app_dir.mkdir(parents=True, exist_ok=True)

            # Write a failing release_gate.json
            fake_gate = {
                "schema_version": "1.0",
                "app_id": "test_app",
                "generated_at": "2026-09-08T00:00:00Z",
                "overall_status": "fail",
                "should_alert": True,
                "alert_severity": "critical",
                "alert_summary": "Quality gate failed",
                "platforms": {},
                "policy_version": "1.0",
                "policy": {},
            }
            (app_dir / "release_gate.json").write_text(json.dumps(fake_gate), encoding="utf-8")

            with patch("crash_trend.pipeline_run.ROOT", tmp_root):
                summary = run_pipeline(
                    app_names=["test_app"],
                    summary_path=tmp_root / "pipeline_run.json",
                    skip_dashboard=True,
                    verbose=False,
                )

                # Pipeline overall status must be SUCCESS
                self.assertEqual(summary["status"], "success")

                # release_gate stage status must be SUCCESS
                app_stages = summary["apps"]["test_app"]["stages"]
                self.assertIn("release_gate", app_stages)
                self.assertEqual(app_stages["release_gate"]["status"], "success")
                self.assertEqual(app_stages["release_gate"]["details"]["gate_status"], "fail")
                self.assertTrue(app_stages["release_gate"]["details"]["should_alert"])

    @patch("crash_trend.release_gate.run_release_gate_for_app")
    def test_release_gate_main_exit_codes(self, mock_run_gate: Any) -> None:
        from crash_trend.release_gate import main as release_gate_main

        # 1. Gate PASS with --fail-on-regression -> exit 0
        mock_run_gate.return_value = {"overall_status": "pass"}
        with patch.object(sys, "argv", ["release_gate.py", "--app", "demo", "--fail-on-regression"]):
            with self.assertRaises(SystemExit) as ctx:
                release_gate_main()
            self.assertEqual(ctx.exception.code, 0)

        # 2. Gate FAIL with --fail-on-regression -> exit 2
        mock_run_gate.return_value = {"overall_status": "fail"}
        with patch.object(sys, "argv", ["release_gate.py", "--app", "demo", "--fail-on-regression"]):
            with self.assertRaises(SystemExit) as ctx:
                release_gate_main()
            self.assertEqual(ctx.exception.code, 2)

        # 3. Gate FAIL WITHOUT --fail-on-regression -> exit 0
        mock_run_gate.return_value = {"overall_status": "fail"}
        with patch.object(sys, "argv", ["release_gate.py", "--app", "demo"]):
            with self.assertRaises(SystemExit) as ctx:
                release_gate_main()
            self.assertEqual(ctx.exception.code, 0)

        # 4. Exception raised -> exit 1
        mock_run_gate.side_effect = RuntimeError("Fatal crash")
        with patch.object(sys, "argv", ["release_gate.py", "--app", "demo"]):
            with self.assertRaises(SystemExit) as ctx:
                release_gate_main()
            self.assertEqual(ctx.exception.code, 1)

    @patch("crash_trend.pipeline_run.run_pipeline")
    def test_pipeline_main_exit_codes(self, mock_run_pipe: Any) -> None:
        from crash_trend.pipeline_run import main as pipeline_main

        # 1. Pipeline success + gate fail + --fail-on-regression -> exit 2
        mock_run_pipe.return_value = {
            "status": "success",
            "apps": {
                "demo": {
                    "stages": {
                        "release_gate": {
                            "status": "success",
                            "details": {"gate_status": "fail"},
                        }
                    }
                }
            },
        }
        with patch.object(sys, "argv", ["pipeline_run.py", "--app", "demo", "--fail-on-regression"]):
            with self.assertRaises(SystemExit) as ctx:
                pipeline_main()
            self.assertEqual(ctx.exception.code, 2)

        # 2. Pipeline success + gate fail WITHOUT --fail-on-regression -> clean finish (no exit 1 or 2)
        with patch.object(sys, "argv", ["pipeline_run.py", "--app", "demo"]):
            pipeline_main()

        # 3. Pipeline failed overall -> exit 1
        mock_run_pipe.return_value = {"status": "failed", "apps": {}}
        with patch.object(sys, "argv", ["pipeline_run.py", "--app", "demo"]):
            with self.assertRaises(SystemExit) as ctx:
                pipeline_main()
            self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
