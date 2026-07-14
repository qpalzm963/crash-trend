"""apps.yaml 讀取與共用工具。所有腳本以 --app <name> 指定目標 app。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
APPS_YAML = ROOT / "apps.yaml"


def load_config() -> dict:
    with open(APPS_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_app(name: str) -> dict:
    """回傳 app 設定。app 不存在時直接退出。"""
    cfg = load_config()
    apps = cfg.get("apps") or {}
    if name not in apps:
        sys.exit(f"[錯誤] apps.yaml 沒有 app「{name}」；現有：{', '.join(apps)}")
    return apps[name]


def out_dir(app_name: str) -> Path:
    d = ROOT / "out" / app_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ 寫入 {path.relative_to(ROOT)}")


def load_prev_month(app_name: str, month: str) -> dict | None:
    """載入 reports/data/<app>/ 中早於 month 的最近一個月快照（無則 None）。
    不能假設本月檔已存在（normalize 首次跑該月時就還沒有），故用 < month 過濾。"""
    data_dir = ROOT / "reports" / "data" / app_name
    prevs = sorted(p.stem for p in data_dir.glob("*.json") if p.stem < month)
    if not prevs:
        return None
    return json.loads((data_dir / f"{prevs[-1]}.json").read_text(encoding="utf-8"))


def app_argparser(desc: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=desc)
    p.add_argument("--app", required=True, help="apps.yaml 中的 app 名稱")
    p.add_argument("--days", type=int, default=90, help="回溯天數（預設 90）")
    return p
