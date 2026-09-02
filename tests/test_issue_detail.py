"""Integration tests for Dashboard V2 Issue Detail data extraction.

These tests intentionally keep Schema V2 on the stable #10 contract while
verifying the Issue Detail implementation added by PR #13.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from crash_trend.fetch_issue_details import (
    build_issue_detail,
    extract_blame_frame_from_frames,
    fetch_issue_details_from_bq,
    format_stack_trace,
    load_issue_details_from_stacktraces_cache,
    parse_blame_frame,
    parse_breadcrumbs,
    parse_symbol,
)
from crash_trend.schema_v2 import validate_app_dashboard_v2, validate_crash_free_metric


class TestIssueDetailIntegration(unittest.TestCase):
    def test_parse_symbol_multilanguage_examples(self) -> None:
        self.assertEqual(
            parse_symbol("com.example.shop.CheckoutActivity.processPayment"),
            ("com.example.shop.CheckoutActivity", "processPayment"),
        )
        self.assertEqual(
            parse_symbol("ShopApp.ImageLoader.cacheImage(_:forKey:)"),
            ("ShopApp.ImageLoader", "cacheImage"),
        )
        self.assertEqual(
            parse_symbol("-[CheckoutViewController processPayment:]"),
            ("CheckoutViewController", "processPayment"),
        )
        self.assertEqual(
            parse_symbol("crash::CrashHandler::handleSignal"),
            ("crash::CrashHandler", "handleSignal"),
        )

    def test_official_blamed_frame_has_priority(self) -> None:
        frames = [
            {"file": "Thread.java", "line": 1, "symbol": "java.lang.Thread.run", "blamed": False},
            {
                "file": "CheckoutActivity.kt",
                "line": 142,
                "symbol": "com.example.shop.CheckoutActivity.processPayment",
                "blamed": True,
            },
            {"file": "Other.kt", "line": 20, "symbol": "com.example.Other.run"},
        ]
        blame = extract_blame_frame_from_frames(frames)
        self.assertIsNotNone(blame)
        self.assertEqual(blame["file"], "CheckoutActivity.kt")
        self.assertEqual(blame["line"], 142)

    def test_apple_error_stack_trace(self) -> None:
        trace = format_stack_trace(
            error=[
                {
                    "type": "NSInvalidArgumentException",
                    "reason": "key cannot be nil",
                    "frames": [
                        {
                            "file": "PaymentManager.swift",
                            "line": 80,
                            "symbol": "PaymentManager.processApplePay",
                        }
                    ],
                }
            ]
        )
        self.assertIsNotNone(trace)
        self.assertIn("NSInvalidArgumentException", trace)
        self.assertIn("PaymentManager.processApplePay(PaymentManager.swift:80)", trace)

    def test_bq_breadcrumb_name_params_format(self) -> None:
        items = parse_breadcrumbs(
            [
                {
                    "name": "user_action",
                    "params": [
                        {"key": "screen", "value": "checkout"},
                        {"key": "action", "value": "pay_click"},
                    ],
                    "timestamp": "2026-09-02T13:30:00Z",
                }
            ]
        )
        self.assertIsNotNone(items)
        self.assertEqual(items[0]["category"], "user_action")
        self.assertEqual(items[0]["data"], {"screen": "checkout", "action": "pay_click"})
        self.assertIn("screen=checkout", items[0]["message"])

    def test_top_level_blame_and_breadcrumbs_from_bq(self) -> None:
        mock_client = MagicMock()
        mock_client.query.return_value.result.side_effect = [
            [
                {
                    "issue_id": "issue-1",
                    "event_timestamp": dt.datetime(2026, 9, 2, 13, 40, tzinfo=dt.timezone.utc),
                    "device_model": "Pixel 8",
                    "os_version": "Android 14",
                    "blame_frame": {
                        "file": "CheckoutActivity.kt",
                        "line": 142,
                        "symbol": "com.example.shop.CheckoutActivity.processPayment",
                    },
                    "exceptions": [
                        {
                            "type": "java.lang.NullPointerException",
                            "frames": [
                                {
                                    "file": "CheckoutActivity.kt",
                                    "line": 142,
                                    "symbol": "com.example.shop.CheckoutActivity.processPayment",
                                }
                            ],
                        }
                    ],
                    "threads": [],
                    "error": [],
                    "breadcrumbs": [
                        {
                            "name": "cart_action",
                            "params": [{"key": "item_id", "value": "123"}],
                            "timestamp": "2026-09-02T13:39:50Z",
                        }
                    ],
                    "custom_keys": [{"key": "env", "value": "prod"}],
                    "logs": [],
                }
            ],
            [{"issue_id": "issue-1", "model": "Pixel 8", "events": 10}],
            [{"issue_id": "issue-1", "os_version": "Android 14", "events": 10}],
        ]

        result = fetch_issue_details_from_bq(
            client=mock_client,
            project="test-proj",
            dataset="firebase_crashlytics",
            tables=["com_example_shop_ANDROID"],
            issue_ids=["issue-1"],
        )
        self.assertIn("issue-1", result)
        self.assertEqual(result["issue-1"]["blame_frame"]["file"], "CheckoutActivity.kt")
        self.assertEqual(result["issue-1"]["detail"]["breadcrumbs"][0]["category"], "cart_action")

    def test_stacktraces_cache_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "stacktraces.json"
            cache.write_text(
                json.dumps(
                    {
                        "issues": {
                            "issue-2": {
                                "stack_trace": "NullPointerException\n\tat Foo.kt:10",
                                "blame_frame": {
                                    "file": "Foo.kt",
                                    "line": 10,
                                    "symbol": "com.example.Foo.bar",
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            result = load_issue_details_from_stacktraces_cache(cache)
            self.assertIn("issue-2", result)
            self.assertEqual(result["issue-2"]["blame_frame"]["file"], "Foo.kt")

    def test_issue_detail_still_conforms_to_dashboard_schema(self) -> None:
        fixture = json.loads((ROOT / "tests" / "fixtures" / "dashboard_v2.json").read_text(encoding="utf-8"))
        app = copy.deepcopy(fixture["apps"]["shop_app"])
        app["top_issues"][0]["blame_frame"] = parse_blame_frame(
            {
                "file": "CheckoutActivity.kt",
                "line": 142,
                "symbol": "com.example.shop.CheckoutActivity.processPayment",
            }
        )
        app["top_issues"][0]["detail"] = build_issue_detail(
            stack_trace="NullPointerException\n\tat CheckoutActivity.kt:142",
            breadcrumbs=[
                {
                    "timestamp": "2026-09-02T13:39:50Z",
                    "category": "navigation",
                    "message": "checkout",
                    "level": "info",
                }
            ],
            logs=[],
            custom_keys={"env": "prod"},
            top_devices=[{"model": "Pixel 8", "events": 10}],
            top_os=[{"os_version": "Android 14", "events": 10}],
        )
        self.assertEqual(validate_app_dashboard_v2(app), [])

    def test_schema_change_pct_points_regression_is_preserved(self) -> None:
        metric = {
            "rate": 0.99,
            "total": 100,
            "crashed": 1,
            "previous_rate": 0.98,
            "change_pct_points": "bad",
            "status": "available",
            "unavailable_reason": None,
        }
        errors: list[str] = []
        validate_crash_free_metric(metric, "metric", errors)
        self.assertTrue(any("change_pct_points" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
