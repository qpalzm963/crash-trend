"""Release Gate Evaluation History Store and State Evolution (Issue #61).

Provides:
- GateSnapshot: Immutable snapshot of a Release Gate evaluation.
- GateTransition: Classification of gate state transitions (recovery, regression, escalation, etc.).
- ReleaseGateTrendItem: Multi-release quality trend summary using canonical SemVer ordering.
- ReleaseGateHistoryStore: SQLite-backed append-only history store with strict evaluation_key idempotency.
- CLI: Command-line query tool for gate evaluation history and trends.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Literal

from crash_trend.config import out_dir
from crash_trend.gate.artifact import ReleaseGateArtifact, RuleEvaluationResult
from crash_trend.versions import version_key


@dataclasses.dataclass(frozen=True)
class GateTransition:
    """Represents a state transition between successive gate evaluations."""

    previous_status: str | None
    current_status: str
    status_changed: bool
    is_recovery: bool
    is_regression: bool
    transition_type: Literal[
        "initial",
        "unchanged",
        "recovery",
        "regression",
        "escalation",
        "de_escalation",
        "insufficient",
        "other",
    ]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def classify_gate_transition(prev_status: str | None, curr_status: str) -> GateTransition:
    """Classifies the transition from previous gate status to current gate status."""
    prev = prev_status.lower().strip() if prev_status else None
    curr = curr_status.lower().strip()

    if prev is None:
        return GateTransition(
            previous_status=None,
            current_status=curr,
            status_changed=False,
            is_recovery=False,
            is_regression=False,
            transition_type="initial",
        )

    if prev == curr:
        return GateTransition(
            previous_status=prev,
            current_status=curr,
            status_changed=False,
            is_recovery=False,
            is_regression=False,
            transition_type="unchanged",
        )

    # State changed
    # Recovery: prior warn/fail -> pass
    if prev in ("warn", "fail") and curr == "pass":
        return GateTransition(
            previous_status=prev,
            current_status=curr,
            status_changed=True,
            is_recovery=True,
            is_regression=False,
            transition_type="recovery",
        )

    # Escalation: warn -> fail
    if prev == "warn" and curr == "fail":
        return GateTransition(
            previous_status=prev,
            current_status=curr,
            status_changed=True,
            is_recovery=False,
            is_regression=True,
            transition_type="escalation",
        )

    # De-escalation: fail -> warn
    if prev == "fail" and curr == "warn":
        return GateTransition(
            previous_status=prev,
            current_status=curr,
            status_changed=True,
            is_recovery=False,
            is_regression=False,
            transition_type="de_escalation",
        )

    # Regression: pass / baseline / insufficient_data -> warn / fail
    if prev in ("pass", "baseline", "insufficient_data") and curr in ("warn", "fail"):
        return GateTransition(
            previous_status=prev,
            current_status=curr,
            status_changed=True,
            is_recovery=False,
            is_regression=True,
            transition_type="regression",
        )

    # Drop to insufficient
    if curr == "insufficient_data":
        return GateTransition(
            previous_status=prev,
            current_status=curr,
            status_changed=True,
            is_recovery=False,
            is_regression=False,
            transition_type="insufficient",
        )

    # Other transitions (e.g. baseline -> pass)
    return GateTransition(
        previous_status=prev,
        current_status=curr,
        status_changed=True,
        is_recovery=False,
        is_regression=False,
        transition_type="other",
    )


@dataclasses.dataclass
class GateSnapshot:
    """Immutable snapshot of a Release Gate evaluation."""

    app_id: str
    platform: str
    version: str
    previous_version: str | None
    gate_status: str
    sample_sufficient: bool
    policy_version: str
    comparison_window: str | None
    evaluated_at: str
    evaluation_key: str
    triggered_reasons: list[str]
    rule_results: list[RuleEvaluationResult]
    normalized_metrics: dict[str, Any]
    summary: str
    policy_identity: str = ""
    schema_version: str = "1.0"
    created_at: str = ""
    id: int | None = None
    transition: GateTransition | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "app_id": self.app_id,
            "platform": self.platform,
            "version": self.version,
            "previous_version": self.previous_version,
            "gate_status": self.gate_status,
            "sample_sufficient": self.sample_sufficient,
            "policy_version": self.policy_version,
            "policy_identity": self.policy_identity,
            "comparison_window": self.comparison_window,
            "evaluated_at": self.evaluated_at,
            "evaluation_key": self.evaluation_key,
            "triggered_reasons": list(self.triggered_reasons),
            "rule_results": list(self.rule_results),
            "normalized_metrics": dict(self.normalized_metrics),
            "summary": self.summary,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
        }
        if self.transition is not None:
            d["transition"] = self.transition.to_dict()
        return d


@dataclasses.dataclass
class ReleaseGateTrendItem:
    """Trend item summarizing gate evaluation history across recent releases."""

    version: str
    platform: str
    latest_status: str
    history_count: int
    first_evaluated_at: str
    latest_evaluated_at: str
    is_recovered: bool
    is_regressed: bool
    latest_transition: GateTransition | None
    latest_triggered_reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "platform": self.platform,
            "latest_status": self.latest_status,
            "history_count": self.history_count,
            "first_evaluated_at": self.first_evaluated_at,
            "latest_evaluated_at": self.latest_evaluated_at,
            "is_recovered": self.is_recovered,
            "is_regressed": self.is_regressed,
            "latest_transition": self.latest_transition.to_dict() if self.latest_transition else None,
            "latest_triggered_reasons": list(self.latest_triggered_reasons),
        }


def compute_policy_identity(policy: dict[str, Any] | Any | None, policy_version: str = "1.0") -> str:
    """Computes a deterministic SHA-256 fingerprint of the effective gate policy rules & thresholds."""
    if policy is None:
        raw_dict = {"policy_version": policy_version}
    elif hasattr(policy, "to_dict"):
        raw_dict = policy.to_dict()
    elif isinstance(policy, dict):
        raw_dict = policy
    else:
        raw_dict = {"policy_version": str(policy)}
    canonical_json = json.dumps(raw_dict, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()[:16]


def compute_evaluation_key(
    app_id: str,
    platform: str,
    version: str,
    evaluated_at: str,
    policy_version: str,
    policy_identity: str = "",
) -> str:
    """Computes a deterministic SHA-256 evaluation key for idempotency."""
    raw = (
        f"{app_id.strip().lower()}:{platform.strip().lower()}:{version.strip()}:"
        f"{evaluated_at.strip()}:{policy_version.strip()}:{policy_identity.strip()}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()



class ReleaseGateHistoryStore:
    """SQLite-backed append-only store for Release Gate evaluation snapshots.

    Features:
    - Immutable snapshots.
    - Idempotent writes via UNIQUE evaluation_key.
    - Zero raw user / installation UUIDs.
    - Sequential transition calculation (insufficient -> warn -> fail -> pass).
    - Multi-release trend retrieval with canonical SemVer ordering.
    """

    SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS release_gate_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        app_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        version TEXT NOT NULL,
        previous_version TEXT,
        gate_status TEXT NOT NULL,
        sample_sufficient INTEGER NOT NULL,
        policy_version TEXT NOT NULL,
        policy_identity TEXT NOT NULL DEFAULT '',
        comparison_window TEXT,
        evaluated_at TEXT NOT NULL,
        evaluation_key TEXT NOT NULL UNIQUE,
        triggered_reasons_json TEXT NOT NULL,
        rule_results_json TEXT NOT NULL,
        normalized_metrics_json TEXT NOT NULL,
        summary TEXT NOT NULL,
        schema_version TEXT NOT NULL DEFAULT '1.0',
        created_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_gate_history_app_platform_ver
        ON release_gate_snapshots(app_id, platform, version, evaluated_at);

    CREATE INDEX IF NOT EXISTS idx_gate_history_eval_key
        ON release_gate_snapshots(evaluation_key);
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = db_path
        self._is_memory = str(db_path) == ":memory:"
        self._persistent_conn: sqlite3.Connection | None = None

        if not self._is_memory:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(self.SCHEMA_SQL)
                self._migrate_schema(conn)
        else:
            # For :memory:, keep a persistent connection so tables aren't lost across calls
            self._persistent_conn = sqlite3.connect(":memory:")
            self._persistent_conn.row_factory = sqlite3.Row
            self._persistent_conn.executescript(self.SCHEMA_SQL)
            self._migrate_schema(self._persistent_conn)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Applies backward-compatible column migrations if necessary."""
        try:
            conn.execute("ALTER TABLE release_gate_snapshots ADD COLUMN policy_identity TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass

    def _connect(self) -> sqlite3.Connection:
        if self._is_memory and self._persistent_conn is not None:
            return self._persistent_conn
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except Exception:
            pass
        return conn

    def close(self) -> None:
        if self._persistent_conn is not None:
            try:
                self._persistent_conn.close()
            except Exception:
                pass
            self._persistent_conn = None

    def record_snapshot(self, snapshot: GateSnapshot) -> tuple[GateSnapshot, bool]:
        """Records an evaluation snapshot idempotently using atomic INSERT OR IGNORE.

        Returns:
            (saved_snapshot, is_new_record)
        """
        app_id = snapshot.app_id.strip()
        pf = snapshot.platform.strip().lower()
        ver = snapshot.version.strip()
        eval_at = snapshot.evaluated_at.strip()
        pol_ver = snapshot.policy_version.strip()
        pol_ident = snapshot.policy_identity.strip()

        eval_key = snapshot.evaluation_key or compute_evaluation_key(app_id, pf, ver, eval_at, pol_ver, pol_ident)
        created_at = snapshot.created_at or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        tr_json = json.dumps(snapshot.triggered_reasons, ensure_ascii=False)
        rr_json = json.dumps(snapshot.rule_results, ensure_ascii=False)
        nm_json = json.dumps(snapshot.normalized_metrics, ensure_ascii=False)

        conn = self._connect()
        try:
            # Atomic idempotent insert: UNIQUE(evaluation_key) prevents race conditions
            with conn:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO release_gate_snapshots (
                        app_id, platform, version, previous_version, gate_status,
                        sample_sufficient, policy_version, policy_identity, comparison_window,
                        evaluated_at, evaluation_key, triggered_reasons_json,
                        rule_results_json, normalized_metrics_json, summary,
                        schema_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        app_id,
                        pf,
                        ver,
                        snapshot.previous_version,
                        snapshot.gate_status,
                        1 if snapshot.sample_sufficient else 0,
                        pol_ver,
                        pol_ident,
                        snapshot.comparison_window,
                        eval_at,
                        eval_key,
                        tr_json,
                        rr_json,
                        nm_json,
                        snapshot.summary,
                        snapshot.schema_version,
                        created_at,
                    ),
                )
                is_new = cur.rowcount > 0

            # Fetch authoritative record from DB
            cur = conn.execute(
                "SELECT * FROM release_gate_snapshots WHERE evaluation_key = ?",
                (eval_key,),
            )
            row = cur.fetchone()
            if row is not None:
                return self._row_to_snapshot(row), is_new

            saved = dataclasses.replace(
                snapshot,
                evaluation_key=eval_key,
                policy_identity=pol_ident,
                created_at=created_at,
            )
            return saved, is_new
        finally:
            if not self._is_memory:
                conn.close()

    def record_gate_artifact(
        self,
        artifact: ReleaseGateArtifact,
        policy_version: str | None = None,
        policy_identity: str | None = None,
    ) -> list[GateSnapshot]:
        """Extracts platform-level evaluations from ReleaseGateArtifact and stores snapshots."""
        app_id = artifact.get("app_id", "").strip()
        pol_ver = policy_version or artifact.get("policy_version", "1.0")
        pol_ident = (
            policy_identity
            or artifact.get("policy_identity")
            or compute_policy_identity(artifact.get("policy"), pol_ver)
        )
        results: list[GateSnapshot] = []

        platforms = artifact.get("platforms") or {}
        for pf_name, pf_res in platforms.items():
            ver = pf_res.get("target_version", "").strip()
            if not ver:
                continue

            prev_ver = pf_res.get("previous_version")
            st = pf_res.get("gate_status", "unknown")
            sufficient = bool(pf_res.get("sample_sufficient", False))
            eval_at = pf_res.get("evaluated_at") or artifact.get("generated_at") or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            alert_obj = pf_res.get("alert") or {}
            reasons = alert_obj.get("trigger_rules") or []
            summary = alert_obj.get("alert_summary") or artifact.get("alert_summary") or ""
            cmp_win = pf_res.get("comparison_window")
            rule_results = pf_res.get("rule_results") or []

            # Extract normalized metrics from rule evaluation results
            normalized_metrics: dict[str, Any] = {}
            for r in rule_results:
                m_name = r.get("metric_name")
                if m_name:
                    normalized_metrics[f"{m_name}_current"] = r.get("current_value")
                    normalized_metrics[f"{m_name}_previous"] = r.get("previous_value")

            eval_key = compute_evaluation_key(app_id, pf_name, ver, eval_at, pol_ver, pol_ident)

            snap = GateSnapshot(
                app_id=app_id,
                platform=pf_name.lower(),
                version=ver,
                previous_version=prev_ver,
                gate_status=st,
                sample_sufficient=sufficient,
                policy_version=pol_ver,
                policy_identity=pol_ident,
                comparison_window=cmp_win,
                evaluated_at=eval_at,
                evaluation_key=eval_key,
                triggered_reasons=reasons,
                rule_results=rule_results,
                normalized_metrics=normalized_metrics,
                summary=summary,
            )

            saved, _ = self.record_snapshot(snap)
            results.append(saved)

        return results

    def get_release_gate_history(
        self,
        app_id: str,
        platform: str,
        version: str,
    ) -> list[GateSnapshot]:
        """Returns chronological snapshots for a specific release, with transitions classified."""
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                SELECT * FROM release_gate_snapshots
                WHERE app_id = ? AND LOWER(platform) = LOWER(?) AND version = ?
                ORDER BY evaluated_at ASC, id ASC
                """,
                (app_id.strip(), platform.strip(), version.strip()),
            )
            rows = cur.fetchall()
            snapshots = [self._row_to_snapshot(r) for r in rows]

            # Populate sequential transitions
            for idx in range(len(snapshots)):
                prev_status = snapshots[idx - 1].gate_status if idx > 0 else None
                transition = classify_gate_transition(prev_status, snapshots[idx].gate_status)
                snapshots[idx].transition = transition

            return snapshots
        finally:
            if not self._is_memory:
                conn.close()

    def get_latest_release_gate_state(
        self,
        app_id: str,
        platform: str,
        version: str,
    ) -> GateSnapshot | None:
        """Returns the latest evaluation snapshot for a release with transition attached."""
        history = self.get_release_gate_history(app_id, platform, version)
        return history[-1] if history else None

    def get_recent_release_gate_trend(
        self,
        app_id: str,
        platform: str,
        limit: int = 5,
    ) -> list[ReleaseGateTrendItem]:
        """Retrieves quality trend summary across recent releases using canonical SemVer ordering.

        Strictly orders releases using version_key from crash_trend.versions (no lexical string sorting).
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                """
                SELECT DISTINCT version FROM release_gate_snapshots
                WHERE app_id = ? AND LOWER(platform) = LOWER(?)
                """,
                (app_id.strip(), platform.strip()),
            )
            raw_versions = [r["version"] for r in cur.fetchall() if r["version"]]
        finally:
            if not self._is_memory:
                conn.close()

        if not raw_versions:
            return []

        # Canonical SemVer ordering (highest version first)
        sorted_versions = sorted(raw_versions, key=version_key, reverse=True)[:limit]

        trend_items: list[ReleaseGateTrendItem] = []
        for ver in sorted_versions:
            history = self.get_release_gate_history(app_id, platform, ver)
            if not history:
                continue

            latest = history[-1]
            first = history[0]

            is_recovered = any(s.transition is not None and s.transition.is_recovery for s in history)
            is_regressed = any(s.transition is not None and s.transition.is_regression for s in history)

            item = ReleaseGateTrendItem(
                version=ver,
                platform=platform.lower(),
                latest_status=latest.gate_status,
                history_count=len(history),
                first_evaluated_at=first.evaluated_at,
                latest_evaluated_at=latest.evaluated_at,
                is_recovered=is_recovered,
                is_regressed=is_regressed,
                latest_transition=latest.transition,
                latest_triggered_reasons=latest.triggered_reasons,
            )
            trend_items.append(item)

        return trend_items

    def _row_to_snapshot(self, row: sqlite3.Row) -> GateSnapshot:
        """Deserializes a database row to a GateSnapshot."""
        try:
            reasons = json.loads(row["triggered_reasons_json"])
        except Exception:
            reasons = []

        try:
            rule_results = json.loads(row["rule_results_json"])
        except Exception:
            rule_results = []

        try:
            norm_metrics = json.loads(row["normalized_metrics_json"])
        except Exception:
            norm_metrics = {}

        pol_ident = row["policy_identity"] if "policy_identity" in row.keys() else ""

        return GateSnapshot(
            id=row["id"],
            app_id=row["app_id"],
            platform=row["platform"],
            version=row["version"],
            previous_version=row["previous_version"],
            gate_status=row["gate_status"],
            sample_sufficient=bool(row["sample_sufficient"]),
            policy_version=row["policy_version"],
            policy_identity=pol_ident,
            comparison_window=row["comparison_window"],
            evaluated_at=row["evaluated_at"],
            evaluation_key=row["evaluation_key"],
            triggered_reasons=reasons,
            rule_results=rule_results,
            normalized_metrics=norm_metrics,
            summary=row["summary"],
            schema_version=row["schema_version"],
            created_at=row["created_at"],
        )

    def prune_snapshots(
        self,
        app_id: str,
        older_than_days: int = 90,
        before: dt.datetime | str | None = None,
    ) -> int:
        """Prunes older snapshots while preserving the latest evaluation for EVERY release and platform.

        Retention Policy (Scope I):
        - Default: Full retention, append-only.
        - Invariant: The authoritative latest snapshot (ROW_NUMBER() OVER (PARTITION BY platform, version
          ORDER BY evaluated_at DESC, id DESC) = 1) is NEVER pruned.

        Args:
            app_id: Application identifier.
            older_than_days: Prune records older than N days (used if `before` is None).
            before: Specific cutoff timestamp (ISO string or datetime). Records evaluated prior to this are pruned.

        Returns:
            Number of deleted snapshots.
        """
        if before is None:
            cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=older_than_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        elif isinstance(before, dt.datetime):
            cutoff = before.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            cutoff = str(before).strip()

        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    """
                    DELETE FROM release_gate_snapshots
                    WHERE app_id = ?
                      AND evaluated_at < ?
                      AND id NOT IN (
                          SELECT id FROM (
                              SELECT id, ROW_NUMBER() OVER (
                                  PARTITION BY platform, version
                                  ORDER BY evaluated_at DESC, id DESC
                              ) AS rn
                              FROM release_gate_snapshots
                              WHERE app_id = ?
                          ) WHERE rn = 1
                      )
                    """,
                    (app_id.strip(), cutoff, app_id.strip()),
                )
                return cur.rowcount
        finally:
            if not self._is_memory:
                conn.close()


def get_gate_history_store(app_id: str, custom_path: Path | None = None) -> ReleaseGateHistoryStore:
    """Helper to obtain the authoritative ReleaseGateHistoryStore for an app."""
    db_file = custom_path or (out_dir(app_id) / "release_gate_history.sqlite3")
    return ReleaseGateHistoryStore(db_file)


def main() -> None:
    """CLI query tool for release gate history and trends."""
    parser = argparse.ArgumentParser(description="查詢版本品質閘門 (Release Gate) 歷史快照與品質趨勢")
    parser.add_argument("--app", required=True, help="App ID 名稱")
    parser.add_argument("--platform", default=None, help="指定平台 (android 或 ios)")
    parser.add_argument("--version", default=None, help="指定版本號")
    parser.add_argument("--trend", action="store_true", help="顯示最近版本品質趨勢")
    parser.add_argument("--limit", type=int, default=5, help="趨勢查詢版本數量上限 (預設 5)")
    parser.add_argument("--prune-older-than-days", type=int, default=None, help="清理指定天數以前的舊快照 (保留各版本最新快照)")
    parser.add_argument("--db", type=Path, default=None, help="自訂 SQLite 資料庫路徑")
    parser.add_argument("--json", action="store_true", help="以 JSON 格式輸出")
    args = parser.parse_args()

    store = get_gate_history_store(args.app, custom_path=args.db)

    if args.prune_older_than_days is not None:
        deleted = store.prune_snapshots(args.app, older_than_days=args.prune_older_than_days)
        print(f"[{args.app}] 已清理 {args.prune_older_than_days} 天以前的歷史評估快照，共刪除 {deleted} 筆 (各版本最新快照已保留)")
        sys.exit(0)

    platforms = [args.platform.lower()] if args.platform else ["android", "ios"]

    if args.trend:
        all_trends: dict[str, list[dict[str, Any]]] = {}
        for pf in platforms:
            trends = store.get_recent_release_gate_trend(args.app, pf, limit=args.limit)
            all_trends[pf] = [t.to_dict() for t in trends]

        if args.json:
            print(json.dumps(all_trends, ensure_ascii=False, indent=2))
        else:
            for pf, items in all_trends.items():
                print(f"\n=== Release Gate Quality Trend: {args.app} [{pf.upper()}] ===")
                if not items:
                    print("  尚無品質歷史評估紀錄")
                    continue
                for item in items:
                    st = item["latest_status"].upper()
                    rec = " (Recovered ↗)" if item["is_recovered"] else ""
                    reg = " (Regressed ↘)" if item["is_regressed"] else ""
                    print(f"  - v{item['version']:<10} [{st:<5}{rec}{reg}] (evaluations: {item['history_count']}, latest: {item['latest_evaluated_at']})")
                    if item["latest_triggered_reasons"]:
                        for r in item["latest_triggered_reasons"]:
                            print(f"      • {r}")
        sys.exit(0)

    if args.version and args.platform:
        history = store.get_release_gate_history(args.app, args.platform, args.version)
        if args.json:
            print(json.dumps([h.to_dict() for h in history], ensure_ascii=False, indent=2))
        else:
            print(f"\n=== Release Gate History: {args.app} [{args.platform.upper()}] v{args.version} ===")
            if not history:
                print("  尚無評估歷史紀錄")
            else:
                for idx, h in enumerate(history, 1):
                    tr_type = f" [{h.transition.transition_type.upper()}]" if h.transition else ""
                    print(f"  #{idx} {h.evaluated_at} -> [{h.gate_status.upper()}]{tr_type}")
                    print(f"     Summary: {h.summary}")
                    if h.triggered_reasons:
                        print(f"     Triggered: {', '.join(h.triggered_reasons)}")
        sys.exit(0)

    # Default overview
    summary_data: dict[str, Any] = {}
    for pf in platforms:
        trends = store.get_recent_release_gate_trend(args.app, pf, limit=args.limit)
        summary_data[pf] = [t.to_dict() for t in trends]

    if args.json:
        print(json.dumps(summary_data, ensure_ascii=False, indent=2))
    else:
        for pf, items in summary_data.items():
            print(f"\n=== Release Gate Overview: {args.app} [{pf.upper()}] ===")
            if not items:
                print("  尚無評估歷史紀錄")
                continue
            for item in items:
                st = item["latest_status"].upper()
                print(f"  - v{item['version']:<10} [{st:<5}] evaluations: {item['history_count']}, latest: {item['latest_evaluated_at']}")


if __name__ == "__main__":
    main()
