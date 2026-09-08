"""CLI entrypoint and standalone runner for Release Regression Gate (Issue #57).

Usage:
    python3 -m crash_trend.release_gate --app <app_name> [--fail-on-regression]
    python3 crash_trend/release_gate.py --app <app_name> [--out <path>]

Exit codes:
    0: Gate passed / warn / baseline / insufficient_data (or --fail-on-regression not specified)
    1: Evaluator execution crash or runtime error
    2: Quality regression FAIL when --fail-on-regression is enabled
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Ensure parent path is in sys.path for direct script execution
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crash_trend.config import get_app, load_config, out_dir
from crash_trend.gate import (
    ReleaseGateArtifact,
    evaluate_app_release_gate,
    load_gate_policy,
    load_gate_policy_from_file,
    save_release_gate_artifact,
)


def load_app_release_catalog(app_name: str) -> list[dict[str, Any]]:
    """Loads release catalog items from out/<app>/dashboard_v2.json or reports."""
    # 1. Check out/<app>/dashboard_v2.json
    out_v2 = out_dir(app_name) / "dashboard_v2.json"
    if out_v2.is_file():
        try:
            data = json.loads(out_v2.read_text(encoding="utf-8"))
            cat = data.get("release_catalog")
            if isinstance(cat, list) and cat:
                return cat
            # Check inside periods
            periods = data.get("periods") or {}
            for snap in periods.values():
                if isinstance(snap, dict) and snap.get("release_catalog"):
                    return snap["release_catalog"]
        except Exception:
            pass

    # 2. Check root out/dashboard_v2.json bundle
    bundle_path = ROOT / "out" / "dashboard_v2.json"
    if bundle_path.is_file():
        try:
            bundle_data = json.loads(bundle_path.read_text(encoding="utf-8"))
            app_data = bundle_data.get("apps", {}).get(app_name, {})
            cat = app_data.get("release_catalog")
            if isinstance(cat, list) and cat:
                return cat
        except Exception:
            pass

    # 3. Fallback: try loading historical catalog to build catalog
    cat_path = out_dir(app_name) / "historical_catalog.json"
    if cat_path.is_file():
        try:
            from crash_trend.catalog.historical import IssueHistoricalCatalog
            from crash_trend.catalog.release_catalog import build_release_catalog

            with IssueHistoricalCatalog(catalog_path=cat_path, app_id=app_name) as cat:
                cat.load()
                cat_items = build_release_catalog(cat)
                if cat_items:
                    return cat_items  # type: ignore[return-value]
        except Exception:
            pass

    raise FileNotFoundError(
        f"找不到 App「{app_name}」之 release_catalog 產物或歷史目錄檔案（請先執行 pipeline 產出資料）"
    )


def run_release_gate_for_app(
    app_name: str,
    out_path: Path | None = None,
    policy_path: Path | None = None,
    verbose: bool = True,
) -> ReleaseGateArtifact:
    """Runs release gate evaluation for a given app and persists artifact."""
    cfg = load_config()
    app_cfg = get_app(app_name, cfg)

    if policy_path is not None:
        policy = load_gate_policy_from_file(policy_path)
    else:
        policy = load_gate_policy(app_cfg)

    catalog_items: list[dict[str, Any]] = []
    if policy.enabled:
        catalog_items = load_app_release_catalog(app_name)
        if not catalog_items:
            raise FileNotFoundError(
                f"找不到 App「{app_name}」之 release_catalog 產物或歷史目錄檔案（請先執行 pipeline 產出資料）"
            )
    else:
        try:
            catalog_items = load_app_release_catalog(app_name)
        except Exception:
            catalog_items = []
    target_platforms = app_cfg.get("platforms")

    artifact = evaluate_app_release_gate(
        app_id=app_name,
        catalog_items=catalog_items,
        policy=policy,
        target_platforms=target_platforms,
    )

    dest = out_path or (out_dir(app_name) / "release_gate.json")
    save_release_gate_artifact(dest, artifact)

    if verbose:
        if not policy.enabled:
            print(f"\n================ [Release Regression Gate: {app_name}] ================")
            print("  Release gate is DISABLED (enabled: false). Generated non-blocking artifact.")
            print(f"  Artifact Path:   {dest}")
            print(f"  Summary:         {artifact['alert_summary']}")
            print("=======================================================================\n")
        else:
            status_disp = artifact["overall_status"].upper()
            status_color = (
                "\033[32m" if status_disp == "PASS" else
                ("\033[33m" if status_disp == "WARN" else
                 ("\033[31m" if status_disp == "FAIL" else "\033[36m"))
            )
            reset_color = "\033[0m"

            print(f"\n================ [Release Regression Gate: {app_name}] ================")
            print(f"  Overall Status:  {status_color}{status_disp}{reset_color}")
            print(f"  Alert Severity:  {artifact['alert_severity'].upper()} (should_alert: {artifact['should_alert']})")
            print(f"  Artifact Path:   {dest}")
            print(f"  Summary:         {artifact['alert_summary']}")

            for pf, pf_res in artifact["platforms"].items():
                pf_st = pf_res["gate_status"].upper()
                print(f"  - Platform [{pf.upper()}]: target={pf_res['target_version']} (prev={pf_res['previous_version'] or 'None'}) -> [{pf_st}]")
                for rule in pf_res["rule_results"]:
                    r_st = rule["status"].upper()
                    r_icon = "✓" if r_st == "PASS" else ("⚠" if r_st == "WARN" else ("✗" if r_st == "FAIL" else "—"))
                    print(f"      {r_icon} {rule['rule_name']:<24} [{r_st:<4}] {rule['reason']}")
            print("=======================================================================\n")

    return artifact


def main() -> None:
    parser = argparse.ArgumentParser(description="評估版本退化閘門 (Release Regression Gate) 並產出 release_gate.json")
    parser.add_argument("--app", required=True, help="apps.yaml 中的 app 名稱")
    parser.add_argument("--fail-on-regression", action="store_true", help="若品質閘門判定為 FAIL，則以 exit code 2 結束")
    parser.add_argument("--policy", type=Path, default=None, help="自訂 GatePolicy 檔案路徑 (JSON 或 YAML)")
    parser.add_argument("--out", type=Path, default=None, help="自訂 release_gate.json 輸出路徑")
    parser.add_argument("--quiet", action="store_true", help="減少詳細輸出")
    args = parser.parse_args()

    try:
        artifact = run_release_gate_for_app(
            app_name=args.app,
            out_path=args.out,
            policy_path=args.policy,
            verbose=not args.quiet,
        )
    except Exception as e:
        print(f"[錯誤] Release Gate 執行異常：{e}", file=sys.stderr)
        sys.exit(1)

    if args.fail_on_regression and artifact["overall_status"] == "fail":
        print(f"[GATE REJECTED] App「{args.app}」版本品質退化閘門 FAIL，CI Quality Gate 阻擋！", file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
