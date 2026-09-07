"""Crash Trend Catalog package (Issue #29, #47, #49, #53).

Provides deterministic lifecycle detection, release catalog assembly,
monotonic watermark tracking, and historical catalog persistence.
"""

from __future__ import annotations

from crash_trend.catalog.bootstrap import (
    _has_verifiable_installation_authority,
    bootstrap_catalog_from_disk,
    main,
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

