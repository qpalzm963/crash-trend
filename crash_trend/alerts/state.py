"""SQLite-backed Delivery Audit and Deduplication Store (Issue #59).

Persists alert delivery history, attempts, suppression reasons, and deduplication states.
Enforces zero-secret storage (scrubs webhook URLs, tokens, keys) and zero raw UUIDs.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from crash_trend.alerts.models import DeliveryRecord
from crash_trend.sqlite_store import connection as sqlite_connection


class AlertStoreError(Exception):
    """Raised when an operation on the alert delivery store fails."""
    pass


def sanitize_audit_text(text: str | None) -> str | None:
    """Removes sensitive webhook credentials, tokens, authorization headers,
    and raw user/installation UUID identifiers from audit text.
    """
    if text is None:
        return None
    s = str(text)
    # 1. Scrub Google Chat and generic webhook URLs
    s = re.sub(r"https?://chat\.googleapis\.com/[^\s'\"<>]+", "https://chat.googleapis.com/...<redacted>", s)
    s = re.sub(r"https?://(?:hooks\.slack\.com|discord\.com/api/webhooks)[^\s'\"<>]+", "<redacted-webhook-url>", s)
    # 2. Scrub Authorization headers across all schemes (Basic, Bearer, ApiKey, Digest, custom)
    # 2a. Quoted header values in JSON / dict strings: {"Authorization": "..."}
    s = re.sub(r"(?i)['\"]?Authorization['\"]?\s*:\s*['\"][^'\"\r\n]+['\"]", '"Authorization": "<redacted>"', s)
    # 2b. Scheme-based headers: Authorization: <Scheme> <token> (stopping at whitespace / delimiter)
    s = re.sub(r"(?i)\bAuthorization:\s*(?:Bearer|Basic|ApiKey|Token)\s+[^\s,;\'\"<>]+", "Authorization: <redacted>", s)
    # 2c. Digest authorization header (with key=value parameters)
    s = re.sub(r"(?i)\bAuthorization:\s*Digest\b(?:[^\r\n;\'\"<>]|\"[^\"]*\")*", "Authorization: <redacted>", s)
    # 2d. Multi-line headers: if Authorization is on its own line in a header block (i.e. followed by newline)
    s = re.sub(r"(?im)^\s*Authorization:\s*(?!\s*<redacted>)[^\r\n]+$", "Authorization: <redacted>", s)
    # 2e. Generic unredacted Authorization header (scrub scheme and credential or raw token)
    s = re.sub(r"(?i)\bAuthorization:\s*(?!\s*<redacted>)[^\s,;\'\"<>]+(?:\s+[^\s,;\'\"<>]+)?", "Authorization: <redacted>", s)
    # 2f. Standalone Bearer token strings
    s = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9_\-\.~+/=]+", "Bearer <redacted>", s)
    # 3. Scrub standard token/key query params or assignments (key=..., token=..., secret=...)
    s = re.sub(r"([?&](?:key|token|access_token|secret|api_key|auth)=)[^&\s'\"]+", r"\1<redacted>", s)
    s = re.sub(r"(?i)\b(key|token|secret|access_token|api_key|auth)=([^\s'\",;&]+)", r"\1=<redacted>", s)
    # 4. Scrub raw UUIDs (e.g. user_id, installation_id)
    s = re.sub(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "<redacted-id>", s)
    # 5. Scrub explicit user_id/installation_id assignments
    s = re.sub(r"(?i)\b(user_id|installation_id|client_id|device_id)=([^\s'\",;&]+)", r"\1=<redacted-id>", s)
    return s


class AlertDeliveryStore:
    """Manages SQLite storage for delivery audit logs and deduplication state."""

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        app_id: str | None = None,
        read_only: bool = False,
    ) -> None:
        self.app_id = str(app_id).strip() if app_id else None
        self.is_memory = str(db_path) == ":memory:"
        self.db_path = Path(db_path) if not self.is_memory else ":memory:"
        self.read_only = read_only
        self._conn: sqlite3.Connection | None = None

        if not self.is_memory and isinstance(self.db_path, Path) and not self.read_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if self.is_memory:
                self._conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
            if not self.read_only:
                self._init_db()
        except sqlite3.Error as e:
            raise AlertStoreError(f"Failed to initialize SQLite Alert Store at '{db_path}': {e}") from e

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if self.is_memory:
            if self._conn is None:
                self._conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
            yield self._conn
        else:
            # 連線設定與生命週期（commit / rollback / close、pragma、
            # check_same_thread）唯一定義於 sqlite_store。
            with sqlite_connection(self.db_path, read_only=self.read_only) as conn:
                yield conn

    def _table_exists(self, conn: sqlite3.Connection, table_name: str = "alert_deliveries") -> bool:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return cur.fetchone() is not None

    def _init_db(self) -> None:
        if self.read_only:
            return
        with self._connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alert_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    version TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    alert_fingerprint TEXT NOT NULL,
                    gate_status TEXT NOT NULL,
                    attempted_at TEXT NOT NULL,
                    delivered_at TEXT,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    http_status INTEGER,
                    error_code TEXT,
                    error_message TEXT,
                    thread_key TEXT,
                    message_name TEXT,
                    reasons_json TEXT,
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    is_recovery INTEGER NOT NULL DEFAULT 0
                );
            """)
            # Migration check: ensure is_recovery column exists on older databases
            cur = conn.execute("PRAGMA table_info(alert_deliveries);")
            col_names = [col[1] for col in cur.fetchall()]
            if "is_recovery" not in col_names:
                conn.execute("ALTER TABLE alert_deliveries ADD COLUMN is_recovery INTEGER NOT NULL DEFAULT 0;")

            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_alert_deliveries_lookup
                    ON alert_deliveries (app_id, platform, version, id DESC);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_alert_deliveries_fingerprint
                    ON alert_deliveries (alert_fingerprint, status);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_alert_deliveries_health
                    ON alert_deliveries (app_id, dry_run, status, id DESC);
            """)

    def record_attempt(
        self,
        app_id: str,
        platform: str,
        version: str,
        provider: str,
        alert_fingerprint: str,
        gate_status: str,
        attempted_at: str | None = None,
        reasons: list[str] | None = None,
        thread_key: str | None = None,
        dry_run: bool = False,
        is_recovery: bool = False,
    ) -> int:
        """Records an initial pending delivery attempt."""
        if self.read_only:
            raise AlertStoreError("Cannot modify alert deliveries on a read-only store.")
        att_time = attempted_at or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        reasons_payload = json.dumps(reasons or [], ensure_ascii=False)
        clean_thread_key = sanitize_audit_text(thread_key)

        with self._connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO alert_deliveries (
                    app_id, platform, version, provider, alert_fingerprint,
                    gate_status, attempted_at, status, attempt_count,
                    thread_key, reasons_json, dry_run, is_recovery
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?, ?, ?)
                """,
                (
                    app_id.strip(),
                    platform.strip().lower(),
                    version.strip(),
                    provider.strip(),
                    alert_fingerprint.strip(),
                    gate_status.strip().lower(),
                    att_time,
                    clean_thread_key,
                    reasons_payload,
                    1 if dry_run else 0,
                    1 if is_recovery else 0,
                ),
            )
            return int(cur.lastrowid or 0)

    def update_result(
        self,
        record_id: int,
        status: str,
        delivered_at: str | None = None,
        attempt_count: int = 1,
        http_status: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        message_name: str | None = None,
    ) -> None:
        """Updates the outcome of a delivery attempt."""
        if self.read_only:
            raise AlertStoreError("Cannot modify alert deliveries on a read-only store.")
        clean_msg = sanitize_audit_text(error_message)
        clean_name = sanitize_audit_text(message_name)

        with self._connection() as conn:
            conn.execute(
                """
                UPDATE alert_deliveries
                SET status = ?,
                    delivered_at = ?,
                    attempt_count = ?,
                    http_status = ?,
                    error_code = ?,
                    error_message = ?,
                    message_name = ?
                WHERE id = ?
                """,
                (
                    status.strip().lower(),
                    delivered_at,
                    attempt_count,
                    http_status,
                    error_code,
                    clean_msg,
                    clean_name,
                    record_id,
                ),
            )

    def record_suppressed(
        self,
        app_id: str,
        platform: str,
        version: str,
        provider: str,
        alert_fingerprint: str,
        gate_status: str,
        reason_text: str,
        attempted_at: str | None = None,
        reasons: list[str] | None = None,
        thread_key: str | None = None,
        dry_run: bool = False,
        is_recovery: bool = False,
    ) -> int:
        """Records an alert evaluation that was suppressed by dedupe, cooldown, or policy."""
        if self.read_only:
            raise AlertStoreError("Cannot modify alert deliveries on a read-only store.")
        att_time = attempted_at or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        reasons_payload = json.dumps(reasons or [], ensure_ascii=False)
        clean_thread_key = sanitize_audit_text(thread_key)
        clean_reason = sanitize_audit_text(reason_text)

        with self._connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO alert_deliveries (
                    app_id, platform, version, provider, alert_fingerprint,
                    gate_status, attempted_at, status, attempt_count,
                    error_code, error_message, thread_key, reasons_json, dry_run, is_recovery
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'suppressed', 0, 'SUPPRESSED', ?, ?, ?, ?, ?)
                """,
                (
                    app_id.strip(),
                    platform.strip().lower(),
                    version.strip(),
                    provider.strip(),
                    alert_fingerprint.strip(),
                    gate_status.strip().lower(),
                    att_time,
                    clean_reason,
                    clean_thread_key,
                    reasons_payload,
                    1 if dry_run else 0,
                    1 if is_recovery else 0,
                ),
            )
            return int(cur.lastrowid or 0)

    def get_last_sent_delivery(
        self,
        app_id: str,
        platform: str,
        version: str,
    ) -> DeliveryRecord | None:
        """Retrieves the most recent successfully sent delivery for this app, platform, and version.

        Crucial: Ignores dry_run records so dry runs never affect real deduplication state.
        """
        with self._connection() as conn:
            if not self._table_exists(conn):
                return None
            cur = conn.execute(
                """
                SELECT * FROM alert_deliveries
                WHERE app_id = ?
                  AND platform = ?
                  AND version = ?
                  AND status = 'sent'
                  AND dry_run = 0
                ORDER BY id DESC
                LIMIT 1
                """,
                (app_id.strip(), platform.strip().lower(), version.strip()),
            )
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_record(row)

    def get_history(
        self,
        app_id: str,
        platform: str | None = None,
        version: str | None = None,
        limit: int = 50,
    ) -> list[DeliveryRecord]:
        """Returns recent audit delivery records with optional platform and version filtering."""
        with self._connection() as conn:
            if not self._table_exists(conn):
                return []
            query = "SELECT * FROM alert_deliveries WHERE app_id = ?"
            params: list[Any] = [app_id.strip()]
            if platform:
                query += " AND platform = ?"
                params.append(platform.strip().lower())
            if version:
                query += " AND version = ?"
                params.append(version.strip())
            query += " ORDER BY attempted_at DESC, id DESC LIMIT ?"
            params.append(max(1, limit))

            cur = conn.execute(query, params)
            return [self._row_to_record(row) for row in cur.fetchall()]

    def get_deliveries_in_window(
        self,
        app_id: str,
        start_iso: str,
        end_iso: str,
    ) -> list[DeliveryRecord]:
        """Returns audit records attempted within a specific ISO 8601 UTC timestamp window."""
        with self._connection() as conn:
            if not self._table_exists(conn):
                return []
            query = """
                SELECT * FROM alert_deliveries
                WHERE app_id = ?
                  AND attempted_at >= ?
                  AND attempted_at <= ?
                ORDER BY attempted_at DESC, id DESC
            """
            cur = conn.execute(query, (app_id.strip(), start_iso.strip(), end_iso.strip()))
            return [self._row_to_record(row) for row in cur.fetchall()]

    def get_health_counts_24h(self, app_id: str, cutoff_iso: str) -> dict[str, int]:
        """Returns 24h status counts ('sent', 'failed', 'suppressed') excluding dry runs."""
        counts = {"sent": 0, "failed": 0, "suppressed": 0}
        with self._connection() as conn:
            if not self._table_exists(conn):
                return counts
            query = """
                SELECT status, COUNT(*) as cnt
                FROM alert_deliveries
                WHERE app_id = ?
                  AND dry_run = 0
                  AND attempted_at >= ?
                GROUP BY status
            """
            cur = conn.execute(query, (app_id.strip(), cutoff_iso.strip()))
            for row in cur.fetchall():
                st = str(row["status"]).strip().lower()
                if st in counts:
                    counts[st] = int(row["cnt"])
        return counts

    def get_latest_delivery_timestamps(self, app_id: str) -> tuple[str | None, str | None]:
        """Returns (latest_success_at, latest_failure_at) for real non-dry-run attempts."""
        with self._connection() as conn:
            if not self._table_exists(conn):
                return None, None
            cur_sent = conn.execute(
                """
                SELECT COALESCE(delivered_at, attempted_at) as ts
                FROM alert_deliveries
                WHERE app_id = ?
                  AND dry_run = 0
                  AND status = 'sent'
                ORDER BY attempted_at DESC, id DESC
                LIMIT 1
                """,
                (app_id.strip(),),
            )
            row_sent = cur_sent.fetchone()
            latest_success = str(row_sent["ts"]) if row_sent and row_sent["ts"] else None

            cur_failed = conn.execute(
                """
                SELECT attempted_at as ts
                FROM alert_deliveries
                WHERE app_id = ?
                  AND dry_run = 0
                  AND status = 'failed'
                ORDER BY attempted_at DESC, id DESC
                LIMIT 1
                """,
                (app_id.strip(),),
            )
            row_failed = cur_failed.fetchone()
            latest_failure = str(row_failed["ts"]) if row_failed and row_failed["ts"] else None

            return latest_success, latest_failure

    def get_latest_real_attempt_status(self, app_id: str) -> str | None:
        """Returns the status ('sent' or 'failed') of the most recent real attempt, or None."""
        with self._connection() as conn:
            if not self._table_exists(conn):
                return None
            cur = conn.execute(
                """
                SELECT status
                FROM alert_deliveries
                WHERE app_id = ?
                  AND dry_run = 0
                  AND status IN ('sent', 'failed')
                ORDER BY attempted_at DESC, id DESC
                LIMIT 1
                """,
                (app_id.strip(),),
            )
            row = cur.fetchone()
            return str(row["status"]).strip().lower() if row else None

    def get_unresolved_failure_count(self, app_id: str) -> int:
        """Computes consecutive failures since the most recent successful sent attempt,
        using authoritative (attempted_at DESC, id DESC) ordering.
        """
        with self._connection() as conn:
            if not self._table_exists(conn):
                return 0

            cur_sent = conn.execute(
                """
                SELECT attempted_at, id
                FROM alert_deliveries
                WHERE app_id = ?
                  AND dry_run = 0
                  AND status = 'sent'
                ORDER BY attempted_at DESC, id DESC
                LIMIT 1
                """,
                (app_id.strip(),),
            )
            row_sent = cur_sent.fetchone()
            if not row_sent:
                cur_fail = conn.execute(
                    """
                    SELECT COUNT(*) as cnt
                    FROM alert_deliveries
                    WHERE app_id = ?
                      AND dry_run = 0
                      AND status = 'failed'
                    """,
                    (app_id.strip(),),
                )
                res = cur_fail.fetchone()
                return int(res["cnt"]) if res else 0

            sent_at = str(row_sent["attempted_at"])
            sent_id = int(row_sent["id"])

            cur_unresolved = conn.execute(
                """
                SELECT COUNT(*) as cnt
                FROM alert_deliveries
                WHERE app_id = ?
                  AND dry_run = 0
                  AND status = 'failed'
                  AND (
                      attempted_at > ?
                      OR (attempted_at = ? AND id > ?)
                  )
                """,
                (app_id.strip(), sent_at, sent_at, sent_id),
            )
            res = cur_unresolved.fetchone()
            return int(res["cnt"]) if res else 0

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DeliveryRecord:
        reasons_raw = row["reasons_json"]
        reasons: list[str] = []
        if reasons_raw:
            try:
                parsed = json.loads(reasons_raw)
                if isinstance(parsed, list):
                    reasons = [str(x) for x in parsed]
            except Exception:
                pass

        keys = row.keys()
        is_recovery = bool(row["is_recovery"]) if "is_recovery" in keys else False

        return DeliveryRecord(
            id=int(row["id"]),
            app_id=str(row["app_id"]),
            platform=str(row["platform"]),
            version=str(row["version"]),
            provider=str(row["provider"]),
            alert_fingerprint=str(row["alert_fingerprint"]),
            gate_status=str(row["gate_status"]),
            attempted_at=str(row["attempted_at"]),
            delivered_at=row["delivered_at"],
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            http_status=row["http_status"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            thread_key=row["thread_key"],
            message_name=row["message_name"],
            reasons=reasons,
            dry_run=bool(row["dry_run"]),
            is_recovery=is_recovery,
        )

    def close(self) -> None:
        if self.is_memory and self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> AlertDeliveryStore:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
