"""Crashlytics 資料拉取（headless 驅動 Firebase MCP server）——兩個角色：

1. 真實 stack trace（永遠）：對 score 前 N 個 issue 抓 sample event 的堆疊＋blame frame
   → out/<app>/stacktraces.json（analyze_gemini 優先使用，取代 subtitle 反猜）。
2. issue 報表（BQ 未接時）：topIssues／分布報表當 issue 清單來源
   → out/<app>/mcp_report.json（normalize 合併，rank 次於 BQ、優於 manual CSV）。
   侷限：報表無 users 分布明細（記 0）、無週趨勢與 custom keys、只回看 90 天。

讀 crash events 唯一的程式化介面是 firebase-tools 的 MCP server（stdio JSON-RPC，
底層打未公開的 firebasecrashlytics v1alpha API）。本腳本 spawn 它當子行程直接對話，
不需要互動 AI agent，可進 weekly_sync.sh。認證沿用 `firebase login` 的 user token
（service account 打 Crashlytics 一律 404，見 firebase-tools#10004）。

前置：firebase-tools ≥15.23（舊版無 crashlytics MCP 工具）、`firebase login` 已登入。
v1alpha quota 很小：call 間節流、429 退避。任何失敗都寫進輸出的 errors 並以 0 退出
（不擋 pipeline；analyze_gemini 端 fallback 回 source_snippet 猜測）。
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import time

import yaml

from analyze_gemini import score_issues
from config import ROOT, app_argparser, get_app, out_dir, write_json
from normalize import bq_issues_to_unified, load_if_exists, norm_error_type

THROTTLE_SEC = 12  # v1alpha quota 很小，call 之間固定歇一下
RETRY_WAITS = (30, 60)  # 429 退避秒數
MAX_TRACE_LINES = 40  # 每條 stack trace 截前 N 行（prompt 端另有整體 12000 字上限）
REPORT_PAGE_SIZE = 40  # topIssues 報表抓多深（需涵蓋 score top-N 的候選）
DIST_PAGE_SIZE = 10  # 分布報表各取前 N 名
WEEKLY_BACKFILL_WEEKS = 12  # 週趨勢首跑回填深度


class McpError(RuntimeError):
    pass


class McpClient:
    """最小 stdio JSON-RPC client：只做本腳本需要的 initialize / tools/*。"""

    def __init__(self) -> None:
        if not shutil.which("firebase"):
            raise McpError("找不到 firebase CLI（npm i -g firebase-tools@latest）")
        self.proc = subprocess.Popen(
            ["firebase", "experimental:mcp", "--only", "crashlytics"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
        )
        self._rid = 0
        self._request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "crash-trend", "version": "1"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = [t["name"] for t in self._request("tools/list", None).get("tools", [])]
        if "crashlytics_get_report" not in tools:
            raise McpError(f"MCP 無 crashlytics 工具——firebase-tools 太舊（需 ≥15.23）？現有：{tools[:6]}")

    def _send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict | None, timeout: int = 120) -> dict:
        self._rid += 1
        rid = self._rid
        msg: dict = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise McpError("MCP server 中斷（stdout EOF）——未登入？跑 firebase login")
            try:
                got = json.loads(line)
            except ValueError:
                continue  # server 的 log 雜訊行
            if got.get("id") == rid:
                if "error" in got:
                    raise McpError(str(got["error"])[:300])
                return got.get("result", {})
        raise McpError(f"{method} 逾時（{timeout}s）")

    def call_tool(self, name: str, args: dict) -> str:
        """呼叫工具回傳文字；429 退避重試。API 錯誤以 'Error:' 開頭文字回來，轉例外。"""
        text = ""
        for attempt in range(len(RETRY_WAITS) + 1):
            r = self._request("tools/call", {"name": name, "arguments": args})
            text = "".join(p.get("text", "") for p in r.get("content", []) if p.get("type") == "text")
            if "HTTP Error: 429" in text and attempt < len(RETRY_WAITS):
                print(f"    …429 quota，{RETRY_WAITS[attempt]}s 後重試")
                time.sleep(RETRY_WAITS[attempt])
                continue
            break
        if text.lstrip().startswith("Error"):
            raise McpError(text[:300])
        return text

    def close(self) -> None:
        try:
            self.proc.terminate()
        except OSError:
            pass


def firebase_app_ids(project: str) -> dict[str, str]:
    """`firebase apps:list` 解出 {platform: appId}（輸出有重複列，取每平台第一個）。"""
    r = subprocess.run(
        ["firebase", "apps:list", "--project", project, "--json"],
        capture_output=True, text=True, timeout=60,
    )
    data = json.loads(r.stdout[r.stdout.index("{"):])
    if data.get("status") != "success":
        raise McpError(f"apps:list 失敗：{str(data)[:200]}")
    ids: dict[str, str] = {}
    for a in data.get("result", []):
        ids.setdefault((a.get("platform") or "").lower(), a.get("appId", ""))
    return ids


def yaml_block(outer: dict, key: str):
    """MCP 回應是 YAML、巢狀內容裝在 block scalar 字串裡——取出再解一層。"""
    v = (outer or {}).get(key)
    return yaml.safe_load(v) if isinstance(v, str) else v


def truncate_trace(text: str, max_lines: int = MAX_TRACE_LINES) -> str:
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n…（截斷，原 {len(lines)} 行）"


def get_report_groups(client: McpClient, app_id: str, report: str, page: int,
                      start: dt.datetime, end: dt.datetime) -> list:
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    text = client.call_tool("crashlytics_get_report", {
        "appId": app_id, "report": report, "pageSize": page,
        "filter": {"intervalStartTime": start.strftime(fmt), "intervalEndTime": end.strftime(fmt)},
    })
    return yaml_block(yaml.safe_load(text), "groups") or []


# ---------- MCP 報表 → unified 形狀（BQ 未接時的 issue 來源） ----------

def issues_from_report(groups: list, platform: str) -> list[dict]:
    """topIssues 報表 → unified issue 物件。version_dist 留空（逐 issue 查會爆 quota）。"""
    out = []
    for g in groups:
        iss = g.get("issue") or {}
        m = (g.get("metrics") or [{}])[0]
        if not iss.get("id"):
            continue
        out.append({
            "platform": platform, "source": "mcp_report",
            "issue_id": iss["id"], "title": iss.get("title", ""), "subtitle": iss.get("subtitle", "") or "",
            "fatal": (iss.get("errorType") or "").upper() == "FATAL",
            "error_type": norm_error_type(iss.get("errorType"), (iss.get("errorType") or "").upper() == "FATAL"),
            "events": int(m.get("eventsCount") or 0), "users": int(m.get("impactedUsersCount") or 0),
            "first_seen_version": iss.get("firstSeenVersion", ""),
            "last_seen_version": iss.get("lastSeenVersion", ""),
            "version_dist": [],
        })
    return out


def dist_rows(groups: list, key: str) -> list[dict]:
    """topVersions / topOperatingSystems → 分布列。報表只給 events、無 users（記 0）。"""
    return [{
        "label": (g.get(key) or {}).get("displayName", "?"),
        "events": int((g.get("metrics") or [{}])[0].get("eventsCount") or 0), "users": 0,
    } for g in groups]


def device_rows(groups: list) -> list[dict]:
    """機型報表兩種形狀：topAndroidDevices 扁平（device 直接在 group）、
    topAppleDevices 巢狀（廠牌群組下一層 subgroups 才是機型）。"""
    rows = []
    for g in groups:
        for item in ([g] if g.get("device") else g.get("subgroups") or []):
            d = item.get("device") or {}
            rows.append({
                "label": d.get("marketingName") or d.get("displayName", "?"),
                "events": int((item.get("metrics") or [{}])[0].get("eventsCount") or 0), "users": 0,
            })
    return rows


def fetch_mcp_report(client: McpClient, app_ids: dict, days: int, errors: dict, report_cache: dict) -> tuple[list, dict]:
    """BQ 未接時：每平台 4 個 call（topIssues＋三種分布報表）。單一報表失敗記 errors 不中斷。"""
    issues, dists = [], {}

    end = dt.datetime.now(dt.timezone.utc)
    start = end - dt.timedelta(days=days)

    def safe(app_id: str, platform: str, report: str, page: int) -> list:
        try:
            return get_report_groups(client, app_id, report, page, start, end)
        except McpError as e:
            errors[f"{platform}:{report}"] = str(e)[:200]
            print(f"[注意] {platform} {report} 報表失敗：{str(e)[:120]}")
            return []
        finally:
            time.sleep(THROTTLE_SEC)

    for platform in ("android", "ios"):
        app_id = app_ids.get(platform)
        if not app_id:
            continue
        print(f"  MCP 報表 {platform}…")
        groups = safe(app_id, platform, "topIssues", REPORT_PAGE_SIZE)
        report_cache[platform] = groups
        issues += issues_from_report(groups, platform)
        dists[platform] = {
            "app_version": dist_rows(safe(app_id, platform, "topVersions", DIST_PAGE_SIZE), "version"),
            "os": dist_rows(safe(app_id, platform, "topOperatingSystems", DIST_PAGE_SIZE), "operatingSystem"),
            "device": device_rows(safe(app_id, platform, "topAppleDevices" if platform == "ios" else "topAndroidDevices", DIST_PAGE_SIZE)),
        }
    return issues, dists


def fetch_weekly_trend(client: McpClient, app_name: str, app_ids: dict, errors: dict) -> list[dict]:
    """MCP 模式的週趨勢：以週一為窗起點按週切窗，每窗每平台 1 call。
    週事件數＝topOperatingSystems(pageSize 20) 各組 eventsCount 加總（低基數報表，近似總量；
    無法跨窗去重用戶，users 記 0）。key 用窗起點 strftime('%Y-%W')，對齊 BQ（fetch_bigquery %Y-%W）。

    歷史存 reports/data/<app>/weekly/mcp.json（committed，自有歷史庫）。幂等規則：
    fetched_at < 該週結束時間 的週視為不完整、一律重抓——weekly_sync 週一早上跑，
    當週只有數小時資料，下次必須覆蓋；已完整的週跳過。首跑自動回填 12 週,缺漏下次補。"""
    hist_dir = ROOT / "reports" / "data" / app_name / "weekly"
    hist_dir.mkdir(parents=True, exist_ok=True)
    hist_path = hist_dir / "mcp.json"
    hist: dict = load_if_exists(hist_path) or {}

    now = dt.datetime.now(dt.timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    this_monday = (now - dt.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    fetched = 0
    for k in range(WEEKLY_BACKFILL_WEEKS):
        monday = this_monday - dt.timedelta(weeks=k)
        week_end = monday + dt.timedelta(days=7)
        key = monday.strftime("%Y-%W")
        entry = hist.get(key)
        if entry and entry.get("fetched_at", "") >= week_end.strftime("%Y-%m-%dT%H:%M:%SZ"):
            continue  # 抓取時該週已結束 → 資料完整，跳過
        events: dict = (entry or {}).get("events", {})
        ok = True
        for platform in ("android", "ios"):
            app_id = app_ids.get(platform)
            if not app_id:
                continue
            try:
                groups = get_report_groups(client, app_id, "topOperatingSystems", 20, monday, min(week_end, now))
                events[platform] = sum(int((g.get("metrics") or [{}])[0].get("eventsCount") or 0) for g in groups)
            except McpError as e:
                errors[f"weekly:{key}:{platform}"] = str(e)[:150]
                ok = False
            time.sleep(THROTTLE_SEC)
        if ok or events:
            hist[key] = {"events": events, "fetched_at": now_iso}
            fetched += 1
    write_json(hist_path, dict(sorted(hist.items())))
    print(f"  週趨勢：本次抓 {fetched} 週（歷史共 {len(hist)} 週）")
    return [{"week": wk, "platform": p, "events": n, "users": 0}
            for wk, e in sorted(hist.items()) for p, n in e.get("events", {}).items()]


# ---------- stack trace 拉取 ----------

def fetch_platform(client: McpClient, app_id: str, wanted_ids: list[str], days: int, groups: list | None) -> tuple[dict, list[str]]:
    """一個平台最多 2 個 call：topIssues 對映 issue→sampleEvent（可用快取）→ 批次抓 events。"""
    if groups is None:
        end = dt.datetime.now(dt.timezone.utc)
        groups = get_report_groups(client, app_id, "topIssues", REPORT_PAGE_SIZE,
                                   end - dt.timedelta(days=days), end)
        time.sleep(THROTTLE_SEC)
    sample_by_issue = {}
    for g in groups:
        iss = g.get("issue") or {}
        if iss.get("id") and iss.get("sampleEvent"):
            sample_by_issue[iss["id"]] = iss["sampleEvent"]
    names = [sample_by_issue[i] for i in wanted_ids if i in sample_by_issue]
    missing = [i for i in wanted_ids if i not in sample_by_issue]
    if not names:
        return {}, missing

    events = yaml.safe_load(client.call_tool("crashlytics_batch_get_events", {"appId": app_id, "names": names})) or []
    got: dict[str, dict] = {}
    for ev in events:
        iid = (yaml_block(ev, "issue") or {}).get("id")
        trace = (ev.get("exceptions") or ev.get("threads") or "").strip()
        if not (iid and trace):
            continue
        got[iid] = {
            "stack_trace": truncate_trace(trace),
            "blame_frame": yaml_block(ev, "blameFrame") or {},
            "sample_event": (ev.get("name") or "").strip(),
        }
    return got, missing


def main() -> None:
    p = app_argparser("拉取 Crashlytics 真實 stack trace／（BQ 未接時）issue 報表（headless MCP）")
    p.add_argument("--top", type=int, default=10, help="抓 score 排序前 N 個 issue 的 stack trace（預設 10）")
    args = p.parse_args()
    app = get_app(args.app)
    days = min(args.days, 89)  # API 只接受最近 90 天內的區間
    odir = out_dir(args.app)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    result: dict = {
        "app": args.app, "generated_at": now, "period_days": days,
        "app_ids": {}, "issues": {}, "missing": [], "errors": {},
    }

    def bail(key: str, msg: str) -> None:
        """fail loud 但不擋 pipeline：記 errors、寫檔、以 0 退出。"""
        result["errors"][key] = msg
        print(f"[注意] {msg}")
        write_json(odir / "stacktraces.json", result)

    bq = load_if_exists(odir / "crashlytics_bq.json")
    bq_mode = bool(bq and bq.get("tables"))
    mcp_report_path = odir / "mcp_report.json"

    try:
        result["app_ids"] = firebase_app_ids(app["firebase_project"])
    except Exception as e:  # apps:list 失敗原因多樣（未登入/無權限/CLI 缺），一律記下不擋
        return bail("apps_list", f"解 Firebase app_id 失敗：{e}")

    try:
        client = McpClient()
    except McpError as e:
        return bail("mcp", f"MCP server 啟動失敗：{e}")

    report_cache: dict[str, list] = {}
    try:
        if bq_mode:
            issues, _, _, _ = bq_issues_to_unified(bq)
            if mcp_report_path.exists():  # BQ 回來了就撤掉舊報表，避免陳舊數字混進合併
                mcp_report_path.unlink()
                print("  （BQ 有資料，已移除舊 mcp_report.json）")
        else:
            print("  BQ 無資料 → 改用 MCP 報表當 issue 來源")
            issues, dists = fetch_mcp_report(client, result["app_ids"], days, result["errors"], report_cache)
            mcp_payload = {
                "app": args.app, "generated_at": now, "period_days": days,
                "issues": issues, "distributions": dists, "weekly_trend": [], "errors": dict(result["errors"]),
            }
            write_json(mcp_report_path, mcp_payload)

        scored = [i for i in score_issues(issues, [], app.get("core_paths", []))[: args.top] if i.get("issue_id")]
        if not scored:
            return bail("issues", "本期無 issue（BQ 與 MCP 報表皆空）；不抓 stack trace")

        for platform in ("android", "ios"):
            wanted = [i for i in scored if i["platform"] == platform]
            if not wanted:
                continue
            app_id = result["app_ids"].get(platform)
            if not app_id:
                result["errors"][platform] = f"專案無 {platform} app，略過 {len(wanted)} 個 issue"
                continue
            print(f"  抓 {platform} stack trace：{len(wanted)} issues…")
            try:
                got, missing = fetch_platform(client, app_id, [i["issue_id"] for i in wanted], days,
                                              report_cache.get(platform))
            except McpError as e:
                result["errors"][platform] = str(e)
                print(f"[注意] {platform} 抓取失敗：{e}")
                continue
            titles = {i["issue_id"]: i["title"] for i in wanted}
            for iid, data in got.items():
                result["issues"][iid] = {"platform": platform, "title": titles.get(iid, ""), **data}
            result["missing"] += missing
            time.sleep(THROTTLE_SEC)

        if not bq_mode:
            # 週趨勢放最後：quota 極小，核心產出（issue 清單、stack trace）優先吃
            mcp_payload["weekly_trend"] = fetch_weekly_trend(client, args.app, result["app_ids"], result["errors"])
            mcp_payload["errors"] = dict(result["errors"])
            write_json(mcp_report_path, mcp_payload)
    finally:
        client.close()

    if result["missing"]:
        print(f"[注意] {len(result['missing'])} 個 issue 不在 topIssues 前 {REPORT_PAGE_SIZE} 名內，無 sampleEvent：{result['missing']}")
    write_json(odir / "stacktraces.json", result)
    print(f"  ✓ stack trace {len(result['issues'])}/{len(scored)} 個 issue")


if __name__ == "__main__":
    main()
