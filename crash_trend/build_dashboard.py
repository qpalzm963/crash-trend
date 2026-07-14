"""產生自包含靜態儀表板 dashboard.html。

讀 reports/data/<app>/*.json（各月摘要，committed），內嵌 Chart.js 與資料，
file:// 離線可開、無 CDN。每月跑 normalize 後重跑本腳本即可更新。
視覺方向：「黑盒子飛航記錄器」儀表面板 — 近黑底、等寬讀數、訊號紅僅用於 fatal。
"""

from __future__ import annotations

import json

from config import ROOT, load_config

VENDOR_JS = ROOT / "vendor" / "chart.umd.min.js"
OUT_HTML = ROOT / "dashboard.html"


def collect_data() -> dict:
    cfg = load_config()
    apps: dict = {}
    for name, app in (cfg.get("apps") or {}).items():
        months = {}
        data_dir = ROOT / "reports" / "data" / name
        if data_dir.is_dir():
            for f in sorted(data_dir.glob("*.json")):
                months[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        if months:
            apps[name] = {"display_name": app.get("display_name", name), "months": months}
    return {"apps": apps}


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crash 趨勢儀表板</title>
<style>
  :root { /* 預設：日間工程圖紙（light） */
    --bg: #f1efe9; --panel: #fbfaf7; --panel-2: #f6f4ee; --line: #d9d4c6; --line-soft: #e6e2d5;
    --ink: #20293a; --dim: #5d6879; --faint: #9aa2ae;
    --live: #0e9f6e; --amber: #b97f0f; --red: #d43d33; --blue: #2f6db8; --steel: #6b7a90;
    --grid: rgba(47,77,120,.055); --glow-a: rgba(47,109,184,.06); --glow-b: rgba(14,159,110,.05);
    --noise-op: .35; --screw: rgba(0,0,0,.06); --hover: rgba(47,109,184,.05);
    --red-soft: rgba(212,61,51,.08); --red-line: rgba(212,61,51,.35);
    --amber-soft: rgba(185,127,15,.09); --amber-line: rgba(185,127,15,.35);
    --bar-and: rgba(91,124,166,.85); --bar-ios: rgba(47,109,184,.7);
    --trend-fill: rgba(212,61,51,.09);
    --mono: "SF Mono", ui-monospace, Menlo, Consolas, monospace;
    --sans: "PingFang TC", "Noto Sans TC", "Microsoft JhengHei", sans-serif;
  }
  [data-theme="dark"] { /* 夜間面板（原深色版，右上 ◐ 切換） */
    --bg: #0a0e15; --panel: #0f141f; --panel-2: #131a28; --line: #1c2536; --line-soft: #161e2d;
    --ink: #dae4f2; --dim: #6b7890; --faint: #414d63;
    --live: #3ddc97; --amber: #ffb454; --red: #ff5f56; --blue: #6ea8fe; --steel: #56719f;
    --grid: rgba(255,255,255,.022); --glow-a: rgba(110,168,254,.075); --glow-b: rgba(61,220,151,.05);
    --noise-op: .5; --screw: rgba(255,255,255,.04); --hover: rgba(110,168,254,.04);
    --red-soft: rgba(255,95,86,.07); --red-line: rgba(255,95,86,.4);
    --amber-soft: rgba(255,180,84,.06); --amber-line: rgba(255,180,84,.35);
    --bar-and: rgba(86,113,159,.85); --bar-ios: rgba(110,168,254,.75);
    --trend-fill: rgba(255,95,86,.12);
  }
  * { box-sizing: border-box; margin: 0; }
  html { -webkit-font-smoothing: antialiased; }
  body {
    background:
      radial-gradient(1100px 480px at 18% -8%, var(--glow-a), transparent 60%),
      radial-gradient(900px 420px at 100% 0%, var(--glow-b), transparent 55%),
      linear-gradient(var(--grid) 1px, transparent 1px),
      linear-gradient(90deg, var(--grid) 1px, transparent 1px),
      var(--bg);
    background-size: auto, auto, 34px 34px, 34px 34px, auto;
    color: var(--ink); font: 14px/1.65 var(--sans);
    min-height: 100vh; padding: clamp(16px, 3vw, 40px);
    transition: background-color .25s, color .25s;
  }
  body::after { /* 細噪點，儀器面板質感 */
    content: ""; position: fixed; inset: 0; pointer-events: none; opacity: var(--noise-op); mix-blend-mode: overlay;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2'/%3E%3CfeColorMatrix values='0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 .04 0'/%3E%3C/filter%3E%3Crect width='120' height='120' filter='url(%23n)'/%3E%3C/svg%3E");
  }
  .wrap { max-width: 1280px; margin: 0 auto; }

  /* ── 頁首：記錄器狀態列 ─────────────────────────── */
  header { display: flex; flex-wrap: wrap; gap: 16px 28px; align-items: flex-end;
           border-bottom: 1px solid var(--line); padding-bottom: 18px; margin-bottom: 26px; }
  .brand .kicker { font: 700 11px/1 var(--mono); letter-spacing: .32em; color: var(--dim); }
  .brand h1 { font: 700 clamp(22px, 3vw, 30px)/1.15 var(--sans); letter-spacing: .02em; margin-top: 8px; }
  .brand h1 .accent { color: var(--red); }
  .statusline { font: 12px/1 var(--mono); color: var(--dim); display: flex; gap: 18px; flex-wrap: wrap;
                padding-bottom: 6px; }
  .statusline b { color: var(--ink); font-weight: 600; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 7px;
         vertical-align: 0; background: var(--faint); }
  .dot.live { background: var(--live); box-shadow: 0 0 0 0 rgba(61,220,151,.5); animation: ping 2.2s infinite; }
  .dot.standby { background: var(--amber); animation: blink 1.6s steps(1) infinite; }
  @keyframes ping { 0% { box-shadow: 0 0 0 0 rgba(61,220,151,.45); } 70% { box-shadow: 0 0 0 9px rgba(61,220,151,0); } 100% { box-shadow: 0 0 0 0 rgba(61,220,151,0); } }
  @keyframes blink { 50% { opacity: .25; } }
  .tabs { margin-left: auto; display: flex; gap: 8px; flex-wrap: wrap; padding-bottom: 2px; }
  .tab { font: 600 12px/1 var(--sans); letter-spacing: .05em; color: var(--dim); cursor: pointer;
         padding: 9px 16px; border: 1px solid var(--line); border-radius: 3px; background: transparent;
         transition: color .15s, border-color .15s, background .15s; }
  .tab:hover { color: var(--ink); border-color: var(--steel); }
  .tab.active { color: var(--ink); border-color: var(--steel); background: var(--panel-2);
                box-shadow: inset 0 -2px 0 var(--live); }

  /* ── 區塊標題（技術編號） ───────────────────────── */
  .sec { display: flex; align-items: baseline; gap: 12px; margin: 30px 0 14px; }
  .sec .no { font: 700 11px/1 var(--mono); color: var(--faint); letter-spacing: .1em; }
  .sec h2 { font: 700 13px/1 var(--sans); letter-spacing: .18em; color: var(--dim); }
  .sec .rule { flex: 1; height: 1px; background: linear-gradient(90deg, var(--line), transparent); }
  .sec .hint { font: 11px/1 var(--mono); color: var(--faint); }

  /* ── KPI 儀表卡 ────────────────────────────────── */
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
  .card { background: linear-gradient(180deg, var(--panel-2), var(--panel));
          border: 1px solid var(--line); border-radius: 4px; position: relative; }
  .card::before { /* 面板螺絲角標 */
    content: ""; position: absolute; top: 8px; right: 8px; width: 5px; height: 5px;
    border-radius: 50%; background: var(--line); box-shadow: 0 0 0 1.5px var(--screw);
  }
  .kpi { padding: 18px 18px 16px; overflow: hidden; }
  .kpi .l { font: 600 10px/1 var(--mono); letter-spacing: .22em; color: var(--dim); }
  .kpi .v { font: 700 clamp(30px, 3.2vw, 40px)/1.1 var(--mono); font-variant-numeric: tabular-nums;
            margin: 10px 0 6px; letter-spacing: -.01em; }
  .kpi .v .unit { font-size: 13px; color: var(--dim); font-weight: 600; margin-left: 4px; letter-spacing: .1em; }
  .kpi .d { font: 11px/1.4 var(--mono); color: var(--faint); min-height: 15px; }
  .kpi .d .up { color: var(--red); } .kpi .d .down { color: var(--live); } .kpi .d .flat { color: var(--faint); }
  .kpi .meter { height: 3px; background: var(--line-soft); border-radius: 2px; margin-top: 12px; overflow: hidden; }
  .kpi .meter i { display: block; height: 100%; width: 0; border-radius: 2px;
                  background: linear-gradient(90deg, var(--amber), var(--red));
                  transition: width 1.1s cubic-bezier(.2,.8,.2,1) .35s; }

  /* ── 圖表面板 ──────────────────────────────────── */
  .charts { display: grid; gap: 12px; grid-template-columns: repeat(12, 1fr); }
  .chart-card { padding: 16px 18px 14px; }
  .chart-card h3 { font: 600 10px/1 var(--mono); letter-spacing: .2em; color: var(--dim); margin-bottom: 14px; }
  .chart-card h3 .src { color: var(--faint); letter-spacing: .05em; text-transform: none; }
  .span6 { grid-column: span 6; } .span4 { grid-column: span 4; } .span8 { grid-column: span 8; }
  @media (max-width: 940px) { .span6, .span4, .span8 { grid-column: span 12; } }
  .chart-box { position: relative; height: 230px; }
  .chart-box canvas { position: absolute; inset: 0; }
  .nodata { position: absolute; inset: 0; display: grid; place-content: center;
            font: 11px/1 var(--mono); letter-spacing: .3em; color: var(--faint); }
  .nodata[hidden] { display: none; }
  .nodata::before, .nodata::after { content: "——"; margin: 0 10px; color: var(--line); }

  /* ── 資料來源面板 ──────────────────────────────── */
  .srcs { padding: 16px 18px; display: flex; flex-direction: column; gap: 10px; }
  .srcrow { display: flex; align-items: center; gap: 10px; font: 12px/1.3 var(--mono); color: var(--dim); }
  .srcrow b { color: var(--ink); font-weight: 600; }
  .srcrow .state { margin-left: auto; font-size: 10px; letter-spacing: .18em; }
  .on  .state { color: var(--live); } .off .state { color: var(--faint); }
  .srcnote { font: 11px/1.6 var(--sans); color: var(--faint); border-top: 1px dashed var(--line);
             padding-top: 10px; margin-top: 2px; }

  /* ── 優先修復清單 ──────────────────────────────── */
  .prio-list { display: flex; flex-direction: column; gap: 10px; }
  details.prio { border: 1px solid var(--line); border-left: 3px solid var(--amber);
                 border-radius: 4px; background: linear-gradient(180deg, var(--panel-2), var(--panel)); }
  details.prio.fatal { border-left-color: var(--red); }
  details.prio summary { list-style: none; cursor: pointer; display: flex; align-items: center;
                         gap: 14px; padding: 14px 18px; }
  details.prio summary::-webkit-details-marker { display: none; }
  .prio .rank { font: 700 20px/1 var(--mono); color: var(--faint); min-width: 44px; }
  details[open].prio .rank, details.prio summary:hover .rank { color: var(--ink); }
  .prio .t { font: 600 14px/1.4 var(--sans); }
  .prio .sub { font: 11px/1.3 var(--mono); color: var(--dim); margin-top: 3px; word-break: break-all; }
  .prio .score { margin-left: auto; text-align: right; white-space: nowrap; }
  .prio .score .n { font: 700 20px/1 var(--mono); color: var(--amber); }
  .prio .score .cap { font: 9px/1 var(--mono); letter-spacing: .2em; color: var(--faint); display: block; margin-top: 4px; }
  .prio .body { border-top: 1px dashed var(--line); margin: 0 18px; padding: 14px 0 16px;
                display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px 26px; }
  .prio .f .k { font: 600 9px/1 var(--mono); letter-spacing: .22em; color: var(--faint); margin-bottom: 6px; }
  .prio .f .x { font: 13px/1.7 var(--sans); color: var(--ink); }
  .prio .f .x code { font: 12px var(--mono); color: var(--blue); word-break: break-all; }
  .prio .trace { font: 11px/1.5 var(--mono); color: var(--dim); white-space: pre; overflow-x: auto;
                 max-height: 220px; margin: 0; padding: 10px 12px; border: 1px solid var(--line);
                 border-radius: 4px; background: var(--panel-2); }
  .copybtn { font: 600 10px/1 var(--mono); letter-spacing: .1em; color: var(--dim); white-space: nowrap;
             background: transparent; border: 1px solid var(--line); border-radius: 3px; padding: 7px 11px; cursor: pointer; }
  .copybtn:hover { color: var(--ink); border-color: var(--dim); }
  .copybtn.done { color: var(--live); border-color: var(--live); }
  .chip { display: inline-block; font: 600 10px/1 var(--mono); letter-spacing: .12em;
          padding: 4px 8px; border-radius: 2px; border: 1px solid; white-space: nowrap; }
  .chip.fatal { color: var(--red); border-color: var(--red-line); background: var(--red-soft); }
  .chip.warn  { color: var(--amber); border-color: var(--amber-line); background: var(--amber-soft); }
  .chip.info  { color: var(--dim); border-color: var(--line); }
  .chip.ok    { color: var(--live); border-color: var(--live); background: transparent; }

  /* ── 上期清單回顧 ──────────────────────────────── */
  .review-list { display: flex; flex-direction: column; gap: 10px; }
  .review-row { border: 1px solid var(--line); border-left: 3px solid var(--live); border-radius: 4px;
                background: linear-gradient(180deg, var(--panel-2), var(--panel));
                display: flex; align-items: center; gap: 14px; padding: 14px 18px; }
  .review-row.warn { border-left-color: var(--amber); }
  .review-row.bad  { border-left-color: var(--red); }
  .review-row .nums { margin-left: auto; text-align: right; white-space: nowrap;
                      font: 600 12.5px var(--mono); color: var(--dim); font-variant-numeric: tabular-nums; }
  .review-row .nums b { color: var(--ink); }

  /* ── Issue 表 ─────────────────────────────────── */
  .tablewrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 4px; background: var(--panel); }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; min-width: 860px; }
  thead th { font: 600 10px/1 var(--mono); letter-spacing: .16em; color: var(--dim); text-align: left;
             padding: 12px 14px; border-bottom: 1px solid var(--line); cursor: pointer; user-select: none;
             white-space: nowrap; background: var(--panel-2); position: sticky; top: 0; }
  thead th:hover { color: var(--ink); }
  thead th.sorted { color: var(--live); }
  thead th.sorted::after { content: " ▾"; }
  thead th.sorted.asc::after { content: " ▴"; }
  tbody td { padding: 11px 14px; border-bottom: 1px solid var(--line-soft); vertical-align: top; }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover td { background: var(--hover); }
  td.num { font: 600 12.5px var(--mono); font-variant-numeric: tabular-nums; }
  td .subtle { color: var(--dim); font: 11.5px var(--mono); word-break: break-all; }
  .platform-badge { font: 700 10px/1 var(--mono); color: var(--dim); }
  .plat { display: inline-flex; vertical-align: middle; }
  .plat svg { width: 16px; height: 16px; display: block; }
  .plat.android { color: #3ddc84; }
  .plat.ios { color: var(--ink); }
  .empty-row td { text-align: center; color: var(--faint); font: 12px var(--mono); letter-spacing: .2em; padding: 26px; }

  /* ── 待命（空狀態）───────────────────────────────*/
  .standby-hero { border: 1px solid var(--line); border-radius: 4px; padding: clamp(28px, 5vw, 56px);
             background: linear-gradient(180deg, var(--panel-2), var(--panel));
             display: grid; gap: 26px; justify-items: center; text-align: center; }
  .standby-hero .scope { width: 118px; height: 118px; border-radius: 50%; border: 1px solid var(--line);
                    position: relative; overflow: hidden;
                    background: radial-gradient(circle at center, rgba(61,220,151,.06), transparent 65%); }
  .standby-hero .scope::before, .standby-hero .scope::after { content: ""; position: absolute; background: var(--line-soft); }
  .standby-hero .scope::before { left: 50%; top: 0; bottom: 0; width: 1px; }
  .standby-hero .scope::after { top: 50%; left: 0; right: 0; height: 1px; }
  .standby-hero .sweep { position: absolute; inset: 0; border-radius: 50%;
                    background: conic-gradient(from 0deg, rgba(61,220,151,.35), transparent 70deg);
                    animation: sweep 3.2s linear infinite; }
  @keyframes sweep { to { transform: rotate(360deg); } }
  .standby-hero h2 { font: 700 20px/1.3 var(--sans); letter-spacing: .06em; }
  .standby-hero p { color: var(--dim); max-width: 52ch; font-size: 13.5px; }
  .standby-hero .steps { display: flex; gap: 10px; flex-wrap: wrap; justify-content: center; }
  .standby-hero .step { font: 12px/1.4 var(--mono); color: var(--dim); border: 1px dashed var(--line);
                   border-radius: 3px; padding: 10px 14px; }
  .standby-hero .step b { color: var(--live); font-weight: 600; }
  .standby-hero .step.todo b { color: var(--amber); }

  footer { margin-top: 34px; padding-top: 14px; border-top: 1px solid var(--line-soft);
           display: flex; gap: 18px; flex-wrap: wrap; font: 10px/1 var(--mono);
           letter-spacing: .14em; color: var(--faint); }
  footer span { white-space: nowrap; }

  /* 進場：儀表逐格點亮 */
  .rise { opacity: 0; transform: translateY(10px); animation: rise .6s cubic-bezier(.2,.8,.2,1) forwards;
          animation-delay: calc(var(--i, 0) * 70ms); }
  @keyframes rise { to { opacity: 1; transform: none; } }
  @media (prefers-reduced-motion: reduce) {
    .rise { animation: none; opacity: 1; transform: none; }
    .dot.live, .dot.standby, .standby-hero .sweep { animation: none; }
  }
  ::-webkit-scrollbar { height: 8px; width: 8px; }
  ::-webkit-scrollbar-thumb { background: var(--line); border-radius: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="kicker">FLIGHT&nbsp;RECORDER&nbsp;//&nbsp;CRASH&nbsp;TELEMETRY</div>
      <h1>Crash 趨勢儀表板<span class="accent">_</span></h1>
    </div>
    <div class="statusline" id="statusline"></div>
    <nav class="tabs" id="appTabs"></nav>
    <button class="tab" id="themeBtn" onclick="toggleTheme()" title="切換深／淺色">◐</button>
  </header>
  <main id="main"></main>
  <footer id="foot"></footer>
</div>

<script>__CHARTJS__</script>
<script>
const DATA = __DATA__;
const appNames = Object.keys(DATA.apps);
let curApp = appNames[0], charts = [], sortKey = "users", sortAsc = false;

/* 主題：預設日間，◐ 切換並記住選擇（file:// 下 localStorage 不可用時靜默略過） */
try { document.documentElement.dataset.theme = localStorage.getItem("ct-theme") || "light"; } catch (e) {}
function toggleTheme() {
  const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("ct-theme", t); } catch (e) {}
  render(); // 圖表顏色取自 CSS 變數，需重建
}

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = n => (n ?? 0).toLocaleString("zh-Hant");
// 平台圖示（inline SVG，離線自包含；Apple 用前景色、Android 用品牌綠）
const PLAT_SVG = {
  ios: '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M18.71 19.5c-.83 1.24-1.71 2.45-3.05 2.47-1.34.03-1.77-.79-3.29-.79-1.53 0-2 .77-3.27.82-1.31.05-2.3-1.32-3.14-2.53C4.25 17 2.94 12.45 4.7 9.39c.87-1.52 2.43-2.48 4.12-2.51 1.28-.02 2.5.87 3.29.87.78 0 2.26-1.07 3.81-.91.65.03 2.47.26 3.64 1.98-.09.06-2.17 1.28-2.15 3.81.03 3.02 2.65 4.03 2.68 4.04-.03.07-.42 1.44-1.38 2.83M13 3.5c.73-.83 1.94-1.46 2.94-1.5.13 1.17-.34 2.35-1.04 3.19-.69.85-1.83 1.51-2.95 1.42-.15-1.15.41-2.35 1.05-3.11z"/></svg>',
  android: '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M17.6 9.48l1.84-3.18c.16-.31.04-.69-.26-.85-.29-.15-.65-.06-.83.22l-1.88 3.24a11.5 11.5 0 0 0-9.42 0L5.17 5.67c-.18-.29-.53-.37-.83-.22-.3.16-.42.54-.26.85L5.92 9.48A10.8 10.8 0 0 0 1 17.34h22a10.8 10.8 0 0 0-4.92-7.86M7 15.25a1 1 0 1 1 0-2 1 1 0 0 1 0 2m10 0a1 1 0 1 1 0-2 1 1 0 0 1 0 2"/></svg>',
};
const platIcon = p => { const k = p === "ios" ? "ios" : "android";
  return `<span class="plat ${k}" title="${k === "ios" ? "iOS" : "Android"}">${PLAT_SVG[k]}</span>`; };
// 層級三態（白話中文；hover 顯示英文原詞）。舊快照無 error_type 時用 fatal 布林 fallback
const LEVEL = { FATAL: ["fatal", "閃退"], ANR: ["warn", "凍結"], NON_FATAL: ["info", "非致命"] };
const levelOf = r => LEVEL[r.error_type] ? r.error_type : (r.fatal ? "FATAL" : "NON_FATAL");
const levelChip = r => { const lv = levelOf(r), [cls, label] = LEVEL[lv];
  return `<span class="chip ${cls}" title="${lv}">${label}</span>`; };
const monthsOf = a => Object.keys(DATA.apps[a].months).sort();
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

function pickDist(dists, platform, kind) {
  return ((dists || {})[platform] || {})[kind] || [];
}
function mergedDist(dists, kind) {
  const a = pickDist(dists, "android", kind), i = pickDist(dists, "ios", kind);
  const rows = [...a.map(r => ({ ...r, p: "AND" })), ...i.map(r => ({ ...r, p: "iOS" }))]
    .sort((x, y) => y.events - x.events).slice(0, 10);
  const src = [a.length && "Android", i.length && "iOS"].filter(Boolean).join(" + ");
  return { rows, src };
}
function deltaHtml(cur, prev) {
  if (prev == null) return `<span class="flat">首期・無基期</span>`;
  if (prev === 0) return `<span class="flat">上期 0</span>`;
  const pct = Math.round(((cur - prev) / prev) * 100);
  const cls = pct > 0 ? "up" : pct < 0 ? "down" : "flat";
  const arrow = pct > 0 ? "▲" : pct < 0 ? "▼" : "＝";
  return `<span class="${cls}">${arrow} ${Math.abs(pct)}% vs 上期</span>`;
}
function countUp(el, target) {
  const t0 = performance.now(), dur = 900;
  const tick = now => {
    const p = Math.min((now - t0) / dur, 1), eased = 1 - Math.pow(1 - p, 3);
    el.textContent = fmt(Math.round(target * eased));
    if (p < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

/* ── 版面渲染 ── */
function render() {
  const app = DATA.apps[curApp], months = monthsOf(curApp);
  const m = months.at(-1), s = app.months[m];
  const ps = months.length > 1 ? app.months[months.at(-2)] : null;
  const srcs = s.sources || {}, liveData = srcs.crashlytics_bq || srcs.mcp_report || srcs.manual_console;

  $("appTabs").innerHTML = appNames.map(a =>
    `<button class="tab ${a === curApp ? "active" : ""}" onclick="switchApp('${a}')">${esc(DATA.apps[a].display_name)}</button>`).join("");

  $("statusline").innerHTML = `
    <span><i class="dot ${liveData ? "live" : "standby"}"></i>${liveData ? "LIVE" : "STANDBY"}</span>
    <span>APP <b>${esc(app.display_name)}</b></span>
    <span>資料月份 <b>${m}</b></span>
    <span>產生 <b>${s.generated_at || m}</b></span>`;

  charts.forEach(c => c.destroy()); charts = [];
  $("main").innerHTML = liveData || (s.kpis?.events > 0) ? layoutLive(s, ps, months, app) : layoutStandby();
  if (liveData || (s.kpis?.events > 0)) hydrateCharts(s, months, app);

  $("foot").innerHTML = [
    `crash-trend // 離線自包含`,
    `來源 BQ:${srcs.crashlytics_bq ? "ON" : "--"} MCP:${srcs.mcp_report ? "ON" : "--"} CONSOLE:${srcs.manual_console ? "ON" : "--"}`,
    `每月 normalize 後重產`,
  ].map(x => `<span>${x}</span>`).join("");
}

function layoutStandby() {
  return `
  <section class="standby-hero rise" style="--i:1">
    <div class="scope"><div class="sweep"></div></div>
    <h2>待命中 — 本月尚無資料</h2>
    <p>若 BigQuery 已連結，首批批次匯出最長 48 小時（通常隔日）；尚未連結則先到該 Firebase 專案的
       Console 點 Link，或改用 console 快照模式。資料備妥後，對 Claude 說「跑 crash 月報」，本頁隨之點亮。</p>
    <div class="steps">
      <div class="step"><b>✓</b>&nbsp;Crashlytics 收集中</div>
      <div class="step todo"><b>⏳</b>&nbsp;等待資料源（BigQuery 或 console 快照）</div>
      <div class="step todo"><b>→</b>&nbsp;「跑 crash 月報」</div>
    </div>
  </section>`;
}

function layoutLive(s, ps, months, app) {
  const k = s.kpis || {}, pk = ps ? (ps.kpis || {}) : null;
  const fatalPct = k.fatal_share == null ? null : Math.round(k.fatal_share * 100);
  const kpi = (i, label, val, sub, extra = "") => `
    <div class="card kpi rise" style="--i:${i}">
      <div class="l">${label}</div>
      <div class="v">${val}</div>
      <div class="d">${sub}</div>${extra}
    </div>`;

  const pr = s.priority_list || [];
  const prioHtml = pr.length ? pr.map((p, i) => `
    <details class="prio ${p.fatal ? "fatal" : ""} rise" style="--i:${8 + i}">
      <summary>
        <span class="rank">${String(i + 1).padStart(2, "0")}</span>
        <span><span class="t">${esc(p.title)}</span>
          <span class="sub">${esc(p.code_location || "")}</span></span>
        ${levelChip(p)}
        <span class="score"><span class="n">${esc(p.score)}</span><span class="cap">PRIORITY</span></span>
        <button class="copybtn" id="copy-${i}" title="複製成可貼給 AI agent 的修復請求"
          onclick="event.preventDefault();event.stopPropagation();copyFix(${i})">COPY ⧉</button>
      </summary>
      <div class="body">
        <div class="f"><div class="k">ROOT CAUSE 推測</div><div class="x">${esc(p.root_cause || "—")}</div></div>
        <div class="f"><div class="k">建議修法</div><div class="x">${esc(p.suggested_fix || "—")}</div></div>
        <div class="f"><div class="k">影響 / 工作量</div>
          <div class="x">${fmt(p.users)} 用戶・${fmt(p.events)} 事件・工作量 <b>${esc(p.effort || "?")}</b></div></div>
        ${p.blame_file ? `<div class="f"><div class="k">程式碼位置</div><div class="x"><code>${esc(p.blame_file)}${p.blame_line ? ":" + esc(p.blame_line) : ""}</code></div></div>`
          : (p.code_location ? `<div class="f"><div class="k">錯誤訊息</div><div class="x"><code>${esc(p.code_location)}</code></div></div>` : "")}
        ${p.stack_trace ? `<div class="f" style="grid-column:1/-1"><div class="k">STACK TRACE（Crashlytics 實際堆疊）</div><pre class="trace">${esc(p.stack_trace)}</pre></div>` : ""}
      </div>
    </details>`).join("")
    : `<div class="card srcs rise" style="--i:8"><div class="srcrow"><b>尚無 AI 優先清單</b>
       <span class="state">PENDING</span></div>
       <div class="srcnote">跑一次「crash 月報」，AI 會依「用戶×3 + 閃退或凍結×2 + 惡化×2 + 最新版仍現×2 + 核心路徑×3 + 事件×1」評分後填入。</div></div>`;

  // 上期清單回顧（fix_review 由 normalize.py 算好存進月快照，這裡只渲染）
  const fr = s.fix_review;
  const hasReview = !!(fr && fr.items && fr.items.length);
  const FR = { resolved:          { cls: "",     chip: "ok",    label: "本期未再出現" },
               old_versions_only: { cls: "warn", chip: "warn",  label: "僅舊版仍出現" },
               still_occurring:   { cls: "bad",  chip: "fatal", label: "最新版仍出現" } };
  const frVer = it => it.status === "resolved" ? ""
    : !it.version_known ? "版本不明"
    : `最新見 ${esc(it.cur_last_seen_version || "?")}（全域最新 ${esc(it.latest_app_version || "?")}）`;
  const reviewHtml = hasReview ? fr.items.map((it, i) => {
    const st = FR[it.status] || FR.still_occurring;
    return `
    <div class="review-row ${st.cls} rise" style="--i:${5 + i}">
      <span class="rank">${String(i + 1).padStart(2, "0")}</span>
      <span><span class="t">${esc(it.title)}</span>
        <span class="sub">${frVer(it)}</span></span>
      <span class="nums">上期 ${fmt(it.prev?.events)}/${fmt(it.prev?.users)} → 本期 <b>${fmt(it.cur?.events)}/${fmt(it.cur?.users)}</b></span>
      <span class="chip ${st.chip}">${st.label}</span>
    </div>`;
  }).join("") + `
    <div class="srcnote">來源：${fr.source === "priority_list" ? "上期優先修復清單" : "上期 Top Issues（上期無 AI 清單）"}
      ${+String(s.generated_at || "").slice(8, 10) < 15 ? "・本月資料未滿月，「未再出現」僅供參考" : ""}</div>` : "";
  const secNo = hasReview ? { review: "02", prio: "03", top: "04" } : { prio: "02", top: "03" };

  return `
  <section class="kpis">
    ${kpi(1, "EVENTS&nbsp;/&nbsp;事件數", `<span data-count="${k.events || 0}">0</span>`, deltaHtml(k.events, pk?.events))}
    ${kpi(2, "USERS&nbsp;/&nbsp;受影響用戶", `<span data-count="${k.users || 0}">0</span>`, deltaHtml(k.users, pk?.users))}
    ${kpi(3, "FATAL&nbsp;SHARE&nbsp;/&nbsp;佔比", fatalPct == null ? "—" : `${fatalPct}<span class="unit">%</span>`,
          fatalPct == null ? "無事件" : "閃退事件佔全部事件（不含凍結）",
          `<div class="meter"><i data-w="${fatalPct || 0}"></i></div>`)}
    ${kpi(4, "ISSUES&nbsp;/&nbsp;問題數", `<span data-count="${k.issue_count || 0}">0</span>`, deltaHtml(k.issue_count, pk?.issue_count))}
  </section>

  <div class="sec"><span class="no">01</span><h2>趨勢與分布</h2><div class="rule"></div>
    <span class="hint">CHART.JS // OFFLINE</span></div>
  <section class="charts">
    <div class="card chart-card span8 rise" style="--i:5"><h3>跨月趨勢 — 事件 / 用戶</h3>
      <div class="chart-box"><canvas id="cTrend"></canvas><div class="nodata" id="nTrend" hidden>NO DATA</div></div></div>
    <div class="card srcs span4 rise" style="--i:5">
      <div class="srcrow ${s.sources?.crashlytics_bq ? "on" : "off"}"><b>Crashlytics BigQuery</b>
        <span class="state">${s.sources?.crashlytics_bq ? "● ACTIVE" : "○ OFFLINE"}</span></div>
      <div class="srcrow ${s.sources?.mcp_report ? "on" : "off"}"><b>Crashlytics API（MCP 報表）</b>
        <span class="state">${s.sources?.mcp_report ? "● ACTIVE" : "○ OFFLINE"}</span></div>
      <div class="srcrow ${s.sources?.manual_console ? "on" : "off"}"><b>Console 快照（人工）</b>
        <span class="state">${s.sources?.manual_console ? "● ACTIVE" : "○ OFFLINE"}</span></div>
      <div class="srcnote">深度資料（自訂 keys 族群交叉、週趨勢）以 BigQuery 為準；BQ 未接的 app 由
        MCP 報表補 issue 清單與分布；Console 快照用於補歷史。月度摘要永久存於工具 repo 的 git。</div>
    </div>
    <div class="card chart-card span4 rise" style="--i:6"><h3>機型分布 <span class="src" id="sDev"></span></h3>
      <div class="chart-box"><canvas id="cDev"></canvas><div class="nodata" id="nDev" hidden>NO DATA</div></div></div>
    <div class="card chart-card span4 rise" style="--i:6"><h3>OS 版本分布</h3>
      <div class="chart-box"><canvas id="cOs"></canvas><div class="nodata" id="nOs" hidden>NO DATA</div></div></div>
    <div class="card chart-card span4 rise" style="--i:7"><h3>APP 版本分布</h3>
      <div class="chart-box"><canvas id="cVer"></canvas><div class="nodata" id="nVer" hidden>NO DATA</div></div></div>
    <div class="card chart-card span4 rise" style="--i:8"><h3>層級分布 <span class="src">// 全量事件</span></h3>
      <div class="chart-box"><canvas id="cLvl"></canvas><div class="nodata" id="nLvl" hidden>NO DATA</div></div></div>
    <div class="card chart-card span8 rise" style="--i:8"><h3>週趨勢 — 事件</h3>
      <div class="chart-box"><canvas id="cWeek"></canvas><div class="nodata" id="nWeek" hidden>NO DATA</div></div></div>
  </section>

  ${hasReview ? `
  <div class="sec"><span class="no">02</span><h2>上期清單回顧</h2><div class="rule"></div>
    <span class="hint">FIX VERIFICATION // vs ${esc(fr.prev_month)}</span></div>
  <section class="review-list">${reviewHtml}</section>` : ""}

  <div class="sec"><span class="no">${secNo.prio}</span><h2>優先修復清單</h2><div class="rule"></div>
    <span class="hint">SCORED // P = 用戶×3 + 閃退或凍結×2 + 惡化×2 + 最新版仍現×2 + 核心路徑×3 + 事件×1</span></div>
  <section class="prio-list">${prioHtml}</section>

  <div class="sec"><span class="no">${secNo.top}</span><h2>TOP ISSUES</h2><div class="rule"></div>
    <span class="hint">點欄位排序・層級：閃退＝APP 強制關閉｜凍結＝畫面卡死無回應(ANR)｜非致命＝已捕捉錯誤未閃退</span></div>
  <section class="tablewrap rise" style="--i:9"><table id="issues"><thead></thead><tbody></tbody></table></section>`;
}

function chartOpts(indexAxis) {
  const dim = css("--dim"), line = css("--line-soft");
  return {
    responsive: true, maintainAspectRatio: false, indexAxis,
    plugins: { legend: { labels: { color: dim, boxWidth: 10, boxHeight: 10,
               font: { family: "SF Mono, Menlo, monospace", size: 10 } } } },
    scales: {
      x: { ticks: { color: dim, font: { family: "SF Mono, Menlo, monospace", size: 10 } }, grid: { color: line } },
      y: { ticks: { color: dim, font: { family: "SF Mono, Menlo, monospace", size: 10 } }, grid: { color: line } },
    },
  };
}

function hydrateCharts(s, months, app) {
  document.querySelectorAll("[data-count]").forEach(el => countUp(el, +el.dataset.count));
  requestAnimationFrame(() =>
    document.querySelectorAll(".meter i").forEach(el => { el.style.width = el.dataset.w + "%"; }));

  const trendEv = months.map(x => app.months[x].kpis?.events ?? 0);
  const trendUs = months.map(x => app.months[x].kpis?.users ?? 0);
  if (months.length) {
    charts.push(new Chart($("cTrend"), {
      type: "line",
      data: { labels: months, datasets: [
        { label: "事件", data: trendEv, borderColor: css("--red"), backgroundColor: css("--trend-fill"),
          fill: true, tension: .35, pointRadius: 3, borderWidth: 2 },
        { label: "用戶", data: trendUs, borderColor: css("--blue"), tension: .35, pointRadius: 3, borderWidth: 2 },
      ]},
      options: chartOpts("x"),
    }));
  } else $("nTrend").hidden = false;

  for (const [cid, nid, kind, srcEl] of [["cDev", "nDev", "device", "sDev"], ["cOs", "nOs", "os", null], ["cVer", "nVer", "app_version", null]]) {
    const { rows, src } = mergedDist(s.distributions, kind);
    if (srcEl && src) $(srcEl).textContent = `// ${src}`;
    if (!rows.length) { $(nid).hidden = false; continue; }
    charts.push(new Chart($(cid), {
      type: "bar",
      data: { labels: rows.map(r => `${r.label}${r.p === "iOS" ? " · iOS" : ""}`),
              datasets: [{ label: "事件", data: rows.map(r => r.events),
                           backgroundColor: rows.map(r => css(r.p === "iOS" ? "--bar-ios" : "--bar-and")),
                           borderRadius: 2, barThickness: 12 }] },
      options: { ...chartOpts("y"), plugins: { legend: { display: false } } },
    }));
  }
  // 層級分布（kpis 全量彙總；舊月快照缺欄時顯示 NO DATA）
  const lvRows = [["閃退", s.kpis?.events_fatal, "--red"], ["凍結", s.kpis?.events_anr, "--amber"], ["非致命", s.kpis?.events_nonfatal, "--dim"]]
    .filter(([, v]) => v > 0);
  if (!lvRows.length) $("nLvl").hidden = false;
  else charts.push(new Chart($("cLvl"), {
    type: "doughnut",
    data: { labels: lvRows.map(r => r[0]),
            datasets: [{ data: lvRows.map(r => r[1]), backgroundColor: lvRows.map(r => css(r[2])), borderWidth: 0 }] },
    options: { responsive: true, maintainAspectRatio: false, cutout: "62%",
               plugins: { legend: { position: "right", labels: { color: css("--dim"), boxWidth: 10, font: { size: 10 } } } } },
  }));

  // 週趨勢（BQ 或 MCP 自建；週 key %Y-%W）
  const wk = {};
  for (const r of s.weekly_trend || []) wk[r.week] = (wk[r.week] || 0) + (r.events || 0);
  const weeks = Object.keys(wk).sort();
  if (!weeks.length) $("nWeek").hidden = false;
  else charts.push(new Chart($("cWeek"), {
    type: "line",
    data: { labels: weeks, datasets: [{ label: "事件", data: weeks.map(w => wk[w]), borderColor: css("--red"),
            backgroundColor: css("--trend-fill"), fill: true, tension: .35, pointRadius: 3, borderWidth: 2 }] },
    options: { ...chartOpts("x"), plugins: { legend: { display: false } } },
  }));

  renderTable((s.top_issues || []));
}

function renderTable(rows) {
  const cols = [["platform", "平台"], ["title", "標題 / 位置"], ["fatal", "層級"], ["events", "事件"],
                ["users", "用戶"], ["first_seen_version", "首見"], ["last_seen_version", "最新見"], ["source", "來源"]];
  const sorted = [...rows].sort((a, b) => {
    const x = a[sortKey] ?? "", y = b[sortKey] ?? "";
    return (typeof x === "number" || typeof x === "boolean"
      ? Number(x) - Number(y) : String(x).localeCompare(String(y))) * (sortAsc ? 1 : -1);
  });
  document.querySelector("#issues thead").innerHTML = "<tr>" + cols.map(([key, label]) =>
    `<th class="${key === sortKey ? "sorted" + (sortAsc ? " asc" : "") : ""}" onclick="sortBy('${key}')">${label}</th>`).join("") + "</tr>";
  document.querySelector("#issues tbody").innerHTML = sorted.length ? sorted.map(r => `<tr>
    <td>${platIcon(r.platform)}</td>
    <td><div>${esc(r.title)}</div><div class="subtle">${esc(r.subtitle || "")}</div></td>
    <td>${levelChip(r)}</td>
    <td class="num">${fmt(r.events)}</td><td class="num">${fmt(r.users)}</td>
    <td class="num">${esc(r.first_seen_version || "—")}</td><td class="num">${esc(r.last_seen_version || "—")}</td>
    <td><span class="chip info">${(r.source || "").replace("crashlytics_bq", "BQ").replace("mcp_report", "MCP").replace("manual_console", "CONSOLE")}</span></td>
  </tr>`).join("") : `<tr class="empty-row"><td colspan="8">本期無 ISSUE 資料</td></tr>`;
}

function switchApp(a) { curApp = a; sortKey = "users"; sortAsc = false; render(); }
// 排序只重畫表格——整頁 render() 會 destroy 重建所有圖表（動畫重跑），視覺上像整頁刷新
function currentRows() { const m = monthsOf(curApp).at(-1); return DATA.apps[curApp].months[m]?.top_issues || []; }
function sortBy(k) { sortAsc = sortKey === k ? !sortAsc : false; sortKey = k; renderTable(currentRows()); }

// 「複製給 agent」——把該修復項組成自包含 markdown，一鍵貼給 coding agent 處理
const TREND_ZH = { new: "新增", worse: "惡化", stable: "穩定" };
function buildFixPrompt(p) {
  const app = DATA.apps[curApp], plat = p.platform === "ios" ? "iOS" : "Android";
  const level = (LEVEL[levelOf(p)] || ["", p.error_type || ""])[1];
  const fence = "```";
  const L = [
    "# Crash 修復請求：" + (p.title || ""),
    "",
    "- App：" + app.display_name + "（" + plat + "）",
    "- 層級：" + level + "（" + (p.error_type || "") + "）",
    "- 影響：" + (p.users || 0).toLocaleString() + " 位用戶 / " + (p.events || 0).toLocaleString() + " 次事件，趨勢：" + (TREND_ZH[p.trend] || p.trend || "—"),
    "- 版本：" + (p.first_seen_version || "?") + " → " + (p.last_seen_version || "?"),
  ];
  if (p.blame_file) L.push("- 程式碼位置：" + p.blame_file + (p.blame_line ? ":" + p.blame_line : ""));
  if (p.code_location) L.push("- 錯誤訊息：" + p.code_location);
  L.push("", "## Root cause 推測（AI 分析）", p.root_cause || "需人工確認",
         "", "## 建議修法（AI 分析，工作量 " + (p.effort || "?") + "）", p.suggested_fix || "—");
  if (p.stack_trace) L.push("", "## Stack trace（Crashlytics 實際堆疊）", fence, p.stack_trace, fence);
  L.push("", "請依上述 stack trace 與程式碼位置定位 root cause 並修復；修好後說明改了什麼、為什麼。");
  return L.join("\n");
}
function flashCopied(i, ok) {
  const b = document.getElementById("copy-" + i);
  if (!b) return;
  b.textContent = ok ? "已複製 ✓" : "複製失敗";
  b.classList.add("done");
  setTimeout(() => { b.textContent = "COPY ⧉"; b.classList.remove("done"); }, 1600);
}
function copyFix(i) {
  const m = monthsOf(curApp).at(-1);
  const p = (DATA.apps[curApp].months[m].priority_list || [])[i];
  if (!p) return;
  const txt = buildFixPrompt(p);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(txt).then(() => flashCopied(i, true)).catch(() => legacyCopy(txt, i));
  } else legacyCopy(txt, i);
}
function legacyCopy(txt, i) {  // 離線 file:// 下 clipboard API 常被擋，退回 textarea + execCommand
  const ta = document.createElement("textarea");
  ta.value = txt; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.focus(); ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  document.body.removeChild(ta);
  if (ok) flashCopied(i, true); else showManual(txt);  // 兩種都失敗 → 給手動複製浮層，不讓使用者卡住
}
function showManual(txt) {
  const ov = document.createElement("div");
  ov.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.55);display:grid;place-items:center;z-index:99;padding:24px";
  const box = document.createElement("div");
  box.style.cssText = "background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:16px;max-width:760px;width:100%;display:flex;flex-direction:column;gap:10px";
  const hint = document.createElement("div");
  hint.textContent = "自動複製被瀏覽器擋下——請按 Cmd/Ctrl+C 複製下方內容，或點右上關閉：";
  hint.style.cssText = "font:12px var(--mono);color:var(--dim)";
  const ta = document.createElement("textarea");
  ta.value = txt; ta.readOnly = true;
  ta.style.cssText = "width:100%;height:52vh;font:12px/1.5 var(--mono);white-space:pre;overflow:auto;background:var(--panel-2);color:var(--ink);border:1px solid var(--line);border-radius:4px;padding:10px";
  const close = document.createElement("button");
  close.className = "copybtn"; close.textContent = "關閉"; close.style.alignSelf = "flex-end";
  close.onclick = () => ov.remove();
  box.append(hint, ta, close); ov.append(box); document.body.appendChild(ov);
  ta.focus(); ta.select();
}

if (appNames.length) render();
else document.body.innerHTML = '<div style="display:grid;place-content:center;min-height:90vh;color:#6b7890;font:12px/2 SF Mono,Menlo,monospace;letter-spacing:.2em">尚無任何 APP 的月度資料 — 先跑 NORMALIZE.PY</div>';
</script>
</body>
</html>
"""


def main() -> None:
    data = collect_data()
    html = TEMPLATE.replace("__CHARTJS__", VENDOR_JS.read_text(encoding="utf-8")).replace(
        "__DATA__", json.dumps(data, ensure_ascii=False)
    )
    OUT_HTML.write_text(html, encoding="utf-8")
    total_months = sum(len(a["months"]) for a in data["apps"].values())
    print(f"  ✓ dashboard.html（{len(data['apps'])} 個 app、{total_months} 個月份摘要）")


if __name__ == "__main__":
    main()
