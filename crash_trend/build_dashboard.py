"""產生自包含靜態儀表板 dashboard.html (Dashboard V2)。

Compatibility wrapper and CLI entrypoint (Issue #53).
All underlying rendering and presentation logic is modularized in `crash_trend.dashboard`.
"""

from __future__ import annotations

from pathlib import Path
import sys

# Ensure repository root is in sys.path when executed directly
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crash_trend.dashboard import (
    DEFAULT_OUT_HTML,
    HTML_TEMPLATE,
    VENDOR_JS,
    assemble_bundle_from_apps,
    assemble_html_template,
    build_html,
    collect_data,
    generate_dashboard,
    get_vendor_chartjs,
    main,
)

__all__ = [
    "ROOT",
    "VENDOR_JS",
    "DEFAULT_OUT_HTML",
    "HTML_TEMPLATE",
    "get_vendor_chartjs",
    "assemble_bundle_from_apps",
    "assemble_html_template",
    "collect_data",
    "build_html",
    "generate_dashboard",
    "main",
]

if __name__ == "__main__":
    main()
