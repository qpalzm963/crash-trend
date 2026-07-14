"""把 Crashlytics BigQuery / 手動 console CSV 正規化成統一 schema（Firebase-only）。

輸出：
  out/<app>/unified.json                 — 完整合併結果（gitignored，供 AI 分析）
  reports/data/<app>/YYYY-MM.json        — 當月小型摘要（committed，供儀表板跨月趨勢）
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

from config import ROOT, app_argparser, get_app, load_prev_month, out_dir, write_json
from versions import max_version, min_version, version_key


def load_if_exists(path: Path) -> dict | None:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def norm_error_type(raw: str, fatal: bool) -> str:
    """error_type 收斂三值 enum（FATAL/ANR/NON_FATAL）；未知值用 fatal 布林推。"""
    et = (raw or "").upper().replace("-", "_")
    return et if et in ("FATAL", "ANR", "NON_FATAL") else ("FATAL" if fatal else "NON_FATAL")


def bq_issues_to_unified(bq: dict) -> tuple[list[dict], dict, list[dict], list[dict]]:
    issues, dists, custom_keys, weekly = [], {}, [], []
    for table, data in (bq or {}).get("tables", {}).items():
        platform = "ios" if table.endswith("_IOS") else "android"
        ver_by_issue: dict[str, list[dict]] = {}
        for r in data.get("issue_versions", []):
            ver_by_issue.setdefault(r.get("issue_id", ""), []).append({
                "version": r.get("app_version", ""),
                "events": int(r.get("events", 0)),
                "users": int(r.get("users", 0)),
            })
        for it in data.get("top_issues", []):
            vd = sorted(ver_by_issue.get(it.get("issue_id", ""), []),
                        key=lambda x: version_key(x["version"]), reverse=True)
            versions = [x["version"] for x in vd]
            issues.append({
                "platform": platform,
                "source": "crashlytics_bq",
                "issue_id": it.get("issue_id", ""),
                "title": it.get("issue_title", ""),
                "subtitle": it.get("issue_subtitle", ""),
                "fatal": (it.get("error_type") or "").upper() == "FATAL",
                "error_type": norm_error_type(it.get("error_type"), (it.get("error_type") or "").upper() == "FATAL"),
                "events": int(it.get("events", 0)),
                "users": int(it.get("users", 0)),
                # BQ 的 MIN/MAX 是字串序（"1.0.10" < "1.0.9"）；有逐版本資料時以 semver 序重算
                "first_seen_version": min_version(versions) or it.get("first_seen_version", ""),
                "last_seen_version": max_version(versions) or it.get("last_seen_version", ""),
                "version_dist": vd,
            })
        d = dists.setdefault(platform, {})
        d["device"] = [{"label": r.get("device_model", "?"), "events": int(r.get("events", 0)), "users": int(r.get("users", 0))} for r in data.get("by_device", [])]
        d["os"] = [{"label": r.get("os_version", "?"), "events": int(r.get("events", 0)), "users": int(r.get("users", 0))} for r in data.get("by_os", [])]
        d["app_version"] = [{"label": r.get("app_version", "?"), "events": int(r.get("events", 0)), "users": int(r.get("users", 0))} for r in data.get("by_app_version", [])]
        custom_keys += [{**r, "platform": platform} for r in data.get("custom_keys", [])]
        weekly += [{**r, "platform": platform} for r in data.get("weekly_trend", [])]
    return issues, dists, custom_keys, weekly


def manual_csv_to_unified(path: Path) -> list[dict]:
    if not path.exists():
        return []
    issues = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (r.get("platform") or "").strip().startswith("#"):  # 模板註解/範例列
                continue
            if not (r.get("issue_title") or "").strip():
                continue
            issues.append({
                "platform": (r.get("platform") or "").strip().lower(),
                "source": "manual_console",
                "issue_id": r.get("issue_id", ""),
                "title": r["issue_title"].strip(),
                "subtitle": (r.get("issue_subtitle") or "").strip(),
                "fatal": (fatal := (r.get("fatal") or "").strip().lower() in ("yes", "true", "y", "1")),
                # console CSV 只有 fatal 欄，無 ANR 資訊 → 由 fatal 推
                "error_type": "FATAL" if fatal else "NON_FATAL",
                "events": int(r.get("events") or 0),
                "users": int(r.get("users") or 0),
                "first_seen_version": (r.get("first_seen_version") or "").strip(),
                "last_seen_version": (r.get("last_seen_version") or "").strip(),
                "devices_top": (r.get("top_devices") or "").strip(),
                "os_top": (r.get("top_os") or "").strip(),
                # CSV 字串「1.0.9:12;1.0.8:3」轉成與 BQ 相同的結構化格式
                "version_dist": [
                    {"version": label, "events": n, "users": 0}
                    for label, n in parse_counted_list((r.get("version_dist") or "").strip())
                ],
            })
    return issues


def parse_counted_list(s: str) -> list[tuple[str, int]]:
    """解析「label:count;label:count」（count 可省略，省略則記 0）。"""
    out = []
    for part in (s or "").split(";"):
        part = part.strip()
        if not part:
            continue
        label, _, n = part.rpartition(":")
        if label and n.isdigit():
            out.append((label.strip(), int(n)))
        else:
            out.append((part, 0))
    return out


def derive_dists_from_manual(manual: list[dict], dists: dict) -> None:
    """BQ 缺資料的平台，用 manual CSV 有數量的欄位補分布（無數量的項目不硬湊，直接略過）。"""
    for platform in {i["platform"] for i in manual}:
        d = dists.setdefault(platform, {})
        for kind, field in [("device", "devices_top"), ("os", "os_top"), ("app_version", "version_dist")]:
            if d.get(kind):  # BQ 已有 → 不覆蓋
                continue
            bucket: dict[str, int] = {}
            for it in (i for i in manual if i["platform"] == platform):
                if kind == "app_version":  # version_dist 已是結構化列表
                    pairs = [(x["version"], x["events"]) for x in it.get(field) or []]
                else:
                    pairs = parse_counted_list(it.get(field, ""))
                for label, n in pairs:
                    if n > 0:
                        bucket[label] = bucket.get(label, 0) + n
            if bucket:
                d[kind] = sorted(
                    ({"label": k, "events": v, "users": 0} for k, v in bucket.items()),
                    key=lambda x: -x["events"],
                )[:30]


def dedupe(issues: list[dict]) -> list[dict]:
    """同一 Crashlytics issue 可能來自多來源：以 (platform,title,subtitle) 去重，BQ > MCP 報表 > 手動 console。"""
    rank = {"crashlytics_bq": 0, "mcp_report": 1, "manual_console": 2}
    seen: dict[tuple, dict] = {}
    for it in sorted(issues, key=lambda i: rank.get(i["source"], 9)):
        key = (it["platform"], it["title"], it["subtitle"])
        if key not in seen:
            seen[key] = it
    return sorted(seen.values(), key=lambda i: (-i["users"], -i["events"]))


def build_fix_review(prev_summary: dict | None, issues: list[dict], dists: dict) -> dict | None:
    """上期清單回顧：上期 priority_list（無則 top_issues[:10]）逐項比對本期 issues。

    狀態：resolved（本期零事件）/ old_versions_only（僅 < 該平台最新版出現，≈已修等升級）/
    still_occurring（最新版仍出現，或版本不明）。比對 key：issue_id 優先，fallback (platform,title)。
    """
    if not prev_summary:
        return None
    watch = [p for p in prev_summary.get("priority_list") or [] if p.get("issue_id")]
    source = "priority_list"
    if not watch:
        watch = (prev_summary.get("top_issues") or [])[:10]
        source = "top_issues"
    if not watch:
        return None

    latest_by_platform = {
        pf: max_version(x.get("label", "") for x in d.get("app_version") or [])
        for pf, d in dists.items()
    }
    by_id = {i["issue_id"]: i for i in issues if i.get("issue_id")}
    by_pt = {(i["platform"], i["title"]): i for i in issues}

    items = []
    for p in watch:
        cur = by_id.get(p.get("issue_id")) or by_pt.get((p.get("platform", ""), p.get("title", "")))
        platform = (cur or {}).get("platform") or p.get("platform", "")
        latest = latest_by_platform.get(platform)
        if cur is None:
            status, version_known, cur_last_seen = "resolved", True, None
        else:
            vd = cur.get("version_dist") or []
            cur_last_seen = cur.get("last_seen_version") or None
            if vd and latest:
                version_known = True
                in_latest = any(version_key(x["version"]) >= version_key(latest) for x in vd)
                status = "still_occurring" if in_latest else "old_versions_only"
            else:
                version_known, status = False, "still_occurring"
        items.append({
            "issue_id": p.get("issue_id") or (cur or {}).get("issue_id", ""),
            "title": p.get("title", ""),
            "platform": platform,
            "status": status,
            "prev": {"events": int(p.get("events") or 0), "users": int(p.get("users") or 0)},
            "cur": {"events": cur["events"], "users": cur["users"]} if cur else {"events": 0, "users": 0},
            "cur_last_seen_version": cur_last_seen,
            "latest_app_version": latest,
            "version_known": version_known,
        })
    return {"prev_month": prev_summary.get("month", ""), "source": source, "items": items}


def main() -> None:
    args = app_argparser("正規化 Crashlytics 資料").parse_args()
    app = get_app(args.app)
    odir = out_dir(args.app)

    bq = load_if_exists(odir / "crashlytics_bq.json")
    mcp = load_if_exists(odir / "mcp_report.json")  # BQ 未接時 fetch_stacktraces.py 產的替代 issue 來源
    manual = manual_csv_to_unified(ROOT / "manual" / args.app / "console_issues.csv")

    bq_issues, dists, custom_keys, weekly = bq_issues_to_unified(bq)
    mcp_issues = (mcp or {}).get("issues") or []
    # 分布補位順序同 issue rank：BQ 已有的不覆蓋，MCP 報表先補、manual 殿後
    for platform, d in ((mcp or {}).get("distributions") or {}).items():
        cur = dists.setdefault(platform, {})
        for kind, rows in d.items():
            if not cur.get(kind) and rows:
                cur[kind] = rows
    derive_dists_from_manual(manual, dists)
    issues = dedupe(bq_issues + mcp_issues + manual)
    # 週趨勢：BQ 有就用 BQ（逐事件精確）；否則用 MCP 按週切窗自建（近似，只有事件數）
    weekly = weekly or (mcp or {}).get("weekly_trend") or []

    unified = {
        "app": args.app,
        "display_name": app.get("display_name", args.app),
        "generated_at": dt.date.today().isoformat(),
        "period_days": args.days,
        "sources": {
            "crashlytics_bq": bool(bq and bq.get("tables")),
            "mcp_report": bool(mcp_issues),  # BQ 未接時的 issue 來源（fetch_stacktraces.py MCP 報表模式）
            "manual_console": bool(manual),
            # 真實 stack trace（fetch_stacktraces.py）；內容不進 unified，analyze_gemini 直接讀原檔
            "mcp_crashlytics": bool((st := load_if_exists(odir / "stacktraces.json")) and st.get("issues")),
        },
        "issues": issues,
        "distributions": dists,
        "custom_keys": custom_keys,
        "weekly_trend": weekly,
    }
    write_json(odir / "unified.json", unified)

    month = dt.date.today().strftime("%Y-%m")
    summary_dir = ROOT / "reports" / "data" / args.app
    summary_dir.mkdir(parents=True, exist_ok=True)
    fatal_events = sum(i["events"] for i in issues if i["fatal"])
    total_events = sum(i["events"] for i in issues)
    # 層級全量彙總（供儀表板層級分布圖精確取數；top_issues 只有前 15 筆）
    by_level = {lv: sum(i["events"] for i in issues if i.get("error_type") == lv) for lv in ("FATAL", "ANR", "NON_FATAL")}
    summary = {
        "month": month,
        "app": args.app,
        "display_name": unified["display_name"],
        "generated_at": unified["generated_at"],
        "kpis": {
            "events": total_events,
            "users": sum(i["users"] for i in issues),
            "fatal_share": round(fatal_events / total_events, 3) if total_events else None,
            "issue_count": len(issues),
            "events_fatal": by_level["FATAL"],
            "events_anr": by_level["ANR"],
            "events_nonfatal": by_level["NON_FATAL"],
        },
        "sources": unified["sources"],
        "top_issues": issues[:15],
        "distributions": dists,
        "weekly_trend": weekly,  # 儀表板週趨勢圖吃摘要（不吃 unified）
        "priority_list": [],  # 由 /crash-report skill 的 AI 分析補上
        "fix_review": build_fix_review(load_prev_month(args.app, month), issues, dists),
    }
    write_json(summary_dir / f"{month}.json", summary)
    print(f"  來源狀態：{unified['sources']}；issues 合計 {len(issues)} 筆")


if __name__ == "__main__":
    main()
