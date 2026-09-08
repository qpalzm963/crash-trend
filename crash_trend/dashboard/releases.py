"""Release catalog, version health, and device analytics rendering (Issue #53, #57)."""

from __future__ import annotations


def get_releases_html() -> str:
    """Returns HTML markup for version health, device distributions, and release catalog views."""
    return """    <!-- VIEW: VERSION HEALTH (版本健康度) -->
    <section class="view-container" id="view-version_health">
      <div class="section-header">
        <div>
          <h2 class="section-title">版本健康度 (Version Health)</h2>
          <div class="section-subtitle">各發佈版本之 Crash-free 指標、採納率與穩定趨勢</div>
        </div>
      </div>

      <div class="table-container">
        <table class="data-table">
          <thead>
            <tr>
              <th>版本號</th>
              <th>平台</th>
              <th>發佈日期</th>
              <th>狀態</th>
              <th>趨勢</th>
              <th>Crash-free (Users)</th>
              <th>Crash-free (Sessions)</th>
              <th>採納率</th>
              <th>崩潰事件數</th>
              <th>受影響用戶</th>
            </tr>
          </thead>
          <tbody id="versionHealthTableBody"></tbody>
        </table>
      </div>
    </section>


    <!-- VIEW: DEVICES (裝置分析) -->
    <section class="view-container" id="view-devices">
      <div class="section-header">
        <div>
          <h2 class="section-title">裝置與系統分析 (Devices & OS)</h2>
          <div class="section-subtitle">主要崩潰機型、作業系統版本分布與自訂 Key 交叉分析</div>
        </div>
      </div>

      <div class="charts-grid">
        <div class="chart-card col-6">
          <div class="chart-card-header">
            <div>
              <div class="chart-title">機型崩潰排行 (Device Models)</div>
              <div class="chart-subtitle">事件數最高之裝置型號</div>
            </div>
          </div>
          <div class="chart-container">
            <canvas id="chartDeviceModels"></canvas>
          </div>
        </div>

        <div class="chart-card col-6">
          <div class="chart-card-header">
            <div>
              <div class="chart-title">OS 版本排行 (OS Versions)</div>
              <div class="chart-subtitle">各作業系統版本分布</div>
            </div>
          </div>
          <div class="chart-container">
            <canvas id="chartOSVersions"></canvas>
          </div>
        </div>
      </div>

      <div class="table-container" style="margin-top:16px">
        <div class="table-toolbar">
          <div class="section-title" style="font-size:14px">機型詳細分布</div>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>機型名稱</th>
              <th>平台</th>
              <th>崩潰事件數</th>
              <th>受影響用戶</th>
              <th>佔比</th>
            </tr>
          </thead>
          <tbody id="deviceModelsTableBody"></tbody>
        </table>
      </div>
    </section>


    <!-- VIEW: RELEASES (發佈版本) -->
    <section class="view-container" id="view-releases">
      <div class="section-header">
        <div>
          <h2 class="section-title">發佈版本 (Release Catalog & Lifecycle)</h2>
          <div class="section-subtitle">版本生命週期追蹤、Lifetime 累積指標與近期健康度 (7/30/90) 解耦</div>
        </div>
        <div class="filter-group" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <select class="filter-select" id="filterReleasePlatform" onchange="renderReleasesTable()">
            <option value="ALL">全部平台 (All Platforms)</option>
            <option value="android">Android</option>
            <option value="ios">iOS</option>
          </select>
          <select class="filter-select" id="filterReleaseStatus" onchange="renderReleasesTable()">
            <option value="ALL">全部狀態 (All Statuses)</option>
            <option value="latest">最新版本 (Latest)</option>
            <option value="active">活躍版本 (Active)</option>
            <option value="legacy">歷史版本 (Legacy >90d)</option>
          </select>
          <input type="text" class="search-input" id="searchReleaseVer" placeholder="搜尋版本號..." oninput="renderReleasesTable()" style="width:140px;height:32px;font-size:12px;padding:4px 8px;border-radius:var(--radius-sm);border:1px solid var(--border);background:var(--bg-surface);color:var(--text-main)">
        </div>
      </div>
      <div class="table-container">
        <table class="data-table">
          <thead>
            <tr>
              <th>版本號</th>
              <th>平台</th>
              <th>狀態</th>
              <th>首次觀察</th>
              <th>最後活動</th>
              <th>累積崩潰</th>
              <th>累積問題</th>
              <th>受影響用戶</th>
              <th>穩定度</th>
              <th>品質閘門</th>
              <th>相較前版</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody id="releasesTableBody"></tbody>
        </table>
      </div>
    </section>

    <!-- MODAL: RELEASE DETAIL -->
    <div class="modal-overlay" id="releaseDetailModal" onclick="if(event.target===this)closeReleaseDetail()">
      <div class="modal-card">
        <div class="modal-header">
          <div class="modal-title-group" id="releaseModalTitle"></div>
          <button class="modal-close-btn" onclick="closeReleaseDetail()" title="關閉">&times;</button>
        </div>
        <div class="modal-body" id="releaseModalBody"></div>
      </div>
    </div>
"""


def get_releases_js() -> str:
    """Returns JavaScript rendering functions for releases, version health, and devices."""
    return """function renderVersionHealth() {
  const app = getCurAppData();
  if (!app) return;
  const snap = getCurPeriodSnapshot();
  const list = snap?.version_health || app.version_health || [];
  const tbody = $("versionHealthTableBody");
  if (!tbody) return;

  if (!list.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="empty-state">尚無版本健康度資料</td></tr>`;
    return;
  }

  tbody.innerHTML = list.map(v => {
    const cfuRate = (v.crash_free_users_rate != null) ? `${(v.crash_free_users_rate * 100).toFixed(2)}%` : "Unavailable";
    const cfsRate = (v.crash_free_sessions_rate != null) ? `${(v.crash_free_sessions_rate * 100).toFixed(2)}%` : "Unavailable";
    const adopt = (v.adoption_rate != null) ? `${(v.adoption_rate * 100).toFixed(1)}%` : "—";
    const stCls = `badge-${v.status || "active"}`;

    return `
      <tr>
        <td class="mono-num"><b>${esc(v.version)}</b></td>
        <td><span class="badge" style="background:var(--bg-subtle)">${esc(v.platform)}</span></td>
        <td class="mono-num">${esc(v.release_date || "—")}</td>
        <td><span class="badge badge-status ${stCls}">${esc(v.status)}</span></td>
        <td><span class="badge" style="background:var(--bg-subtle)">${esc(v.trend)}</span></td>
        <td class="mono-num">${cfuRate}</td>
        <td class="mono-num">${cfsRate}</td>
        <td class="mono-num">${adopt}</td>
        <td class="mono-num">${fmt(v.crash_events)}</td>
        <td class="mono-num">${fmt(v.affected_users)}</td>
      </tr>
    `;
  }).join("");
}


function renderDevicesTable() {
  const app = getCurAppData();
  if (!app) return;
  const snap = getCurPeriodSnapshot();
  const models = snap?.distributions?.device_models || app.distributions?.device_models || [];
  const tbody = $("deviceModelsTableBody");
  if (!tbody) return;

  tbody.innerHTML = models.map(m => `
    <tr>
      <td><b>${esc(m.model)}</b></td>
      <td><span class="badge" style="background:var(--bg-subtle)">${esc(m.platform)}</span></td>
      <td class="mono-num">${fmt(m.events)}</td>
      <td class="mono-num">${fmt(m.users)}</td>
      <td class="mono-num">${(m.share * 100).toFixed(1)}%</td>
    </tr>
  `).join("") || `<tr><td colspan="5" class="empty-state">尚無機型資料</td></tr>`;
}


let curDetailVersion = null;
let curDetailPlatform = null;
let curDetailWindow = "30d";

function renderReleasesTable() {
  const app = getCurAppData();
  if (!app) return;
  const snap = getCurPeriodSnapshot();
  const catalog = app.release_catalog || snap?.release_catalog;
  const tbody = $("releasesTableBody");
  if (!tbody) return;

  const pfFilter = $("filterReleasePlatform") ? $("filterReleasePlatform").value : "ALL";
  const stFilter = $("filterReleaseStatus") ? $("filterReleaseStatus").value : "ALL";
  const searchQ = ($("searchReleaseVer") ? $("searchReleaseVer").value : "").trim().toLowerCase();

  // If modern release_catalog is available
  if (catalog && catalog.length > 0) {
    let filtered = catalog.filter(v => {
      if (pfFilter !== "ALL" && (v.platform || "").toLowerCase() !== pfFilter.toLowerCase()) return false;
      if (stFilter !== "ALL" && (v.status || "").toLowerCase() !== stFilter.toLowerCase()) return false;
      if (searchQ && !(v.version || "").toLowerCase().includes(searchQ)) return false;
      return true;
    });

    tbody.innerHTML = filtered.map(v => {
      let stabBadge = "";
      if (v.stability_status === "improved" || v.stability_status === "improving") {
        stabBadge = '<span class="badge badge-stability-improving">改善 ↗</span>';
      } else if (v.stability_status === "regressed" || v.stability_status === "degrading") {
        stabBadge = '<span class="badge badge-stability-degrading">惡化 ↘</span>';
      } else if (v.stability_status === "stable") {
        stabBadge = '<span class="badge badge-stability-stable">穩定 →</span>';
      } else {
        stabBadge = '<span class="badge badge-stability-baseline">基準 —</span>';
      }

      let gateBadge = '<span class="badge" style="background:var(--bg-subtle);color:var(--text-muted)">—</span>';
      const rg = v.release_gate;
      if (rg && rg.status) {
        if (rg.status === "pass") {
          gateBadge = '<span class="badge" style="background:#e6f4ea;color:#137333;font-weight:600">PASS</span>';
        } else if (rg.status === "warn") {
          gateBadge = '<span class="badge" style="background:#fef7e0;color:#b06000;font-weight:600">WARN</span>';
        } else if (rg.status === "fail") {
          gateBadge = '<span class="badge badge-fatal" style="font-weight:600">FAIL</span>';
        } else if (rg.status === "insufficient_data") {
          gateBadge = '<span class="badge" style="background:var(--bg-subtle);color:var(--text-muted)">INSUFFICIENT</span>';
        } else if (rg.status === "baseline") {
          gateBadge = '<span class="badge badge-stability-baseline">BASELINE</span>';
        }
      }

      let vsPrevHtml = '<span class="mono-num" style="color:var(--text-muted);font-size:11.5px">—</span>';
      if (v.vs_previous && v.vs_previous.previous_version) {
        const vp = v.vs_previous;
        const ratePct = vp.crash_rate_change_pct != null ? vp.crash_rate_change_pct * 100 : null;
        const rateVal = ratePct != null ? `${ratePct > 0 ? '+' : ''}${ratePct.toFixed(1)}%` : '—';
        const colorClass = (ratePct != null && ratePct > 0) ? 'color:var(--danger)' : ((ratePct != null && ratePct < 0) ? 'color:var(--success)' : 'color:var(--text-muted)');
        vsPrevHtml = `
          <div style="font-size:11.5px;line-height:1.2">
            <span style="color:var(--text-muted)">vs ${esc(vp.previous_version)}</span>
            <span class="mono-num" style="${colorClass};font-weight:600">${rateVal}</span>
          </div>
        `;
      }

      return `
        <tr>
          <td class="mono-num"><b>${esc(v.version)}</b></td>
          <td><span class="badge" style="background:var(--bg-subtle)">${esc((v.platform || "android").toUpperCase())}</span></td>
          <td><span class="badge badge-status badge-${v.status}">${esc(v.status)}</span></td>
          <td class="mono-num">${esc(v.first_seen ? v.first_seen.split("T")[0] : "—")}</td>
          <td class="mono-num">${esc(v.last_seen ? v.last_seen.split("T")[0] : "—")}</td>
          <td class="mono-num">${fmt(v.lifetime_crashes != null ? v.lifetime_crashes : 0)}</td>
          <td class="mono-num">${fmt(v.lifetime_issues != null ? v.lifetime_issues : 0)}</td>
          <td class="mono-num">${fmt(v.lifetime_affected_users != null ? v.lifetime_affected_users : 0)}</td>
          <td>${stabBadge}</td>
          <td>${gateBadge}</td>
          <td>${vsPrevHtml}</td>
          <td>
            <button class="btn btn-secondary btn-sm" onclick="openReleaseDetail('${esc(v.version)}', '${esc(v.platform || '')}')" style="padding:2px 8px;font-size:11.5px">詳情</button>
          </td>
        </tr>
      `;
    }).join("") || `<tr><td colspan="12" class="empty-state">無符合條件之發佈版本</td></tr>`;
    return;
  }

  // Fallback for older snapshots without release_catalog
  const list = snap?.version_health || app.version_health || [];
  tbody.innerHTML = list.map(v => `
    <tr>
      <td class="mono-num"><b>${esc(v.version)}</b></td>
      <td><span class="badge" style="background:var(--bg-subtle)">ALL</span></td>
      <td><span class="badge badge-status badge-${v.status}">${esc(v.status)}</span></td>
      <td class="mono-num">${esc(v.release_date || "—")}</td>
      <td class="mono-num">—</td>
      <td class="mono-num">${fmt(v.crash_events)}</td>
      <td class="mono-num">—</td>
      <td class="mono-num">${fmt(v.affected_users)}</td>
      <td><span class="badge badge-stability-baseline">—</span></td>
      <td><span class="badge badge-stability-baseline">—</span></td>
      <td class="mono-num" style="color:var(--text-muted)">—</td>
      <td>
        <button class="btn btn-secondary btn-sm" onclick="openReleaseDetail('${esc(v.version)}', '')" style="padding:2px 8px;font-size:11.5px">詳情</button>
      </td>
    </tr>
  `).join("") || `<tr><td colspan="12" class="empty-state">尚無發佈版本資料</td></tr>`;
}

function openReleaseDetail(ver, pf) {
  const app = getCurAppData();
  if (!app) return;
  const snap = getCurPeriodSnapshot();
  const catalog = app.release_catalog || snap?.release_catalog || [];
  let item = catalog.find(x => x.version === ver && (!pf || x.platform === pf));

  if (!item) {
    const fallback = (snap?.version_health || app.version_health || []).find(x => x.version === ver);
    if (fallback) {
      item = {
        version: fallback.version,
        platform: pf || "android",
        status: fallback.status || "active",
        release_date: fallback.release_date || null,
        first_seen: "—",
        last_seen: "—",
        lifetime_crashes: fallback.crash_events || 0,
        lifetime_issues: 0,
        lifetime_affected_users: fallback.affected_users || 0,
        lifetime_fatal: 0,
        lifetime_anr: 0,
        stability_status: "unknown",
        recent_health: {
          "30d": {
            window: "30d",
            crash_events: fallback.crash_events || 0,
            affected_users: fallback.affected_users || 0,
            crash_free_users_rate: fallback.crash_free_users_rate,
            crash_free_sessions_rate: null,
            fatal_count: 0,
            anr_count: 0,
            active_issues_count: 0
          }
        },
        vs_previous: null,
        issue_lifecycle: { introduced_issues: [], persistent_issues: [], regressed_issues: [], resolved_issues: [] }
      };
    }
  }
  if (!item) return;

  curDetailVersion = ver;
  curDetailPlatform = pf || item.platform;
  const winKeys = Object.keys(item.recent_health || {});
  curDetailWindow = (item.recent_health && item.recent_health[curPeriodDays + "d"]) ? (curPeriodDays + "d") : (winKeys.includes("30d") ? "30d" : (winKeys[0] || "30d"));

  let stabBadge = "";
  if (item.stability_status === "improved" || item.stability_status === "improving") {
    stabBadge = '<span class="badge badge-stability-improving">改善 ↗</span>';
  } else if (item.stability_status === "regressed" || item.stability_status === "degrading") {
    stabBadge = '<span class="badge badge-stability-degrading">惡化 ↘</span>';
  } else if (item.stability_status === "stable") {
    stabBadge = '<span class="badge badge-stability-stable">穩定 →</span>';
  } else {
    stabBadge = '<span class="badge badge-stability-baseline">基準 —</span>';
  }

  const titleGroup = $("releaseModalTitle");
  if (titleGroup) {
    titleGroup.innerHTML = `
      <h3 style="margin:0;font-size:17px">版本 ${esc(item.version)}</h3>
      <span class="badge" style="background:var(--bg-subtle)">${esc((item.platform || "android").toUpperCase())}</span>
      <span class="badge badge-status badge-${item.status}">${esc(item.status)}</span>
      ${stabBadge}
    `;
  }

  renderReleaseModalBody(item);

  const modal = $("releaseDetailModal");
  if (modal) modal.classList.add("active");
}

function closeReleaseDetail() {
  const modal = $("releaseDetailModal");
  if (modal) modal.classList.remove("active");
}

function switchReleaseRecentHealthTab(winKey) {
  curDetailWindow = winKey;
  const app = getCurAppData();
  if (!app) return;
  const catalog = app.release_catalog || getCurPeriodSnapshot()?.release_catalog || [];
  const item = catalog.find(x => x.version === curDetailVersion && (!curDetailPlatform || x.platform === curDetailPlatform));
  if (item) {
    renderReleaseModalBody(item);
  }
}

function renderReleaseModalBody(item) {
  const body = $("releaseModalBody");
  if (!body) return;

  // Release Quality Gate Section
  const rg = item.release_gate;
  let gateCardHtml = "";
  if (rg) {
    let gateStatusBadge = "";
    if (rg.status === "pass") {
      gateStatusBadge = '<span class="badge" style="background:#e6f4ea;color:#137333;font-size:13px;font-weight:700">✓ PASS (通過)</span>';
    } else if (rg.status === "warn") {
      gateStatusBadge = '<span class="badge" style="background:#fef7e0;color:#b06000;font-size:13px;font-weight:700">⚠ WARN (預警)</span>';
    } else if (rg.status === "fail") {
      gateStatusBadge = '<span class="badge badge-fatal" style="font-size:13px;font-weight:700">✗ FAIL (退化阻擋)</span>';
    } else if (rg.status === "insufficient_data") {
      gateStatusBadge = '<span class="badge" style="background:var(--bg-subtle);color:var(--text-muted);font-size:13px;font-weight:700">INSUFFICIENT (樣本不足)</span>';
    } else {
      gateStatusBadge = '<span class="badge badge-stability-baseline" style="font-size:13px;font-weight:700">BASELINE (基準版本)</span>';
    }

    const alertBox = rg.should_alert
      ? `<div style="margin-top:8px;padding:8px 12px;background:#fce8e6;border:1px solid #fad2cf;border-radius:var(--radius-sm);color:#c5221f;font-size:12.5px;display:flex;align-items:center;gap:6px">
           <span>🚨</span><b>警示摘要：</b> ${esc(rg.alert_summary)}
         </div>`
      : `<div style="margin-top:8px;font-size:12.5px;color:var(--text-muted)">${esc(rg.alert_summary)}</div>`;

    const triggeredPills = (rg.rules_triggered && rg.rules_triggered.length > 0)
      ? `<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">
           <span style="font-size:11.5px;color:var(--text-muted)">觸發指標：</span>
           ${rg.rules_triggered.map(t => `<span class="badge badge-fatal" style="font-size:11px">${esc(t)}</span>`).join(" ")}
         </div>`
      : "";

    gateCardHtml = `
      <div>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <h4 style="margin:0;font-size:13.5px;color:var(--text-main)">版本品質閘門判定 (Release Quality Gate)</h4>
          <div>${gateStatusBadge}</div>
        </div>
        <div style="background:var(--bg-subtle);border-radius:var(--radius-md);padding:12px 14px;border:1px solid var(--border)">
          ${alertBox}
          ${triggeredPills}
        </div>
      </div>
    `;
  }

  const vp = item.vs_previous;
  let vsPrevHtml = '<div style="font-size:12.5px;color:var(--text-muted)">此版本為該平台最早記錄版本或無可供比較的前版基準。</div>';
  if (vp && vp.previous_version) {
    const ratePct = vp.crash_rate_change_pct != null ? vp.crash_rate_change_pct * 100 : null;
    const rateChg = ratePct != null ? `${ratePct > 0 ? '+' : ''}${ratePct.toFixed(2)}%` : '—';
    const cfuDiff = vp.crash_free_users_diff != null ? `${vp.crash_free_users_diff > 0 ? '+' : ''}${(vp.crash_free_users_diff * 100).toFixed(2)}%` : '—';
    const fatalVal = vp.fatal_rate_change_pct != null ? vp.fatal_rate_change_pct : vp.fatal_change_pct;
    const fatalPct = fatalVal != null ? fatalVal * 100 : null;
    const fatalChg = fatalPct != null ? `${fatalPct > 0 ? '+' : ''}${fatalPct.toFixed(2)}%` : '—';
    const anrVal = vp.anr_rate_change_pct != null ? vp.anr_rate_change_pct : vp.anr_change_pct;
    const anrPct = anrVal != null ? anrVal * 100 : null;
    const anrChg = anrPct != null ? `${anrPct > 0 ? '+' : ''}${anrPct.toFixed(2)}%` : '—';
    const newIss = vp.new_issues_count != null ? vp.new_issues_count : (vp.new_issues_diff != null ? vp.new_issues_diff : 0);

    vsPrevHtml = `
      <div style="font-size:12.5px;color:var(--text-main);margin-bottom:8px">
        基準前版：<b class="mono-num">${esc(vp.previous_version)}</b>
      </div>
      <div class="release-stat-grid">
        <div class="release-stat-box">
          <div class="release-stat-label">崩潰率變動 (Crash Rate)</div>
          <div class="release-stat-value ${ratePct != null && ratePct > 0 ? 'text-danger' : (ratePct != null && ratePct < 0 ? 'text-success' : '')}">${rateChg}</div>
        </div>
        <div class="release-stat-box">
          <div class="release-stat-label">無崩潰用戶率差異 (CFU Diff)</div>
          <div class="release-stat-value ${vp.crash_free_users_diff < 0 ? 'text-danger' : (vp.crash_free_users_diff > 0 ? 'text-success' : '')}">${cfuDiff}</div>
        </div>
        <div class="release-stat-box">
          <div class="release-stat-label">Fatal 率變動</div>
          <div class="release-stat-value">${fatalChg}</div>
        </div>
        <div class="release-stat-box">
          <div class="release-stat-label">ANR 率變動</div>
          <div class="release-stat-value">${anrChg}</div>
        </div>
        <div class="release-stat-box">
          <div class="release-stat-label">此版新增問題數 (New Issues)</div>
          <div class="release-stat-value">${fmt(newIss)}</div>
        </div>
      </div>
    `;
  }

  // Recent Health Tab
  const recentHealthMap = item.recent_health || {};
  const availWindows = ["7d", "30d", "90d"];
  const curWinData = recentHealthMap[curDetailWindow] || recentHealthMap[curDetailWindow.replace("d", "")] || recentHealthMap["30d"] || recentHealthMap["30"] || recentHealthMap["7d"] || recentHealthMap["7"] || recentHealthMap["90d"] || recentHealthMap["90"] || {};

  const pillsHtml = availWindows.map(w => {
    const isActive = w === curDetailWindow || w.replace("d", "") === curDetailWindow;
    return `<button class="sub-pill-btn ${isActive ? 'active' : ''}" onclick="switchReleaseRecentHealthTab('${w}')">${w.toUpperCase()} 近期指標</button>`;
  }).join(" ");

  const cfuRate = curWinData.crash_free_users_rate != null ? (curWinData.crash_free_users_rate * 100).toFixed(2) + "%" : "—";
  const cfsRate = curWinData.crash_free_sessions_rate != null ? (curWinData.crash_free_sessions_rate * 100).toFixed(2) + "%" : "—";
  const fatalCnt = curWinData.fatal_events != null ? curWinData.fatal_events : (curWinData.fatal_count != null ? curWinData.fatal_count : 0);
  const anrCnt = curWinData.anr_events != null ? curWinData.anr_events : (curWinData.anr_count != null ? curWinData.anr_count : 0);
  const activeIss = curWinData.active_issues_count != null ? curWinData.active_issues_count : (curWinData.new_issues_count != null ? curWinData.new_issues_count : 0);

  // Issue Lifecycle
  const lc = item.issue_lifecycle || { introduced_issues: [], persistent_issues: [], regressed_issues: [], resolved_issues: [] };
  const introducedList = lc.introduced_issues || lc.introduced || [];
  const persistentList = lc.persistent_issues || lc.persistent || [];
  const regressedList = lc.regressed_issues || lc.regressed || [];
  const resolvedList = lc.resolved_issues || lc.resolved || [];
  const renderIssuePills = (arr, badgeClass) => {
    if (!arr || arr.length === 0) return '<span style="font-size:12px;color:var(--text-muted)">無</span>';
    return arr.map(id => `<span class="badge ${badgeClass}" style="font-family:var(--font-mono);font-size:11px;margin:2px" title="Issue ID: ${esc(id)}">${esc(id.slice(0, 10))}...</span>`).join(" ");
  };

  body.innerHTML = `
    <!-- Release Facts -->
    <div style="background:var(--bg-subtle);border-radius:var(--radius-md);padding:12px 16px;border:1px solid var(--border);display:flex;flex-wrap:wrap;gap:20px;font-size:12.5px">
      <div><span style="color:var(--text-muted)">發佈日期 (Release Date):</span> <b>${esc(item.release_date || "— (未由外部指派)")}</b></div>
      <div><span style="color:var(--text-muted)">首次觀察 (First Seen):</span> <b class="mono-num">${esc(item.first_seen ? item.first_seen.replace("T", " ") : "—")}</b></div>
      <div><span style="color:var(--text-muted)">最後活動 (Last Active):</span> <b class="mono-num">${esc(item.last_seen ? item.last_seen.replace("T", " ") : "—")}</b></div>
      <div><span style="color:var(--text-muted)">平台 (Platform):</span> <b>${esc((item.platform || "android").toUpperCase())}</b></div>
    </div>

    ${gateCardHtml}

    <!-- Lifetime Metrics -->
    <div>
      <h4 style="margin:0 0 8px 0;font-size:13.5px;color:var(--text-main)">全生命週期累積數據 (Lifetime Metrics)</h4>
      <div class="release-stat-grid">
        <div class="release-stat-box">
          <div class="release-stat-label">累積崩潰數 (Lifetime Crashes)</div>
          <div class="release-stat-value">${fmt(item.lifetime_crashes != null ? item.lifetime_crashes : 0)}</div>
        </div>
        <div class="release-stat-box">
          <div class="release-stat-label">累積問題數 (Lifetime Issues)</div>
          <div class="release-stat-value">${fmt(item.lifetime_issues != null ? item.lifetime_issues : 0)}</div>
        </div>
        <div class="release-stat-box">
          <div class="release-stat-label">累積受影響用戶 (Deduplicated Users)</div>
          <div class="release-stat-value">${fmt(item.lifetime_affected_users != null ? item.lifetime_affected_users : 0)}</div>
        </div>
        <div class="release-stat-box">
          <div class="release-stat-label">致命崩潰數 (Fatal)</div>
          <div class="release-stat-value">${fmt(item.lifetime_fatal != null ? item.lifetime_fatal : 0)}</div>
        </div>
        <div class="release-stat-box">
          <div class="release-stat-label">ANR 數</div>
          <div class="release-stat-value">${fmt(item.lifetime_anr != null ? item.lifetime_anr : 0)}</div>
        </div>
      </div>
    </div>

    <!-- Previous Release Comparison -->
    <div>
      <h4 style="margin:0 0 8px 0;font-size:13.5px;color:var(--text-main)">同平台前一版本對比 (Vs Previous Release)</h4>
      <div style="background:var(--bg-subtle);border-radius:var(--radius-md);padding:14px;border:1px solid var(--border)">
        ${vsPrevHtml}
      </div>
    </div>

    <!-- Recent Health Tabs -->
    <div>
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <h4 style="margin:0;font-size:13.5px;color:var(--text-main)">近期健康度視窗 (Recent Health)</h4>
        <div style="display:flex;gap:6px">${pillsHtml}</div>
      </div>
      <div class="release-stat-grid">
        <div class="release-stat-box">
          <div class="release-stat-label">${curDetailWindow} 視窗崩潰數</div>
          <div class="release-stat-value">${fmt(curWinData.crash_events != null ? curWinData.crash_events : 0)}</div>
        </div>
        <div class="release-stat-box">
          <div class="release-stat-label">${curDetailWindow} 視窗受影響用戶</div>
          <div class="release-stat-value">${fmt(curWinData.affected_users != null ? curWinData.affected_users : 0)}</div>
        </div>
        <div class="release-stat-box">
          <div class="release-stat-label">無崩潰用戶率 (CFU %)</div>
          <div class="release-stat-value">${cfuRate}</div>
        </div>
        <div class="release-stat-box">
          <div class="release-stat-label">無崩潰工作階段率 (CFS %)</div>
          <div class="release-stat-value">${cfsRate}</div>
        </div>
        <div class="release-stat-box">
          <div class="release-stat-label">致命崩潰 / ANR</div>
          <div class="release-stat-value" style="font-size:15px">${fmt(fatalCnt)} / ${fmt(anrCnt)}</div>
        </div>
        <div class="release-stat-box">
          <div class="release-stat-label">活躍問題數</div>
          <div class="release-stat-value">${fmt(activeIss)}</div>
        </div>
      </div>
    </div>

    <!-- Issue Lifecycle Categorization -->
    <div>
      <h4 style="margin:0 0 8px 0;font-size:13.5px;color:var(--text-main)">版本問題生命週期分類 (Issue Lifecycle)</h4>
      <div style="display:flex;flex-direction:column;gap:10px">
        <div style="background:var(--bg-subtle);border-radius:var(--radius-sm);padding:10px 12px;border:1px solid var(--border)">
          <div style="font-size:12px;font-weight:600;margin-bottom:6px;display:flex;align-items:center;gap:6px">
            <span class="badge badge-lifecycle-new">🆕 新引入問題 (${introducedList.length})</span>
            <span style="font-size:11px;color:var(--text-muted)">在此版本首度出現</span>
          </div>
          <div style="display:flex;flex-wrap:gap:4px">${renderIssuePills(introducedList, 'badge-lifecycle-new')}</div>
        </div>

        <div style="background:var(--bg-subtle);border-radius:var(--radius-sm);padding:10px 12px;border:1px solid var(--border)">
          <div style="font-size:12px;font-weight:600;margin-bottom:6px;display:flex;align-items:center;gap:6px">
            <span class="badge badge-lifecycle-persistent">🔄 持續存在問題 (${persistentList.length})</span>
            <span style="font-size:11px;color:var(--text-muted)">前版已有且此版仍持續發生</span>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:4px">${renderIssuePills(persistentList, 'badge-lifecycle-persistent')}</div>
        </div>

        <div style="background:var(--bg-subtle);border-radius:var(--radius-sm);padding:10px 12px;border:1px solid var(--border)">
          <div style="font-size:12px;font-weight:600;margin-bottom:6px;display:flex;align-items:center;gap:6px">
            <span class="badge badge-lifecycle-regressed">⚡ 再次復發問題 (${regressedList.length})</span>
            <span style="font-size:11px;color:var(--text-muted)">前版曾解決但在此版再次發生</span>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:4px">${renderIssuePills(regressedList, 'badge-lifecycle-regressed')}</div>
        </div>

        <div style="background:var(--bg-subtle);border-radius:var(--radius-sm);padding:10px 12px;border:1px solid var(--border)">
          <div style="font-size:12px;font-weight:600;margin-bottom:6px;display:flex;align-items:center;gap:6px">
            <span class="badge badge-lifecycle-resolved">✅ 已修復解決問題 (${resolvedList.length})</span>
            <span style="font-size:11px;color:var(--text-muted)">前版存在但在此版未再出現</span>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:4px">${renderIssuePills(resolvedList, 'badge-lifecycle-resolved')}</div>
        </div>
      </div>
    </div>
  `;
}

// Global Escape listener for modals
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeReleaseDetail();
});
"""
