"""Catalog Bootstrap, disk recovery, and authority verification (Issue #29, #49, #53).

Provides:
- _has_verifiable_installation_authority: Validates installation UUID authority presence.
- should_trigger_catalog_bootstrap: Deterministic evaluation of full bootstrap requirement.
- bootstrap_catalog_from_disk: Offline / local bootstrap from existing monthly report data and snapshots.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional, Tuple, TYPE_CHECKING

from crash_trend.authority_store import CatalogAuthorityStore
from crash_trend.config import ROOT

if TYPE_CHECKING:
    from crash_trend.catalog.historical import IssueHistoricalCatalog


def _has_verifiable_installation_authority(
    v: dict,
    app_id: Optional[str] = None,
    platform: Optional[str] = None,
    store: Optional[CatalogAuthorityStore] = None,
) -> bool:
    """Verifies that a version entity possesses authoritative installation UUID state.

    Authority is verified if either:
    1. The SQLite authority store contains complete records (count >= lifetime_affected_users)
       or bootstrap status for (app_id, platform, ver), OR
    2. The version entity contains legacy installation_ids / user_ids matching or exceeding lifetime_affected_users.
    """
    if not isinstance(v, dict):
        return False
    ver = str(v.get("version") or "").strip()
    pf = v.get("platform") or platform or "android"
    eff_app = app_id or "default"

    users = int(v.get("lifetime_affected_users") or v.get("affected_users") or 0)
    crashes = int(v.get("lifetime_crashes") or v.get("crash_events") or 0)

    # 1. Check SQLite authority store if available (do NOT catch AuthorityStoreError: let it propagate)
    if store is not None and ver:
        has_auth = store.has_version_authority(eff_app, pf, ver)
        if not has_auth:
            return False
        sqlite_count = store.count_installations(eff_app, pf, ver)
        if users > 0:
            return sqlite_count >= users
        return True

    # 2. Check legacy JSON installation_ids
    ids = v.get("installation_ids") or v.get("user_ids")
    if users > 0:
        if isinstance(ids, (list, set, tuple)) and len(ids) >= users:
            return True
        return False
    elif crashes > 0:
        return bool(ids)

    # For zero-crash/zero-user placeholder versions, the installation_ids field must at least be explicitly present
    return ("installation_ids" in v) or ("user_ids" in v)


def should_trigger_catalog_bootstrap(
    cat_data: Optional[dict],
    cat_file_exists: bool,
    explicit_bootstrap: bool = False,
    explicit_watermark: Optional[str] = None,
    authority_store: Optional[CatalogAuthorityStore] = None,
    authority_store_path: Optional[Path] = None,
    app_id: Optional[str] = None,
    cat_file_path: Optional[Path] = None,
) -> Tuple[bool, Optional[str]]:
    """Evaluates whether a catalog requires a full historical bootstrap.

    Triggers full bootstrap (SQLS["version_catalog_bootstrap"]) if:
    1. explicit_bootstrap is True (--bootstrap CLI flag passed).
    2. Catalog file does not exist on disk.
    3. Catalog file exists, but lacks a valid watermark (pre-watermark legacy catalog).
    4. Catalog file explicitly records bootstrap_complete as False.
    5. Catalog file has app_versions, but ANY persisted version lacks verifiable installation authority state
       in either the SQLite authority store or legacy JSON format.

    Returns:
        (is_bootstrap: bool, watermark: Optional[str])
    """
    if explicit_bootstrap:
        return True, explicit_watermark

    if not cat_file_exists or not isinstance(cat_data, dict):
        return True, explicit_watermark

    watermark = explicit_watermark or cat_data.get("watermark")
    if not watermark:
        return True, None

    if cat_data.get("bootstrap_complete") is False:
        return True, None

    if isinstance(cat_data.get("authority"), dict) and cat_data["authority"].get("bootstrap_complete") is False:
        return True, None

    eff_app = app_id or (cat_data.get("app_id") if isinstance(cat_data, dict) else None) or "default"
    store = authority_store
    opened_store_to_close: Optional[CatalogAuthorityStore] = None

    if store is None:
        if authority_store_path is not None and Path(authority_store_path).is_file():
            # If authority store exists on disk, open it. Any AuthorityStoreError will propagate upwards.
            store = CatalogAuthorityStore(authority_store_path, app_id=eff_app)
            opened_store_to_close = store
        elif cat_file_path is not None and (Path(cat_file_path).parent / "catalog_authority.sqlite3").is_file():
            store = CatalogAuthorityStore(Path(cat_file_path).parent / "catalog_authority.sqlite3", app_id=eff_app)
            opened_store_to_close = store

    try:
        app_vers = cat_data.get("app_versions")
        if not isinstance(app_vers, dict) or not app_vers:
            return True, None

        all_v_objs: List[Tuple[str, dict]] = []
        for pf_or_ver, val in app_vers.items():
            if isinstance(val, dict):
                if pf_or_ver in ("android", "ios"):
                    for v_dict in val.values():
                        if isinstance(v_dict, dict):
                            all_v_objs.append((pf_or_ver, v_dict))
                else:
                    all_v_objs.append((val.get("platform", "android"), val))

        if not all_v_objs:
            return True, None

        for pf, v in all_v_objs:
            if not _has_verifiable_installation_authority(v, app_id=eff_app, platform=pf, store=store):
                return True, None

        return False, watermark
    finally:
        if opened_store_to_close is not None:
            opened_store_to_close.close()


def bootstrap_catalog_from_disk(
    app_name: str,
    root_dir: Optional[Path] = None,
    catalog: Optional[IssueHistoricalCatalog] = None,
) -> IssueHistoricalCatalog:
    """Bootstraps an IssueHistoricalCatalog by scanning historical reports and archives on disk."""
    base = root_dir or ROOT
    cat = catalog
    if cat is None:
        from crash_trend.catalog.historical import IssueHistoricalCatalog
        cat_file = base / "out" / app_name / "historical_catalog.json"
        cat = IssueHistoricalCatalog(cat_file, app_id=app_name)
        cat.load()

    # 1. Scan reports/data/<app_name>/*.json (monthly historical reports)
    reports_dir = base / "reports" / "data" / app_name
    if reports_dir.is_dir():
        for r_file in sorted(reports_dir.glob("*.json")):
            try:
                r_data = json.loads(r_file.read_text(encoding="utf-8"))
                issues = r_data.get("issues") or r_data.get("top_issues") or []
                if issues:
                    cat.update_from_issues(issues)
                vh = r_data.get("version_health")
                if vh:
                    cat.update_app_versions(vh)
                app_vers = r_data.get("distributions", {}).get("app_versions") or []
                for av in app_vers:
                    if isinstance(av, dict) and av.get("app_version"):
                        cat.update_from_catalog_rows([{
                            "app_version": av["app_version"],
                            "events": av.get("events", 0),
                            "users": av.get("users", 0),
                            "platform": av.get("platform", "android"),
                        }])
            except Exception:
                pass

    # 2. Scan out/<app_name>/unified.json
    unified_file = base / "out" / app_name / "unified.json"
    if unified_file.is_file():
        try:
            u_data = json.loads(unified_file.read_text(encoding="utf-8"))
            u_issues = u_data.get("issues") or []
            if u_issues:
                cat.update_from_issues(u_issues)
            u_vh = u_data.get("version_health")
            if u_vh:
                cat.update_app_versions(u_vh)
        except Exception:
            pass

    # 3. Scan out/<app_name>/dashboard_v2.json
    v2_file = base / "out" / app_name / "dashboard_v2.json"
    if v2_file.is_file():
        try:
            v2_data = json.loads(v2_file.read_text(encoding="utf-8"))
            v2_issues = v2_data.get("top_issues") or []
            cat.update_from_issues(v2_issues)
            for p_k, snap in (v2_data.get("periods") or {}).items():
                if isinstance(snap, dict):
                    cat.update_from_issues(snap.get("top_issues") or [])
                    cat.update_app_versions(snap.get("version_health") or [], window=p_k)
            cat.update_app_versions(v2_data.get("version_health") or [])
        except Exception:
            pass

    cat.save()
    return cat


def main() -> None:
    """CLI entrypoint for standalone catalog bootstrap and lifecycle maintenance."""
    import argparse
    from crash_trend.catalog.historical import IssueHistoricalCatalog
    from crash_trend.config import get_app, load_config, out_dir

    parser = argparse.ArgumentParser(description="Issue Historical Catalog & Lifecycle CLI")
    parser.add_argument("--app", required=True, help="Application ID defined in apps.yaml")
    parser.add_argument("--bootstrap", action="store_true", help="Bootstrap catalog from reports/data and out/ artifacts")
    args = parser.parse_args()

    cfg = load_config()
    app_cfg = get_app(args.app, cfg)
    print(f"=== Issue Historical Catalog: {args.app} ({app_cfg.get('display_name')}) ===")

    target_cat = out_dir(args.app) / "historical_catalog.json"
    cat = IssueHistoricalCatalog(target_cat, app_id=args.app)
    cat.load()

    if args.bootstrap:
        print("  ⏳ 正在執行歷史冷啟動 (Bootstrap from disk archives)...")
        cat = bootstrap_catalog_from_disk(args.app, root_dir=ROOT, catalog=cat)
        print(f"  ✓ 冷啟動完成！已保存 {len(cat.issues)} 筆歷史 Issue 與版本資訊至 {target_cat.relative_to(ROOT)}")
    else:
        print(f"  現有 Catalog 記錄: {len(cat.issues)} 筆 Issue, updated_at: {cat.updated_at}")

