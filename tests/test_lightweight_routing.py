"""Tests for Lightweight Production Routing (Dashboard V2.5 - Issue #40).

Tests:
1. Task taxonomy constants and routing decisions in auto, gemini_only, and openrouter_only modes.
2. Lightweight Triage JSON Schema contract and response parsing.
3. End-to-end multi-task execution in enrich_app_data_with_priority_and_ai:
   - Auto mode routes triage to OpenRouter Free worker and deep analysis to Gemini Direct.
   - Telemetry records both tasks with respective providers, models, and reasons.
   - Issue AI analysis is enriched with both deep analysis and lightweight triage metadata.
4. Graceful degradation:
   - OpenRouter 429/timeout does not block the pipeline or crash priority scoring.
   - Deep analysis still succeeds when lightweight worker is degraded or unconfigured.
5. Strict Cost Guard enforcement on lightweight tasks.
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from crash_trend.ai_provider import (
    CANONICAL_AI_RESPONSE_SCHEMA,
    CANONICAL_LIGHTWEIGHT_TRIAGE_SCHEMA,
    DEFAULT_GEMINI_MODEL,
    GeminiProvider,
    OpenRouterProvider,
)
from crash_trend.ai_router import (
    LIGHTWEIGHT_TASKS,
    TASK_DEEP_ANALYSIS,
    TASK_ISSUE_CLASSIFICATION,
    TASK_ISSUE_SUMMARY,
    TASK_ISSUE_TAGGING,
    TASK_ISSUE_TRIAGE,
    TASK_LIGHTWEIGHT,
    AIRouterConfig,
    AITaskRouter,
    get_ai_router,
)
from crash_trend.analyze_gemini import (
    build_lightweight_triage_prompt,
    enrich_app_data_with_priority_and_ai,
    parse_lightweight_triage_response,
)
from crash_trend.schema_v2 import validate_app_dashboard_v2

ROOT = Path(__file__).resolve().parent.parent


class TestLightweightRouting(unittest.TestCase):
    """Test suite for Issue #40 Lightweight Production Routing."""

    def test_1_taxonomy_and_auto_routing(self) -> None:
        """Test 1: Auto mode routes all lightweight tasks to OpenRouter Free worker."""
        router = get_ai_router(global_cfg={"ai": {"mode": "auto"}})

        # Deep analysis -> Gemini
        d_deep = router.route(TASK_DEEP_ANALYSIS)
        self.assertEqual(d_deep.selected_provider, "gemini")
        self.assertEqual(d_deep.selected_model, DEFAULT_GEMINI_MODEL)
        self.assertIn("Gemini Direct", d_deep.routing_reason)

        # All lightweight tasks -> OpenRouter Free
        for task in (
            TASK_LIGHTWEIGHT,
            TASK_ISSUE_TRIAGE,
            TASK_ISSUE_SUMMARY,
            TASK_ISSUE_CLASSIFICATION,
            TASK_ISSUE_TAGGING,
        ):
            self.assertIn(task, LIGHTWEIGHT_TASKS)
            d_light = router.route(task)
            self.assertEqual(d_light.selected_provider, "openrouter")
            self.assertEqual(d_light.selected_model, "openrouter/free")
            self.assertIn("lightweight worker (OpenRouter)", d_light.routing_reason)

    def test_2_manual_overrides_respect_all_tasks(self) -> None:
        """Test 2: gemini_only and openrouter_only override modes force designated provider."""
        router_gemini = get_ai_router(global_cfg={"ai": {"mode": "gemini_only"}})
        for task in (TASK_DEEP_ANALYSIS, TASK_ISSUE_TRIAGE, TASK_ISSUE_SUMMARY):
            d = router_gemini.route(task)
            self.assertEqual(d.selected_provider, "gemini")
            self.assertIn("Gemini only mode", d.routing_reason)

        router_or = get_ai_router(global_cfg={"ai": {"mode": "openrouter_only"}})
        for task in (TASK_DEEP_ANALYSIS, TASK_ISSUE_TRIAGE, TASK_ISSUE_SUMMARY):
            d = router_or.route(task)
            self.assertEqual(d.selected_provider, "openrouter")
            self.assertIn("OpenRouter only mode", d.routing_reason)

    def test_3_triage_prompt_and_parser(self) -> None:
        """Test 3: build_lightweight_triage_prompt and parse_lightweight_triage_response."""
        issues = [
            {
                "issue_id": "ISSUE_101",
                "title": "NullPointerException: Attempt to invoke virtual method",
                "subtitle": "MainActivity.kt:42",
                "error_type": "FATAL",
                "events": 250,
                "users": 180,
                "priority": {"score": 85, "level": "P0"},
            },
            {
                "issue_id": "ISSUE_102",
                "title": "SocketTimeoutException: failed to connect",
                "subtitle": "NetworkClient.kt:110",
                "error_type": "NON_FATAL",
                "events": 20,
                "users": 15,
                "priority": {"score": 30, "level": "P3"},
            },
        ]
        prompt = build_lightweight_triage_prompt("TestApp", issues)
        self.assertIn("TestApp", prompt)
        self.assertIn("ISSUE_101", prompt)
        self.assertIn("NullPointerException", prompt)
        self.assertIn("ISSUE_102", prompt)

        # Parse valid response
        raw_res = {
            "items": [
                {
                    "issue_id": "ISSUE_101",
                    "short_summary": "空指標異常於主畫面載入時觸發崩潰",
                    "category": "NULL_POINTER",
                    "tags": ["null_pointer", "main_activity", "init"],
                    "warrants_deep_analysis": True,
                    "triage_reason": "P0 致命崩潰且影響大量用戶，需深度排查修復",
                },
                {
                    "issue_id": "ISSUE_102",
                    "short_summary": "網路連線逾時偶發異常",
                    "category": "NETWORK",
                    "tags": ["timeout", "socket"],
                    "warrants_deep_analysis": False,
                    "triage_reason": "非致命網路抖動，無需深入推理",
                },
            ]
        }
        parsed = parse_lightweight_triage_response(raw_res)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed["ISSUE_101"]["category"], "NULL_POINTER")
        self.assertTrue(parsed["ISSUE_101"]["warrants_deep_analysis"])
        self.assertEqual(parsed["ISSUE_101"]["tags"], ["null_pointer", "main_activity", "init"])
        self.assertEqual(parsed["ISSUE_102"]["category"], "NETWORK")
        self.assertFalse(parsed["ISSUE_102"]["warrants_deep_analysis"])

        # Parse response with invalid category -> defaults to OTHER
        invalid_cat_res = {
            "items": [
                {
                    "issue_id": "ISSUE_103",
                    "short_summary": "未知問題",
                    "category": "WEIRD_CUSTOM_CAT",
                    "tags": ["unknown"],
                    "warrants_deep_analysis": True,
                    "triage_reason": "不明",
                }
            ]
        }
        parsed_inv = parse_lightweight_triage_response(invalid_cat_res)
        self.assertEqual(parsed_inv["ISSUE_103"]["category"], "OTHER")

    @patch("crash_trend.ai_router.AITaskRouter.analyze")
    def test_4_e2e_production_multi_task_routing(self, mock_analyze: MagicMock) -> None:
        """Test 4: Auto mode executes both lightweight triage (OpenRouter) and deep analysis (Gemini)."""
        fixture_path = ROOT / "tests" / "fixtures" / "dashboard_v2_no_sessions.json"
        fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))
        app_data = copy.deepcopy(fixture_data["apps"]["legacy_app"])

        router_cfg = AIRouterConfig(
            mode="auto",
            primary_provider="gemini",
            primary_model=DEFAULT_GEMINI_MODEL,
            primary_api_key="AIzaDirectTestKey",
            lightweight_provider="openrouter",
            lightweight_model="openrouter/free",
            lightweight_api_key="sk-or-test-free-key",
            allow_paid_models=False,
        )
        router = AITaskRouter(router_cfg)

        def mock_side_effect(prompt: str, schema=None, task_type="deep_analysis"):
            if task_type == TASK_ISSUE_TRIAGE:
                return MagicMock(
                    data={
                        "items": [
                            {
                                "issue_id": "aa11bb22",
                                "short_summary": "空指針閃退",
                                "category": "NULL_POINTER",
                                "tags": ["npe", "launch"],
                                "warrants_deep_analysis": True,
                                "triage_reason": "關鍵路徑致命崩潰",
                            }
                        ]
                    },
                    decision=MagicMock(routing_reason="Auto mode: issue_triage routed to lightweight worker (OpenRouter)"),
                    fallback_used=False,
                    fallback_reason=None,
                    active_provider="openrouter",
                    active_model="openrouter/free",
                )
            else:  # deep_analysis
                return MagicMock(
                    data={
                        "overview": "整體穩定度分析正常",
                        "key_takeaways": ["需優先處理空指針問題"],
                        "distribution_insights": "主要集中於 Android 14",
                        "recommended_actions": [
                            {"priority": "P0", "issue_id": "aa11bb22", "action": "修復空指針保護", "effort": "S"}
                        ],
                        "data_limitations": None,
                        "items": [
                            {
                                "issue_id": "aa11bb22",
                                "root_cause": "變數未初始化即調用",
                                "suggested_fix": "增加 null check",
                                "effort": "S",
                                "confidence": "high",
                                "reasoning_sources": ["stack_trace"],
                            }
                        ],
                    },
                    decision=MagicMock(routing_reason="Auto mode: deep_analysis routed to primary provider (Gemini Direct)"),
                    fallback_used=False,
                    fallback_reason=None,
                    active_provider="gemini",
                    active_model=DEFAULT_GEMINI_MODEL,
                )

        mock_analyze.side_effect = mock_side_effect

        enriched = enrich_app_data_with_priority_and_ai(app_data, router=router)

        # 1. Verify schema validity
        errs = validate_app_dashboard_v2(enriched)
        self.assertEqual(errs, [])

        # 2. Verify router was called twice: once for triage, once for deep_analysis
        self.assertEqual(mock_analyze.call_count, 2)
        call_tasks = [c[1].get("task_type") or (c[0][2] if len(c[0]) > 2 else c[1].get("task_type")) for c in mock_analyze.call_args_list]
        self.assertIn(TASK_ISSUE_TRIAGE, call_tasks)
        self.assertIn(TASK_DEEP_ANALYSIS, call_tasks)

        # 3. Verify telemetry contracts
        src_ai = enriched["sources"]["ai"]
        self.assertEqual(src_ai["status"], "available")
        self.assertEqual(src_ai["requested_mode"], "auto")
        self.assertIn("tasks", src_ai)
        tasks = src_ai["tasks"]
        self.assertIn("lightweight", tasks)
        self.assertIn("deep_analysis", tasks)
        self.assertEqual(tasks["lightweight"]["provider"], "openrouter")
        self.assertEqual(tasks["lightweight"]["model"], "openrouter/free")
        self.assertEqual(tasks["deep_analysis"]["provider"], "gemini")
        self.assertEqual(tasks["deep_analysis"]["model"], DEFAULT_GEMINI_MODEL)

        # 4. Verify issue enrichment includes both deep and lightweight attributes
        iss = next(i for i in enriched["top_issues"] if i["issue_id"] == "aa11bb22")
        ai_an = iss["ai_analysis"]
        self.assertEqual(ai_an["status"], "available")
        self.assertEqual(ai_an["root_cause"], "變數未初始化即調用")
        self.assertEqual(ai_an["suggested_fix"], "增加 null check")
        self.assertEqual(ai_an["category"], "NULL_POINTER")
        self.assertEqual(ai_an["tags"], ["npe", "launch"])
        self.assertEqual(ai_an["short_summary"], "空指針閃退")
        self.assertTrue(ai_an["warrants_deep_analysis"])

    @patch("crash_trend.ai_router.AITaskRouter.analyze")
    def test_5_lightweight_failure_graceful_degradation(self, mock_analyze: MagicMock) -> None:
        """Test 5: Lightweight worker failure (e.g. 429) gracefully degrades without blocking deep analysis."""
        fixture_path = ROOT / "tests" / "fixtures" / "dashboard_v2_no_sessions.json"
        fixture_data = json.loads(fixture_path.read_text(encoding="utf-8"))
        app_data = copy.deepcopy(fixture_data["apps"]["legacy_app"])

        router_cfg = AIRouterConfig(
            mode="auto",
            primary_provider="gemini",
            primary_model=DEFAULT_GEMINI_MODEL,
            primary_api_key="AIzaDirectTestKey",
            lightweight_provider="openrouter",
            lightweight_model="openrouter/free",
            lightweight_api_key="sk-or-test-free-key",
            allow_paid_models=False,
        )
        router = AITaskRouter(router_cfg)

        def mock_side_effect(prompt: str, schema=None, task_type="deep_analysis"):
            if task_type == TASK_ISSUE_TRIAGE:
                # Simulate OpenRouter 429 rate limit
                raise RuntimeError("OpenRouter API 429: Free rate limit exceeded")
            else:
                return MagicMock(
                    data={
                        "overview": "Deep analysis succeeded despite lightweight failure",
                        "key_takeaways": ["正常完成深度推理"],
                        "distribution_insights": "正常",
                        "recommended_actions": [],
                        "data_limitations": None,
                        "items": [
                            {
                                "issue_id": "aa11bb22",
                                "root_cause": "記憶體洩漏",
                                "suggested_fix": "釋放監聽器",
                                "effort": "M",
                                "confidence": "high",
                                "reasoning_sources": ["stack_trace"],
                            }
                        ],
                    },
                    decision=MagicMock(routing_reason="Gemini Direct"),
                    fallback_used=False,
                    fallback_reason=None,
                    active_provider="gemini",
                    active_model=DEFAULT_GEMINI_MODEL,
                )

        mock_analyze.side_effect = mock_side_effect

        enriched = enrich_app_data_with_priority_and_ai(app_data, router=router)

        # Core pipeline and schema MUST remain valid
        errs = validate_app_dashboard_v2(enriched)
        self.assertEqual(errs, [])

        # Priority score must be calculated
        self.assertGreater(enriched["top_issues"][0]["priority"]["score"], 0)

        # Deep analysis succeeded
        self.assertEqual(enriched["sources"]["ai"]["status"], "available")
        tasks = enriched["sources"]["ai"]["tasks"]
        self.assertEqual(tasks["lightweight"]["status"], "error")
        self.assertIn("429", tasks["lightweight"]["error_message"])
        self.assertEqual(tasks["deep_analysis"]["status"], "available")

        # Deep analysis results intact
        iss = next(i for i in enriched["top_issues"] if i["issue_id"] == "aa11bb22")
        self.assertEqual(iss["ai_analysis"]["root_cause"], "記憶體洩漏")

    def test_6_lightweight_free_guard_blocks_paid_models(self) -> None:
        """Test 6: Free Guard strictly rejects paid model in lightweight configuration."""
        cfg = AIRouterConfig(
            mode="auto",
            lightweight_provider="openrouter",
            lightweight_model="anthropic/claude-3.5-sonnet",  # Paid model!
            allow_paid_models=False,
        )
        router = AITaskRouter(cfg)

        with self.assertRaises(ValueError) as ctx:
            router.route(TASK_ISSUE_TRIAGE)
        self.assertIn("Paid model 'anthropic/claude-3.5-sonnet' not allowed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
