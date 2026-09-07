"""Watermark progression and ISO datetime comparison helpers (Issue #29, #53).

Provides:
- advance_watermark: Monotonic progression of ISO-8601 timestamps with UTC awareness.
- is_ts_le: Timezone-aware ISO-8601 boundary comparison (ts <= watermark).
"""

from __future__ import annotations

import datetime as dt
from typing import Optional


def advance_watermark(current_watermark: Optional[str], candidate_ts: Optional[str]) -> Optional[str]:
    """Advances catalog watermark if candidate_ts is newer than current watermark."""
    if not candidate_ts:
        return current_watermark
    ts_str = str(candidate_ts).strip()
    if not ts_str:
        return current_watermark
    if not current_watermark:
        return ts_str
    try:
        c_clean = ts_str.replace("Z", "+00:00")
        w_clean = current_watermark.replace("Z", "+00:00")
        dt_cand = dt.datetime.fromisoformat(c_clean)
        dt_curr = dt.datetime.fromisoformat(w_clean)
        if dt_cand.tzinfo is None:
            dt_cand = dt_cand.replace(tzinfo=dt.timezone.utc)
        if dt_curr.tzinfo is None:
            dt_curr = dt_curr.replace(tzinfo=dt.timezone.utc)
        if dt_cand > dt_curr:
            return ts_str
        return current_watermark
    except Exception:
        if ts_str > current_watermark:
            return ts_str
        return current_watermark


def is_ts_le(ts: Optional[str], watermark: Optional[str]) -> bool:
    """Returns True if ts <= watermark (comparing ISO datetimes with timezone awareness)."""
    if not ts or not watermark:
        return False
    try:
        t_clean = str(ts).strip().replace("Z", "+00:00")
        w_clean = str(watermark).strip().replace("Z", "+00:00")
        dt_t = dt.datetime.fromisoformat(t_clean)
        dt_w = dt.datetime.fromisoformat(w_clean)
        if dt_t.tzinfo is None:
            dt_t = dt_t.replace(tzinfo=dt.timezone.utc)
        if dt_w.tzinfo is None:
            dt_w = dt_w.replace(tzinfo=dt.timezone.utc)
        return dt_t <= dt_w
    except Exception:
        return str(ts).strip() <= str(watermark).strip()


_is_ts_le = is_ts_le
