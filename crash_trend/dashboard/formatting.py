"""Formatting and display helpers for Dashboard V2 client script (Issue #53).

Provides client-side JavaScript formatting functions, escaping,
SemVer comparison helpers, and badge rendering logic.
"""

from __future__ import annotations


def get_formatting_js() -> str:
    """Returns JavaScript formatting, escaping, and display helper functions."""
    return r"""
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = n => (n != null && !isNaN(n)) ? Number(n).toLocaleString("zh-Hant") : "0";

// ── Data Freshness and Source Health Resolution (Issue #23) ──
function formatFreshness(isoStr, refIsoStr) {
  if (!isoStr) return "—";
  const d = new Date(isoStr);
  if (isNaN(d.getTime())) return isoStr;
  const now = refIsoStr ? new Date(refIsoStr) : new Date();
  const diffMs = Math.max(0, now.getTime() - d.getTime());
  const diffSec = Math.floor(diffMs / 1000);
  const diffMin = Math.floor(diffSec / 60);
  const diffHours = Math.floor(diffMin / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffMin < 2) return "本次同步";
  if (diffMin < 60) return `${diffMin} 分鐘前`;
  if (diffHours < 24) return `${diffHours} 小時前`;
  if (diffDays < 30) return `${diffDays} 天前`;
  const diffMonths = Math.floor(diffDays / 30);
  return `${diffMonths} 個月前`;
}

function resolveSourceHealth(sourceKey, srcObj, generatedAt) {
  if (!srcObj) {
    return {
      status: "unavailable",
      label: "未提供",
      badgeClass: "badge-deprecated",
      dotClass: "disabled",
      freshness: "—",
      timestamp: "—",
      note: "無來源設定",
      isSupplemental: false,
    };
  }

  const rawStatus = (srcObj.status || "").toLowerCase();
  const errMsg = srcObj.error_message || "";
  const ts = srcObj.last_sync_timestamp;
  const freshness = formatFreshness(ts, generatedAt);

  let status = rawStatus || "unavailable";
  let label = "正常";
  let badgeClass = "badge-active";
  let dotClass = "available";
  let note = errMsg || "";
  let isSupplemental = false;

  const isExplicitDisabled = status === "disabled" ||
    errMsg.includes("disabled") ||
    errMsg.includes("已停用") ||
    errMsg.includes("未開啟") ||
    errMsg.includes("not configured");

  const isStale = status === "stale" ||
    (status === "available" && (errMsg.includes("過期") || errMsg.includes("stale")));

  if (isExplicitDisabled) {
    status = "disabled";
    label = "— 未開啟";
    badgeClass = "badge-deprecated";
    dotClass = "disabled";
    if (!note) note = "來源未啟用";
  } else if (isStale) {
    status = "stale";
    label = "⚠ 過期";
    badgeClass = "badge-maintenance";
    dotClass = "stale";
    isSupplemental = true;
    if (!note) note = "快取已逾期（使用上次快取中）";
    else if (!note.includes("使用")) note = `${note}（使用上次快取中）`;
  } else if (status === "error" || (status !== "available" && errMsg && !ts)) {
    status = "error";
    label = "✕ 錯誤";
    badgeClass = "badge-fatal";
    dotClass = "error";
    if (!note) note = "資料來源查詢或連線失敗";
  } else if (status === "insufficient_data") {
    status = "insufficient_data";
    label = "ℹ 資料不足";
    badgeClass = "badge-info";
    dotClass = "insufficient_data";
    if (!note) note = "區間內無足夠資料";
  } else if (status === "available") {
    status = "available";
    label = "✓ 正常";
    badgeClass = "badge-active";
    dotClass = "available";
    if (!note) {
      if (sourceKey === "ai" || sourceKey === "gemini_ai") {
        const modePart = srcObj.requested_mode ? ` [${srcObj.requested_mode}]` : "";
        const fbPart = srcObj.fallback_used ? " (Fallback)" : "";
        note = `模型: ${srcObj.model || "—"}${modePart}${fbPart}`;
      } else {
        note = "資料來源正常運作";
      }
    }
  } else {
    status = "unavailable";
    label = "未提供";
    badgeClass = "badge-deprecated";
    dotClass = "disabled";
  }

  return {
    status,
    label,
    badgeClass,
    dotClass,
    freshness,
    timestamp: ts || "—",
    note,
    isSupplemental,
    raw: srcObj,
  };
}

function getLifecycleBadgeHtml(lc) {
  if (!lc || !lc.status) return "";
  const st = lc.status;
  const reason = lc.reason || "";
  if (st === "new_in_latest") {
    return `<span class="badge badge-lifecycle-new" title="${esc(reason)}">🔴 新版引入</span>`;
  } else if (st === "persistent") {
    return `<span class="badge badge-lifecycle-persistent" title="${esc(reason)}">🟡 持續存在</span>`;
  } else if (st === "regressed") {
    return `<span class="badge badge-lifecycle-regressed" title="${esc(reason)}">🟣 回歸</span>`;
  } else if (st === "resolved") {
    return `<span class="badge badge-lifecycle-resolved" title="${esc(reason)}">🟢 已收斂</span>`;
  } else if (st === "not_observed_latest") {
    return `<span class="badge badge-lifecycle-not-observed" title="${esc(reason)}">⚪ 最新版未見</span>`;
  }
  return "";
}

function getErrorTypeBadgeHtml(errorType) {
  const t = String(errorType || "").toUpperCase();
  if (t === "FATAL") {
    return `<span class="badge badge-fatal" title="【致命閃退 (FATAL)】&#10;未捕獲的嚴重崩潰，App 強制中斷退出，屬影響最嚴重的錯誤層級。">💥 FATAL 閃退</span>`;
  } else if (t === "ANR") {
    return `<span class="badge badge-anr" title="【當機無回應 (ANR)】&#10;Application Not Responding：主執行緒卡住超過 5 秒，系統跳出等待或強制關閉對話框。">⏳ ANR 無回應</span>`;
  } else if (t === "NON_FATAL" || t === "NON-FATAL") {
    return `<span class="badge badge-nonfatal" title="【非致命異常 (NON_FATAL)】&#10;App 依然正常運作未閃退，但內部發生錯誤或拋出 Exception（已被程式 try-catch 攔截或 Flutter 框架捕獲）。&#10;可能導致局部功能失效、按鈕沒反應或特定畫面報錯。">⚠️ NON_FATAL (未閃退)</span>`;
  }
  return `<span class="badge" title="未知的錯誤類型">${esc(errorType || "—")}</span>`;
}

// SemVer comparison and authoritative version discovery
function parseSemverParts(v) {
  if (!v) return [0, 0, 0];
  const cleaned = String(v).replace(/^v/i, "").trim();
  const main = cleaned.split(/[-+]/)[0];
  const parts = main.split(".").map(p => {
    const num = parseInt(p, 10);
    return isNaN(num) ? 0 : num;
  });
  while (parts.length < 3) parts.push(0);
  return parts;
}

function compareSemver(v1, v2) {
  const p1 = parseSemverParts(v1);
  const p2 = parseSemverParts(v2);
  for (let i = 0; i < Math.max(p1.length, p2.length); i++) {
    const a = p1[i] || 0;
    const b = p2[i] || 0;
    if (a !== b) return a - b;
  }
  return String(v1).localeCompare(String(v2));
}

function getAppAuthoritativeVersions(app, snap, platformFilter) {
  if (!app) return [];
  const versionMap = new Map();

  // 1. From version_health (top-level and snapshot)
  const vhList = snap?.version_health || app.version_health || [];
  vhList.forEach(vh => {
    if (!vh || !vh.version) return;
    const ver = String(vh.version).trim();
    if (!ver) return;
    const pf = (vh.platform || "android").toLowerCase();
    if (!versionMap.has(ver)) {
      versionMap.set(ver, { version: ver, platforms: new Set(), latestPlatforms: new Set() });
    }
    const entry = versionMap.get(ver);
    entry.platforms.add(pf);
    if (vh.status === "latest") entry.latestPlatforms.add(pf);
  });

  // 2. From distributions.app_versions
  const distList = snap?.distributions?.app_versions || app.distributions?.app_versions || [];
  distList.forEach(dist => {
    const ver = String(dist.app_version || dist.version || "").trim();
    if (!ver) return;
    const pf = (dist.platform || "").toLowerCase();
    if (!versionMap.has(ver)) {
      versionMap.set(ver, { version: ver, platforms: new Set(), latestPlatforms: new Set() });
    }
    if (pf) versionMap.get(ver).platforms.add(pf);
  });

  // 3. Filter by platform if specified
  let versions = Array.from(versionMap.values());
  if (platformFilter && platformFilter !== "ALL") {
    const pfLow = platformFilter.toLowerCase();
    versions = versions.filter(entry => entry.platforms.size === 0 || entry.platforms.has(pfLow));
  }

  // Sort descending by semver
  versions.sort((a, b) => compareSemver(b.version, a.version));
  return versions;
}

function resolveLatestVersion(app, snap, platformFilter) {
  const versions = getAppAuthoritativeVersions(app, snap, platformFilter);
  if (!versions.length) return null;
  if (platformFilter && platformFilter !== "ALL") {
    const pfLow = platformFilter.toLowerCase();
    const marked = versions.find(v => v.latestPlatforms && v.latestPlatforms.has(pfLow));
    if (marked) return marked.version;
  } else {
    const marked = versions.find(v => v.latestPlatforms && v.latestPlatforms.size > 0);
    if (marked) return marked.version;
  }
  return versions[0].version;
}

function resolveLatestVersionsByPlatform(app, snap) {
  const result = { android: null, ios: null };
  if (!app) return result;
  result.android = resolveLatestVersion(app, snap, "android");
  result.ios = resolveLatestVersion(app, snap, "ios");
  return result;
}
"""
