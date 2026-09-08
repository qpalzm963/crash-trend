"""CLI entrypoint for Release Gate Evaluation History and Trend Query (Issue #61).

Usage:
    python -m crash_trend.release_gate_history --app <app> [--platform <pf>] [--version <ver>] [--trend] [--json]
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure ROOT is in sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crash_trend.gate.history import main  # noqa: E402

if __name__ == "__main__":
    main()
