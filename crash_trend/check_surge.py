"""Crash 暴增偵測：上一個完整週事件數 ≥ N 倍於前一週 → 發即時告警卡到聊天室。

與月報卡（post_report.py）分開：月報是月頻摘要，這支跟著週同步跑、逮突發異常
（如某版上線後單週事件暴增十幾倍——等月卡才知道就晚了）。

判斷邏輯（純程式，無 AI）：
  - 資料源 unified.json 的 weekly_trend（BQ 精確或 MCP 近似，normalize 已統一）
  - 只比「完整週」：排除本週（進行中，數字必然偏低）；取最近兩個完整週
  - 觸發門檻：ratio ≥ SURGE_RATIO（預設 2）且事件數 ≥ SURGE_MIN_EVENTS（預設 500，
    小量 app 的 2→5 件雜訊不觸發）
  - 同一週只告警一次（out/<app>/.surge_alerted 記上次告警的週 key）

環境變數：CRASH_REPORT_URL / INTERNAL_API_TOKEN（同 post_report）、DASHBOARD_URL、
SURGE_RATIO、SURGE_MIN_EVENTS。未設 CRASH_REPORT_URL＝跳過。失敗不擋 pipeline。

每日檢查（另一套，發到聊天室 Webhook，見 chat_webhook.py）：
  - 各平台分開算（大平台的量會蓋掉小平台的暴增）
  - 某一天事件數 ≥ DAILY_SURGE_RATIO（預設 3）倍「前 7 天平均」且 ≥ DAILY_SURGE_MIN_EVENTS（預設 50）
  - 看「昨天」與「前天」：BigQuery 匯出晚一天才補齊，昨天常常還不完整（偏低、不會誤報），
    隔天它變成「前天」時用完整數字再判一次
  - 同一天同一平台只告警一次（out/<app>/.daily_surge_alerted）
  - 未設該 app 的 CHAT_WEBHOOK_<APP>＝跳過
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

import requests
from chat_webhook import send_text, webhook_env_name, webhook_url
from config import app_argparser, get_app, out_dir

DAILY_MARK_KEEP = 60  # 告警紀錄只留最近幾筆，避免檔案無限長大


def weekly_totals_from_daily(daily_trend: list[dict]) -> dict[str, int]:
    """從 Dashboard V2 daily_trend (date, crash_events) → {週key: 總事件數}。"""
    totals: dict[str, int] = {}
    for r in daily_trend or []:
        date_str = r.get("date")
        if not date_str:
            continue
        try:
            d = dt.date.fromisoformat(date_str[:10])
            wk = (d - dt.timedelta(days=d.weekday())).strftime("%Y-%W")
            totals[wk] = totals.get(wk, 0) + int(r.get("crash_events", 0))
        except Exception:
            continue
    return totals


def weekly_totals(unified: dict) -> dict[str, int]:
    """weekly_trend（各平台分列）→ {週key: 總事件數}。"""
    totals: dict[str, int] = {}
    for r in unified.get("weekly_trend") or []:
        wk = r.get("week")
        if wk:
            totals[wk] = totals.get(wk, 0) + int(r.get("events") or 0)
    return totals


def load_daily_trend(odir, app: str) -> list[dict]:
    """Dashboard V2 的 daily_trend（優先 app_v2.json，其次 dashboard_v2.json）。"""
    for v2_name in ["app_v2.json", "dashboard_v2.json"]:
        v2_path = odir / v2_name
        if v2_path.exists():
            try:
                v2_data = json.loads(v2_path.read_text(encoding="utf-8"))
                app_obj = v2_data.get("apps", {}).get(app, v2_data)
                daily = app_obj.get("daily_trend") or []
                if daily:
                    return daily
            except Exception:
                pass
    return []


def find_daily_surges(
    daily_trend: list[dict],
    today: dt.date,
    ratio_min: float = 3.0,
    events_min: int = 50,
    lookback_days: int = 2,
    baseline_days: int = 7,
) -> list[dict]:
    """找出「昨天、前天」中，哪個平台的事件數暴增。

    基準＝該日之前 baseline_days 天的平均；基準期有任何一天沒資料就不判（歷史不足）。
    基準為 0 也不判：沒有可比的正常量。
    平台第一次出現事件的日子晚於基準期起點也不判：daily_trend 會把沒資料的日子補成 0，
    平台剛開始匯出時前幾天的 0 不是「正常量很低」，拿來當基準會把正常的一天誤判成暴增。
    """
    per_day: dict[dt.date, dict[str, int]] = {}
    for r in daily_trend or []:
        try:
            d = dt.date.fromisoformat(str(r.get("date"))[:10])
        except ValueError:
            continue
        by_platform = r.get("by_platform") or {}
        if by_platform:
            per_day[d] = {p: int((v or {}).get("events") or 0) for p, v in by_platform.items()}
        else:
            per_day[d] = {"all": int(r.get("crash_events") or 0)}

    first_seen: dict[str, dt.date] = {}
    for d in sorted(per_day):
        for platform, events in per_day[d].items():
            if events > 0 and platform not in first_seen:
                first_seen[platform] = d

    surges: list[dict] = []
    for offset in range(1, lookback_days + 1):
        day = today - dt.timedelta(days=offset)
        if day not in per_day:
            continue
        base_dates = [day - dt.timedelta(days=i) for i in range(1, baseline_days + 1)]
        if any(b not in per_day for b in base_dates):
            continue
        for platform, events in sorted(per_day[day].items()):
            if first_seen.get(platform, day) > base_dates[-1]:
                continue
            baseline = sum(per_day[b].get(platform, 0) for b in base_dates) / baseline_days
            if baseline > 0 and events >= events_min and events >= ratio_min * baseline:
                surges.append({
                    "date": day.isoformat(),
                    "platform": platform,
                    "events": events,
                    "baseline": round(baseline, 1),
                    "ratio": round(events / baseline, 1),
                })
    return surges


def top_issue_on(bq: dict, date: str, platform: str) -> dict | None:
    """從 crashlytics_bq.json 找出某天某平台事件最多的 issue（找不到就回 None，不擋告警）。"""
    best: dict | None = None
    for table, data in (bq.get("tables") or {}).items():
        if platform != "all" and platform.upper() not in table.upper().split("_"):
            continue
        titles = {str(i.get("issue_id")): i for i in (data.get("top_issues") or [])}
        for row in data.get("issue_daily_trend") or []:
            if str(row.get("date"))[:10] != date:
                continue
            events = int(row.get("events") or 0)
            if best is None or events > best["events"]:
                meta = titles.get(str(row.get("issue_id"))) or {}
                versions = row.get("versions_json")
                if isinstance(versions, str):
                    try:
                        versions = json.loads(versions)
                    except ValueError:
                        versions = None
                top_version = None
                if isinstance(versions, list) and versions:
                    top_version = max(versions, key=lambda v: int(v.get("events") or 0)).get("version")
                best = {
                    "title": meta.get("issue_title") or meta.get("title") or str(row.get("issue_id")),
                    "events": events,
                    "version": top_version,
                }
    return best


PLATFORM_ZH = {"ios": "iOS", "android": "Android", "all": "全平台"}


def format_daily_surge(display_name: str, surge: dict, top: dict | None, dashboard_url: str) -> str:
    lines = [
        f"🚨 *{display_name} {PLATFORM_ZH.get(surge['platform'], surge['platform'])} 閃退暴增*",
        f"{surge['date']}：{surge['events']:,} 次（前 7 天平均 {surge['baseline']:,} 次，約 {surge['ratio']} 倍）",
    ]
    if top:
        ver = f"（{top['version']}）" if top.get("version") else ""
        lines.append(f"主要來源：{top['title']}{ver} {top['events']:,} 次")
    if dashboard_url:
        lines.append(f"<{dashboard_url}|打開儀表板>")
    return "\n".join(lines)


def daily_check(app: str, odir, daily: list[dict], today: dt.date) -> str | None:
    """每日暴增檢查；回傳錯誤訊息（發送失敗）或 None。"""
    url = webhook_url(app)
    if not url:
        print(f"  （未設 {webhook_env_name(app)}，跳過每日暴增檢查）")
        return None
    ratio_min = float(os.environ.get("DAILY_SURGE_RATIO", "3"))
    events_min = int(os.environ.get("DAILY_SURGE_MIN_EVENTS", "50"))
    surges = find_daily_surges(daily, today, ratio_min=ratio_min, events_min=events_min)
    if not surges:
        print(f"  每日檢查：昨天、前天無暴增（門檻 ×{ratio_min:g} 且 ≥{events_min}）")
        return None

    mark = odir / ".daily_surge_alerted"
    try:
        alerted = json.loads(mark.read_text(encoding="utf-8")) if mark.exists() else []
    except ValueError:
        alerted = []
    bq_path = odir / "crashlytics_bq.json"
    try:
        bq = json.loads(bq_path.read_text(encoding="utf-8")) if bq_path.exists() else {}
    except ValueError:
        bq = {}
    display_name = get_app(app).get("display_name", app)
    dashboard = os.environ.get("DASHBOARD_URL", "")
    dashboard_url = f"{dashboard}#{app}" if dashboard else ""

    error = None
    for s in surges:
        key = f"{s['date']}:{s['platform']}"
        if key in alerted:
            print(f"  （{key} 已告警過，跳過）")
            continue
        text = format_daily_surge(display_name, s, top_issue_on(bq, s["date"], s["platform"]), dashboard_url)
        try:
            send_text(url, text)
        except Exception as e:  # noqa: BLE001 - 失敗要回報，但不影響其他平台
            error = f"每日暴增告警發送失敗（{key}）：{e}"
            print(f"  [錯誤] {error}", file=sys.stderr)
            continue
        alerted.append(key)
        print(f"  🚨 已發每日暴增告警：{key} 事件 {s['events']}（前 7 天平均 {s['baseline']} 的 {s['ratio']} 倍）")
    mark.write_text(json.dumps(alerted[-DAILY_MARK_KEEP:]), encoding="utf-8")
    return error


def main() -> None:
    args = app_argparser("Crash 暴增偵測（週比較＋每日檢查）").parse_args()
    odir = out_dir(args.app)
    daily = load_daily_trend(odir, args.app)
    # 週告警失敗（sys.exit 或網路例外）先接住，讓每日檢查照樣跑，最後再一起回報失敗
    weekly_error = None
    try:
        weekly_check(args, odir, daily)
    except SystemExit as e:
        if e.code not in (None, 0):
            weekly_error = str(e.code)
    except Exception as e:  # noqa: BLE001 - 例如 requests 連線錯誤
        weekly_error = f"週暴增檢查失敗：{e}"
    daily_error = daily_check(args.app, odir, daily, dt.date.today())
    errors = [e for e in (weekly_error, daily_error) if e]
    if errors:
        sys.exit("；".join(errors))


def weekly_check(args, odir, daily: list[dict]) -> None:
    url = os.environ.get("CRASH_REPORT_URL")
    if not url:
        print("  （未設 CRASH_REPORT_URL，跳過暴增偵測）")
        return

    ratio_min = float(os.environ.get("SURGE_RATIO", "2"))
    events_min = int(os.environ.get("SURGE_MIN_EVENTS", "500"))

    totals: dict[str, int] = weekly_totals_from_daily(daily) if daily else {}

    if not totals:
        unified_path = odir / "unified.json"
        if unified_path.exists():
            totals = weekly_totals(json.loads(unified_path.read_text(encoding="utf-8")))

    if not totals:
        print("  （無足夠趨勢資料，跳過暴增偵測）")
        return

    # 排除本週（進行中）；週 key 與 BQ/fetch_stacktraces 同用「週一起點 %Y-%W」
    now = dt.datetime.now(dt.UTC)
    this_week = (now - dt.timedelta(days=now.weekday())).strftime("%Y-%W")
    weeks = sorted(k for k in totals if k < this_week)
    if len(weeks) < 2:
        print("  （完整週不足 2 週，無從比較）")
        return
    last, prev = weeks[-1], weeks[-2]
    last_n, prev_n = totals[last], totals[prev]

    if not (prev_n > 0 and last_n >= events_min and last_n >= prev_n * ratio_min):
        print(f"  無暴增（{prev}: {prev_n} → {last}: {last_n}；門檻 ×{ratio_min} 且 ≥{events_min}）")
        return

    mark = out_dir(args.app) / ".surge_alerted"
    if mark.exists() and mark.read_text().strip() == last:
        print(f"  （週 {last} 已告警過，跳過）")
        return

    ratio = round(last_n / prev_n, 1)
    dashboard = os.environ.get("DASHBOARD_URL", "")
    payload = {
        "type": "surge_alert",
        "app": args.app,
        "display_name": get_app(args.app).get("display_name", args.app),
        "week": last,
        "events": last_n,
        "prev_events": prev_n,
        "ratio": ratio,
        "dashboard_url": f"{dashboard}#{args.app}" if dashboard else "",
    }
    r = requests.post(url, json=payload,
                      headers={"x-internal-token": os.environ.get("INTERNAL_API_TOKEN", "")}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"[錯誤] 告警發送失敗 {r.status_code}：{r.text[:200]}")
    mark.write_text(last)
    print(f"  🚨 已發暴增告警：週 {last} 事件 {last_n}（前一週 {prev_n} 的 {ratio} 倍）")


if __name__ == "__main__":
    main()
