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
    if not APPS_YAML.exists():
        example_yaml = ROOT / "apps.example.yaml"
        if example_yaml.exists():
            with open(example_yaml, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}
    with open(APPS_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


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


def get_data_sources(app_cfg: dict) -> dict:
    """Extracts and normalizes data_sources configuration for an app."""
    ds = dict(app_cfg.get("data_sources") or {})

    # Check shorthand flags
    if "sessions" not in ds:
        if "sessions" in app_cfg:
            ds["sessions"] = app_cfg["sessions"]
        elif "sessions_enabled" in app_cfg:
            ds["sessions"] = app_cfg["sessions_enabled"]
        elif "sessions_dataset" in app_cfg and app_cfg["sessions_dataset"] is not None:
            ds["sessions"] = bool(app_cfg["sessions_dataset"])
        else:
            ds["sessions"] = False

    if "crashlytics_bigquery" not in ds:
        if "crashlytics_bigquery" in app_cfg:
            ds["crashlytics_bigquery"] = app_cfg["crashlytics_bigquery"]
        else:
            ds["crashlytics_bigquery"] = True

    if "mcp" not in ds:
        if "mcp" in app_cfg:
            ds["mcp"] = app_cfg["mcp"]
        else:
            ds["mcp"] = "optional"

    return ds


def is_sessions_enabled(app_cfg: dict) -> bool:
    """Returns True only if Sessions data source is explicitly enabled for the app."""
    ds = get_data_sources(app_cfg)
    val = ds.get("sessions")
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes", "enabled", "on")
    return bool(val)

