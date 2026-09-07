"""產生自包含靜態儀表板 dashboard.html (Dashboard V2)。

Compatibility wrapper and CLI entrypoint (Issue #53).
All underlying rendering and presentation logic is modularized in `crash_trend.dashboard`.
"""

from __future__ import annotations

from pathlib import Path
import sys

from typing import Any, Optional, Union

# Ensure repository root is in sys.path when executed directly
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import crash_trend.dashboard as _dashboard
from crash_trend.dashboard import (
    DEFAULT_OUT_HTML,
    HTML_TEMPLATE,
    VENDOR_JS,
    assemble_html_template,
    get_vendor_chartjs,
)


def assemble_bundle_from_apps(cfg: Optional[dict] = None, root_dir: Optional[Union[str, Path]] = None) -> Optional[dict]:
    """Compatibility wrapper delegating to crash_trend.dashboard.assemble_bundle_from_apps."""
    eff_root = Path(root_dir) if root_dir is not None else ROOT
    return _dashboard.assemble_bundle_from_apps(cfg=cfg, root_dir=eff_root)


def collect_data(
    data_path: Optional[Union[str, Path]] = None,
    root_dir: Optional[Union[str, Path]] = None,
) -> dict:
    """Compatibility wrapper delegating to crash_trend.dashboard.collect_data."""
    eff_root = Path(root_dir) if root_dir is not None else ROOT
    return _dashboard.collect_data(data_path=data_path, root_dir=eff_root)


def build_html(
    data: Union[dict, Any],
    vendor_chartjs_path: Optional[Union[str, Path]] = None,
) -> str:
    """Compatibility wrapper delegating to crash_trend.dashboard.build_html."""
    eff_vendor = Path(vendor_chartjs_path) if vendor_chartjs_path is not None else ROOT / "vendor" / "chart.umd.min.js"
    return _dashboard.build_html(data, vendor_chartjs_path=eff_vendor)


def generate_dashboard(
    data: Optional[Union[dict, Any]] = None,
    output_path: Optional[Union[str, Path]] = None,
    data_path: Optional[Union[str, Path]] = None,
    root_dir: Optional[Union[str, Path]] = None,
) -> Path:
    """Compatibility wrapper delegating to crash_trend.dashboard.generate_dashboard."""
    eff_root = Path(root_dir) if root_dir is not None else ROOT
    return _dashboard.generate_dashboard(
        data=data,
        output_path=output_path,
        data_path=data_path,
        root_dir=eff_root,
    )


def main(argv: Optional[list[str]] = None) -> None:
    """Compatibility CLI entrypoint delegating to crash_trend.dashboard.main."""
    _dashboard.main(argv=argv, root_dir=ROOT)


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
