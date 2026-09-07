"""Issue Historical Catalog and Deterministic Lifecycle Engine (Issue #29, #53).

Compatibility wrapper and CLI entrypoint.
All underlying implementations are modularized in `crash_trend.catalog`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure repository root is in sys.path when executed directly
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crash_trend.catalog.bootstrap import (
    _has_verifiable_installation_authority,
    bootstrap_catalog_from_disk,
    should_trigger_catalog_bootstrap,
)
from crash_trend.catalog.comparison import compute_previous_release_comparison
from crash_trend.catalog.historical import (
    IssueHistoricalCatalog,
    enrich_app_data_with_lifecycle,
)
from crash_trend.catalog.issue_lifecycle import (
    detect_issue_lifecycle,
    is_version_sample_sufficient,
)
from crash_trend.catalog.release_catalog import (
    build_release_catalog,
    calculate_version_status,
    get_latest_app_version,
)
from crash_trend.catalog.watermark import advance_watermark, is_ts_le

__all__ = [
    "IssueHistoricalCatalog",
    "detect_issue_lifecycle",
    "is_version_sample_sufficient",
    "get_latest_app_version",
    "enrich_app_data_with_lifecycle",
    "bootstrap_catalog_from_disk",
    "_has_verifiable_installation_authority",
    "should_trigger_catalog_bootstrap",
    "calculate_version_status",
    "build_release_catalog",
    "compute_previous_release_comparison",
    "advance_watermark",
    "is_ts_le",
    "main",
]

from crash_trend.catalog.bootstrap import main

if __name__ == "__main__":
    main()

