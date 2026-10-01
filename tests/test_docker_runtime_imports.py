"""排程容器裡的模組匯入：Docker image 不安裝 crash_trend 本身。

開發機與 CI 會 editable install 這個專案（site-packages 裡有指回 repo 的 .pth），所以
`crash_trend` 在哪裡都 import 得到；容器只 `pip install -r requirements.txt`，沒有這個 .pth。
排程以 `python crash_trend/<stage>.py` 逐步執行時 sys.path[0] 是 crash_trend/ 而非 repo 根目錄，
各模組 `from crash_trend.x import ...` 的 fallback 因此找不到套件——容器裡整條管線會在
fetch_stacktraces、pm_brief、AI 分析等步驟中止，而所有既有測試都是綠的。

這裡用 `python -S`（不處理 site / .pth）＋ PYTHONPATH 指向 site-packages 模擬容器：相依套件可用、
專案未安裝，再套上 Dockerfile 宣告的 PYTHONPATH，驗證每個排程步驟都能啟動。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import sysconfig
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE_PACKAGES = sysconfig.get_paths()["purelib"]


def dockerfile_pythonpath() -> list[str]:
    """Dockerfile 宣告的 PYTHONPATH，容器內的 /app 換成本機 repo 路徑。"""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    m = re.search(r"^ENV\s+PYTHONPATH=(\S+)", text, re.MULTILINE)
    if not m:
        return []
    return [p.replace("/app", str(ROOT), 1) for p in m.group(1).split(":") if p]


def scheduled_stage_scripts() -> list[str]:
    """排程實際會以腳本方式執行的檔案：pipeline_run 的各階段＋weekly_sync 直接呼叫的。"""
    names = set(re.findall(r'"crash_trend"\s*/\s*"(\w+)\.py"', (ROOT / "crash_trend" / "pipeline_run.py").read_text(encoding="utf-8")))
    names |= set(re.findall(r'crash_trend/(\w+)\.py', (ROOT / "scripts" / "weekly_sync.sh").read_text(encoding="utf-8")))
    return sorted(names)


def run_like_container(args: list[str], extra_path: list[str]) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = os.pathsep.join([SITE_PACKAGES, *extra_path])
    # -S：不處理 site 與 .pth（排除 editable install）。
    # 執行腳本時保留「腳本所在目錄在 sys.path[0]」——容器裡就是這樣跑，腳本靠它 import config；
    # 只有 -c 才加 -P，否則 cwd（repo 根目錄）會被放進 sys.path，等於偷偷讓 crash_trend 可被 import。
    flags = ["-S", "-P"] if args[:1] == ["-c"] else ["-S"]
    return subprocess.run([sys.executable, *flags, *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


class TestScheduledStagesImportInContainer(unittest.TestCase):
    def test_dockerfile_declares_pythonpath(self):
        self.assertIn(str(ROOT), dockerfile_pythonpath(), "Dockerfile 必須讓 /app 在 PYTHONPATH 上")

    def test_stage_list_is_discovered(self):
        """防呆：清單是從原始碼解析的，解析壞掉時下面的測試會變成空轉。"""
        stages = scheduled_stage_scripts()
        for expected in ("fetch_bigquery", "normalize", "pipeline_run", "post_report", "pm_brief"):
            self.assertIn(expected, stages)

    def test_every_scheduled_stage_starts(self):
        path = dockerfile_pythonpath()
        for stage in scheduled_stage_scripts():
            with self.subTest(stage=stage):
                r = run_like_container([f"crash_trend/{stage}.py", "--help"], path)
                self.assertEqual(r.returncode, 0, f"{stage} 在容器環境無法啟動：{r.stderr[-400:]}")

    def test_ai_router_resolves_at_runtime(self):
        """有些匯入在執行中才發生（AI 分析取 router 時），--help 測不到。"""
        code = 'import sys; sys.path.insert(0, "crash_trend"); import ai_provider; ai_provider.get_ai_router()'
        r = run_like_container(["-c", code], dockerfile_pythonpath())
        self.assertEqual(r.returncode, 0, r.stderr[-400:])

    def test_simulation_really_excludes_the_editable_install(self):
        """對照組：不給 PYTHONPATH 時必須重現失敗；否則這組測試沒有在模擬容器，全綠也不代表什麼。"""
        r = run_like_container(["crash_trend/fetch_stacktraces.py", "--help"], [])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("crash_trend", r.stderr)


if __name__ == "__main__":
    unittest.main()
