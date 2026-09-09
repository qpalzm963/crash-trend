"""Issues view HTML markup and JS rendering logic (Issue #53)."""

from __future__ import annotations


def get_issues_html() -> str:
    """Returns HTML markup for #view-issues."""
    return '    <!-- VIEW: ISSUES (問題列表) -->\n    <section class="view-container" id="view-issues">\n      <div class="section-header">\n        <div>\n          <h2 class="section-title">問題列表 (Issues)</h2>\n          <div class="section-subtitle">點擊展開查看發生趨勢、AI 建議修法、元兇程式碼與 Stack Trace</div>\n        </div>\n      </div>\n\n      <div class="table-container">\n        <div class="table-toolbar">\n          <div class="filters-group">\n            <select class="filter-select" id="filterErrorType" onchange="renderIssuesList()">\n              <option value="ALL">全部層級</option>\n              <option value="FATAL">FATAL (致命閃退)</option>\n              <option value="ANR">ANR (無回應)</option>\n              <option value="NON_FATAL">NON_FATAL (非致命)</option>\n            </select>\n            <select class="filter-select" id="filterPlatform" onchange="handlePlatformFilterChange()">\n              <option value="ALL">全部平台</option>\n              <option value="android">Android</option>\n              <option value="ios">iOS</option>\n            </select>\n            <select class="filter-select" id="filterVersion" onchange="renderIssuesList()">\n              <option value="ALL">全部版本</option>\n              <option value="LATEST">最新版本</option>\n            </select>\n            <select class="filter-select" id="filterPriority" onchange="renderIssuesList()">\n              <option value="ALL">全部優先級</option>\n              <option value="P0">P0</option>\n              <option value="P1">P1</option>\n              <option value="P2">P2</option>\n              <option value="P3">P3</option>\n            </select>\n            <select class="filter-select" id="filterLifecycle" onchange="renderIssuesList()">\n              <option value="ALL">全部生命週期</option>\n              <option value="new_in_latest">🔴 新版引入</option>\n              <option value="persistent">🟡 持續存在</option>\n              <option value="regressed">🟣 回歸</option>\n              <option value="resolved">🟢 已收斂</option>\n              <option value="not_observed_latest">⚪ 最新版未見</option>\n            </select>\n            <select class="filter-select" id="sortIssuesSelect" onchange="setSort(this.value)">\n              <option value="priority">依優先級 (P0 ~ P3)</option>\n              <option value="events">依事件數 (Events)</option>\n              <option value="users">依受影響用戶 (Users)</option>\n              <option value="last_seen">依最近出現時間</option>\n            </select>\n          </div>\n          <div id="issuesCountBadge" style="font-size:12px;color:var(--text-muted)">共 0 個問題</div>\n        </div>\n\n        <div id="issuesListContainer">\n          <!-- Accordion list items -->\n        </div>\n      </div>\n    </section>\n\n'


def get_issues_js() -> str:
    """Returns JavaScript rendering functions for Issues list and accordion rows."""
    return '''function formatTs(ts) {
  if (!ts) return "—";
  const s = String(ts).replace("T", " ").replace("Z", "");
  return s.length >= 16 ? s.slice(0, 16) : s;
}

function renderOccurrenceTimelineHtml(timeline, errorType) {
  if (!timeline || !timeline.daily || !timeline.daily.length) {
    return `<div style="padding:16px;text-align:center;color:var(--text-muted);font-size:12px;background:var(--bg-surface);border:1px dashed var(--border);border-radius:var(--radius-md)">尚無每日發生趨勢資料 (No daily occurrence data)</div>`;
  }

  const daily = timeline.daily;
  const maxEvents = Math.max(...daily.map(d => d.events || 0), 1);
  const chartHeight = 84;

  // Track unique versions seen across daily points in chronological order
  const versionRanges = {};
  daily.forEach(d => {
    Object.keys(d.versions || {}).forEach(v => {
      if (!versionRanges[v]) {
        versionRanges[v] = { version: v, firstDate: d.date, lastDate: d.date, totalEvents: 0 };
      }
      versionRanges[v].lastDate = d.date;
      versionRanges[v].totalEvents += (d.versions[v] || 0);
    });
  });

  // Render bars
  const barsHtml = daily.map((d, i) => {
    const ev = d.events || 0;
    const barH = ev === 0 ? 3 : Math.max(4, Math.round((ev / maxEvents) * chartHeight));
    const isPeak = timeline.peak_date && d.date === timeline.peak_date && ev > 0;

    // Determine bar color
    let barColor = "var(--accent)";
    if (d.fatal_events > 0) {
      barColor = "var(--danger)";
    } else if (d.anr_events > 0) {
      barColor = "var(--warning)";
    }

    // Versions breakdown for tooltip
    const verList = Object.entries(d.versions || {})
      .sort((a, b) => b[1] - a[1])
      .map(([ver, cnt]) => `v${ver}: ${cnt}次`)
      .join(", ");

    const tooltip = `${d.date}\\n` +
      `事件數: ${ev} 次${isPeak ? ' (高峰)' : ''}\\n` +
      `受影響用戶: ${d.affected_users || 0} 位\\n` +
      `Fatal: ${d.fatal_events || 0} · ANR: ${d.anr_events || 0} · Non-fatal: ${d.non_fatal_events || 0}` +
      (verList ? `\\n版本: ${verList}` : '');

    // Show date label on every bar for short lists (<=14 days), or periodically
    const showLabel = daily.length <= 14 || (i % Math.ceil(daily.length / 10) === 0) || (i === daily.length - 1);
    const dateLabel = d.date.slice(5);

    // Check if new version appeared on this day
    const newVersionsOnDay = Object.keys(d.versions || {}).filter(v => versionRanges[v]?.firstDate === d.date);

    return `
      <div style="flex:1;min-width:14px;max-width:36px;display:flex;flex-direction:column;align-items:center;position:relative" title="${esc(tooltip)}">
        ${newVersionsOnDay.length ? `
          <div style="font-size:9px;font-family:var(--font-mono);font-weight:700;color:var(--accent);white-space:nowrap;margin-bottom:2px;transform:scale(0.85);transform-origin:bottom center" title="版本首次出現: ${esc(newVersionsOnDay.join(', '))}">
            v${esc(newVersionsOnDay[0])}
          </div>
        ` : `<div style="height:14px"></div>`}
        <div style="width:100%;height:${chartHeight}px;display:flex;align-items:flex-end;justify-content:center;background:var(--bg-surface);border-radius:2px">
          <div style="width:80%;height:${barH}px;background:${barColor};border-radius:2px 2px 0 0;opacity:${ev === 0 ? 0.2 : 0.9};transition:height 0.2s,opacity 0.2s;${isPeak ? 'box-shadow:0 0 0 2px var(--danger-text)' : ''}"></div>
        </div>
        <div style="font-size:10px;color:${isPeak ? 'var(--danger-text)' : 'var(--text-muted)'};font-family:var(--font-mono);margin-top:4px;white-space:nowrap;font-weight:${isPeak ? '700' : 'normal'}">
          ${showLabel ? dateLabel : ''}
        </div>
      </div>
    `;
  }).join("");

  // Version overlay / correlation summary bands
  const verSummaryHtml = Object.values(versionRanges).length > 0 ? `
    <div style="margin-top:10px;padding-top:8px;border-top:1px dashed var(--border);display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:11px">
      <span style="font-weight:600;color:var(--text-muted)">版本切換與跨度：</span>
      ${Object.values(versionRanges).map(vr => `
        <span style="background:var(--bg-surface);border:1px solid var(--border);padding:2px 8px;border-radius:var(--radius-sm);font-family:var(--font-mono);color:var(--text-main)">
          <b>v${esc(vr.version)}</b>: ${vr.firstDate.slice(5)} ~ ${vr.lastDate.slice(5)} (${fmt(vr.totalEvents)} 次)
        </span>
      `).join("")}
    </div>
  ` : "";

  return `
    <div style="background:var(--bg-subtle);border-radius:var(--radius-md);padding:12px;border:1px solid var(--border)">
      <div style="display:flex;align-items:flex-end;gap:3px;overflow-x:auto;padding-bottom:4px">
        ${barsHtml}
      </div>
      ${verSummaryHtml}
    </div>
  `;
}

function renderIssuesList() {
  const app = getCurAppData();
  if (!app) return;
  const snap = getCurPeriodSnapshot();
  const issues = snap?.top_issues || app.top_issues || [];

  const filterErr = $("filterErrorType") ? $("filterErrorType").value : "ALL";
  const filterPlat = $("filterPlatform") ? $("filterPlatform").value : "ALL";
  const filterPrio = $("filterPriority") ? $("filterPriority").value : "ALL";
  const filterLife = $("filterLifecycle") ? $("filterLifecycle").value : "ALL";
  const filterVer = $("filterVersion") ? $("filterVersion").value : "ALL";

  const latestMap = resolveLatestVersionsByPlatform(app, snap);

  const filtered = issues.map(iss => {
    let scopedEvents = iss.events ?? 0;
    let scopedUsers = iss.affected_users ?? 0;
    let matchesVersion = true;
    let targetVersion = null;

    if (filterVer === "LATEST") {
      if (filterPlat !== "ALL") {
        targetVersion = filterPlat === "ios" ? latestMap.ios : latestMap.android;
      } else {
        const pf = (iss.platform || "android").toLowerCase();
        targetVersion = latestMap[pf] || latestMap.android || latestMap.ios;
      }
    } else if (filterVer !== "ALL") {
      targetVersion = filterVer;
    }

    if (targetVersion) {
      const cleanTarget = String(targetVersion).replace(/^v/i, "").trim();
      const vDist = (iss.version_distribution || []).find(
        v => String(v.version).replace(/^v/i, "").trim() === cleanTarget
      );
      if (vDist && ((vDist.events || 0) > 0 || (vDist.users || 0) > 0)) {
        scopedEvents = vDist.events || 0;
        scopedUsers = vDist.users || 0;
      } else {
        matchesVersion = false;
      }
    }

    return {
      raw: iss,
      scopedEvents,
      scopedUsers,
      targetVersion,
      matchesVersion,
    };
  }).filter(item => {
    if (!item.matchesVersion) return false;
    const iss = item.raw;
    if (filterErr !== "ALL" && iss.error_type !== filterErr) return false;
    if (filterPlat !== "ALL" && iss.platform !== filterPlat) return false;
    if (filterPrio !== "ALL" && iss.priority?.level !== filterPrio) return false;
    if (filterLife !== "ALL" && (iss.lifecycle?.status || "persistent") !== filterLife) return false;
    if (searchQuery) {
      const target = `${iss.title} ${iss.subtitle} ${iss.issue_id} ${iss.blame_frame?.file || ""}`.toLowerCase();
      if (!target.includes(searchQuery)) return false;
    }
    return true;
  });

  // Sort filtered list
  filtered.sort((itemA, itemB) => {
    const a = itemA.raw;
    const b = itemB.raw;
    let valA, valB;
    if (curSortField === "priority") {
      valA = a.priority?.score ?? 0;
      valB = b.priority?.score ?? 0;
    } else if (curSortField === "events") {
      valA = itemA.scopedEvents;
      valB = itemB.scopedEvents;
    } else if (curSortField === "users") {
      valA = itemA.scopedUsers;
      valB = itemB.scopedUsers;
    } else if (curSortField === "last_seen") {
      valA = a.last_seen_timestamp || "";
      valB = b.last_seen_timestamp || "";
    } else {
      valA = itemA.scopedEvents;
      valB = itemB.scopedEvents;
    }
    if (valA < valB) return curSortAsc ? -1 : 1;
    if (valA > valB) return curSortAsc ? 1 : -1;
    return 0;
  });

  let verBadgeNote = "";
  if (filterVer === "LATEST") {
    if (filterPlat === "ALL") {
      const parts = [];
      if (latestMap.android) parts.push(`Android: ${latestMap.android}`);
      if (latestMap.ios) parts.push(`iOS: ${latestMap.ios}`);
      verBadgeNote = ` [版本: 最新 (依各平台: ${parts.join(', ')})]`;
    } else {
      const pfVer = filterPlat === "ios" ? latestMap.ios : latestMap.android;
      verBadgeNote = ` [版本: 最新 (${pfVer || ''})]`;
    }
  } else if (filterVer !== "ALL") {
    verBadgeNote = ` [版本: ${filterVer}]`;
  }
  $("issuesCountBadge").textContent = `顯示 ${filtered.length} / ${issues.length} 個問題${verBadgeNote} (排序: ${curSortField} ${curSortAsc ? '▲' : '▼'})`;

  // Full Issues Accordion List
  const container = $("issuesListContainer");
  if (!filtered.length) {
    container.innerHTML = `<div class="empty-state"><div class="empty-state-title">無符合條件之問題</div></div>`;
    return;
  }

  container.innerHTML = filtered.map((item, idx) => {
    const iss = item.raw;
    const pLevel = iss.priority?.level || "P2";
    const errCls = iss.error_type === "FATAL" ? "badge-fatal" : (iss.error_type === "ANR" ? "badge-anr" : "badge-nonfatal");
    const ai = iss.ai_analysis || {};
    const blame = iss.blame_frame || {};
    const detail = iss.detail || {};
    const lc = iss.lifecycle;
    const timeline = iss.occurrence_timeline;
    const verScopedTag = item.targetVersion ? ` <span class="badge" style="background:var(--bg-subtle);font-size:11px;font-weight:normal;color:var(--text-secondary)">v${esc(item.targetVersion)}</span>` : "";

    return `
      <div class="issue-accordion" id="issue-acc-${idx}">
        <div class="issue-summary-row" onclick="toggleIssueDetail(${idx})">
          <span class="issue-rank">${String(idx + 1).padStart(2, "0")}</span>
          <span class="badge badge-${pLevel.toLowerCase()}">${esc(pLevel)}</span>
          <span class="badge ${errCls}">${esc(iss.error_type)}</span>
          ${getLifecycleBadgeHtml(lc)}
          <div class="issue-title-group">
            <div class="issue-main-title">${esc(iss.title)}</div>
            <div class="issue-sub-title">${esc(iss.subtitle || "")}</div>
          </div>
          <div class="issue-stats-group">
            <span>${esc(iss.platform.toUpperCase())}</span>
            <span><b>${fmt(item.scopedEvents)}</b> 次事件${verScopedTag}</span>
            <span><b>${fmt(item.scopedUsers)}</b> 位用戶${verScopedTag}</span>
            <span style="font-size:11px">見於 ${esc(iss.last_seen_version || "?")}</span>
          </div>
        </div>

        <div class="issue-detail-panel" id="issue-detail-${idx}">
          <!-- 首次與最近出現時間與版本 (First / Last Seen) -->
          <div class="detail-box" style="margin-bottom:12px">
            <div class="detail-box-title">出現時間與版本 (First / Last Seen)</div>
            <div class="detail-box-content" style="display:flex;gap:20px;flex-wrap:wrap;align-items:center;font-size:12.5px">
              <div><b>首次發生：</b><span class="mono-num">${esc(formatTs(iss.first_seen_timestamp))}</span> <span class="badge" style="background:var(--bg-subtle)">v${esc(iss.first_seen_version || "?")}</span></div>
              <div><b>最近發生：</b><span class="mono-num">${esc(formatTs(iss.last_seen_timestamp))}</span> <span class="badge" style="background:var(--bg-subtle)">v${esc(iss.last_seen_version || "?")}</span></div>
              ${timeline?.peak_date ? `<div><b>最高峰日：</b><span class="mono-num">${esc(timeline.peak_date)}</span> (<b>${fmt(timeline.peak_events)}</b> 次事件)</div>` : ""}
            </div>
          </div>

          <!-- 發生趨勢 (Occurrence Timeline) -->
          <div class="detail-box" style="margin-bottom:12px">
            <div class="detail-box-title" style="display:flex;justify-content:space-between;align-items:center">
              <span>發生趨勢 (Occurrence Timeline)</span>
              <span style="font-size:11px;font-weight:normal;color:var(--text-muted)">
                ${timeline?.daily?.length ? `共 ${timeline.daily.length} 天` : "無每日資料"}
                ${timeline?.peak_date ? ` · 高峰日 ${esc(timeline.peak_date)} (${fmt(timeline.peak_events)} 次)` : ""}
              </span>
            </div>
            <div class="detail-box-content" style="margin-top:6px">
              ${renderOccurrenceTimelineHtml(timeline, iss.error_type)}
            </div>
          </div>

          <div class="detail-grid">
            <div class="detail-box">
              <div class="detail-box-title">AI Root Cause 推測</div>
              <div class="detail-box-content">${esc(ai.root_cause || "待分析")}</div>
            </div>
            <div class="detail-box">
              <div class="detail-box-title">AI 建議修法 (預估工作量 ${esc(ai.effort || "?")})</div>
              <div class="detail-box-content">${esc(ai.suggested_fix || "待分析")}</div>
            </div>
          </div>

          ${lc ? `
            <div class="detail-box" style="margin-bottom:12px">
              <div class="detail-box-title">生命週期與回歸狀態 (Issue Lifecycle)</div>
              <div class="detail-box-content" style="display:flex;gap:16px;flex-wrap:wrap;align-items:center;font-size:13px">
                <div>${getLifecycleBadgeHtml(lc)}</div>
                <div><b>說明：</b>${esc(lc.reason || "—")}</div>
                <div><b>版本範圍：</b><code>${esc(lc.first_seen_version)}</code> → <code>${esc(lc.last_seen_version)}</code> (見於 ${lc.versions_seen} 個版本)</div>
                <div><b>可信度：</b><span class="badge" style="background:var(--bg-subtle)">${esc(lc.confidence)}</span></div>
                ${lc.previously_absent_since ? `<div><b>曾消失自版本：</b><code>${esc(lc.previously_absent_since)}</code></div>` : ""}
                ${lc.reappeared_version ? `<div><b>回歸版本：</b><code>${esc(lc.reappeared_version)}</code></div>` : ""}
              </div>
            </div>
          ` : ""}

          ${(iss.version_distribution && iss.version_distribution.length > 0) ? `
            <div class="detail-box" style="margin-bottom:12px">
              <div class="detail-box-title">各版本影響分布 (Version Breakdown)</div>
              <div class="detail-box-content">
                <table style="width:100%;font-size:12px;border-collapse:collapse">
                  <thead>
                    <tr style="border-bottom:1px solid var(--border-color);color:var(--text-muted);text-align:left">
                      <th style="padding:4px 8px">版本</th>
                      <th style="padding:4px 8px;text-align:right">事件數</th>
                      <th style="padding:4px 8px;text-align:right">受影響用戶</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${iss.version_distribution.map(vd => `
                      <tr style="border-bottom:1px solid var(--border-color)">
                        <td style="padding:4px 8px"><code>${esc(vd.version)}</code></td>
                        <td style="padding:4px 8px;text-align:right" class="mono-num">${fmt(vd.events)}</td>
                        <td style="padding:4px 8px;text-align:right" class="mono-num">${fmt(vd.users)}</td>
                      </tr>
                    `).join("")}
                  </tbody>
                </table>
              </div>
            </div>
          ` : ""}

          ${blame.file ? `
            <div class="detail-box">
              <div class="detail-box-title">元兇程式碼位置 (Blame Frame)</div>
              <div class="detail-box-content" style="font-family:var(--font-mono);font-size:12px">
                <code>${esc(blame.file)}${blame.line ? ":" + esc(blame.line) : ""}</code>
                ${blame.symbol ? ` · <code>${esc(blame.symbol)}</code>` : ""}
              </div>
            </div>
          ` : ""}

          ${detail.stack_trace ? `
            <div class="detail-box">
              <div class="detail-box-title">Crashlytics Stack Trace</div>
              <pre class="code-stack">${esc(detail.stack_trace)}</pre>
            </div>
          ` : ""}

          <button class="btn-copy-prompt" onclick="copyFixPrompt('${esc(iss.issue_id)}')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            複製 AI 修復 Prompt
          </button>
        </div>
      </div>
    `;
  }).join("");
}

function toggleIssueDetail(idx) {
  const panel = $("issue-detail-" + idx);
  if (panel) panel.classList.toggle("open");
}

function copyFixPrompt(issueId) {
  const app = getCurAppData();
  if (!app) return;
  const snap = getCurPeriodSnapshot();
  const issues = snap?.top_issues || app.top_issues || [];
  const iss = issues.find(i => i.issue_id === issueId) || (app.top_issues || []).find(i => i.issue_id === issueId);
  if (!iss) return;

  const ai = iss.ai_analysis || {};
  const blame = iss.blame_frame || {};
  const detail = iss.detail || {};
  const lc = iss.lifecycle;
  const timeline = iss.occurrence_timeline;

  const promptLines = [
    `# Crash 修復請求：${iss.title}`,
    "",
    `- App: ${app.metadata?.display_name || curAppId} (${iss.platform})`,
    `- Issue ID: ${iss.issue_id}`,
    `- 層級: ${iss.error_type} | 優先級: ${iss.priority?.level || "P2"} (分數: ${iss.priority?.score || 0})`,
    `- 影響: ${fmt(iss.affected_users)} 位用戶 / ${fmt(iss.events)} 次崩潰事件`,
    `- 首次發生: ${iss.first_seen_timestamp || "?"} (v${iss.first_seen_version || "?"})`,
    `- 最近發生: ${iss.last_seen_timestamp || "?"} (v${iss.last_seen_version || "?"})`,
  ];

  if (timeline?.peak_date) {
    promptLines.push(`- 發生高峰: ${timeline.peak_date} (${fmt(timeline.peak_events)} 次事件)`);
  }

  const selVer = $("filterVersion") ? $("filterVersion").value : "ALL";
  if (selVer !== "ALL") {
    const platFilter = $("filterPlatform") ? $("filterPlatform").value : "ALL";
    const latestMap = resolveLatestVersionsByPlatform(app, snap);
    let targetVer = null;
    if (selVer === "LATEST") {
      if (platFilter !== "ALL") {
        targetVer = platFilter === "ios" ? latestMap.ios : latestMap.android;
      } else {
        const pf = (iss.platform || "android").toLowerCase();
        targetVer = latestMap[pf] || latestMap.android || latestMap.ios;
      }
    } else {
      targetVer = selVer;
    }
    if (targetVer) {
      const cleanTarget = String(targetVer).replace(/^v/i, "").trim();
      const vDist = (iss.version_distribution || []).find(v => String(v.version).replace(/^v/i, "").trim() === cleanTarget);
      if (vDist) {
        promptLines.push(`- 篩選版本: ${targetVer} (此版本事件數: ${fmt(vDist.events)}, 受影響用戶: ${fmt(vDist.users)})`);
      } else {
        promptLines.push(`- 篩選版本: ${targetVer}`);
      }
    }
  }

  if (lc) {
    promptLines.push(`- 生命週期: ${lc.status} (${lc.reason || "無說明"})`);
  }
  if (blame.file) promptLines.push(`- 元兇位置: ${blame.file}${blame.line ? ":" + blame.line : ""}`);
  if (iss.subtitle) promptLines.push(`- 錯誤特徵: ${iss.subtitle}`);

  promptLines.push("", "## AI 分析與建議", `Root Cause: ${ai.root_cause || "需人工檢驗"}`, `建議修法: ${ai.suggested_fix || "—"}`);

  if (detail.stack_trace) {
    promptLines.push("", "## Stack Trace", "```", detail.stack_trace, "```");
  }

  promptLines.push("", "請依據上述資訊定位 Root cause 並進行代碼修復。修復完成後請說明修正方案。");

  const fullText = promptLines.join("\\n");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(fullText).then(() => showToast("已複製 AI 修復 Prompt")).catch(() => fallbackCopy(fullText));
  } else {
    fallbackCopy(fullText);
  }
}

function fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try {
    document.execCommand("copy");
    showToast("已複製 AI 修復 Prompt");
  } catch (e) {
    showToast("複製失敗，請手動複製");
  }
  document.body.removeChild(ta);
}
'''

