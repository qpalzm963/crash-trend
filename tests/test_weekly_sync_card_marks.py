"""weekly_sync.sh 的月報發卡紀錄：每個 app 各自獨立。

先前的設計是「全部 app 都成功才記本月已發」：只要一個 app 失敗，下週重試時會把其他 app
已經發過的卡再發一次，同一張卡在團隊聊天室出現兩次。改為每個 app 各自記錄後：

- post_report 結束碼 0 → 記這個 app 本月已發
- 結束碼 3（資料不完整或無法確認）→ 不記，下週重試，log 標記 post-延後:<app>
- 其他結束碼 → 不記，log 標記 post:<app>

這裡以假的 pipeline / pm_brief / post_report 實際執行腳本，驗證的是腳本本身的發卡邏輯。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STUB_OK = "import sys\nsys.exit(0)\n"
STUB_POST = '''import json, sys
from pathlib import Path
ct = Path(__file__).resolve().parents[1]
app = sys.argv[sys.argv.index("--app") + 1]
code = json.loads((ct / "behavior.json").read_text()).get(app, 0)
if code == 0:
    with open(ct / "posted.log", "a") as f:
        f.write(app + "\\n")
sys.exit(code)
'''

# 備份與健康檢查：記下執行順序，結束碼由 behavior.json 的 "backup" / "health" 決定
STUB_STAGE = '''import json, sys
from pathlib import Path
ct = Path(__file__).resolve().parents[1]
name = Path(__file__).stem
with open(ct / "stages.log", "a") as f:
    f.write(name + "\\n")
sys.exit(json.loads((ct / "behavior.json").read_text()).get(name, 0))
'''


@unittest.skipUnless(shutil.which("bash"), "需要 bash")
class TestWeeklySyncPerAppCardMarks(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ct = Path(self._tmp.name)
        (self.ct / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "weekly_sync.sh", self.ct / "scripts" / "weekly_sync.sh")
        venv_bin = self.ct / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        # 腳本優先用 $CT/.venv/bin/python，且要 yaml 來列出 apps。不能直接 symlink：venv 的
        # python 被 symlink 到別處後找不到原 venv 的套件，改用轉呼叫的 wrapper。
        wrapper = venv_bin / "python"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
        (self.ct / "apps.yaml").write_text("apps:\n  app_a: {}\n  app_b: {}\n", encoding="utf-8")
        (self.ct / ".env").write_text("CRASH_REPORT_URL=http://chat.invalid/api\nINTERNAL_API_TOKEN=t\n", encoding="utf-8")
        ct_pkg = self.ct / "crash_trend"
        ct_pkg.mkdir()
        (ct_pkg / "pipeline_run.py").write_text(STUB_OK, encoding="utf-8")
        (ct_pkg / "pm_brief.py").write_text(STUB_OK, encoding="utf-8")
        (ct_pkg / "post_report.py").write_text(STUB_POST, encoding="utf-8")
        (ct_pkg / "health_check.py").write_text(STUB_STAGE.replace("Path(__file__).stem", '"health"'), encoding="utf-8")
        # backup.sh 由腳本以 bash 呼叫：stub 轉給 python 執行同一份記錄邏輯
        backup_py = self.ct / "scripts" / "backup_stub.py"
        backup_py.write_text(STUB_STAGE.replace("Path(__file__).stem", '"backup"'), encoding="utf-8")
        (self.ct / "scripts" / "backup.sh").write_text(f'exec "{sys.executable}" "{backup_py}"\n', encoding="utf-8")
        self.month = dt.date.today().strftime("%Y-%m")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, behavior: dict) -> str:
        (self.ct / "behavior.json").write_text(json.dumps(behavior), encoding="utf-8")
        log = self.ct / "logs" / "weekly_sync.log"
        if log.exists():
            log.unlink()
        env = {**os.environ, "CRASH_TREND_NO_NOTIFY": "1"}
        subprocess.run(["bash", str(self.ct / "scripts" / "weekly_sync.sh")], env=env, check=False, timeout=60)
        text = log.read_text(encoding="utf-8", errors="replace")
        # macOS bash 3.2 會把 $var 後緊接的全形字吃進變數名，在 set -u 下中止整個腳本
        self.assertNotIn("unbound variable", text)
        self.assertIn("===== done", text, "weekly_sync.sh 沒有跑到結尾")
        return text

    def _posted(self) -> list[str]:
        f = self.ct / "posted.log"
        return f.read_text().split() if f.exists() else []

    def _marked(self, app: str) -> bool:
        mark = self.ct / "out" / app / ".card_sent_month"
        return mark.exists() and mark.read_text().strip() == self.month

    def test_deferred_app_retries_next_week_without_resending_others(self):
        log = self._run({"app_a": 0, "app_b": 3})
        self.assertEqual(self._posted(), ["app_a"])
        self.assertTrue(self._marked("app_a"))
        self.assertFalse(self._marked("app_b"))
        self.assertIn("post-延後:app_b", log)

        # 下週：app_b 資料齊了。app_a 不得重發；app_b 補發
        self._run({"app_a": 0, "app_b": 0})
        self.assertEqual(self._posted(), ["app_a", "app_b"])
        self.assertTrue(self._marked("app_b"))

    def test_send_failure_is_reported_and_retried_but_isolated(self):
        log = self._run({"app_a": 0, "app_b": 1})
        self.assertIn("post:app_b", log)
        self.assertNotIn("post-延後:app_b", log)
        self.assertTrue(self._marked("app_a"))
        self.assertFalse(self._marked("app_b"))

    def test_legacy_month_mark_counts_as_all_sent(self):
        """升級當月：舊版單一標記已記本月，代表那次已全部發過；新版不得再發一輪。"""
        (self.ct / "out").mkdir()
        (self.ct / "out" / ".card_sent_month").write_text(self.month + "\n", encoding="utf-8")
        self._run({"app_a": 0, "app_b": 0})
        self.assertEqual(self._posted(), [])

    def test_legacy_mark_from_previous_month_is_ignored(self):
        (self.ct / "out").mkdir()
        (self.ct / "out" / ".card_sent_month").write_text("1999-01\n", encoding="utf-8")
        self._run({"app_a": 0, "app_b": 0})
        self.assertEqual(sorted(self._posted()), ["app_a", "app_b"])

    def _stages(self) -> list[str]:
        f = self.ct / "stages.log"
        return f.read_text().split() if f.exists() else []

    def test_monthly_card_off_sends_nothing_and_marks_nothing(self):
        """改由定期彙整取代月報卡時，不能還偷偷發卡，也不能記成已發（日後切回來才會正常發）。"""
        with open(self.ct / ".env", "a", encoding="utf-8") as f:
            f.write("MONTHLY_CARD=off\n")
        log = self._run({"app_a": 0, "app_b": 0})
        self.assertEqual(self._posted(), [])
        self.assertFalse(self._marked("app_a"))
        self.assertIn("MONTHLY_CARD=off", log)

    def test_backup_then_health_check_run_every_time(self):
        """健康檢查要看得到今天的備份結果，所以必須在備份之後。"""
        self._run({"app_a": 0, "app_b": 0})
        self.assertEqual(self._stages(), ["backup", "health"])

    def test_backup_and_health_failures_are_reported(self):
        log = self._run({"app_a": 0, "app_b": 0, "backup": 1, "health": 1})
        self.assertIn("backup", log.split("failed:")[-1])
        self.assertIn("health", log.split("failed:")[-1])
        # 備份失敗仍要跑健康檢查，否則沒人會被通知
        self.assertEqual(self._stages(), ["backup", "health"])


if __name__ == "__main__":
    unittest.main()
