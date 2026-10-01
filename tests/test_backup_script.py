"""scripts/backup.sh：備份要「確認真的到了雲端」才算成功。

out/ 不進 git、BigQuery 只留 60 天——備份是部署機外唯一的一份。最危險的失敗是「以為備份了」：
上傳指令沒報錯，雲端檔案卻不完整。所以腳本上傳後回讀雲端 md5 比對，對不上就記失敗，
健康檢查會據此發通知。這裡用假的 rclone（以本機資料夾模擬雲端）實際執行腳本，
並用 macOS 內建的 bash 3.2 跑，守住全形字緊接變數名的老問題。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = "/bin/bash" if Path("/bin/bash").exists() else shutil.which("bash")

# 假 rclone：remote 是本機資料夾；FAKE_RCLONE_CORRUPT=1 時上傳只寫一半（模擬傳輸不完整）
FAKE_RCLONE = '''import hashlib, os, shutil, sys
from pathlib import Path
cmd, args = sys.argv[1], sys.argv[2:]
if cmd == "copyto":
    src, dst = Path(args[0]), Path(args[1])
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = src.read_bytes()
    if os.environ.get("FAKE_RCLONE_CORRUPT") and "daily" in str(dst):
        data = data[: len(data) // 2]
    dst.write_bytes(data)
elif cmd == "md5sum":
    p = Path(args[0])
    if p.exists():
        print(hashlib.md5(p.read_bytes()).hexdigest() + "  " + p.name)
elif cmd == "delete":
    pass
'''


@unittest.skipUnless(BASH, "需要 bash")
class TestBackupScript(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.ct = base / "ct"
        (self.ct / "out" / "app_a").mkdir(parents=True)
        (self.ct / "out" / "app_a" / "historical_catalog.json").write_text('{"issues": {}}', encoding="utf-8")
        (self.ct / "reports" / "data").mkdir(parents=True)
        (self.ct / "reports" / "data" / "2026-10.json").write_text("{}", encoding="utf-8")
        (self.ct / "apps.yaml").write_text("apps: {}\n", encoding="utf-8")
        self.remote = base / "remote"
        fake = base / "fake_rclone.py"
        fake.write_text(FAKE_RCLONE, encoding="utf-8")
        self.rclone = base / "rclone"
        self.rclone.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
        self.rclone.chmod(0o755)
        self.status = self.ct / "out" / ".backup_status.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, **env_extra) -> subprocess.CompletedProcess:
        env = {**os.environ, "CT": str(self.ct), "RCLONE": str(self.rclone),
               "BACKUP_RCLONE_REMOTE": str(self.remote), **env_extra}
        r = subprocess.run([BASH, str(ROOT / "scripts" / "backup.sh")], env=env,
                           capture_output=True, text=True, timeout=60, check=False)
        self.assertNotIn("unbound variable", r.stdout + r.stderr)
        return r

    def test_success_uploads_verified_archive_with_history(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        status = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertTrue(status["ok"])
        uploaded = self.remote / "daily" / status["file"]
        self.assertEqual(hashlib.md5(uploaded.read_bytes()).hexdigest(), status["md5"])
        with tarfile.open(uploaded) as tf:
            names = tf.getnames()
        # 版本歷史（out/）與月摘要（reports/）都要在裡面，缺一個就等於沒備份到
        self.assertIn("out/app_a/historical_catalog.json", names)
        self.assertIn("reports/data/2026-10.json", names)
        self.assertIn("apps.yaml", names)

    def test_incomplete_upload_is_recorded_as_failure(self):
        r = self._run(FAKE_RCLONE_CORRUPT="1")
        self.assertNotEqual(r.returncode, 0)
        status = json.loads(self.status.read_text(encoding="utf-8"))
        self.assertFalse(status["ok"])
        self.assertIn("比對不符", status["error"])

    def test_missing_remote_skips_without_failing(self):
        r = self._run(BACKUP_RCLONE_REMOTE="")
        self.assertEqual(r.returncode, 0)
        self.assertFalse(self.status.exists())


if __name__ == "__main__":
    unittest.main()
