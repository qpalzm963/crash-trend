"""Overview section HTML markup and JS rendering logic (Issue #53)."""

from __future__ import annotations

from crash_trend.dashboard.navigation import get_switch_view_call, get_view_container_open_tag

#: 首屏決策面的 DOM id（#73）。測試反向驗證它們仍存在於 render 結果，
#: 避免 renderer 與 JS 之間靠字串巧合維繫。
DECISION_GRID_ID = "overviewDecisionGrid"
DECISION_SUBTITLE_ID = "overviewDecisionSubtitle"

#: 卡片色調 class。``decision-tone-neutral`` 是「不得被讀成綠燈」的那一種：
#: insufficient_data / baseline / gate 未評估 一律落在這裡。
DECISION_TONE_CLASS_PREFIX = "decision-tone-"
DECISION_NEUTRAL_TONE_CLASS = "decision-tone-neutral"
DECISION_PASS_TONE_CLASS = "decision-tone-good"

#: 首屏「與上一版相比」面的 DOM id（#74）。
COMPARISON_GRID_ID = "overviewComparisonGrid"
COMPARISON_SUBTITLE_ID = "overviewComparisonSubtitle"

#: 每個 metric 的 classification 色調 class。``comparison-class-neutral`` 是
#: 「沒有比較資料」的那一種，不得與 ``pass`` 同色。
COMPARISON_CLASS_PREFIX = "comparison-class-"

#: 首屏「版本輔助資訊」面的 DOM id（#91）。lifecycle 摘要與 gate history
#: timeline 都是**輔助**資訊，視覺層級刻意低於發布決策面（見 assets.py 的
#: ``.aux-section-title`` 與 ``.decision-section-title`` 字級差）。
AUX_GRID_ID = "overviewAuxGrid"
AUX_SUBTITLE_ID = "overviewAuxSubtitle"

#: gate history 狀態 pill 的色調 class 前綴。色調本身一律讀 #73 的
#: ``DECISION_TONES``——五種 gate 狀態的語意（尤其 insufficient_data / baseline
#: 不得與 pass 同色）只有那一份定義，這裡只負責把它變成 class 名。
AUX_GATE_TONE_PREFIX = "aux-gate-"
AUX_GATE_NEUTRAL_TONE_CLASS = "aux-gate-neutral"

#: 首屏 timeline 只呈現「近期」；完整歷程走 Releases 頁的詳細 timeline（#61）。
AUX_GATE_HISTORY_LIMIT = 6

#: lifecycle 摘要的四個計數欄位（順序即呈現順序，對應 #91 驗收條件）。
#: 標籤只是計數欄位的顯示名，沒有任何衍生計算。
AUX_LIFECYCLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("introduced_count", "新引入"),
    ("regressed_count", "復發"),
    ("persistent_count", "持續存在"),
    ("resolved_count", "已解決"),
)

#: Top Issues 預覽的 DOM id（#74）。
TOP_ISSUES_BODY_ID = "topIssuesPreviewBody"

#: Top Issues 預覽的欄位（順序即表頭順序）。表頭與 empty-state 的 colspan 共讀
#: 這一份，因此不可能出現「加了一欄卻忘了改 colspan」的錯位。
TOP_ISSUES_COLUMNS: tuple[str, ...] = (
    "等級",
    "標題與位置",
    "平台",
    "層級",
    "趨勢",
    "生命週期",
    "事件數",
    "受影響用戶",
    "最近出現",
    "操作",
)
TOP_ISSUES_COLUMN_COUNT = len(TOP_ISSUES_COLUMNS)

#: 首屏只列最該處理的前幾筆；完整清單走「查看完整問題列表」。
TOP_ISSUES_PREVIEW_LIMIT = 5

#: 首屏 action surface 的 DOM id（#102）。測試用它核對「Top Issues 排在比較面之後、
#: 輔助資訊之前」這個閱讀順序。
ACTION_SECTION_ID = "overviewActionSection"


def get_release_decision_html() -> str:
    """Returns the Overview first-screen Release Decision surface markup (Issue #73).

    只有容器；卡片內容由 ``renderReleaseDecisions()`` 依 ``release_gate.decision``
    產生，因此「有沒有建議」完全由資料決定，不會有一份寫死在 HTML 裡的預設綠燈。
    """
    return """      <!-- Release Decision (V3.3 首屏決策面) -->
      <div class="decision-section" id="overviewDecisionSection">
        <div class="decision-section-head">
          <div class="decision-section-title">發布決策 (Release Decision)</div>
          <div class="decision-section-subtitle" id="__SUBTITLE_ID__">載入中...</div>
        </div>
        <div class="decision-grid" id="__GRID_ID__">
          <!-- Populated dynamically -->
        </div>
      </div>

""".replace("__SUBTITLE_ID__", DECISION_SUBTITLE_ID).replace("__GRID_ID__", DECISION_GRID_ID)


def get_release_comparison_html() -> str:
    """Returns the Overview previous-release comparison surface markup (Issue #74).

    只有容器；內容由 ``renderReleaseComparison()`` 依
    ``vs_previous.metric_evaluations`` 產生。舊 bundle 沒有那個欄位時不會有任何
    卡片內容被憑空補出來。
    """
    return """      <!-- Previous Release Comparison (V3.4 gate 對齊比較面) -->
      <div class="comparison-section" id="overviewComparisonSection">
        <div class="comparison-section-head">
          <div class="comparison-section-title">與上一版相比 (Previous Release Comparison)</div>
          <div class="comparison-section-subtitle" id="__SUBTITLE_ID__">載入中...</div>
        </div>
        <div class="comparison-grid" id="__GRID_ID__">
          <!-- Populated dynamically -->
        </div>
      </div>

""".replace("__SUBTITLE_ID__", COMPARISON_SUBTITLE_ID).replace("__GRID_ID__", COMPARISON_GRID_ID)


def get_release_comparison_js() -> str:
    """Returns the previous-release comparison rendering JS (Issue #74).

    契約（#74）：``vs_previous.metric_evaluations[]`` 已經帶著 ``classification``、
    ``warn_threshold`` / ``fail_threshold``、``threshold_display`` 與
    ``threshold_source``，全部由 Python 端以 Release Gate policy 算好。

    **本函式回傳的 JS 不含任何 threshold 數值，也不做任何門檻比較。**
    ``tests/test_overview_comparison.py`` 會機械式掃描這段字串裡的數字字面值並
    與 ``GatePolicy`` 的門檻值比對——因為「前端偷偷寫死一個 0.25」是本單最容易被
    默默違反的驗收條件，必須可被機器抓到而不是靠 code review 記得。
    """
    return """// ── 與上一版相比（#74）────────────────────────────────────────────
// 這裡只**讀欄位**。classification 與門檻都是資料，不是這段程式的知識。
const COMPARISON_CLASS_PREFIX = "__COMPARISON_CLASS_PREFIX__";

// classification → 色調 + 顯示名。純粹是 classification 本身的顯示名，
// 不夾帶門檻數值、也不夾帶任何發布建議（那只有 release_gate.decision 能說）。
// `skip`（沒有比較資料）刻意與 `pass` 不同色：沒有資料不是正常。
const COMPARISON_CLASS_TONES = {
  pass: "good",
  warn: "warn",
  fail: "bad",
  skip: "neutral",
};
const COMPARISON_CLASS_LABELS = {
  pass: "正常",
  warn: "警告",
  fail: "失敗",
  skip: "無資料",
};

// 缺欄位 = 沒有 gate 對齊的比較資料（舊 bundle、或前版基準不存在）。
// 這種情況回 null，讓呼叫端印出中性說明，而不是畫一張空的指標表。
function readComparisonMetrics(release) {
  const vp = release ? release.vs_previous : null;
  if (!vp || typeof vp !== "object") return null;
  const evs = vp.metric_evaluations;
  if (!Array.isArray(evs)) return null;
  const usable = evs.filter(e => e && typeof e === "object" && e.metric_name && e.classification);
  return usable.length ? usable : null;
}

// 變化量的排版。這裡乘的是**資料**（change 是比例），不是門檻。
function formatComparisonChange(ev) {
  if (ev.change === null || ev.change === undefined) return "—";
  const pct = Number(ev.change) * 100;
  return (pct > 0 ? "+" : "") + pct.toFixed(2) + "%";
}

function buildComparisonMetricRowHtml(ev) {
  const cls = String(ev.classification);
  const tone = COMPARISON_CLASS_TONES[cls] || "neutral";
  const clsLabel = COMPARISON_CLASS_LABELS[cls] || cls.toUpperCase();
  const zeroChip = ev.zero_baseline
    ? ' <span class="comparison-chip">零基準退化</span>'
    : "";
  // threshold 與其來源一律印契約給的字串；前端不知道也不需要知道數值語意。
  // 兩者分行（#102）：門檻是可掃讀的次級細節，來源與 policy 版本再低一級，
  // 但仍然必須印出來——那是「這個判定憑什麼」的可追溯性。
  const thresholdLine = `門檻 ${esc(String(ev.threshold_display || ""))}`;
  const sourceLine = `來源 ${esc(String(ev.threshold_source || ""))}`
    + ` (policy ${esc(String(ev.policy_version || ""))})`;
  return `
    <div class="comparison-metric" data-comparison-metric="${esc(String(ev.metric_name))}" data-comparison-rule="${esc(String(ev.rule_name || ""))}">
      <div class="comparison-metric-head">
        <span class="comparison-metric-label">${esc(String(ev.label || ev.metric_name))}</span>
        <span class="comparison-metric-change">${esc(formatComparisonChange(ev))}</span>
        <span class="${COMPARISON_CLASS_PREFIX}${esc(tone)}" data-comparison-classification="${esc(cls)}">${esc(clsLabel)}</span>${zeroChip}
      </div>
      <div class="comparison-metric-threshold">${thresholdLine}</div>
      <div class="comparison-metric-source">${sourceLine}</div>
      <div class="comparison-metric-reason">${esc(String(ev.reason || ""))}</div>
    </div>`;
}

function buildReleaseComparisonCardHtml(platform, label, release, opts) {
  const o = opts || {};
  const requestedChip = o.requested
    ? ' <span class="comparison-chip is-requested">連結指定版本</span>'
    : "";
  const head = (version, note) => `
      <div class="comparison-card-head">
        <span class="comparison-platform">${esc(label)}</span>
        <span class="comparison-window">${esc(note)}</span>
      </div>
      <div class="comparison-version">${esc(version)}${requestedChip}</div>`;

  if (!release) {
    return `
    <div class="comparison-card is-neutral" data-comparison-platform="${esc(platform)}">
      ${head("—", "無資料")}
      <div class="comparison-unavailable">此平台在本資料中沒有發佈版本，沒有可比較的前版。</div>
    </div>`;
  }

  const version = String(release.version || "");
  const vp = release.vs_previous;
  const prevVersion = (vp && typeof vp === "object") ? (vp.previous_version || "") : "";
  const metrics = readComparisonMetrics(release);

  if (!prevVersion) {
    return `
    <div class="comparison-card is-neutral" data-comparison-platform="${esc(platform)}" data-comparison-version="${esc(version)}">
      ${head(version, "無前版基準")}
      <div class="comparison-unavailable">此版本沒有前版基準（該平台首個記錄版本），因此沒有可比較的變化量。</div>
    </div>`;
  }

  if (!metrics) {
    // 舊 bundle 沒有 metric_evaluations：不得自行拿 change 值套門檻補判定。
    return `
    <div class="comparison-card is-neutral" data-comparison-platform="${esc(platform)}" data-comparison-version="${esc(version)}">
      ${head(version, `前版 ${prevVersion}`)}
      <div class="comparison-unavailable">此資料未包含門檻判定 (metric_evaluations)，因此只呈現版本對照，不呈現正常／異常判定。</div>
    </div>`;
  }

  const windowNote = vp.comparison_window ? `前版 ${prevVersion} · 視窗 ${vp.comparison_window}` : `前版 ${prevVersion}`;
  // gate 沒有對此版本做出判定時（policy 未啟用、樣本不足），比較面仍然有觀測值可看，
  // 但必須講清楚它不是發布判定——否則讀者會把「ANR 失敗」讀成 gate 的結論。
  const decided = (typeof readReleaseDecision === "function") ? readReleaseDecision(release) : null;
  const scopeNote = decided
    ? '<div class="comparison-scope">逐項指標門檻比較；發布建議請看上方發布決策。</div>'
    : '<div class="comparison-scope is-neutral">品質閘門未對此版本做出判定，以下僅為逐項指標門檻比較，不構成發布判定。</div>';

  return `
    <div class="comparison-card" data-comparison-platform="${esc(platform)}" data-comparison-version="${esc(version)}">
      ${head(version, windowNote)}
      ${scopeNote}
      <div class="comparison-metrics">${metrics.map(buildComparisonMetricRowHtml).join("")}</div>
    </div>`;
}

function renderReleaseComparison() {
  const grid = $("__GRID_ID__");
  if (!grid) return;
  const sub = $("__SUBTITLE_ID__");
  const app = getCurAppData();
  if (!app) {
    grid.innerHTML = "";
    return;
  }
  const catalog = getReleaseCatalogForDecision(app, getCurPeriodSnapshot());

  if (!catalog.length) {
    grid.innerHTML = `
      <div class="comparison-card is-neutral">
        <div class="comparison-unavailable">此 App 沒有發佈版本目錄 (release_catalog)，沒有可呈現的前版比較。</div>
      </div>`;
    if (sub) sub.textContent = "無發佈版本資料";
    return;
  }

  // 與決策面共用同一份 pin 規則，兩張卡永遠指向同一個 release。
  const resolved = resolveOverviewPlatformReleases(catalog);
  grid.innerHTML = resolved.slots.map(
    slot => buildReleaseComparisonCardHtml(slot.key, slot.label, slot.release, { requested: slot.requested })
  ).join("");

  if (sub) {
    const meta = app.metadata || {};
    const name = meta.display_name || curAppId;
    sub.textContent = resolved.pinned
      ? `${name} · 連結指定版本 ${resolved.pinned} 與其前版的逐項指標比較`
      : `${name} · 各平台最新版本與其前版的逐項指標比較`;
  }
}

""".replace("__GRID_ID__", COMPARISON_GRID_ID).replace(
        "__SUBTITLE_ID__", COMPARISON_SUBTITLE_ID
    ).replace("__COMPARISON_CLASS_PREFIX__", COMPARISON_CLASS_PREFIX)


def get_release_aux_html() -> str:
    """Returns the Overview auxiliary release-detail surface markup (Issue #91).

    只有容器；內容由 ``renderReleaseAux()`` 依 ``issue_lifecycle`` 與
    ``gate_history`` 產生。兩者都是輔助資訊，因此這個區塊刻意共用一個 section
    header——多開一個標題就會把輔助資訊的視覺重量往上推，違反 #70 的
    decision-first 原則。
    """
    return """      <!-- Release Auxiliary Detail (V3.5 輔助面板：lifecycle + gate history) -->
      <div class="aux-section" id="overviewAuxSection">
        <div class="aux-section-head">
          <div class="aux-section-title">版本輔助資訊 (Release Details)</div>
          <div class="aux-section-subtitle" id="__SUBTITLE_ID__">載入中...</div>
        </div>
        <div class="aux-grid" id="__GRID_ID__">
          <!-- Populated dynamically -->
        </div>
      </div>

""".replace("__SUBTITLE_ID__", AUX_SUBTITLE_ID).replace("__GRID_ID__", AUX_GRID_ID)


def get_release_aux_js() -> str:
    """Returns the auxiliary lifecycle / gate-history rendering JS (Issue #91).

    兩個面板都**只讀既有欄位**：``issue_lifecycle`` 的四個計數與
    ``gate_history[]`` 的 ``gate_status`` / ``evaluated_at`` / ``sample_sufficient``。
    這裡不重算任何 gate 判定，也不從 gate status 反推任何發布建議——建議的唯一
    來源仍是 ``release_gate.decision``（#72／#73）。

    兩個關鍵不變量：

    * **「沒有資料」不得被畫成 0。** 缺 ``issue_lifecycle``（或缺其中某個計數）
      一律印 ``AUX_NO_DATA_TEXT``；退回 0 會把「沒統計到」讀成「這個版本沒有新
      引入問題」，那是不同的事實。
    * **``insufficient_data`` / ``baseline`` 不得呈現為 PASS。** 狀態色調直接讀
      #73 的 ``DECISION_TONES``，因此不存在第二套狀態語意；未知狀態退回中性。
    """
    return """// ── 版本輔助資訊（#91）────────────────────────────────────────────
// lifecycle 摘要 + gate history timeline。兩者都是輔助資訊：只讀欄位、不判定、
// 不產生任何發布建議。
const AUX_GATE_TONE_PREFIX = "__AUX_TONE_PREFIX__";
const AUX_GATE_NEUTRAL_TONE_CLASS = "__AUX_NEUTRAL_TONE_CLASS__";
const AUX_GATE_HISTORY_LIMIT = __AUX_HISTORY_LIMIT__;
const AUX_LIFECYCLE_FIELDS = __AUX_LIFECYCLE_FIELDS__;

// 「欄位不存在」與「數量為 0」是兩個不同的事實（#91 驗收條件）。缺資料一律印
// 這句話，永遠不退回 0。
const AUX_NO_DATA_TEXT = "無資料";

// gate 狀態 → 色調 class。色調來源只有 #73 的 DECISION_TONES：那裡已經定死
// insufficient_data / baseline 為中性，因此這兩種狀態在 timeline 上也不可能
// 變成 pass 的綠色。未知狀態同樣落在中性，而不是預設綠燈。
function auxGateToneClass(status) {
  const tones = (typeof DECISION_TONES === "object" && DECISION_TONES) ? DECISION_TONES : null;
  const tone = tones ? tones[status] : null;
  return tone ? (AUX_GATE_TONE_PREFIX + tone) : AUX_GATE_NEUTRAL_TONE_CLASS;
}

// 缺欄位 / 型別不符 = 沒有 lifecycle 摘要可呈現（回 null 讓呼叫端印中性說明）。
function readReleaseLifecycle(release) {
  const lc = release ? release.issue_lifecycle : null;
  if (!lc || typeof lc !== "object") return null;
  const usable = AUX_LIFECYCLE_FIELDS.filter(f => typeof lc[f[0]] === "number");
  return usable.length ? lc : null;
}

// 近期的 gate 評估點。順序沿用資料本身（產生端已按評估時間遞增），只取尾端
// AUX_GATE_HISTORY_LIMIT 筆——前端不重新排序，排序規則屬於產生 history 的後端。
function readGateHistoryPoints(release) {
  const gh = release ? release.gate_history : null;
  if (!Array.isArray(gh)) return null;
  const usable = gh.filter(p => p && typeof p === "object" && p.gate_status);
  return usable.length ? usable.slice(-AUX_GATE_HISTORY_LIMIT) : null;
}

function buildAuxLifecycleHtml(release) {
  const lc = readReleaseLifecycle(release);
  if (!lc) {
    return `
      <div class="aux-block" data-aux-block="lifecycle">
        <div class="aux-block-title">問題生命週期</div>
        <div class="aux-empty">此版本沒有問題生命週期資料 (issue_lifecycle)，因此不呈現任何數量。</div>
      </div>`;
  }
  const cells = AUX_LIFECYCLE_FIELDS.map(f => {
    const raw = lc[f[0]];
    const has = typeof raw === "number";
    return `
        <div class="aux-metric" data-aux-lifecycle="${esc(f[0])}" data-aux-has-value="${has ? "true" : "false"}">
          <span class="aux-metric-label">${esc(f[1])}</span>
          <span class="aux-metric-value${has ? "" : " is-nodata"}">${has ? fmt(raw) : esc(AUX_NO_DATA_TEXT)}</span>
        </div>`;
  }).join("");
  return `
      <div class="aux-block" data-aux-block="lifecycle">
        <div class="aux-block-title">問題生命週期</div>
        <div class="aux-metrics">${cells}</div>
      </div>`;
}

function buildAuxGateHistoryHtml(release) {
  const pts = readGateHistoryPoints(release);
  if (!pts) {
    return `
      <div class="aux-block" data-aux-block="gate_history">
        <div class="aux-block-title">品質閘門歷程</div>
        <div class="aux-empty">此版本沒有品質閘門評估紀錄 (gate_history)，因此沒有可呈現的歷程。</div>
      </div>`;
  }
  const labels = (typeof DECISION_STATUS_LABELS === "object" && DECISION_STATUS_LABELS)
    ? DECISION_STATUS_LABELS
    : {};
  const points = pts.map(p => {
    const st = String(p.gate_status || "").toLowerCase();
    // pill 的文字就是狀態本身（資料），tooltip 用 #73 的狀態標籤，兩者都不是這段
    // 程式自己的映射表。
    const title = labels[st] || st;
    const day = String(p.evaluated_at || "").slice(0, "0000-00-00".length);
    const sampleChip = p.sample_sufficient === false
      ? ' <span class="aux-chip is-neutral">樣本不足</span>'
      : "";
    return `
          <li class="aux-gate-point" data-aux-gate-status="${esc(st)}">
            <span class="aux-gate-time" title="${esc(String(p.evaluated_at || ""))}">${esc(day || "—")}</span>
            <span class="${auxGateToneClass(st)}" title="${esc(String(title))}">${esc(st.toUpperCase())}</span>${sampleChip}
          </li>`;
  }).join('<li class="aux-gate-arrow" aria-hidden="true">→</li>');
  return `
      <div class="aux-block" data-aux-block="gate_history">
        <div class="aux-block-title">品質閘門歷程</div>
        <ol class="aux-gate-timeline">${points}</ol>
        <div class="aux-note">此版本的歷次評估（近 ${pts.length} 筆）；發布建議請看上方發布決策。</div>
      </div>`;
}

function buildReleaseAuxCardHtml(platform, label, release, opts) {
  const o = opts || {};
  const requestedChip = o.requested
    ? ' <span class="aux-chip is-requested">連結指定版本</span>'
    : "";

  if (!release) {
    return `
    <div class="aux-card is-neutral" data-aux-platform="${esc(platform)}">
      <div class="aux-card-head">
        <span class="aux-platform">${esc(label)}</span>
        <span class="aux-version">—</span>
      </div>
      <div class="aux-empty">此平台在本資料中沒有發佈版本，沒有可呈現的輔助資訊。</div>
    </div>`;
  }

  const version = String(release.version || "");
  return `
    <div class="aux-card" data-aux-platform="${esc(platform)}" data-aux-version="${esc(version)}">
      <div class="aux-card-head">
        <span class="aux-platform">${esc(label)}</span>
        <span class="aux-version">${esc(version)}${requestedChip}</span>
      </div>
      ${buildAuxLifecycleHtml(release)}
      ${buildAuxGateHistoryHtml(release)}
    </div>`;
}

function renderReleaseAux() {
  const grid = $("__GRID_ID__");
  if (!grid) return;
  const sub = $("__SUBTITLE_ID__");
  const app = getCurAppData();
  if (!app) {
    grid.innerHTML = "";
    return;
  }
  const catalog = getReleaseCatalogForDecision(app, getCurPeriodSnapshot());

  if (!catalog.length) {
    grid.innerHTML = `
      <div class="aux-card is-neutral">
        <div class="aux-empty">此 App 沒有發佈版本目錄 (release_catalog)，沒有可呈現的輔助資訊。</div>
      </div>`;
    if (sub) sub.textContent = "無發佈版本資料";
    return;
  }

  // 與決策面／比較面共用同一份 pin 規則，三個面永遠指向同一個 release。
  const resolved = resolveOverviewPlatformReleases(catalog);
  grid.innerHTML = resolved.slots.map(
    slot => buildReleaseAuxCardHtml(slot.key, slot.label, slot.release, { requested: slot.requested })
  ).join("");

  if (sub) {
    const meta = app.metadata || {};
    const name = meta.display_name || curAppId;
    sub.textContent = resolved.pinned
      ? `${name} · 連結指定版本 ${resolved.pinned} 的問題生命週期與品質閘門歷程`
      : `${name} · 各平台最新版本的問題生命週期與品質閘門歷程`;
  }
}

""".replace("__GRID_ID__", AUX_GRID_ID).replace(
        "__SUBTITLE_ID__", AUX_SUBTITLE_ID
    ).replace("__AUX_TONE_PREFIX__", AUX_GATE_TONE_PREFIX).replace(
        "__AUX_NEUTRAL_TONE_CLASS__", AUX_GATE_NEUTRAL_TONE_CLASS
    ).replace("__AUX_HISTORY_LIMIT__", str(AUX_GATE_HISTORY_LIMIT)).replace(
        "__AUX_LIFECYCLE_FIELDS__",
        "[" + ", ".join(f'["{k}", "{lbl}"]' for k, lbl in AUX_LIFECYCLE_FIELDS) + "]",
    )


def get_top_issues_header_html() -> str:
    """Returns the Top Issues preview table header cells (Issue #74).

    表頭由 ``TOP_ISSUES_COLUMNS`` 生成，empty-state 的 colspan 讀同一份長度，
    因此欄位增減不會留下對不上的 colspan。
    """
    return "\n              ".join(f"<th>{col}</th>" for col in TOP_ISSUES_COLUMNS)


def get_top_issues_preview_html() -> str:
    """Returns the Overview Top Issues action surface markup (Issue #102).

    首屏的 action surface：緊接在決策面與比較面之後，排在 lifecycle / gate history
    等輔助資訊**之前**。`決策 → 為什麼 → 現在該做什麼` 這個閱讀順序是 #102 的驗收
    條件，而順序在這份程式裡就是 ``get_overview_html()`` 的組裝順序，因此由測試反向
    核對 DOM 位置，不靠 CSS 事後搬動。

    表身、排序與操作入口完全沿用既有的 ``renderOverviewTopIssuesPreview()``——這一單
    只調整視覺層級，不新增任何 UI-only 的 ranking，也不動 deep-link 入口。
    """
    return """      <!-- Top Issues (V3.6 首屏 action surface) -->
      <div class="action-section" id="__SECTION_ID__">
        <div class="action-section-head">
          <div>
            <div class="action-section-title">現在最該處理的問題 (Top Priority Issues)</div>
            <div class="action-section-subtitle">依既有優先級排序，首屏只列前 __LIMIT__ 筆</div>
          </div>
          <button class="action-section-link" onclick="__SWITCH_ISSUES__">查看完整問題列表 →</button>
        </div>
        <div class="table-container is-action-surface">
          <table class="data-table">
            <thead>
              <tr>
                __HEADER__
              </tr>
            </thead>
            <tbody id="__BODY_ID__"></tbody>
          </table>
        </div>
      </div>

""".replace("__SECTION_ID__", ACTION_SECTION_ID).replace(
        "__LIMIT__", str(TOP_ISSUES_PREVIEW_LIMIT)
    ).replace("__SWITCH_ISSUES__", get_switch_view_call("issues")).replace(
        "__HEADER__", get_top_issues_header_html()
    ).replace("__BODY_ID__", TOP_ISSUES_BODY_ID)


def get_overview_top_issues_js() -> str:
    """Returns the Overview Top Issues preview rendering JS (Issue #74).

    排序**重用既有 deterministic priority**：``priority.score`` 由
    ``crash_trend/analyze_gemini.py`` 的 ``calculate_priority()`` 產生，而 release
    regression 已經以 ``regressed_boost`` 計入該分數（``lifecycle.status ==
    "regressed"`` 時 +2 分）。因此「依 priority.score 遞減」同時反映了 regression
    與 deterministic priority，而不需要另建一套 UI-only ranking——那正是本單的非目標。

    「查看分析」走 #78 的 canonical deep-link helper (``buildRouteHash``)，不自行
    拼 URL；「複製修復 Prompt」重用 ``copyFixPrompt()``（定義於 issues 模組），
    只在該 issue 真的有 AI 修復資料時出現。
    """
    return """// ── Top Issues 預覽（#74）─────────────────────────────────────────
// 只讀既有欄位：priority / lifecycle / trend / affected_users / last_seen。
// 沒有任何新的評分或排名邏輯。
const TOP_ISSUES_PREVIEW_LIMIT = __LIMIT__;

function getTopIssuePriorityScore(iss) {
  const raw = iss && iss.priority ? iss.priority.score : null;
  return (typeof raw === "number") ? raw : 0;
}

// 依既有 priority.score 遞減；同分時以 issue_id 決勝，讓輸出對同一份資料完全確定
// （否則同分 issue 的先後會隨 JS 引擎的排序穩定性而變）。
function sortTopIssuesForOverview(issues) {
  return (issues || []).slice().sort((a, b) => {
    const diff = getTopIssuePriorityScore(b) - getTopIssuePriorityScore(a);
    if (diff !== 0) return diff;
    return String(a.issue_id || "").localeCompare(String(b.issue_id || ""));
  });
}

// 「有 AI 修復資料」的判準：ai_analysis 明確 available 且真的有根因或修復建議。
// 沒有資料時不得放一顆按下去只會複製空殼 prompt 的按鈕。
function hasAiFixData(iss) {
  const ai = iss ? iss.ai_analysis : null;
  if (!ai || typeof ai !== "object") return false;
  if (ai.status !== "available") return false;
  return !!(ai.root_cause || ai.suggested_fix);
}

// 「查看分析」的連結一律由 #78 的 canonical helper 產生。helper 不在時（例如
// 未來有人拆掉 navigation 模組）就不放連結，而不是退回自行拼字串。
function buildTopIssueAnalysisHref(iss) {
  if (typeof buildRouteHash !== "function") return "";
  return buildRouteHash({
    view: "issues",
    app: routeCurAppId(),
    platform: iss.platform,
    version: iss.last_seen_version || "",
  });
}

function buildTopIssueActionsHtml(iss) {
  const parts = [];
  const href = buildTopIssueAnalysisHref(iss);
  if (href) {
    parts.push(`<a class="top-issue-action" href="${esc(href)}">查看分析</a>`);
  }
  if (hasAiFixData(iss)) {
    parts.push(
      `<button class="top-issue-action" onclick="copyFixPrompt('${esc(iss.issue_id)}', '${esc(iss.platform)}')">複製修復 Prompt</button>`
    );
  } else {
    parts.push('<span class="top-issue-action-absent">無 AI 修復資料</span>');
  }
  return `<div class="top-issue-actions">${parts.join("")}</div>`;
}

function formatTopIssueLastSeen(iss) {
  const ts = String(iss.last_seen_timestamp || "");
  const day = ts ? ts.slice(0, "0000-00-00".length) : "—";
  const ver = iss.last_seen_version ? ` · v${iss.last_seen_version}` : "";
  return `${day}${ver}`;
}

function renderOverviewTopIssuesPreview() {
  const prevBody = $("__BODY_ID__");
  if (!prevBody) return;
  const app = getCurAppData();
  if (!app) return;
  const snap = getCurPeriodSnapshot();
  const issues = snap?.top_issues || app.top_issues || [];
  const ranked = sortTopIssuesForOverview(issues);

  prevBody.innerHTML = ranked.slice(0, TOP_ISSUES_PREVIEW_LIMIT).map(iss => {
    const pLevel = iss.priority?.level || "P2";
    return `
      <tr data-top-issue-id="${esc(iss.issue_id)}">
        <td><span class="badge badge-${pLevel.toLowerCase()}">${esc(pLevel)}</span></td>
        <td>
          <div style="font-weight:600">${esc(iss.title)}</div>
          <div style="font-size:11px;font-family:var(--font-mono);color:var(--text-muted)">${esc(iss.subtitle || "")}</div>
        </td>
        <td><span class="badge" style="background:var(--bg-subtle)">${esc(iss.platform)}</span></td>
        <td>${getErrorTypeBadgeHtml(iss.error_type)}</td>
        <td>${getTrendBadgeHtml(iss.priority?.trend)}</td>
        <td>${getLifecycleBadgeHtml(iss.lifecycle)}</td>
        <td class="mono-num">${fmt(iss.events)}</td>
        <td class="mono-num">${fmt(iss.affected_users)}</td>
        <td class="mono-num">${esc(formatTopIssueLastSeen(iss))}</td>
        <td>${buildTopIssueActionsHtml(iss)}</td>
      </tr>
    `;
  }).join("") || `<tr><td colspan="__COLSPAN__" class="empty-state">尚無問題資料</td></tr>`;
}

""".replace("__BODY_ID__", TOP_ISSUES_BODY_ID).replace(
        "__COLSPAN__", str(TOP_ISSUES_COLUMN_COUNT)
    ).replace("__LIMIT__", str(TOP_ISSUES_PREVIEW_LIMIT))


def get_release_decision_js() -> str:
    """Returns the Release Decision first-screen rendering JS (Issue #73).

    契約（#72）：``release_gate.decision`` 是發布建議的唯一來源。本模組只
    **讀欄位**（``status`` / ``action`` / ``recommendation`` / ``reasons``），
    不從 gate status 反推文案或建議行動——一旦這裡長出第二套映射，Dashboard
    與 Google Chat 就會對同一個 release 給出不同建議，而兩邊都自稱權威。
    """
    return """// ── Release Decision 首屏（#73）────────────────────────────────────
// 色調 class 由 Python 端常數注入，Python 產生的 CSS 與 JS 產生的 class
// 因此不可能各自漂移。
const DECISION_TONE_CLASS_PREFIX = "__TONE_PREFIX__";
const DECISION_NEUTRAL_TONE_CLASS = "__NEUTRAL_TONE_CLASS__";

// Android / iOS 並列，讓兩個平台的最新版本可以直接對照。
const DECISION_PLATFORMS = [
  { key: "android", label: "Android" },
  { key: "ios", label: "iOS" },
];

// gate status → 呈現色調。這是**色調**而非文案／建議行動：文案一律讀
// decision.recommendation、行動一律讀 decision.action。
// insufficient_data 與 baseline 刻意與 pass 不同色且為中性——這兩種狀態代表
// 「還無法判定」，跟 pass 同色會讓沒被評估過的版本看起來像已驗證安全。
const DECISION_TONES = {
  pass: "good",
  warn: "warn",
  fail: "bad",
  insufficient_data: "neutral",
  baseline: "neutral",
};

// gate status → 狀態標籤。僅為 status 本身的顯示名（沒有夾帶任何建議行動）。
const DECISION_STATUS_LABELS = {
  pass: "PASS 通過",
  warn: "WARN 預警",
  fail: "FAIL 阻擋",
  insufficient_data: "INSUFFICIENT 樣本不足",
  baseline: "BASELINE 基準版",
};

function getReleaseCatalogForDecision(app, snap) {
  return (app && app.release_catalog) || (snap && snap.release_catalog) || [];
}

// 取某平台「最新」的 release：以 catalog 自己標記的 status === "latest" 為準，
// 沒有標記時退回 last_seen 最大者。刻意不做版本號字串比較——版本排序規則屬於
// 產生 catalog 的後端，前端自行推導只會與後端分歧。
function findPlatformLatestRelease(catalog, platform) {
  const onPlatform = (catalog || []).filter(
    r => r && String(r.platform || "").toLowerCase() === platform
  );
  if (!onPlatform.length) return null;
  const flagged = onPlatform.filter(r => String(r.status || "").toLowerCase() === "latest");
  const pool = flagged.length ? flagged : onPlatform;
  return pool.reduce(
    (best, r) => (!best || String(r.last_seen || "") > String(best.last_seen || "")) ? r : best,
    null
  );
}

// 讀取 canonical decision。沒有 `decision` 欄位就代表這個 gate 沒有評估品質
// （例如 policy enabled: false，此時 release_gate 本身也可能是 null），
// 這種情況**不得**憑空生出建議。
//
// 這裡刻意不 fallback 到 release_gate.status：gate 未啟用時該欄位仍然是 "pass"，
// 把它當綠燈正是 #72 review 抓到兩次的錯誤——「沒人評估過」被說成「已驗證安全」。
// 契約殘缺（少了 status / action / recommendation）同樣視為未評估：寧可少一則建議，
// 不要生出半句建議。
function readReleaseDecision(release) {
  const rg = release ? release.release_gate : null;
  if (!rg || typeof rg !== "object") return null;
  const d = rg.decision;
  if (!d || typeof d !== "object") return null;
  if (!d.status || !d.action || !d.recommendation) return null;
  return d;
}

// sample / baseline 狀態。讀的是資料欄位（sample_sufficient、vs_previous），
// 不是從 decision status 反推。
function buildDecisionStateChipsHtml(release) {
  const rg = release ? release.release_gate : null;
  const chips = [];
  if (rg && typeof rg.sample_sufficient === "boolean") {
    chips.push(rg.sample_sufficient
      ? '<span class="decision-chip">樣本充足</span>'
      : '<span class="decision-chip is-neutral">樣本不足</span>');
  } else {
    chips.push('<span class="decision-chip is-neutral">樣本狀態未知</span>');
  }
  const vp = release ? release.vs_previous : null;
  const prev = vp && vp.previous_version;
  chips.push(prev
    ? `<span class="decision-chip">前版基準 ${esc(prev)}</span>`
    : '<span class="decision-chip is-neutral">無前版基準</span>');
  return `<div class="decision-chip-row">${chips.join(" ")}</div>`;
}

// 此版本決策的 canonical 連結；一律走 #78 的 buildRouteHash，不自行拼 URL。
function buildDecisionPermalinkHtml(platform, version) {
  if (typeof buildRouteHash !== "function" || !version) return "";
  const href = buildRouteHash({
    view: ROUTE_DECISION_VIEW,
    app: routeCurAppId(),
    platform: platform,
    version: version,
  });
  return `<a class="decision-permalink" href="${esc(href)}">此版本決策連結</a>`;
}

// 單一平台的決策卡。三條分支對應三種資料現實：沒有 release、有 release 但
// gate 沒評估、有 canonical decision。前兩者都不產生任何建議文字。
function buildReleaseDecisionCardHtml(platform, label, release, opts) {
  const o = opts || {};
  const requestedChip = o.requested
    ? ' <span class="decision-chip is-requested">連結指定版本</span>'
    : "";

  if (!release) {
    return `
      <div class="decision-card ${DECISION_NEUTRAL_TONE_CLASS}" data-decision-platform="${esc(platform)}">
        <div class="decision-card-head">
          <span class="decision-platform">${esc(label)}</span>
          <span class="decision-gate-badge">無 release 資料</span>
        </div>
        <div class="decision-version">—</div>
        <div class="decision-unevaluated">此平台在本資料中沒有發佈版本，沒有可呈現的發布決策。</div>
      </div>`;
  }

  const version = String(release.version || "");
  const decision = readReleaseDecision(release);

  if (!decision) {
    return `
      <div class="decision-card ${DECISION_NEUTRAL_TONE_CLASS}" data-decision-platform="${esc(platform)}" data-decision-version="${esc(version)}">
        <div class="decision-card-head">
          <span class="decision-platform">${esc(label)}</span>
          <span class="decision-gate-badge">未評估</span>
        </div>
        <div class="decision-version">${esc(version)}${requestedChip}</div>
        <div class="decision-unevaluated">品質閘門未對此版本做出判定，因此沒有發布建議可呈現。請確認閘門政策是否已啟用。</div>
        ${buildDecisionStateChipsHtml(release)}
        ${buildDecisionPermalinkHtml(platform, version)}
      </div>`;
  }

  const status = String(decision.status);
  const tone = DECISION_TONES[status] || "neutral";
  const statusLabel = DECISION_STATUS_LABELS[status] || status.toUpperCase();
  const reasons = Array.isArray(decision.reasons) ? decision.reasons : [];
  const reasonsHtml = reasons.length
    ? `<div class="decision-reasons-label">主要原因</div>
       <ul class="decision-reasons">${reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>`
    : "";

  return `
    <div class="decision-card ${DECISION_TONE_CLASS_PREFIX}${esc(tone)}" data-decision-platform="${esc(platform)}" data-decision-version="${esc(version)}">
      <div class="decision-card-head">
        <span class="decision-platform">${esc(label)}</span>
        <span class="decision-gate-badge">${esc(statusLabel)}</span>
      </div>
      <div class="decision-version">${esc(version)}${requestedChip}</div>
      <div class="decision-recommendation">${esc(decision.recommendation)}</div>
      <div class="decision-action-row">建議行動：<span class="decision-action">${esc(decision.action)}</span></div>
      ${reasonsHtml}
      ${buildDecisionStateChipsHtml(release)}
      ${buildDecisionPermalinkHtml(platform, version)}
    </div>`;
}

// deep link 指定的 release 優先於「該平台最新」——外部連結必須開到它所指的那一版。
// context 一律對**目前** app 的 catalog 重新比對，因此切換 app 後不會沿用上一個
// app 的版本號（那會開到另一個 release，比沒有連結更糟）。
//
// #74：決策面與比較面必須指向**同一個** release，否則首屏會出現「決策講 3.3.0、
// 比較講 3.2.0」這種讀者無從察覺的錯位。因此 pin 規則只有這一份。
function resolveOverviewPlatformReleases(catalog) {
  const ctx = (typeof getRouteContext === "function") ? getRouteContext() : null;
  const wantVersion = (ctx && ctx.version) ? String(ctx.version) : "";
  const wantPlatform = (ctx && ctx.platform) ? String(ctx.platform).toLowerCase() : "";

  let pinned = "";
  const slots = DECISION_PLATFORMS.map(pf => {
    const latest = findPlatformLatestRelease(catalog, pf.key);
    let requested = null;
    if (wantVersion && (!wantPlatform || wantPlatform === pf.key)) {
      requested = catalog.filter(r =>
        r &&
        String(r.version || "") === wantVersion &&
        String(r.platform || "").toLowerCase() === pf.key
      )[0] || null;
    }
    if (requested) pinned = wantVersion;
    return {
      key: pf.key,
      label: pf.label,
      release: requested || latest,
      requested: !!requested && requested !== latest,
    };
  });
  return { slots: slots, pinned: pinned };
}

function renderReleaseDecisions() {
  const grid = $("__GRID_ID__");
  if (!grid) return;
  const sub = $("__SUBTITLE_ID__");
  const app = getCurAppData();
  if (!app) {
    grid.innerHTML = "";
    return;
  }
  const catalog = getReleaseCatalogForDecision(app, getCurPeriodSnapshot());

  if (!catalog.length) {
    grid.innerHTML = `
      <div class="decision-card ${DECISION_NEUTRAL_TONE_CLASS}">
        <div class="decision-unevaluated">此 App 沒有發佈版本目錄 (release_catalog)，沒有可呈現的發布決策。</div>
      </div>`;
    if (sub) sub.textContent = "無發佈版本資料";
    return;
  }

  const resolved = resolveOverviewPlatformReleases(catalog);
  const pinned = resolved.pinned;
  grid.innerHTML = resolved.slots.map(
    slot => buildReleaseDecisionCardHtml(slot.key, slot.label, slot.release, { requested: slot.requested })
  ).join("");

  if (sub) {
    const meta = app.metadata || {};
    const name = meta.display_name || curAppId;
    sub.textContent = pinned
      ? `${name} · 連結指定版本 ${pinned}（其餘平台顯示最新版本）`
      : `${name} · 各平台最新版本的發布決策`;
  }
}

""".replace("__GRID_ID__", DECISION_GRID_ID).replace(
        "__SUBTITLE_ID__", DECISION_SUBTITLE_ID
    ).replace("__TONE_PREFIX__", DECISION_TONE_CLASS_PREFIX).replace(
        "__NEUTRAL_TONE_CLASS__", DECISION_NEUTRAL_TONE_CLASS
    )


def get_overview_html() -> str:
    """Returns HTML markup for #view-overview."""
    return '    <!-- VIEW: OVERVIEW (總覽) -->\n    ' + get_view_container_open_tag("overview") + '\n      <div class="section-header">\n        <div>\n          <h2 class="section-title">總覽 (Overview)</h2>\n          <div class="section-subtitle" id="overviewPeriodSubtitle">載入中...</div>\n        </div>\n      </div>\n\n' + get_release_decision_html() + get_release_comparison_html() + get_top_issues_preview_html() + get_release_aux_html() + '      <!-- KPI Cards -->\n      <div class="kpi-grid">\n        <!-- KPI 1: Crash-free Users -->\n        <div class="kpi-card" id="cardCrashFreeUsers">\n          <div class="kpi-top">\n            <div class="kpi-meta">\n              <span class="kpi-title">無當機用戶率 <small style="font-weight: normal; opacity: 0.7;">(Crash-free Users)</small></span>\n              <div class="kpi-value-row" id="cfUsersValueRow">\n                <span class="kpi-value" id="kpiCFUsers">—</span>\n              </div>\n            </div>\n            <div class="ring-wrap" id="cfUsersRing">\n              <svg class="ring-svg" viewBox="0 0 44 44">\n                <circle class="ring-bg" cx="22" cy="22" r="18"/>\n                <circle class="ring-progress good" id="cfUsersProgress" cx="22" cy="22" r="18" stroke-dasharray="113.097" stroke-dashoffset="113.097"/>\n              </svg>\n            </div>\n          </div>\n          <div class="kpi-bottom">\n            <span class="delta-pill" id="kpiCFUsersDelta">—</span>\n            <span id="kpiCFUsersCounts">—</span>\n          </div>\n        </div>\n\n        <!-- KPI 2: Crash Events -->\n        <div class="kpi-card" id="cardCrashEvents">\n          <div class="kpi-top">\n            <div class="kpi-meta">\n              <span class="kpi-title">當機事件總數 <small style="font-weight: normal; opacity: 0.7;">(Crash Events)</small></span>\n              <div class="kpi-value-row">\n                <span class="kpi-value" id="kpiEvents">0</span>\n              </div>\n            </div>\n            <div class="kpi-icon-wrap">\n              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>\n            </div>\n          </div>\n          <div class="kpi-bottom">\n            <span class="delta-pill" id="kpiEventsDelta">—</span>\n            <span id="kpiErrorBreakdown">Fatal: 0 · ANR: 0</span>\n          </div>\n        </div>\n\n        <!-- KPI 3: Affected Users -->\n        <div class="kpi-card" id="cardAffectedUsers">\n          <div class="kpi-top">\n            <div class="kpi-meta">\n              <span class="kpi-title">受影響人數 <small style="font-weight: normal; opacity: 0.7;">(Affected Users)</small></span>\n              <div class="kpi-value-row">\n                <span class="kpi-value" id="kpiUsers">0</span>\n              </div>\n            </div>\n            <div class="kpi-icon-wrap">\n              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>\n            </div>\n          </div>\n          <div class="kpi-bottom">\n            <span class="delta-pill" id="kpiUsersDelta">—</span>\n            <span>去重受影響用戶</span>\n          </div>\n        </div>\n\n        <!-- KPI 4: New Issues -->\n        <div class="kpi-card" id="cardNewIssues">\n          <div class="kpi-top">\n            <div class="kpi-meta">\n              <span class="kpi-title">新增問題數 <small style="font-weight: normal; opacity: 0.7;">(New Issues)</small></span>\n              <div class="kpi-value-row">\n                <span class="kpi-value" id="kpiNewIssues">0</span>\n              </div>\n            </div>\n            <div class="kpi-icon-wrap">\n              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>\n            </div>\n          </div>\n          <div class="kpi-bottom">\n            <span class="delta-pill" id="kpiNewIssuesDelta">—</span>\n            <span>本期首見問題數</span>\n          </div>\n        </div>\n      </div>\n\n      <!-- Data Sources Health -->\n      <div class="data-sources-card" id="overviewDataSourcesCard">\n        <div class="data-sources-header">\n          <div class="data-sources-title">\n            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>\n            資料來源健康度 (Data Sources Health)\n          </div>\n          <div class="data-sources-run-info" id="overviewLatestRunInfo">載入中...</div>\n        </div>\n        <div class="data-sources-grid" id="overviewDataSourcesGrid">\n          <!-- Populated dynamically -->\n        </div>\n      </div>\n\n      <!-- AI Quick Insights -->\n      <div class="ai-summary-card" id="aiQuickCard">\n        <div class="ai-header">\n          <div class="ai-title">\n            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/></svg>\n            AI 策略摘要 (AI Insights)\n          </div>\n          <span class="ai-model-tag" id="aiModelTag">Gemini Flash</span>\n        </div>\n        <p class="ai-overview-text" id="aiOverviewText">載入中...</p>\n        <div class="ai-takeaways-list" id="aiTakeawaysList"></div>\n      </div>\n\n      <!-- Trend & Breakdown Charts -->\n      <div class="charts-grid">\n        <div class="chart-card col-8">\n          <div class="chart-card-header">\n            <div>\n              <div class="chart-title">每日趨勢 (Daily Trend)</div>\n              <div class="chart-subtitle" id="dailyTrendChartSubtitle">事件數與受影響用戶每日變化</div>\n            </div>\n          </div>\n          <div class="chart-container">\n            <canvas id="chartDailyTrend"></canvas>\n          </div>\n        </div>\n\n        <div class="chart-card col-4">\n          <div class="chart-card-header">\n            <div>\n              <div class="chart-title">錯誤類型佔比 (Error Types)</div>\n              <div class="chart-subtitle">Fatal vs ANR vs Non-fatal</div>\n            </div>\n          </div>\n          <div class="chart-container">\n            <canvas id="chartErrorTypes"></canvas>\n          </div>\n        </div>\n\n        <div class="chart-card col-6">\n          <div class="chart-card-header">\n            <div>\n              <div class="chart-title">平台分布 (Platforms)</div>\n              <div class="chart-subtitle">Android vs iOS 事件與用戶佔比</div>\n            </div>\n          </div>\n          <div class="chart-container">\n            <canvas id="chartPlatforms"></canvas>\n          </div>\n        </div>\n\n        <div class="chart-card col-6">\n          <div class="chart-card-header">\n            <div>\n              <div class="chart-title">Top App 版本分布</div>\n              <div class="chart-subtitle">依崩潰事件量排序</div>\n            </div>\n          </div>\n          <div class="chart-container">\n            <canvas id="chartAppVersions"></canvas>\n          </div>\n        </div>\n      </div>\n    </section>\n\n'


def get_overview_js() -> str:
    """Returns JavaScript rendering functions for Overview: renderKPIs, chart helpers, renderCharts, renderOverviewTopIssuesPreview, updateVersionFilterOptions, handlePlatformFilterChange."""
    return get_release_decision_js() + get_release_comparison_js() + get_release_aux_js() + 'function renderKPIs() {\n  const app = getCurAppData();\n  if (!app) return;\n  const snap = getCurPeriodSnapshot();\n  if (!snap) return;\n\n  const kpi = snap.kpi || {};\n  const meta = app.metadata || {};\n  const period = snap.period || {};\n  const srcs = app.sources || {};\n\n  const pDays = period.days || curPeriodDays || 30;\n  const startStr = (period.start_time || "").slice(0, 10);\n  const endStr = (period.end_time || "").slice(0, 10);\n  $("overviewPeriodSubtitle").textContent = `${meta.display_name || curAppId} · 總覽指標 (${pDays} 天：${startStr} ~ ${endStr})`;\n\n  // Authoritative metrics directly from Schema V2 (no client-side derivation)\n  const totalEvents = kpi.crash_events?.value || 0;\n  const totalUsers = kpi.affected_users?.value || 0;\n  const fatalEvents = kpi.events_by_error_type?.fatal || 0;\n  const anrEvents = kpi.events_by_error_type?.anr || 0;\n  const nonFatalEvents = kpi.events_by_error_type?.non_fatal || 0;\n\n  // 1. Crash-free Users\n  const cfu = kpi.crash_free_users || {};\n  const cfuVal = $("kpiCFUsers");\n  const cfuProg = $("cfUsersProgress");\n  const cfuDelta = $("kpiCFUsersDelta");\n  const cfuCounts = $("kpiCFUsersCounts");\n\n  if (cfu.status === "available" && cfu.rate != null) {\n    const ratePct = (cfu.rate * 100).toFixed(2);\n    cfuVal.innerHTML = `${ratePct}<span style="font-size:16px;font-weight:600;margin-left:2px">%</span>`;\n    cfuVal.classList.remove("unavailable-text");\n\n    const circumference = 2 * Math.PI * 18; // ~113.097\n    const offset = circumference * (1 - cfu.rate);\n    cfuProg.style.strokeDashoffset = offset;\n\n    if (cfu.change_pct_points != null) {\n      const isUp = cfu.change_pct_points >= 0;\n      cfuDelta.className = `delta-pill ${isUp ? "good" : "bad"}`;\n      cfuDelta.innerHTML = `${isUp ? "▲ +" : "▼ "}${cfu.change_pct_points.toFixed(2)}% vs 上期`;\n    } else {\n      cfuDelta.className = "delta-pill neutral";\n      cfuDelta.textContent = "無基期";\n    }\n    cfuCounts.textContent = `${fmt(cfu.crashed)} 崩潰 / ${fmt(cfu.total)} 用戶`;\n  } else {\n    // Explicit Unavailable / Error / Insufficient Data semantics - strictly no 0%\n    cfuProg.style.strokeDashoffset = 113.097;\n    cfuVal.classList.add("unavailable-text");\n    cfuDelta.className = "delta-pill neutral";\n\n    const reason = cfu.unavailable_reason || (srcs.firebase_sessions ? srcs.firebase_sessions.error_message : "") || "";\n    const isExplicitlyDisabled = reason.toLowerCase().includes("disabled") || reason.includes("已停用") || reason.includes("未開啟");\n\n    if (cfu.status === "error") {\n      cfuVal.innerHTML = `<span class="kpi-badge-unavailable" style="background:var(--danger-light);color:var(--danger-text)">錯誤</span>`;\n      cfuDelta.textContent = "BigQuery 查詢失敗";\n      cfuCounts.textContent = reason || "請檢查查詢或權限";\n    } else if (cfu.status === "insufficient_data") {\n      cfuVal.innerHTML = `<span class="kpi-badge-unavailable" style="background:var(--warning-light);color:var(--warning-text)">資料不足</span>`;\n      cfuDelta.textContent = "連線樣本過少";\n      cfuCounts.textContent = reason || "無法計算無當機率";\n    } else if (isExplicitlyDisabled) {\n      cfuVal.innerHTML = `<span class="kpi-badge-unavailable">未開啟</span>`;\n      cfuDelta.textContent = "未開啟連線統計";\n      cfuCounts.textContent = "缺少總上線人數";\n    } else {\n      cfuVal.innerHTML = `<span class="kpi-badge-unavailable">無資料</span>`;\n      cfuDelta.textContent = "未找到 Sessions 資料";\n      cfuCounts.textContent = reason || "未匯出連線資料";\n    }\n  }\n\n  // 2. Crash Events\n  const ev = kpi.crash_events || {};\n  if (ev.status === "error") {\n    $("kpiEvents").innerHTML = `<span style="font-size:16px;color:var(--danger-text)">查詢失敗</span>`;\n    $("kpiEventsDelta").className = "delta-pill neutral";\n    $("kpiEventsDelta").textContent = "BigQuery 錯誤";\n  } else {\n    $("kpiEvents").textContent = fmt(totalEvents);\n    const evDelta = $("kpiEventsDelta");\n    if (ev.change_pct != null) {\n      const isDown = ev.change_pct <= 0;\n      evDelta.className = `delta-pill ${isDown ? "good" : "bad"}`;\n      evDelta.innerHTML = `${ev.change_pct > 0 ? "▲ +" : "▼ "}${ev.change_pct.toFixed(1)}% vs 上期`;\n    } else {\n      evDelta.className = "delta-pill neutral";\n      evDelta.textContent = "無基期";\n    }\n  }\n  $("kpiErrorBreakdown").textContent = `Fatal 閃退: ${fmt(fatalEvents)} · ANR 無回應: ${fmt(anrEvents)} · Non-fatal 未閃退: ${fmt(nonFatalEvents)}`;\n\n  // 3. Affected Users\n  const us = kpi.affected_users || {};\n  if (us.status === "error") {\n    $("kpiUsers").innerHTML = `<span style="font-size:16px;color:var(--danger-text)">查詢失敗</span>`;\n    $("kpiUsersDelta").className = "delta-pill neutral";\n    $("kpiUsersDelta").textContent = "Overview 錯誤";\n  } else if (us.status === "insufficient_data") {\n    $("kpiUsers").innerHTML = `<span style="font-size:16px;color:var(--warning-text)">資料不足</span>`;\n    $("kpiUsersDelta").className = "delta-pill neutral";\n    $("kpiUsersDelta").textContent = "無去重指標";\n  } else {\n    $("kpiUsers").textContent = fmt(totalUsers);\n    const usDelta = $("kpiUsersDelta");\n    if (us.change_pct != null) {\n      const isDown = us.change_pct <= 0;\n      usDelta.className = `delta-pill ${isDown ? "good" : "bad"}`;\n      usDelta.innerHTML = `${us.change_pct > 0 ? "▲ +" : "▼ "}${us.change_pct.toFixed(1)}% vs 上期`;\n    } else {\n      usDelta.className = "delta-pill neutral";\n      usDelta.textContent = "無基期";\n    }\n  }\n\n  // 4. New Issues\n  const ni = kpi.new_issues_count || {};\n  $("kpiNewIssues").textContent = fmt(ni.value);\n  const niDelta = $("kpiNewIssuesDelta");\n  if (ni.change_pct != null) {\n    const isDown = ni.change_pct <= 0;\n    niDelta.className = `delta-pill ${isDown ? "good" : "bad"}`;\n    niDelta.innerHTML = `${ni.change_pct > 0 ? "▲ +" : "▼ "}${ni.change_pct.toFixed(1)}% vs 上期`;\n  } else {\n    niDelta.className = "delta-pill neutral";\n    niDelta.textContent = "無基期";\n  }\n}\n\n// Render AI Summary Cards\n\n\n// Chart Helpers\nfunction getChartColors() {\n  const isDark = document.documentElement.dataset.theme === "dark";\n  return {\n    text: isDark ? "#94a3b8" : "#64748b",\n    grid: isDark ? "#1e293b" : "#f1f5f9",\n    accent: isDark ? "#3b82f6" : "#2563eb",\n    danger: isDark ? "#f87171" : "#ef4444",\n    warning: isDark ? "#fbbf24" : "#f59e0b",\n    success: isDark ? "#34d399" : "#10b981",\n  };\n}\n\nfunction destroyChart(id) {\n  if (chartInstances[id]) {\n    chartInstances[id].destroy();\n    delete chartInstances[id];\n  }\n}\n\nfunction renderCharts() {\n  if (typeof Chart === "undefined") return;\n  const app = getCurAppData();\n  if (!app) return;\n  const snap = getCurPeriodSnapshot();\n  const colors = getChartColors();\n  const period = snap?.period || app.period || {};\n\n  // 1. Daily Trend (sliced by curPeriodDays)\n  destroyChart("chartDailyTrend");\n  const daily = (snap && snap.daily_trend && snap.daily_trend.length) ? snap.daily_trend : (app.daily_trend || []);\n  const activeDays = Math.min(curPeriodDays || period.days || 30, daily.length || 30);\n  const activeDaily = daily.slice(-activeDays);\n\n  const subEl = $("dailyTrendChartSubtitle");\n  if (subEl) {\n    if (curPeriodDays && curPeriodDays > daily.length) {\n      subEl.textContent = `事件數與受影響用戶（實際僅有 ${daily.length} 天資料）`;\n    } else {\n      subEl.textContent = `事件數與受影響用戶每日變化（顯示最近 ${activeDaily.length} 天）`;\n    }\n  }\n\n  const trendCtx = $("chartDailyTrend");\n  if (trendCtx && activeDaily.length) {\n    chartInstances["chartDailyTrend"] = new Chart(trendCtx, {\n      type: "line",\n      data: {\n        labels: activeDaily.map(d => d.date.slice(5)),\n        datasets: [\n          {\n            label: "事件數 (Events)",\n            data: activeDaily.map(d => d.crash_events),\n            borderColor: colors.danger,\n            backgroundColor: "rgba(239, 68, 68, 0.08)",\n            fill: true,\n            tension: 0.3,\n            borderWidth: 2,\n            pointRadius: 2,\n          },\n          {\n            label: "受影響用戶 (Users)",\n            data: activeDaily.map(d => d.affected_users),\n            borderColor: colors.accent,\n            backgroundColor: "transparent",\n            tension: 0.3,\n            borderWidth: 2,\n            pointRadius: 2,\n          }\n        ]\n      },\n      options: {\n        responsive: true,\n        maintainAspectRatio: false,\n        plugins: {\n          legend: { position: "top", labels: { color: colors.text, boxWidth: 12 } }\n        },\n        scales: {\n          x: { ticks: { color: colors.text }, grid: { color: colors.grid } },\n          y: { ticks: { color: colors.text }, grid: { color: colors.grid } }\n        }\n      }\n    });\n  }\n\n  // 2. Error Types\n  destroyChart("chartErrorTypes");\n  const errCtx = $("chartErrorTypes");\n  const kpiErr = snap?.kpi?.events_by_error_type || app.kpi?.events_by_error_type || {};\n  if (errCtx) {\n    chartInstances["chartErrorTypes"] = new Chart(errCtx, {\n      type: "doughnut",\n      data: {\n        labels: ["Fatal 閃退", "ANR 凍結", "Non-fatal 未閃退"],\n        datasets: [{\n          data: [kpiErr.fatal || 0, kpiErr.anr || 0, kpiErr.non_fatal || 0],\n          backgroundColor: [colors.danger, colors.warning, "#94a3b8"],\n          borderWidth: 0,\n        }]\n      },\n      options: {\n        responsive: true,\n        maintainAspectRatio: false,\n        cutout: "68%",\n        plugins: {\n          legend: { position: "bottom", labels: { color: colors.text, boxWidth: 10 } }\n        }\n      }\n    });\n  }\n\n  // 3. Platform Breakdown\n  destroyChart("chartPlatforms");\n  const platCtx = $("chartPlatforms");\n  const platData = snap?.distributions?.platform || app.distributions?.platform || [];\n  if (platCtx && platData.length) {\n    chartInstances["chartPlatforms"] = new Chart(platCtx, {\n      type: "bar",\n      data: {\n        labels: platData.map(p => p.name.toUpperCase()),\n        datasets: [\n          {\n            label: "事件數",\n            data: platData.map(p => p.events),\n            backgroundColor: colors.accent,\n            borderRadius: 4,\n          },\n          {\n            label: "用戶數",\n            data: platData.map(p => p.users),\n            backgroundColor: "#94a3b8",\n            borderRadius: 4,\n          }\n        ]\n      },\n      options: {\n        responsive: true,\n        maintainAspectRatio: false,\n        plugins: { legend: { position: "top", labels: { color: colors.text, boxWidth: 12 } } },\n        scales: {\n          x: { ticks: { color: colors.text }, grid: { display: false } },\n          y: { ticks: { color: colors.text }, grid: { color: colors.grid } }\n        }\n      }\n    });\n  }\n\n  // 4. App Versions\n  destroyChart("chartAppVersions");\n  const verCtx = $("chartAppVersions");\n  const verData = snap?.distributions?.app_versions || app.distributions?.app_versions || [];\n  if (verCtx && verData.length) {\n    chartInstances["chartAppVersions"] = new Chart(verCtx, {\n      type: "bar",\n      data: {\n        labels: verData.map(v => v.app_version),\n        datasets: [{\n          label: "事件數",\n          data: verData.map(v => v.events),\n          backgroundColor: colors.danger,\n          borderRadius: 4,\n        }]\n      },\n      options: {\n        indexAxis: "y",\n        responsive: true,\n        maintainAspectRatio: false,\n        plugins: { legend: { display: false } },\n        scales: {\n          x: { ticks: { color: colors.text }, grid: { color: colors.grid } },\n          y: { ticks: { color: colors.text }, grid: { display: false } }\n        }\n      }\n    });\n  }\n\n  // 5. Device Models\n  destroyChart("chartDeviceModels");\n  const devCtx = $("chartDeviceModels");\n  const devData = snap?.distributions?.device_models || app.distributions?.device_models || [];\n  if (devCtx && devData.length) {\n    chartInstances["chartDeviceModels"] = new Chart(devCtx, {\n      type: "bar",\n      data: {\n        labels: devData.slice(0, 8).map(d => d.model),\n        datasets: [{\n          label: "事件數",\n          data: devData.slice(0, 8).map(d => d.events),\n          backgroundColor: colors.accent,\n          borderRadius: 4,\n        }]\n      },\n      options: {\n        indexAxis: "y",\n        responsive: true,\n        maintainAspectRatio: false,\n        plugins: { legend: { display: false } },\n        scales: {\n          x: { ticks: { color: colors.text }, grid: { color: colors.grid } },\n          y: { ticks: { color: colors.text }, grid: { display: false } }\n        }\n      }\n    });\n  }\n\n  // 6. OS Versions\n  destroyChart("chartOSVersions");\n  const osCtx = $("chartOSVersions");\n  const osData = snap?.distributions?.os_versions || app.distributions?.os_versions || [];\n  if (osCtx && osData.length) {\n    chartInstances["chartOSVersions"] = new Chart(osCtx, {\n      type: "bar",\n      data: {\n        labels: osData.slice(0, 8).map(o => o.os_version),\n        datasets: [{\n          label: "事件數",\n          data: osData.slice(0, 8).map(o => o.events),\n          backgroundColor: colors.warning,\n          borderRadius: 4,\n        }]\n      },\n      options: {\n        indexAxis: "y",\n        responsive: true,\n        maintainAspectRatio: false,\n        plugins: { legend: { display: false } },\n        scales: {\n          x: { ticks: { color: colors.text }, grid: { color: colors.grid } },\n          y: { ticks: { color: colors.text }, grid: { display: false } }\n        }\n      }\n    });\n  }\n}\n\n\n' + get_overview_top_issues_js() + 'function updateVersionFilterOptions(preserveSelected = true) {\n  const sel = $("filterVersion");\n  if (!sel) return;\n  const app = getCurAppData();\n  if (!app) return;\n  const snap = getCurPeriodSnapshot();\n  const platFilter = $("filterPlatform") ? $("filterPlatform").value : "ALL";\n\n  const prevVal = preserveSelected ? sel.value : "ALL";\n  const authVersions = getAppAuthoritativeVersions(app, snap, platFilter);\n  const latestMap = resolveLatestVersionsByPlatform(app, snap);\n\n  let html = `<option value="ALL">全部版本</option>`;\n  if (platFilter !== "ALL") {\n    const latestVer = platFilter === "ios" ? latestMap.ios : latestMap.android;\n    if (latestVer) {\n      html += `<option value="LATEST">最新版本 (${esc(latestVer)})</option>`;\n    } else {\n      html += `<option value="LATEST">最新版本</option>`;\n    }\n  } else {\n    const parts = [];\n    if (latestMap.android) parts.push(`Android: ${latestMap.android}`);\n    if (latestMap.ios) parts.push(`iOS: ${latestMap.ios}`);\n    if (parts.length > 1) {\n      html += `<option value="LATEST">最新版本 (依各平台: ${esc(parts.join(", "))})</option>`;\n    } else if (parts.length === 1) {\n      html += `<option value="LATEST">最新版本 (${esc(parts[0])})</option>`;\n    } else {\n      html += `<option value="LATEST">最新版本 (依平台)</option>`;\n    }\n  }\n\n  authVersions.forEach(v => {\n    const isLatestPf = (v.platforms.has("android") && v.version === latestMap.android) ||\n                       (v.platforms.has("ios") && v.version === latestMap.ios);\n    const latestBadge = (v.isLatest || isLatestPf) ? " (最新)" : "";\n    const pfNote = (platFilter === "ALL" && v.platforms.size === 1) ? ` [${Array.from(v.platforms)[0]}]` : "";\n    html += `<option value="${esc(v.version)}">${esc(v.version)}${latestBadge}${pfNote}</option>`;\n  });\n\n  sel.innerHTML = html;\n\n  // Restore selection if valid; otherwise fallback to ALL\n  if (prevVal === "ALL" || prevVal === "LATEST") {\n    sel.value = prevVal;\n  } else if (authVersions.some(v => v.version === prevVal)) {\n    sel.value = prevVal;\n  } else {\n    sel.value = "ALL";\n  }\n}\n\nfunction handlePlatformFilterChange() {\n  updateVersionFilterOptions(true);\n  renderIssuesList();\n}\n'
