"""每日健康檢查：有問題才通知，而且要通知到「受影響的」聊天室。

- 排程沒跑、備份失敗、Firebase 登入失效 → 每個 app 都受影響，全部通知
- 某 app 的步驟失敗 → 只通知那個 app，不吵其他聊天室
- disabled / skipped 是設定使然（例如停用 AI），不是故障，不能每天誤報
- 沒設備份目的地就不檢查備份；有設卻沒成功紀錄，要報（不能把「從沒成功」當成正常）
"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crash_trend"))

import health_check  # noqa: E402

NOW = dt.datetime(2026, 10, 2, 3, 0, tzinfo=dt.UTC)
APPS = ["app_a", "app_b"]


def run(finished: dt.datetime, stages_a: dict | None = None, stages_b: dict | None = None) -> dict:
    ok = {"status": "success"}
    return {
        "finished_at": finished.isoformat().replace("+00:00", "Z"),
        "apps": {
            "app_a": {"stages": stages_a or {"crashlytics_bigquery": ok}},
            "app_b": {"stages": stages_b or {"crashlytics_bigquery": ok}},
        },
    }


GOOD_BACKUP = {"ok": True, "at": (NOW - dt.timedelta(hours=1)).isoformat()}


class TestCollectProblems(unittest.TestCase):
    def check(self, **kw):
        args = {"apps": APPS, "run_summary": run(NOW - dt.timedelta(hours=1)), "backup_status": GOOD_BACKUP,
                "backup_expected": True, "firebase_ok": True, "now": NOW}
        args.update(kw)
        return health_check.collect_problems(**args)

    def test_all_good_sends_nothing(self):
        self.assertEqual(self.check(), {})

    def test_stale_run_is_reported_to_every_app(self):
        problems = self.check(run_summary=run(NOW - dt.timedelta(hours=30)))
        self.assertEqual(sorted(problems), APPS)
        self.assertIn("沒更新", problems["app_a"][0])

    def test_missing_run_summary_is_reported(self):
        self.assertEqual(sorted(self.check(run_summary=None)), APPS)

    def test_failed_stage_only_alerts_that_app(self):
        stages = {"crashlytics_bigquery": {"status": "failed"}, "ai": {"status": "disabled"}}
        problems = self.check(run_summary=run(NOW - dt.timedelta(hours=1), stages_a=stages))
        self.assertEqual(list(problems), ["app_a"])
        self.assertIn("BigQuery 資料", problems["app_a"][0])

    def test_disabled_and_skipped_stages_are_not_failures(self):
        stages = {"ai": {"status": "disabled"}, "sessions": {"status": "skipped"}}
        self.assertEqual(self.check(run_summary=run(NOW - dt.timedelta(hours=1), stages_a=stages)), {})

    def test_backup_failure_missing_and_stale_are_reported(self):
        for status in ({"ok": False, "error": "quota"}, None, {"ok": True, "at": (NOW - dt.timedelta(hours=40)).isoformat()}):
            with self.subTest(status=status):
                self.assertEqual(sorted(self.check(backup_status=status)), APPS)

    def test_backup_not_checked_when_not_configured(self):
        self.assertEqual(self.check(backup_status=None, backup_expected=False), {})

    def test_firebase_failure_is_reported_but_unknown_is_not(self):
        self.assertEqual(sorted(self.check(firebase_ok=False)), APPS)
        self.assertEqual(self.check(firebase_ok=None), {})


if __name__ == "__main__":
    unittest.main()
