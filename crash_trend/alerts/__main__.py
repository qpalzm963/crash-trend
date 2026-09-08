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

from crash_trend.alerts.dispatcher import dispatch_alerts_for_app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="發送版本品質退化閘門警告通知至 Google Chat (Google Chat Quality Alerts Delivery)"
    )
    parser.add_argument("--app", required=True, help="apps.yaml 中的 App 名稱")
    parser.add_argument("--dry-run", action="store_true", help="產出實際通知 payload 預覽，不實際發送 HTTP request 且不更新 sent 狀態")
    parser.add_argument("--force", action="store_true", help="忽略 cooldown 與去重機制，強制送出通知")
    parser.add_argument("--gate-artifact", type=Path, default=None, help="自訂 release_gate.json 檔案路徑")
    parser.add_argument("--quiet", action="store_true", help="減少詳細輸出")
    args = parser.parse_args()

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
