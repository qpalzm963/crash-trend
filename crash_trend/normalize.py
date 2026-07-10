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

from config import ROOT, app_argparser, get_app, out_dir, write_json


def load_if_exists(path: Path) -> dict | None:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def bq_issues_to_unified(bq: dict) -> tuple[list[dict], dict, list[dict], list[dict]]:
    issues, dists, custom_keys, weekly = [], {}, [], []
    for table, data in (bq or {}).get("tables", {}).items():
        platform = "ios" if table.endswith("_IOS") else "android"
        for it in data.get("top_issues", []):
            issues.append({
                "platform": platform,
                "source": "crashlytics_bq",
                "issue_id": it.get("issue_id", ""),
                "title": it.get("issue_title", ""),
                "subtitle": it.get("issue_subtitle", ""),
                "fatal": (it.get("error_type") or "").upper() == "FATAL",
                "events": int(it.get("events", 0)),
                "users": int(it.get("users", 0)),
                "first_seen_version": it.get("first_seen_version", ""),
                "last_seen_version": it.get("last_seen_version", ""),
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
                "fatal": (r.get("fatal") or "").strip().lower() in ("yes", "true", "y", "1"),
                "events": int(r.get("events") or 0),
                "users": int(r.get("users") or 0),
                "first_seen_version": (r.get("first_seen_version") or "").strip(),
                "last_seen_version": (r.get("last_seen_version") or "").strip(),
                "devices_top": (r.get("top_devices") or "").strip(),
                "os_top": (r.get("top_os") or "").strip(),
                "version_dist": (r.get("version_dist") or "").strip(),
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
                for label, n in parse_counted_list(it.get(field, "")):
                    if n > 0:
                        bucket[label] = bucket.get(label, 0) + n
            if bucket:
                d[kind] = sorted(
                    ({"label": k, "events": v, "users": 0} for k, v in bucket.items()),
                    key=lambda x: -x["events"],
                )[:30]


def dedupe(issues: list[dict]) -> list[dict]:
    """同一 Crashlytics issue 可能同時來自 BQ 與手動 console：以 (platform,title,subtitle) 去重，BQ 優先。"""
    rank = {"crashlytics_bq": 0, "manual_console": 1}
    seen: dict[tuple, dict] = {}
    for it in sorted(issues, key=lambda i: rank.get(i["source"], 9)):
        key = (it["platform"], it["title"], it["subtitle"])
        if key not in seen:
            seen[key] = it
    return sorted(seen.values(), key=lambda i: (-i["users"], -i["events"]))


def main() -> None:
    args = app_argparser("正規化 Crashlytics 資料").parse_args()
    app = get_app(args.app)
    odir = out_dir(args.app)

    bq = load_if_exists(odir / "crashlytics_bq.json")
    manual = manual_csv_to_unified(ROOT / "manual" / args.app / "console_issues.csv")

    bq_issues, dists, custom_keys, weekly = bq_issues_to_unified(bq)
    derive_dists_from_manual(manual, dists)
    issues = dedupe(bq_issues + manual)

    unified = {
        "app": args.app,
        "display_name": app.get("display_name", args.app),
        "generated_at": dt.date.today().isoformat(),
        "period_days": args.days,
        "sources": {
            "crashlytics_bq": bool(bq and bq.get("tables")),
            "manual_console": bool(manual),
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
        },
        "sources": unified["sources"],
        "top_issues": issues[:15],
        "distributions": dists,
        "priority_list": [],  # 由 /crash-report skill 的 AI 分析補上
    }
    write_json(summary_dir / f"{month}.json", summary)
    print(f"  來源狀態：{unified['sources']}；issues 合計 {len(issues)} 筆")


if __name__ == "__main__":
    main()
