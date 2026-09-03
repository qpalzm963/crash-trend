"""Provider-neutral Crash Intelligence analysis module (Dashboard V2.3 - Issue #26).

Re-exports all symbols from analyze_gemini for backward and forward compatibility.
"""

from __future__ import annotations

from crash_trend.analyze_gemini import *
from crash_trend.analyze_gemini import (
    AIIssueAnalysis,
    AISummary,
    IssueSummary,
    PriorityBreakdown,
    PriorityInfo,
    RecommendedAction,
    build_ai_prompt,
    calculate_priority,
    call_gemini,
    enrich_app_data_with_priority_and_ai,
    generate_disabled_ai_summary,
    generate_disabled_issue_analysis,
    generate_error_ai_summary,
    get_latest_app_version,
    iso_utc_now,
    main,
    map_score_to_level,
    parse_ai_response,
    parse_gemini_response,
    render_md,
    resolve_api_key,
    score_issues,
    source_snippet,
)

if __name__ == "__main__":
    main()
