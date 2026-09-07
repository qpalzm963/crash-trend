"""SQLite-backed Catalog Authority Store for exact Lifetime Unique Users deduplication (Issue #49).

Decouples exact installation membership from historical_catalog.json, providing
deterministic, privacy-preserving, and idempotent exact set deduplication.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import logging
import sqlite3
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AuthorityStoreError(Exception):
    """Raised when an operation on the authority store fails."""
    pass


class CatalogAuthorityStore:
    """Manages exact membership of installation identifiers per (app_id, platform, app_version)

    using local SQLite with deterministic hashing to isolate privacy and optimize storage.
    """

    DEFAULT_STATE_VERSION = 1

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        app_id: str | None = None,
    ) -> None:
        self.app_id = str(app_id).strip() if app_id else None
        self.is_memory = str(db_path) == ":memory:"
        self.db_path = Path(db_path) if not self.is_memory else ":memory:"
        self._conn: sqlite3.Connection | None = None
        self._explicit_conn: sqlite3.Connection | None = None

        if not self.is_memory and isinstance(self.db_path, Path):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if self.is_memory:
                self._conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._conn.row_factory = sqlite3.Row
            self._init_db()
        except sqlite3.Error as e:
            raise AuthorityStoreError(f"Failed to initialize SQLite Authority Store at '{db_path}': {e}") from e

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Provides a thread-safe sqlite connection context."""
        if self.is_memory:
            if self._conn is None:
                raise AuthorityStoreError("In-memory SQLite database connection has been closed.")
            yield self._conn
        else:
            if self._explicit_conn is not None:
                yield self._explicit_conn
            else:
                conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                try:
                    yield conn
                finally:
                    conn.close()

    def _init_db(self) -> None:
        """Configures pragmas and creates tables and indexes if not existing."""
        with self._connection() as conn:
            cur = conn.cursor()
            if not self.is_memory:
                cur.execute("PRAGMA journal_mode = WAL;")
                cur.execute("PRAGMA synchronous = NORMAL;")
                cur.execute("PRAGMA busy_timeout = 5000;")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS release_installations (
                    app_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    app_version TEXT NOT NULL,
                    installation_hash TEXT NOT NULL,
                    first_seen TEXT,
                    last_seen TEXT,
                    PRIMARY KEY (app_id, platform, app_version, installation_hash)
                );
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_release_installations_version
                ON release_installations(app_id, platform, app_version);
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS version_authority_status (
                    app_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    app_version TEXT NOT NULL,
                    bootstrap_complete INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT,
                    PRIMARY KEY (app_id, platform, app_version)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS authority_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            # Default state version metadata
            cur.execute(
                """
                INSERT OR IGNORE INTO authority_metadata (key, value)
                VALUES ('state_version', ?)
                """,
                (str(self.DEFAULT_STATE_VERSION),),
            )
            conn.commit()

    def hash_installation(self, raw_id: str | int, app_id: str | None = None) -> str:
        """Computes a deterministic, non-reversible SHA-256 hash for an installation identifier.

        Salts with app_id to prevent rainbow-table identification across apps while
        remaining perfectly stable across process restarts for exact deduplication.
        """
        raw_str = str(raw_id).strip()
        eff_app = app_id or self.app_id or "default"
        salted = f"{eff_app}:{raw_str}".encode()
        return hashlib.sha256(salted).hexdigest()

    def _normalize_pf(self, platform: str | None) -> str:
        pf = str(platform or "").strip().lower()
        return "ios" if pf == "ios" else "android"

    def add_installations(
        self,
        app_id: str,
        platform: str,
        app_version: str,
        installation_ids: Iterable[str | int],
        first_seen: str | None = None,
        last_seen: str | None = None,
    ) -> int:
        """Idempotently adds installation identifiers for a specific (app_id, platform, app_version).

        Returns:
            The number of newly added unique installations.
        """
        eff_app = str(app_id or self.app_id or "default").strip()
        pf = self._normalize_pf(platform)
        ver = str(app_version or "").strip()
        if not ver:
            return 0

        hashes: set[str] = set()
        for raw in installation_ids:
            if raw is not None:
                s = str(raw).strip()
                if s:
                    hashes.add(self.hash_installation(s, eff_app))

        if not hashes:
            return 0

        now_iso = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
        fs = first_seen or now_iso
        ls = last_seen or now_iso

        records = [(eff_app, pf, ver, h, fs, ls) for h in hashes]

        try:
            with self._connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM release_installations WHERE app_id = ? AND platform = ? AND app_version = ?",
                    (eff_app, pf, ver),
                )
                count_before = cur.fetchone()[0]

                cur.executemany(
                    """
                    INSERT OR IGNORE INTO release_installations
                    (app_id, platform, app_version, installation_hash, first_seen, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    records,
                )

                # Ensure version authority status row exists with updated timestamp
                cur.execute(
                    """
                    INSERT INTO version_authority_status (app_id, platform, app_version, bootstrap_complete, updated_at)
                    VALUES (?, ?, ?, 0, ?)
                    ON CONFLICT(app_id, platform, app_version) DO UPDATE SET
                        updated_at = excluded.updated_at
                    """,
                    (eff_app, pf, ver, now_iso),
                )

                conn.commit()

                cur.execute(
                    "SELECT COUNT(*) FROM release_installations WHERE app_id = ? AND platform = ? AND app_version = ?",
                    (eff_app, pf, ver),
                )
                count_after = cur.fetchone()[0]
                return max(0, count_after - count_before)
        except sqlite3.Error as e:
            raise AuthorityStoreError(
                f"Failed to add installations for app '{eff_app}', {pf} version '{ver}': {e}"
            ) from e

    def count_installations(
        self,
        app_id: str,
        platform: str,
        app_version: str,
    ) -> int:
        """Returns the exact count of unique installations for a specific (app_id, platform, app_version)."""
        eff_app = str(app_id or self.app_id or "default").strip()
        pf = self._normalize_pf(platform)
        ver = str(app_version or "").strip()
        if not ver:
            return 0

        try:
            with self._connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM release_installations WHERE app_id = ? AND platform = ? AND app_version = ?",
                    (eff_app, pf, ver),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error as e:
            raise AuthorityStoreError(
                f"Failed to count installations for app '{eff_app}', {pf} version '{ver}': {e}"
            ) from e

    def has_version_authority(
        self,
        app_id: str,
        platform: str,
        app_version: str,
    ) -> bool:
        """Returns True if the authority store has verified complete authority (bootstrap_complete == 1)."""
        eff_app = str(app_id or self.app_id or "default").strip()
        pf = self._normalize_pf(platform)
        ver = str(app_version or "").strip()
        if not ver:
            return False

        try:
            with self._connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT bootstrap_complete FROM version_authority_status WHERE app_id = ? AND platform = ? AND app_version = ?",
                    (eff_app, pf, ver),
                )
                row = cur.fetchone()
                return bool(row and int(row[0]) == 1)
        except sqlite3.Error as e:
            raise AuthorityStoreError(
                f"Failed to check authority for app '{eff_app}', {pf} version '{ver}': {e}"
            ) from e

    def mark_version_bootstrapped(
        self,
        app_id: str,
        platform: str,
        app_version: str,
        complete: bool = True,
    ) -> None:
        """Explicitly records bootstrap status for a version (even if it had 0 crash events)."""
        eff_app = str(app_id or self.app_id or "default").strip()
        pf = self._normalize_pf(platform)
        ver = str(app_version or "").strip()
        if not ver:
            return

        now_iso = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
        try:
            with self._connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO version_authority_status (app_id, platform, app_version, bootstrap_complete, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(app_id, platform, app_version) DO UPDATE SET
                        bootstrap_complete = excluded.bootstrap_complete,
                        updated_at = excluded.updated_at
                    """,
                    (eff_app, pf, ver, 1 if complete else 0, now_iso),
                )
                conn.commit()
        except sqlite3.Error as e:
            raise AuthorityStoreError(
                f"Failed to mark version bootstrapped for '{eff_app}', {pf} '{ver}': {e}"
            ) from e

    def get_known_versions(self, app_id: str, platform: str | None = None) -> list[str]:
        """Returns sorted list of distinct app_versions tracked in authority store."""
        eff_app = str(app_id or self.app_id or "default").strip()
        try:
            with self._connection() as conn:
                cur = conn.cursor()
                if platform:
                    pf = self._normalize_pf(platform)
                    cur.execute(
                        """
                        SELECT DISTINCT app_version FROM release_installations
                        WHERE app_id = ? AND platform = ?
                        UNION
                        SELECT DISTINCT app_version FROM version_authority_status
                        WHERE app_id = ? AND platform = ?
                        ORDER BY app_version
                        """,
                        (eff_app, pf, eff_app, pf),
                    )
                else:
                    cur.execute(
                        """
                        SELECT DISTINCT app_version FROM release_installations
                        WHERE app_id = ?
                        UNION
                        SELECT DISTINCT app_version FROM version_authority_status
                        WHERE app_id = ?
                        ORDER BY app_version
                        """,
                        (eff_app, eff_app),
                    )
                rows = cur.fetchall()
                return [str(r[0]) for r in rows if r and r[0]]
        except sqlite3.Error as e:
            raise AuthorityStoreError(f"Failed to query known versions for app '{eff_app}': {e}") from e

    def clear_version(self, app_id: str, platform: str, app_version: str) -> None:
        """Clears all installation records and status for a specific version."""
        eff_app = str(app_id or self.app_id or "default").strip()
        pf = self._normalize_pf(platform)
        ver = str(app_version or "").strip()
        try:
            with self._connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "DELETE FROM release_installations WHERE app_id = ? AND platform = ? AND app_version = ?",
                    (eff_app, pf, ver),
                )
                cur.execute(
                    "DELETE FROM version_authority_status WHERE app_id = ? AND platform = ? AND app_version = ?",
                    (eff_app, pf, ver),
                )
                conn.commit()
        except sqlite3.Error as e:
            raise AuthorityStoreError(
                f"Failed to clear version '{ver}' for app '{eff_app}', platform '{pf}': {e}"
            ) from e

    def clear_app(self, app_id: str) -> None:
        """Clears all records for an app."""
        eff_app = str(app_id or self.app_id or "default").strip()
        try:
            with self._connection() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM release_installations WHERE app_id = ?", (eff_app,))
                cur.execute("DELETE FROM version_authority_status WHERE app_id = ?", (eff_app,))
                conn.commit()
        except sqlite3.Error as e:
            raise AuthorityStoreError(f"Failed to clear app '{eff_app}': {e}") from e

    def get_metadata(self, key: str) -> str | None:
        """Reads a key from authority_metadata table."""
        try:
            with self._connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT value FROM authority_metadata WHERE key = ?", (str(key),))
                row = cur.fetchone()
                return str(row[0]) if row and row[0] is not None else None
        except sqlite3.Error as e:
            raise AuthorityStoreError(f"Failed to get metadata key '{key}': {e}") from e

    def set_metadata(self, key: str, value: str) -> None:
        """Writes or updates a key in authority_metadata table."""
        try:
            with self._connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO authority_metadata (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (str(key), str(value)),
                )
                conn.commit()
        except sqlite3.Error as e:
            raise AuthorityStoreError(f"Failed to set metadata key '{key}': {e}") from e

    def is_bootstrap_complete(self, app_id: str | None = None) -> bool:
        """Checks whether bootstrap completion has been marked in authority metadata."""
        eff_app = app_id or self.app_id
        if eff_app:
            app_val = self.get_metadata(f"bootstrap_complete:{eff_app}")
            if app_val is not None:
                return app_val == "1"
        global_val = self.get_metadata("bootstrap_complete")
        return global_val == "1"

    def mark_bootstrap_complete(self, app_id: str | None = None, complete: bool = True) -> None:
        """Marks bootstrap completion in authority metadata."""
        val = "1" if complete else "0"
        eff_app = app_id or self.app_id
        if eff_app:
            self.set_metadata(f"bootstrap_complete:{eff_app}", val)
        self.set_metadata("bootstrap_complete", val)

    def get_state_version(self) -> int:
        """Returns the current authority state version schema number."""
        val = self.get_metadata("state_version")
        try:
            return int(val) if val is not None else self.DEFAULT_STATE_VERSION
        except Exception:
            return self.DEFAULT_STATE_VERSION

    def close(self) -> None:
        """Closes any open SQLite database connections."""
        if self._explicit_conn is not None:
            try:
                self._explicit_conn.close()
            except Exception:
                pass
            self._explicit_conn = None
        if self.is_memory and self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __enter__(self) -> CatalogAuthorityStore:
        if not self.is_memory and self._explicit_conn is None:
            self._explicit_conn = sqlite3.connect(str(self.db_path), timeout=10.0, check_same_thread=False)
            self._explicit_conn.row_factory = sqlite3.Row
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
