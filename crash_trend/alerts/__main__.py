"""CLI entrypoint for Google Chat Quality Alerts Delivery (Issue #59).

Usage:
    python -m crash_trend.alerts --app <app> [--dry-run] [--force] [--gate-artifact <path>] [--quiet]

Exit codes:
    0: Succeeded (alerts sent or cleanly suppressed)
    1: Runtime error or alert delivery failed
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure ROOT is in sys.path
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json

from crash_trend.alerts.dispatcher import dispatch_alerts_for_app
from crash_trend.alerts.observability import get_recent_alert_deliveries


def format_history_table(app_id: str, items: list[dict]) -> str:
    lines = [
        f"[Alert Delivery History] App: {app_id} (Records: {len(items)})",
        "-" * 100,
        f"{'Attempted At':<22} {'Platform':<10} {'Version':<12} {'Status':<12} {'Gate':<8} {'HTTP':<6} {'Details'}",
        "-" * 100,
    ]
    if not items:
        lines.append("  (No delivery records found)")
    for it in items:
        status_tag = it["status"]
        if it.get("is_recovery"):
            status_tag += " ↗"
        if it.get("dry_run"):
            status_tag += " (dry-run)"
        http_str = str(it["http_status"]) if it["http_status"] is not None else "-"
        detail = it.get("suppression_reason") or it.get("error_message") or "-"
        lines.append(
            f"{it['attempted_at']:<22} {it['platform']:<10} {it['version']:<12} "
            f"{status_tag:<12} {it['gate_status']:<8} {http_str:<6} {detail}"
        )
    lines.append("-" * 100)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="發送或查詢版本品質退化閘門警告通知至 Google Chat (Google Chat Quality Alerts Delivery & Audit)"
    )
    parser.add_argument("--app", required=True, help="apps.yaml 中的 App 名稱")
    parser.add_argument("--history", action="store_true", help="查詢發送歷史審計紀錄 (唯讀)")
    parser.add_argument("--platform", default=None, help="篩選平台 (例如 ios, android)")
    parser.add_argument("--version", default=None, help="篩選版本 (例如 1.2.3)")
    parser.add_argument("--limit", type=int, default=50, help="最多回傳幾筆紀錄 (預設 50)")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式輸出歷史紀錄")
    parser.add_argument("--db-path", type=Path, default=None, help="自訂 alert_delivery.sqlite3 路徑")
    parser.add_argument("--dry-run", action="store_true", help="產出實際通知 payload 預覽，不實際發送 HTTP request 且不更新 sent 狀態")
    parser.add_argument("--force", action="store_true", help="忽略 cooldown 與去重機制，強制送出通知")
    parser.add_argument("--gate-artifact", type=Path, default=None, help="自訂 release_gate.json 檔案路徑")
    parser.add_argument("--quiet", action="store_true", help="減少詳細輸出")
    args = parser.parse_args()

    if args.history:
        try:
            items = get_recent_alert_deliveries(
                app_id=args.app,
                platform=args.platform,
                version=args.version,
                limit=args.limit,
                custom_path=args.db_path,
            )
            dict_items = [it.to_dict() for it in items]
            if args.json:
                print(json.dumps(dict_items, ensure_ascii=False, indent=2))
            else:
                print(format_history_table(args.app, dict_items))
            sys.exit(0)
        except Exception as e:
            print(f"[錯誤] 查詢 Alert Delivery 歷史失敗：{e}", file=sys.stderr)
            sys.exit(1)

    try:
        summary = dispatch_alerts_for_app(
            app_name=args.app,
            dry_run=args.dry_run,
            force=args.force,
            gate_path=args.gate_artifact,
            verbose=not args.quiet,
        )
    except Exception as e:
        print(f"[錯誤] Quality Alerts Dispatcher 執行失敗：{e}", file=sys.stderr)
        sys.exit(1)

    if summary.total_failed > 0:
        print(
            f"[注意] App「{args.app}」有 {summary.total_failed} 項平台通知發送失敗",
            file=sys.stderr,
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
