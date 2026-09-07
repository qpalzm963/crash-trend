"""Dashboard V2 generation and rendering package (Issue #53).

Provides self-contained static HTML dashboard generation from DashboardV2Bundle data.
"""

from __future__ import annotations

from crash_trend.dashboard.assets import VENDOR_JS, get_vendor_chartjs
from crash_trend.dashboard.renderer import (
    DEFAULT_OUT_HTML,
    HTML_TEMPLATE,
    ROOT,
    assemble_bundle_from_apps,
    assemble_html_template,
    build_html,
    collect_data,
    generate_dashboard,
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
