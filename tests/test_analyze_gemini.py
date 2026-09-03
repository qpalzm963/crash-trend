"""Unit tests for crash_trend/analyze_gemini.py: Priority scoring, Gemini AI parsing, and graceful degradation."""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.analyze_gemini import (
    calculate_priority,
    enrich_app_data_with_priority_and_ai,
    generate_disabled_ai_summary,
    generate_disabled_issue_analysis,
    generate_error_ai_summary,
    map_score_to_level,
    parse_gemini_response,
    score_issues,
    source_snippet,
)
from crash_trend.schema_v2 import (
    is_valid_iso8601_utc,
    validate_app_dashboard_v2,
    validate_dashboard_v2,
)


class TestPriorityCalculation(unittest.TestCase):
    """Tests for deterministic priority score (0-100), level mapping (P0-P3), trend detection, and boosts."""

    def test_map_score_to_level(self) -> None:
        self.assertEqual(map_score_to_level(100), "P0")
        self.assertEqual(map_score_to_level(85), "P0")
        self.assertEqual(map_score_to_level(80), "P0")

        self.assertEqual(map_score_to_level(79), "P1")
        self.assertEqual(map_score_to_level(65), "P1")
        self.assertEqual(map_score_to_level(60), "P1")

        self.assertEqual(map_score_to_level(59), "P2")
        self.assertEqual(map_score_to_level(45), "P2")
        self.assertEqual(map_score_to_level(40), "P2")

        self.assertEqual(map_score_to_level(39), "P3")
        self.assertEqual(map_score_to_level(15), "P3")
        self.assertEqual(map_score_to_level(0), "P3")

    def test_priority_score_max_values(self) -> None:
        issue = {
            "issue_id": "test_max",
            "title": "NullPointerException in PaymentCheckout",
            "subtitle": "CheckoutActivity.kt:142",
            "error_type": "FATAL",
            "fatal": True,
            "events": 5000,
            "affected_users": 2000,
            "last_seen_version": "3.2.0",
        }
        prio = calculate_priority(
            issue=issue,
            max_users=2000,
            max_events=5000,
            prev_issue=None,  # trend = "new" -> worsening_boost = 2
            core_paths=["checkout", "payment"],  # core_path_boost = 3
            latest_app_version="3.2.0",  # latest_version_boost = 2
        )

        self.assertEqual(prio["score"], 100)
        self.assertEqual(prio["level"], "P0")
        self.assertEqual(prio["trend"], "new")

        bd = prio["score_breakdown"]
        self.assertIsNotNone(bd)
        self.assertEqual(bd["users_normalized"], 10.0)
        self.assertEqual(bd["events_normalized"], 10.0)
        self.assertEqual(bd["fatal_anr_boost"], 2)
        self.assertEqual(bd["worsening_boost"], 2)
        self.assertEqual(bd["latest_version_boost"], 2)
        self.assertEqual(bd["core_path_boost"], 3)

    def test_trend_detection(self) -> None:
        base_issue = {
            "issue_id": "i1",
            "title": "Crash A",
            "events": 100,
            "affected_users": 50,
            "fatal": False,
        }

        # Case 1: No previous issue -> "new"
        prio_new = calculate_priority(base_issue, max_users=100, max_events=200, prev_issue=None)
        self.assertEqual(prio_new["trend"], "new")
        self.assertEqual(prio_new["score_breakdown"]["worsening_boost"], 2)

        # Case 2: Events increased by > 20% (100 -> 130) -> "worsening"
        prio_worse = calculate_priority(
            {**base_issue, "events": 130},
            max_users=100,
            max_events=200,
            prev_issue={"events": 100},
        )
        self.assertEqual(prio_worse["trend"], "worsening")
        self.assertEqual(prio_worse["score_breakdown"]["worsening_boost"], 2)

        # Case 3: Events decreased by > 20% (100 -> 70) -> "improving"
        prio_improving = calculate_priority(
            {**base_issue, "events": 70},
            max_users=100,
            max_events=200,
            prev_issue={"events": 100},
        )
        self.assertEqual(prio_improving["trend"], "improving")
        self.assertEqual(prio_improving["score_breakdown"]["worsening_boost"], 0)

        # Case 4: Events within 0.8x to 1.2x (100 -> 105) -> "stable"
        prio_stable = calculate_priority(
            {**base_issue, "events": 105},
            max_users=100,
            max_events=200,
            prev_issue={"events": 100},
        )
        self.assertEqual(prio_stable["trend"], "stable")
        self.assertEqual(prio_stable["score_breakdown"]["worsening_boost"], 0)

    def test_boost_factors(self) -> None:
        # Fatal vs Non-Fatal / ANR
        issue_fatal = {"events": 100, "affected_users": 50, "fatal": True, "error_type": "FATAL"}
        prio_f = calculate_priority(issue_fatal, max_users=100, max_events=100)
        self.assertEqual(prio_f["score_breakdown"]["fatal_anr_boost"], 2)

        issue_anr = {"events": 100, "affected_users": 50, "fatal": False, "error_type": "ANR"}
        prio_anr = calculate_priority(issue_anr, max_users=100, max_events=100)
        self.assertEqual(prio_anr["score_breakdown"]["fatal_anr_boost"], 2)

        issue_non_fatal = {"events": 100, "affected_users": 50, "fatal": False, "error_type": "NON_FATAL"}
        prio_nf = calculate_priority(issue_non_fatal, max_users=100, max_events=100)
        self.assertEqual(prio_nf["score_breakdown"]["fatal_anr_boost"], 0)

        # Core path matching in title, subtitle, or blame_frame
        issue_core_title = {"title": "CartActivity crashed", "subtitle": "ui/view.kt", "events": 10, "users": 5}
        prio_core1 = calculate_priority(issue_core_title, max_users=10, max_events=10, core_paths=["cart"])
        self.assertEqual(prio_core1["score_breakdown"]["core_path_boost"], 3)

        issue_core_blame = {
            "title": "Exception",
            "subtitle": "other.kt",
            "blame_frame": {"file": "features/payment/gateway.kt"},
            "events": 10,
            "users": 5,
        }
        prio_core2 = calculate_priority(issue_core_blame, max_users=10, max_events=10, core_paths=["payment"])
        self.assertEqual(prio_core2["score_breakdown"]["core_path_boost"], 3)

        prio_no_core = calculate_priority(issue_core_title, max_users=10, max_events=10, core_paths=["auth", "login"])
        self.assertEqual(prio_no_core["score_breakdown"]["core_path_boost"], 0)

    def test_latest_app_version_boost_not_given_to_older_versions_when_latest_has_zero_crashes(self) -> None:
        """測試情境：3.3.0 是最新版（例如在 version_health status == 'latest'），且 3.3.0 完全無 crash。
        Issues 只有 3.2.0 / 3.1.0 的崩潰，驗證 3.2.0 不會被誤判為 latest 並錯誤獲得 latest_version_boost。
        """
        app_data = {
            "version_health": [
                {"version": "3.3.0", "status": "latest", "crash_events": 0, "affected_users": 0},
                {"version": "3.2.0", "status": "active", "crash_events": 100, "affected_users": 50},
                {"version": "3.1.0", "status": "deprecated", "crash_events": 20, "affected_users": 10},
            ],
            "top_issues": [
                {
                    "issue_id": "issue_320",
                    "title": "Crash on 3.2.0",
                    "events": 100,
                    "affected_users": 50,
                    "last_seen_version": "3.2.0",
                },
                {
                    "issue_id": "issue_310",
                    "title": "Crash on 3.1.0",
                    "events": 20,
                    "affected_users": 10,
                    "last_seen_version": "3.1.0",
                },
            ],
        }

        # Enrich without API key to test deterministic scoring
        with patch.dict(os.environ, {}, clear=True):
            enriched = enrich_app_data_with_priority_and_ai(app_data, api_key=None)

        issues_by_id = {i["issue_id"]: i for i in enriched["top_issues"]}
        issue_320 = issues_by_id["issue_320"]

        # 3.2.0 last_seen_version != 3.3.0 (true latest), so latest_version_boost must be 0
        self.assertEqual(issue_320["priority"]["score_breakdown"]["latest_version_boost"], 0)

    def test_score_issues_sorting_and_edge_cases(self) -> None:
        # Empty list
        self.assertEqual(score_issues([]), [])

        # Single issue
        single = [{"issue_id": "s1", "title": "Single", "events": 50, "users": 20, "fatal": True}]
        scored_single = score_issues(single)
        self.assertEqual(len(scored_single), 1)
        self.assertIn("priority", scored_single[0])
        self.assertEqual(scored_single[0]["score"], scored_single[0]["priority"]["score"])

        # Multiple issues sorting descending
        issues = [
            {"issue_id": "low", "title": "Low", "events": 10, "users": 5, "fatal": False},
            {"issue_id": "high", "title": "High", "events": 1000, "users": 500, "fatal": True},
            {"issue_id": "mid", "title": "Mid", "events": 200, "users": 100, "fatal": True},
        ]
        scored = score_issues(issues)
        self.assertEqual(scored[0]["issue_id"], "high")
        self.assertEqual(scored[1]["issue_id"], "mid")
        self.assertEqual(scored[2]["issue_id"], "low")
        self.assertTrue(scored[0]["priority"]["score"] >= scored[1]["priority"]["score"] >= scored[2]["priority"]["score"])


class TestGeminiParsingAndSchema(unittest.TestCase):
    """Tests for Gemini JSON response parsing, sanitization, and structured schema alignment."""

    def test_parse_valid_gemini_response(self) -> None:
        mock_ai_json = {
            "overview": "本期整體崩潰率下降 15%，主要風險集中在結帳模組 NPE。",
            "key_takeaways": [
                "P0 結帳 NPE 佔總崩潰 30%，需優先修復",
                "Android 14 啟動 ANR 需非同步載入",
            ],
            "distribution_insights": "崩潰集中於 Android 平台與 3.2.0 版本。",
            "recommended_actions": [
                {
                    "priority": "P0",
                    "issue_id": "issue_1",
                    "action": "修復 CheckoutActivity 空值保護",
                    "effort": "S",
                },
                {
                    "priority": "P1",
                    "issue_id": "issue_2",
                    "action": "將 Room 資料庫移出 Main Thread",
                    "effort": "M",
                },
            ],
            "data_limitations": "Firebase Sessions 僅 30 天數據。",
            "items": [
                {
                    "issue_id": "issue_1",
                    "root_cause": "userProfile 未初始化引發 NPE",
                    "suggested_fix": "加入 null check 與 loading 防護",
                    "effort": "S",
                    "confidence": "high",
                    "reasoning_sources": ["stack_trace", "blame_frame"],
                },
                {
                    "issue_id": "issue_2",
                    "root_cause": "主執行緒執行資料庫 migration 超過 5 秒",
                    "suggested_fix": "使用 Dispatchers.IO 協程非同步載入",
                    "effort": "M",
                    "confidence": "medium",
                    "reasoning_sources": ["stack_trace"],
                },
            ],
        }

        scored_issues = [
            {"issue_id": "issue_1", "title": "NPE", "blame_frame": {"file": "CheckoutActivity.kt"}},
            {"issue_id": "issue_2", "title": "ANR", "blame_frame": None},
        ]

        ai_summary, analysis_map = parse_gemini_response(mock_ai_json, scored_issues, model_name="gemini-flash-latest")

        # 1. AISummary validation
        self.assertEqual(ai_summary["status"], "available")
        self.assertEqual(ai_summary["model"], "gemini-flash-latest")
        self.assertTrue(is_valid_iso8601_utc(ai_summary["generated_at"]))
        self.assertEqual(ai_summary["overview"], mock_ai_json["overview"])
        self.assertEqual(len(ai_summary["key_takeaways"]), 2)
        self.assertEqual(len(ai_summary["recommended_actions"]), 2)
        self.assertEqual(ai_summary["recommended_actions"][0]["priority"], "P0")
        self.assertEqual(ai_summary["recommended_actions"][0]["effort"], "S")

        # 2. AIIssueAnalysis validation
        self.assertIn("issue_1", analysis_map)
        self.assertIn("issue_2", analysis_map)

        ia1 = analysis_map["issue_1"]
        self.assertEqual(ia1["status"], "available")
        self.assertEqual(ia1["effort"], "S")
        self.assertEqual(ia1["confidence"], "high")
        self.assertEqual(ia1["root_cause"], "userProfile 未初始化引發 NPE")

        ia2 = analysis_map["issue_2"]
        self.assertEqual(ia2["status"], "available")
        self.assertEqual(ia2["effort"], "M")
        self.assertEqual(ia2["confidence"], "medium")

    def test_parse_defensive_sanitization(self) -> None:
        # Incomplete or non-conforming responses
        raw_json = {
            "overview": "Overview text",
            "key_takeaways": "Single string takeaway",  # string instead of list
            "distribution_insights": None,
            "recommended_actions": [
                {"priority": "invalid_prio", "issue_id": "i1", "action": "Act 1", "effort": "s"},  # lowercase s
                {"priority": "P2", "issue_id": "i2", "action": "Act 2", "effort": "XL"},  # invalid effort
            ],
            "data_limitations": "",
            "items": [
                {
                    "issue_id": "i1",
                    "root_cause": "",
                    "suggested_fix": "",
                    "effort": "l",
                    "confidence": "positive",  # invalid confidence
                }
            ],
        }

        scored_issues = [
            {"issue_id": "i1", "title": "Issue 1"},
            {"issue_id": "i2_omitted", "title": "Issue 2 Omitted"},
        ]

        ai_summary, analysis_map = parse_gemini_response(raw_json, scored_issues)

        # Sanitized takeaway
        self.assertEqual(ai_summary["key_takeaways"], ["Single string takeaway"])

        # Sanitized actions
        self.assertEqual(ai_summary["recommended_actions"][0]["priority"], "P1")
        self.assertEqual(ai_summary["recommended_actions"][0]["effort"], "S")
        self.assertEqual(ai_summary["recommended_actions"][1]["priority"], "P2")
        self.assertEqual(ai_summary["recommended_actions"][1]["effort"], "M")

        # Sanitized issue item
        self.assertEqual(analysis_map["i1"]["effort"], "L")
        self.assertEqual(analysis_map["i1"]["confidence"], "needs_manual_review")
        self.assertEqual(analysis_map["i1"]["root_cause"], "需人工確認")

        # Omitted issue marked as skipped
        self.assertEqual(analysis_map["i2_omitted"]["status"], "skipped")
        self.assertEqual(analysis_map["i2_omitted"]["confidence"], "needs_manual_review")


class TestGracefulDegradation(unittest.TestCase):
    """Tests graceful degradation when API key is missing or when Gemini API call fails."""

    def setUp(self) -> None:
        self.fixtures_dir = ROOT / "tests" / "fixtures"
        self.fixture_path = self.fixtures_dir / "dashboard_v2.json"

    def test_disabled_generators(self) -> None:
        ai_summary = generate_disabled_ai_summary()
        self.assertEqual(ai_summary["status"], "disabled")
        self.assertIsNone(ai_summary["model"])
        self.assertIsNone(ai_summary["generated_at"])
        self.assertEqual(ai_summary["recommended_actions"], [])

        issue_analysis = generate_disabled_issue_analysis()
        self.assertEqual(issue_analysis["status"], "unavailable")
        self.assertIsNone(issue_analysis["root_cause"])
        self.assertIsNone(issue_analysis["suggested_fix"])
        self.assertIsNone(issue_analysis["effort"])

    def test_enrich_with_no_api_key_gracefully_degrades(self) -> None:
        data = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        shop_app = copy.deepcopy(data["apps"]["shop_app"])

        # Run enrichment with api_key=None and clear env
        with patch.dict(os.environ, {}, clear=True):
            enriched = enrich_app_data_with_priority_and_ai(
                shop_app,
                core_paths=["CheckoutActivity"],
                api_key=None,
            )

        # 1. AI Summary should be marked disabled
        self.assertEqual(enriched["ai_summary"]["status"], "disabled")
        self.assertIsNone(enriched["ai_summary"]["model"])
        self.assertIsNone(enriched["ai_summary"]["generated_at"])

        # 2. Source status should be disabled
        self.assertEqual(enriched["sources"]["gemini_ai"]["status"], "disabled")
        self.assertIsNone(enriched["sources"]["gemini_ai"]["last_sync_timestamp"])

        # 3. Each issue's ai_analysis should be unavailable
        for issue in enriched["top_issues"]:
            self.assertEqual(issue["ai_analysis"]["status"], "unavailable")
            self.assertIsNone(issue["ai_analysis"]["root_cause"])
            # Priority score must still be calculated and valid
            self.assertIn("score", issue["priority"])
            self.assertIn("level", issue["priority"])
            self.assertIn(issue["priority"]["level"], {"P0", "P1", "P2", "P3"})

        # 4. Strict Schema V2 validation must pass with 0 errors
        errors = validate_app_dashboard_v2(enriched, prefix="apps['shop_app']")
        self.assertEqual(errors, [], f"Schema validation failed: {errors}")

    def test_enrich_with_api_error_gracefully_degrades(self) -> None:
        data = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        shop_app = copy.deepcopy(data["apps"]["shop_app"])

        # Mock GeminiProvider.analyze to raise an error
        with patch("crash_trend.ai_provider.GeminiProvider.analyze", side_effect=RuntimeError("API Quota Exceeded")):
            enriched = enrich_app_data_with_priority_and_ai(
                shop_app,
                api_key="mock_api_key_123",
            )

        # 1. AI Summary should be marked error
        self.assertEqual(enriched["ai_summary"]["status"], "error")
        self.assertIn("API Quota Exceeded", enriched["ai_summary"]["overview"])

        # 2. Source status should be error
        self.assertEqual(enriched["sources"]["gemini_ai"]["status"], "error")

        # 3. Each issue's ai_analysis should be unavailable
        for issue in enriched["top_issues"]:
            self.assertEqual(issue["ai_analysis"]["status"], "unavailable")
            self.assertIn("score", issue["priority"])

        # 4. Strict Schema V2 validation must pass
        errors = validate_app_dashboard_v2(enriched, prefix="apps['shop_app']")
        self.assertEqual(errors, [], f"Schema validation failed: {errors}")

    def test_enrich_with_successful_mock_api(self) -> None:
        data = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        shop_app = copy.deepcopy(data["apps"]["shop_app"])

        mock_response = {
            "overview": "本期整體崩潰收斂 20.2%，然而 3.2.0 新版在結帳流程中引入 1 個 P0 NPE 崩潰。",
            "key_takeaways": [
                "P0: CheckoutActivity.kt:142 佔本期 27.5%，建議立即發布 Hotfix",
                "Android 14 上的 ANR 集中在啟動階段 Database init",
            ],
            "distribution_insights": "崩潰高度集中於 Android 平台與 3.2.0 版本。",
            "recommended_actions": [
                {
                    "priority": "P0",
                    "issue_id": "8a7f1b2c",
                    "action": "修復 CheckoutActivity 空值指標保護",
                    "effort": "S",
                }
            ],
            "data_limitations": "Firebase Sessions 僅收集最近 30 天數據。",
            "items": [
                {
                    "issue_id": "8a7f1b2c",
                    "root_cause": "結帳流程中 userProfile 尚未初始化即被呼叫導致 NPE。",
                    "suggested_fix": "在呼叫 processPayment 前加入 profile 空值防護並補齊 fallback 提示。",
                    "effort": "S",
                    "confidence": "high",
                    "reasoning_sources": ["stack_trace", "blame_frame"],
                }
            ],
        }

        with patch("crash_trend.ai_provider.GeminiProvider.analyze", return_value=mock_response):
            enriched = enrich_app_data_with_priority_and_ai(
                shop_app,
                core_paths=["CheckoutActivity"],
                api_key="valid_test_key",
                model="gemini-flash-latest",
            )

        # 1. AI Summary should be available
        self.assertEqual(enriched["ai_summary"]["status"], "available")
        self.assertEqual(enriched["ai_summary"]["model"], "gemini-flash-latest")
        self.assertTrue(is_valid_iso8601_utc(enriched["ai_summary"]["generated_at"]))

        # 2. Source status should be available
        self.assertEqual(enriched["sources"]["gemini_ai"]["status"], "available")
        self.assertTrue(is_valid_iso8601_utc(enriched["sources"]["gemini_ai"]["last_sync_timestamp"]))

        # 3. Issue ai_analysis
        top_issue = enriched["top_issues"][0]
        self.assertEqual(top_issue["issue_id"], "8a7f1b2c")
        self.assertEqual(top_issue["ai_analysis"]["status"], "available")
        self.assertEqual(top_issue["ai_analysis"]["effort"], "S")
        self.assertEqual(top_issue["ai_analysis"]["confidence"], "high")

        # 4. Strict Schema V2 validation must pass
        errors = validate_app_dashboard_v2(enriched, prefix="apps['shop_app']")
        self.assertEqual(errors, [], f"Schema validation failed: {errors}")


class TestSourceSnippet(unittest.TestCase):
    """Tests source code snippet extraction."""

    def test_snippet_handles_missing_safely(self) -> None:
        self.assertEqual(source_snippet(None, "CheckoutActivity.kt:142"), "")
        self.assertEqual(source_snippet("/invalid/path", "CheckoutActivity.kt:142"), "")
        self.assertEqual(source_snippet(ROOT, ""), "")
        self.assertEqual(source_snippet(ROOT, "non_existent_file_xyz.kt:10"), "")


if __name__ == "__main__":
    unittest.main()
