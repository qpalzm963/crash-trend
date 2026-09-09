"""Data source profiles and pipeline health rendering (Issue #53)."""

from __future__ import annotations

from crash_trend.dashboard.navigation import get_view_container_open_tag


def get_sources_html() -> str:
    """Returns HTML markup for #view-notifications (data pipelines & alert observability)."""
    return """    <!-- VIEW: NOTIFICATIONS (通知與管道狀態) -->
    """ + get_view_container_open_tag("notifications") + """
      <div class="section-header">
        <div>
          <h2 class="section-title">數據管道與通知 (Data Pipelines)</h2>
          <div class="section-subtitle">數據來源連線狀態、最後同步時間與資料限制備註</div>
        </div>
      </div>

      <div class="charts-grid" id="pipelineCardsGrid">
        <!-- Filled dynamically -->
      </div>

      <div class="ai-summary-card" style="margin-top:16px" id="limitationsCard">
        <div class="ai-title" style="color:var(--text-main)">資料收集限制與注意事項 (Data Limitations)</div>
        <ul id="limitationsList" style="padding-left:20px;font-size:13px;color:var(--text-muted);display:flex;flex-direction:column;gap:6px"></ul>
      </div>

      <!-- Google Chat Alert Delivery Section (Issue #63) -->
      <div style="margin-top:24px" id="alertDeliverySection">
        <div class="section-header" style="margin-bottom:12px">
          <div>
            <h3 class="section-title" style="font-size:16px">Google Chat 品質警報發送狀態與審計 (Alert Delivery Observability)</h3>
            <div class="section-subtitle">Webhook 投遞健康度、24 小時統計與發送歷史審計紀錄</div>
          </div>
        </div>

        <div class="charts-grid" id="alertDeliveryHealthGrid">
          <!-- Filled dynamically: Health Card & 24h Stats -->
        </div>

        <div class="chart-card" style="margin-top:16px" id="alertDeliveryRecentCard">
          <div class="chart-card-header">
            <div class="chart-title">最近通知發送審計紀錄 (Recent Alert Deliveries)</div>
            <span class="badge" style="background:var(--bg-subtle);border:1px solid var(--border);font-size:11px" id="alertDeliveryCountBadge">0 筆紀錄</span>
          </div>
          <div style="overflow-x:auto" id="alertDeliveryTableContainer">
            <!-- Filled dynamically: Recent Deliveries Table -->
          </div>
        </div>
      </div>
    </section>

"""


def get_sources_js() -> str:
    """Returns JavaScript rendering functions for data sources and alert delivery observability."""
    return """function renderDataSourcesHealth() {
  const app = getCurAppData();
  if (!app) return;
  const grid = $("overviewDataSourcesGrid");
  if (!grid) return;

  const runInfo = $("overviewLatestRunInfo");
  if (runInfo) {
    if (DATA.pipeline_run) {
      const pr = DATA.pipeline_run;
      const appRun = pr.apps && pr.apps[curAppId];
      const runStatus = appRun ? appRun.status : pr.status;
      const icon = runStatus === "success" ? "✓" : (runStatus === "degraded" ? "⚠" : "✕");
      const statusText = runStatus === "success" ? "同步正常" : (runStatus === "degraded" ? "部分降級" : "同步失敗");
      const dur = pr.duration_sec != null ? ` · 耗時 ${pr.duration_sec}s` : "";
      runInfo.innerHTML = `<span class="badge badge-status ${runStatus === 'success' ? 'badge-active' : (runStatus === 'degraded' ? 'badge-maintenance' : 'badge-fatal')}">${icon} ${statusText}</span> 最近執行: ${esc(formatFreshness(pr.finished_at, DATA.generated_at))}${dur}`;
    } else {
      runInfo.textContent = `報表產生於 ${formatFreshness(DATA.generated_at, null)}`;
    }
  }

  const srcs = app.sources || {};
  const aiSrc = srcs.ai || srcs.gemini_ai;
  const aiName = (aiSrc && aiSrc.provider === "openrouter") ? "OpenRouter AI" : "Gemini AI";
  const sourcesList = [
    { key: "crashlytics_bq", name: "Crashlytics BigQuery", obj: srcs.crashlytics_bq },
    { key: "firebase_sessions", name: "Firebase Sessions", obj: srcs.firebase_sessions },
    { key: "mcp_crashlytics", name: "Crashlytics MCP", obj: srcs.mcp_crashlytics },
    { key: (srcs.ai ? "ai" : "gemini_ai"), name: aiName, obj: aiSrc },
  ];

  grid.innerHTML = sourcesList.map(s => {
    const res = resolveSourceHealth(s.key, s.obj, DATA.generated_at);
    return `
      <div class="data-source-item">
        <div class="data-source-item-top">
          <span class="data-source-name">${esc(s.name)}</span>
          <span class="badge badge-status ${res.badgeClass}">${esc(res.label)}</span>
        </div>
        <div class="data-source-freshness">
          <span>新鮮度</span>
          <b>${esc(res.freshness)}</b>
        </div>
        <div class="data-source-note ${res.status === 'error' ? 'error' : (res.status === 'stale' ? 'warning' : '')}">
          ${esc(res.note)}
        </div>
      </div>
    `;
  }).join("");
}


function renderAlertDeliveryObservability() {
  const app = getCurAppData();
  if (!app) return;
  const ad = app.alert_delivery;
  const healthGrid = $("alertDeliveryHealthGrid");
  const countBadge = $("alertDeliveryCountBadge");
  const tableContainer = $("alertDeliveryTableContainer");

  if (!healthGrid || !tableContainer) return;

  if (!ad || !ad.health) {
    healthGrid.innerHTML = `
      <div class="chart-card col-12">
        <div style="font-size:12.5px;color:var(--text-muted)">尚未載入警報投遞狀態資料。</div>
      </div>
    `;
    tableContainer.innerHTML = '<div style="padding:12px;font-size:12.5px;color:var(--text-muted)">尚無發送審計紀錄。</div>';
    return;
  }

  const h = ad.health;
  let hBadgeClass = "badge-stability-baseline";
  let hBadgeText = "無投遞紀錄 (NO DATA)";
  let hIcon = "○";
  if (h.status === "healthy") {
    hBadgeClass = "badge-active";
    hBadgeText = "正常 (HEALTHY)";
    hIcon = "✓";
  } else if (h.status === "degraded") {
    hBadgeClass = "badge-fatal";
    hBadgeText = "異常降級 (DEGRADED)";
    hIcon = "✕";
  } else if (h.status === "unavailable") {
    hBadgeClass = "badge-fatal";
    hBadgeText = "無法讀取 (UNAVAILABLE)";
    hIcon = "⚠";
  }

  const lastSuccess = h.latest_success_at ? esc(formatFreshness(h.latest_success_at, DATA.generated_at)) : "無成功紀錄";
  const lastFailure = h.latest_failure_at ? esc(formatFreshness(h.latest_failure_at, DATA.generated_at)) : "無失敗紀錄";

  healthGrid.innerHTML = `
    <div class="chart-card col-6">
      <div class="chart-card-header">
        <div class="chart-title">服務連線健康度 (${esc(h.provider || "google_chat")})</div>
        <span class="badge badge-status ${hBadgeClass}">${hIcon} ${esc(hBadgeText)}</span>
      </div>
      <div style="font-size:12.5px;color:var(--text-muted);display:flex;flex-direction:column;gap:6px">
        <div>最後成功發送: <b>${lastSuccess}</b></div>
        <div>最後發送失敗: <b>${lastFailure}</b></div>
        ${h.unresolved_failures > 0 ? `<div style="color:var(--fatal-color);font-weight:600">※ 連續失敗次數: ${h.unresolved_failures} 次</div>` : ""}
        ${h.error_diagnostic ? `<div style="color:var(--fatal-color);font-size:11.5px;padding:4px 8px;background:var(--bg-subtle);border-radius:var(--radius-sm)">診斷訊息: ${esc(h.error_diagnostic)}</div>` : ""}
      </div>
    </div>
    <div class="chart-card col-6">
      <div class="chart-card-header">
        <div class="chart-title">過去 24 小時統計 (24h Activity)</div>
      </div>
      <div style="display:flex;gap:16px;align-items:center;padding:4px 0">
        <div style="flex:1;text-align:center;padding:8px;background:var(--bg-subtle);border-radius:var(--radius-sm)">
          <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">成功送出</div>
          <div style="font-size:18px;font-weight:700;color:#137333" class="mono-num">${h.sent_24h || 0}</div>
        </div>
        <div style="flex:1;text-align:center;padding:8px;background:var(--bg-subtle);border-radius:var(--radius-sm)">
          <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">發送失敗</div>
          <div style="font-size:18px;font-weight:700;color:${(h.failed_24h || 0) > 0 ? '#c5221f' : 'var(--text-main)'}" class="mono-num">${h.failed_24h || 0}</div>
        </div>
        <div style="flex:1;text-align:center;padding:8px;background:var(--bg-subtle);border-radius:var(--radius-sm)">
          <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">抑制/冷卻</div>
          <div style="font-size:18px;font-weight:700;color:#b06000" class="mono-num">${h.suppressed_24h || 0}</div>
        </div>
      </div>
    </div>
  `;

  const recent = ad.recent || [];
  if (countBadge) {
    countBadge.textContent = `${recent.length} 筆紀錄`;
  }

  if (recent.length === 0) {
    tableContainer.innerHTML = '<div style="padding:16px;font-size:12.5px;color:var(--text-muted);text-align:center">尚無發送審計紀錄</div>';
    return;
  }

  const tableRows = recent.map(r => {
    let stBadge = "";
    const st = (r.status || "").toLowerCase();
    if (st === "sent") {
      stBadge = '<span class="badge" style="background:#e6f4ea;color:#137333;font-weight:600">SENT</span>';
    } else if (st === "failed") {
      stBadge = '<span class="badge badge-fatal" style="font-weight:600">FAILED</span>';
    } else if (st === "suppressed") {
      stBadge = '<span class="badge" style="background:#fef7e0;color:#b06000;font-weight:600">SUPPRESSED</span>';
    } else {
      stBadge = '<span class="badge" style="background:var(--bg-subtle);color:var(--text-muted)">PENDING</span>';
    }

    let extraBadges = [];
    if (r.is_recovery) {
      extraBadges.push('<span class="badge" style="background:#ceead6;color:#0d652d;font-weight:700;font-size:10px">復原通知 ↗</span>');
    }
    if (r.dry_run) {
      extraBadges.push('<span class="badge" style="background:var(--bg-subtle);font-size:10px;color:var(--text-muted)">dry-run</span>');
    }

    const httpText = r.http_status != null ? `<span class="badge" style="background:var(--bg-subtle);border:1px solid var(--border);font-size:10px;font-family:var(--font-mono)">${r.http_status}</span>` : '—';
    const detailText = r.suppression_reason || r.error_message || '—';
    const timeDisp = esc((r.attempted_at || "").replace("T", " ").replace("Z", " UTC"));

    return `
      <tr style="border-bottom:1px solid var(--border)">
        <td style="padding:8px 10px;font-family:var(--font-mono);font-size:11.5px;white-space:nowrap">${timeDisp}</td>
        <td style="padding:8px 10px;font-size:12px">${esc((r.platform || "").toUpperCase())}</td>
        <td style="padding:8px 10px;font-family:var(--font-mono);font-size:12px;font-weight:600">${esc(r.version || "")}</td>
        <td style="padding:8px 10px;white-space:nowrap">${stBadge} ${extraBadges.join(" ")}</td>
        <td style="padding:8px 10px;font-size:11.5px">${esc((r.gate_status || "").toUpperCase())}</td>
        <td style="padding:8px 10px">${httpText}</td>
        <td style="padding:8px 10px;font-size:11px;color:${st === 'failed' ? '#c5221f' : 'var(--text-muted)'};max-width:320px;word-break:break-all">${esc(detailText)}</td>
      </tr>
    `;
  }).join("");

  tableContainer.innerHTML = `
    <table style="width:100%;border-collapse:collapse;font-size:12px;text-align:left">
      <thead>
        <tr style="border-bottom:1px solid var(--border);color:var(--text-muted);font-size:11.5px;background:var(--bg-subtle)">
          <th style="padding:8px 10px">時間 (Attempted At)</th>
          <th style="padding:8px 10px">平台</th>
          <th style="padding:8px 10px">版本</th>
          <th style="padding:8px 10px">發送狀態</th>
          <th style="padding:8px 10px">閘門結果</th>
          <th style="padding:8px 10px">HTTP</th>
          <th style="padding:8px 10px">原因 / 錯誤摘要 (Sanitized Detail)</th>
        </tr>
      </thead>
      <tbody>
        ${tableRows}
      </tbody>
    </table>
  `;
}


function renderPipelines() {
  const app = getCurAppData();
  if (!app) return;
  const srcs = app.sources || {};
  const grid = $("pipelineCardsGrid");
  if (!grid) return;

  const aiSrc = srcs.ai || srcs.gemini_ai;
  const aiName = (aiSrc && aiSrc.provider === "openrouter") ? "OpenRouter AI Analysis" : "Gemini AI Analysis";
  const pipelines = [
    { key: "crashlytics_bq", name: "Crashlytics BigQuery", obj: srcs.crashlytics_bq },
    { key: "firebase_sessions", name: "Firebase Sessions Export", obj: srcs.firebase_sessions },
    { key: "mcp_crashlytics", name: "Crashlytics MCP Server", obj: srcs.mcp_crashlytics },
    { key: (srcs.ai ? "ai" : "gemini_ai"), name: aiName, obj: aiSrc },
  ];

  grid.innerHTML = pipelines.map(p => {
    const res = resolveSourceHealth(p.key, p.obj, DATA.generated_at);
    return `
      <div class="chart-card col-6">
        <div class="chart-card-header">
          <div class="chart-title">${esc(p.name)}</div>
          <span class="badge badge-status ${res.badgeClass}">
            ${esc(res.label)}
          </span>
        </div>
        <div style="font-size:12.5px;color:var(--text-muted);display:flex;flex-direction:column;gap:6px">
          <div>最後同步時間: <b>${esc(res.timestamp)}</b> (${esc(res.freshness)})</div>
          <div class="data-source-note ${res.status === 'error' ? 'error' : (res.status === 'stale' ? 'warning' : '')}">
            備註: ${esc(res.note)}
          </div>
          ${res.isSupplemental ? `<div style="font-size:11.5px;color:var(--warning-text)">※ 正在使用 last-known-good supplemental 快取資料補強</div>` : ""}
        </div>
      </div>
    `;
  }).join("");

  const limits = app.limitations || [];
  const limitList = $("limitationsList");
  if (limitList) {
    limitList.innerHTML = limits.map(l => `<li>${esc(l)}</li>`).join("") || `<li>無特殊資料限制。</li>`;
  }

  renderAlertDeliveryObservability();
}
"""
