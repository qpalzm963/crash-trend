# crash-trend

**Firebase Crashlytics 趨勢分析與 Dashboard V2.7** — 將 Crashlytics BigQuery、Firebase Sessions 與可選的 Crashlytics MCP 資料整合成可排序的修復優先級、AI 分析、Pipeline Health、Persistent Release Catalog、版本退化閘門（Release Regression Gate）、Google Chat 品質通知與觀測度審計，以及自包含式 Dashboard。

![dashboard](docs/screenshot.png)

## 為什麼需要 crash-trend

Firebase Crashlytics 很適合查看單一 crash，但要回答下面這些問題，通常仍需要人工整理：

- 哪些 crash / ANR 最值得先修？
- 最新版本是否正在惡化？
- Crash-free Users / Sessions 是否下降？
- 問題集中在哪些版本、裝置或使用者族群？
- 哪些 issue 需要進一步做 Root Cause 分析？
- 發布版本是否符合品質要求？能否安全推進 Rollout？
- Google Chat 警報是否即時送達？是否有重試失敗或抑制？
- 本週資料源、AI 與同步流程是否正常？

crash-trend 將這些工作整理成一條可重複執行的資料管線：

```text
Crashlytics BigQuery ──┐
Firebase Sessions ─────┼─→ fetch / enrich ─→ normalize ─→ deterministic priority (P0~P3)
Crashlytics MCP ───────┘        │                         │
                                │                         ├─→ AI Task Router (Gemini / OpenRouter)
                                │                         │
                                ├─→ SQLite Authority ─────┼─→ Persistent Release Catalog
                                │    (exact dedupe/salt)  ├─→ Release Regression Gate (pass/warn/fail)
                                │                         │    ├─→ SQLite Gate History (snapshots/trends)
                                │                         │    └─→ Google Chat Alerts (dedupe/cooldown)
                                │                         │         └─→ SQLite Alert Delivery Audit
                                │                         │
                                └─────────────────────────┼─→ dashboard.html & JSON Bundle
                                                          ├─→ pipeline health (pipeline_run.json)
                                                          ├─→ surge detection
                                                          └─→ monthly chat report
```

---

## Dashboard V2.7 核心能力

### Crash Intelligence Dashboard

- 現代 SaaS Analytics 風格與響應式側邊欄。
- 8 大功能區：總覽、問題列表、版本健康度、裝置分析、發佈版本、通知管線、AI 分析、系統設定。
- 支援多 App 切換與 `#<app>` URL hash 直達。
- 單一 `dashboard.html` 自包含輸出，可直接使用 `file://` 開啟。
- Docker Compose 另提供 Nginx 靜態服務，預設可從 `http://localhost:8787` 查看。

### Crash-free 與版本健康度

- Crashlytics BigQuery 作為核心 crash / ANR 資料源。
- Firebase Sessions 可選，用來計算 Crash-free Users / Sessions。
- 支援期間去重 Affected Users、每日趨勢、版本與裝置分布。
- Sessions 未啟用或不可用時會明確顯示 `Unavailable` / `未開啟`，不以假 `0%` 代替缺失資料。

### Persistent Release Catalog（發佈版本目錄與跨視窗健康度）

- 獨立於單期 7d / 30d / 90d 快照的長期發布版本追蹤，嚴格依平台（Android / iOS）隔離。
- **近期健康度（Recent Health）**：提供 7 天、30 天、90 天視窗數據對照，搭配 `sample_sufficient` 樣本作收斂保護。
- **前後版本對比（Previous Release Comparison: `vs_previous`）**：計算歸一化暴險率（Normalized Exposure: crash / fatal / ANR per 1,000 sessions）變化與整體穩定度評級（`improving` / `stable` / `degrading` / `baseline`）。
- **Issue 生命週期統計（`issue_lifecycle`）**：自動分類該版本引入（introduced）、持續存在（persistent）、舊疾回歸（regressed）與已修復（resolved）的 Issue 數量與清單。

### SQLite Authority Store 精確去重與隱私防護

- **去重權威儲存**：採用本機 SQLite (`out/<app>/catalog_authority.sqlite3`) 專責管理設備級精確去重，徹底將大量 raw UUID 從 JSON 解耦。
- **Zero PII 加鹽雜湊**：裝置識別碼在寫入資料庫前，一律以 `app_id` 作為 Salt 進行 SHA-256 確定性加鹽雜湊（`SHA-256(f"{app_id}:{raw_uuid}")`），不儲存任何明文識別碼。
- **高效並行與安全**：啟用 SQLite WAL（Write-Ahead Logging）模式、`synchronous = NORMAL` 與 `busy_timeout = 5000`，確保背景執行緒與排程穩定寫入。
- **JSON 去重解耦**：`historical_catalog.json` 與 `dashboard_v2.json` 絕不包含 raw installation IDs，僅記錄去重後的計數值與 authority metadata。

### 六大解耦契約架構 (Six Distinct Decoupled Contracts)

系統嚴格劃分六種獨立契約，確保責任邊界清晰與儲存效能最佳化：

| 契約名稱 | 儲存檔案 | 核心職責 | 格式與特性 |
| :--- | :--- | :--- | :--- |
| **Dashboard JSON Contract** | `out/dashboard_v2.json` 或 `out/<app>/dashboard_v2.json` | 供前端 Web UI 呈現的聚合資料容器（Bundle） | 包含多週期快照、KPI、趨勢、問題排行、發佈目錄、閘門歷史與發送審計 |
| **Historical Catalog JSON Contract** | `out/<app>/historical_catalog.json` | 跨視窗版本演進與 Issue 生命週期累積狀態 | 儲存版本時間戳、水線、生命週期與去重後指標，**零 raw IDs** |
| **Release Gate Artifact JSON Contract** | `out/<app>/release_gate.json` | 單次最新版本退化判定機器產物與 CI 阻擋依據 | 結構化指標快照、違規原因、嚴重度與判定結果，**零 raw IDs** |
| **SQLite Authority Store Contract** | `out/<app>/catalog_authority.sqlite3` | 受影響用戶去重的唯一事實來源（Single Source of Truth） | 本機 SQLite DB，儲存加鹽雜湊集合與版本權威狀態 |
| **SQLite Gate History Store Contract** | `out/<app>/release_gate_history.sqlite3` | 閘門評估快照不可變歷史序列與品質演進趨勢 | 本機 SQLite DB，以 `evaluation_key` 保證冪等，追蹤狀態轉移軌跡 |
| **SQLite Alert Delivery Audit Contract** | `out/<app>/alert_delivery.sqlite3` | 品質通知發送嘗試、去重冷卻、重試與健康度審計 | 本機 SQLite DB，嚴格抹除 Webhook URL / Token / UUID，支援唯讀模式 |

### 資料管線生命週期（Migration / Bootstrap / Incremental）

- **Migration（舊版相容升級）**：載入包含 legacy `installation_ids` 的舊版 catalog 時，自動將 UUID 匯入 SQLite Authority Store，計算精確去重值，並從記憶體與 JSON 輸出中永久清除 raw IDs。若舊有 ID 數量不足，則安全保底並標記需全量 bootstrap。
- **Bootstrap（全量初始化）**：初次執行或未具備完整權威時，執行 BigQuery 全量版本掃描（`version_catalog_bootstrap`），建立完整去重集合。零崩潰/零用戶之版本亦顯式標記 `bootstrap_complete`，防止增量同步時重複誤判。
- **Incremental（增量同步）**：以 `watermark`（事件時間戳）為基準只拉取新資料，透過 SQLite `INSERT OR IGNORE` 進行冪等追加去重，自動更新水線與版本生命週期指標。

### Deterministic Priority Score

每個 issue 先由程式規則計算 `P0` / `P1` / `P2` / `P3`，評分可考慮：

- Affected users
- Fatal / ANR
- 趨勢是否惡化
- 是否影響最新版本
- 是否命中 `core_paths` 核心業務路徑

AI 不負責取代這個基礎排序；即使沒有 AI Key，核心資料管線仍可產出 Dashboard 與 deterministic priority。

### AI Runtime & Routing

Dashboard V2.5 將 AI 分析拆成兩層：

| 任務 | 預設角色 | 用途 |
|---|---|---|
| Lightweight triage | OpenRouter Free Worker | 摘要、分類、標籤、判斷是否值得做深度分析 |
| Deep analysis | Gemini Direct | Root Cause、Suggested Fix、策略摘要與修復建議 |

目前支援：

- `auto`：輕量任務與深度分析依角色自動路由。
- `gemini_only`：全部交給 Gemini。
- `openrouter_only`：全部交給 OpenRouter。
- **Triage Gating**：低風險 issue 可跳過深度分析，降低不必要的 Gemini 呼叫。
- **Canonical JSON Schema**：Gemini / OpenRouter 共用一致的結構化輸出契約。
- **Cost Guard**：`allow_paid_models: false` 時，僅允許專案明確列入免費模型 allowlist 的模型。
- **Transient fallback**：可選擇只在 429 / 5xx / timeout 等暫時性錯誤時切換 provider；預設關閉。
- **Privacy Guard**：可控制是否把本地 source snippet 放入 AI prompt。

> [!IMPORTANT]
> `Free Tier` / `free model` 判斷是依專案內目前的 provider allowlist 與設定保護機制執行。實際 API 可用額度、資格與計費仍以 Google AI Studio / OpenRouter 當下帳號與官方政策為準。

### AI Policy Admin & Telemetry

- `ai_config_service.py`：查看或調整全域 / App AI policy。
- 可啟動本機 Admin API，讓 Dashboard 設定頁安全寫回 `apps.yaml`。
- Admin API 使用 token 驗證與 CORS Origin 防護。
- `ai_telemetry.py`：記錄 AI request、成功率、429、fallback、provider / model 與實際可取得的 token usage。
- 沒有 token 資料時不做推估，避免產生看似精準但實際不存在的用量數字。

### Blame Frame 與 Issue Detail

- 解析 Stack Trace、關鍵 frame、檔案、行號與 symbol。
- MCP 可補充 BigQuery 不足的 Stack Trace / Breadcrumbs / Logs。
- 支援產生可直接貼給 Coding Agent 的修復 Prompt。

### Pipeline Health

每次執行會產出 `out/pipeline_run.json`，紀錄：

- App / Stage 狀態：`success`、`failed`、`skipped`、`disabled`、`degraded`
- Stage 執行時間
- 經過 credential sanitization 的錯誤原因
- BigQuery / Sessions / MCP / AI 資料源狀態與新鮮度

非核心資料源失敗時會盡可能 graceful degradation，不讓 Sessions、MCP 或 AI 單點失敗直接破壞整條 crash 分析流程。

---

## 快速開始

### 1. Clone 與安裝

```bash
git clone https://github.com/qpalzm963/crash-trend.git
cd crash-trend
cp apps.example.yaml apps.yaml
```

Python 本機執行：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

CI 目前使用 Python 3.11 / 3.12 執行單元測試。

### 2. 建立 BigQuery 唯讀 Service Account

先登入有 Firebase / GCP 專案權限的帳號：

```bash
gcloud auth login
scripts/create_sa.sh <firebase_project_id>
```

預設憑證位置：

```text
~/.config/crash-trend/sa.json
```

接著到 Firebase Console 將 Crashlytics 連結至 BigQuery；若需要 Crash-free Users / Sessions，再另外啟用 Firebase Sessions 匯出。

### 3. 設定 AI Key

預設 `auto` 模式建議同時準備 Gemini 與 OpenRouter：

```bash
cat > .env <<'EOF'
GEMINI_API_KEY=your_gemini_key
OPENROUTER_API_KEY=your_openrouter_key
EOF
```

可選環境變數：

```text
GEMINI_MODEL
OPENROUTER_MODEL
AI_ROUTING_MODE
AI_ALLOW_PAID_MODELS
```

若不設定 AI Key，AI stage 會停用或降級；BigQuery、Priority Score、Dashboard 等非 AI 核心功能仍可使用。

### 4. 執行完整 Pipeline

直接執行：

```bash
python3 crash_trend/pipeline_run.py
open dashboard.html
```

只跑指定 App / 指定天數：

```bash
python3 crash_trend/pipeline_run.py --app shop_app --days 30
```

### 5. Docker Compose

```bash
docker compose up -d --build
```

- `crash-trend`：依 `docker/crontab` 每週三 10:00（Asia/Taipei）執行 `weekly_sync.sh`。
- `dashboard`：Nginx 靜態服務，預設 port `8787`。

手動驗證排程流程：

```bash
docker compose run --rm crash-trend /bin/bash /app/scripts/weekly_sync.sh
```

瀏覽器開啟：

```text
http://localhost:8787
```

---

## `apps.yaml` 設定

`apps.example.yaml` 是完整範例。設定分成全域 credentials / AI policy，以及各 App 的資料源與專案資訊。

```yaml
credentials:
  bq_service_account: ~/.config/crash-trend/sa.json

ai:
  mode: auto
  primary:
    provider: gemini
    model: gemini-3.8-flash
  lightweight:
    provider: openrouter
    model: openrouter/free
    zdr: true
  allow_paid_models: false
  privacy:
    include_source_snippet: true
  fallback:
    enabled: false

apps:
  shop_app:
    display_name: 購物商城 App
    firebase_project: shop-app-12345
    data_sources:
      crashlytics_bigquery: true
      sessions: true
      mcp:
        mode: manual
        max_age_days: 7
    bq_dataset: firebase_crashlytics
    sessions_dataset: firebase_sessions
    package_name: com.example.shop
    bundle_id: com.example.shop.ios
    source_repo: ~/develop/shop_app
    platforms: [android, ios]
    core_paths: [checkout, payment, CartActivity]
    custom_keys: [user_tier, network_type]
```

### Data Source Profile

可依 App 需求選擇兩種常見模式：

**Crashlytics-only**

```yaml
data_sources:
  crashlytics_bigquery: true
  sessions: false
  mcp: off
```

適合只需要 crash / ANR、Affected Users、Stack Trace 與 AI 優先級，不需要 Crash-free Sessions 的 App。

**Full Sessions**

```yaml
data_sources:
  crashlytics_bigquery: true
  sessions: true
  mcp:
    mode: manual
    max_age_days: 7
```

適合需要 Crash-free Users / Sessions 與版本健康度的 App。

---

## MCP 補強策略

Crashlytics MCP 是**可選資料源**，主要用於補充 BigQuery 不足的 Stack Trace、Blame Frame、Breadcrumbs 與 Logs。

| `mcp.mode` | 行為 | 適用情境 |
|---|---|---|
| `manual` | 週排程略過 MCP，只使用既有 cache；需要時手動刷新 | 預設，最穩定 |
| `weekly` | cache 過期才嘗試刷新；失敗時 graceful degradation | 有固定 Firebase 使用者登入態的本機環境 |
| `off` | 完全關閉 MCP | 純 BigQuery / 不需要 MCP |

手動刷新：

```bash
npm i -g firebase-tools@latest
firebase login
python3 crash_trend/fetch_stacktraces.py --app shop_app
```

> [!NOTE]
> MCP 依賴 Firebase CLI 的使用者登入態。Service Account 主要用於 BigQuery，不應假設它可以取代 MCP 所需的 Firebase 使用者授權。

---

## AI Runtime 治理與 Structured Output 契約

系統支援雙層架構生產路由與嚴格零費用防護（Cost Guard）：

| 功能領域 | 實作機制 | 說明 |
| :--- | :--- | :--- |
| **預設模型升級 (#38)** | `gemini-3.8-flash` | Direct Gemini 升級至 2026-09 最新 GA 模型，支援高思考深度，自動省略過時 temperature 參數。 |
| **Interactions 原生契約 (#39)** | `POST /v1beta/interactions` & `response_format` | 升級為 Google 官方最新 Interactions API，透過頂層 `response_format` 直接套用 Canonical JSON Schema，由 `steps` 時間軸解析輸出並從 `usage` 審計 Token，雙 provider 零耗損共用契約。 |
| **生產輕量路由 (#40)** | `auto` 雙工模式 | 輕量任務（Triage、分類、打標）由 OpenRouter Free Worker 承接；**具備 Triage Gating 機制**，低危/非致命問題跳過深度推理以節省 100% Gemini 配額，高危問題精準過濾發送。 |
| **後台 Policy 治理 (#41)** | `ai_config_service.py` | 儀表板提供即時互動表單，搭配本機 Admin API (`--serve 8080`) 實現一鍵寫回 `apps.yaml`；支援 `auto` / `gemini_only` / `openrouter_only` 安全切換與 Cost Guard 阻擋。 |
| **使用量與配額觀測 (#42)** | `ai_telemetry.py` | 審計近 7 天請求量、成功率、429 Rate Limit、備援次數；嚴格 Token 審計（不造假估算）；清楚標示 Free Tier 適用資格與 Google Cloud 計費聲明。 |

### AI Structured Output 契約規格 (Issue #39)

Gemini Direct 與 OpenRouter Free Worker 全面共用同一份標準 `CANONICAL_AI_RESPONSE_SCHEMA`，不依賴 legacy `generationConfig.responseJsonSchema` 或 lossy OpenAPI 3.0 schema rewrite：
- **Google Gemini Direct**：
  - 端點：`POST https://generativelanguage.googleapis.com/v1beta/interactions`
  - Structured Output 契約：頂層 `response_format: {"type": "text", "mime_type": "application/json", "schema": CANONICAL_AI_RESPONSE_SCHEMA}`
  - 響應解析：由 `steps[].type == "model_output"` 之 content text 提取純淨 JSON 區塊
  - Token 審計：由 `usage.total_input_tokens` / `total_output_tokens` / `total_tokens` 精確記錄
  - 向下相容：支援 `api_type="generate_content"` 及 legacy `candidates` 結構回退
- **OpenRouter Free Worker**：
  - 端點：`POST https://openrouter.ai/api/v1/chat/completions`
  - Structured Output 契約：頂層 `response_format: {"type": "json_schema", "json_schema": {"name": "...", "strict": true, "schema": CANONICAL_AI_RESPONSE_SCHEMA}}`
  - 零資料保留：自動注入 `provider: { data_collection: "deny" }` 實踐 Zero Data Retention (ZDR)

---

## AI Policy 管理

查看有效設定：
```bash
python3 -m crash_trend.ai_config_service --app shop_app --show
```

切換模式：

```bash
python3 -m crash_trend.ai_config_service --app shop_app --mode auto
python3 -m crash_trend.ai_config_service --app shop_app --mode gemini_only
python3 -m crash_trend.ai_config_service --app shop_app --mode openrouter_only
```

調整 provider / model：

```bash
python3 -m crash_trend.ai_config_service \
  --app shop_app \
  --primary-provider gemini \
  --primary-model gemini-3.8-flash \
  --lightweight-provider openrouter \
  --lightweight-model openrouter/free
```

重設 App override：

```bash
python3 -m crash_trend.ai_config_service --app shop_app --reset
```

啟動本機 Admin API：

```bash
python3 -m crash_trend.ai_config_service --serve 8080
```

此服務用於 Dashboard 設定頁寫回 `apps.yaml`，包含 token authentication 與 CORS Origin 驗證；請維持在受信任的本機 / 內網環境使用。

---

## 版本品質退化閘門 (Release Regression Gate - Issue #57)

系統內建確定性（Deterministic）版本退化判定閘門，嚴格依據正規化指標（Normalized Metrics）評估發佈版本健康度，AI 僅於下游解釋，絕不介入判定 PASS / FAIL：

- **判定狀態**：`pass`、`warn`、`fail`、`insufficient_data`（樣本數不足，絕不誤判 pass/fail）、`baseline`（平台首發版本）。
- **評估指標**：
  - 工作階段崩潰率上升幅度 (`crash_rate_change_pct`：預設 +10% 預警, +25% 失敗阻擋)
  - 無崩潰用戶率下降幅度 (`crash_free_users_drop`：預設 0.5% 預警, 1.5% 失敗阻擋)
  - Fatal / ANR 率上升幅度 (`fatal_rate_change_pct`, `anr_rate_change_pct`)
  - 復發問題數與新引入問題數 (`regressed_issues_count`, `introduced_issues_count`)
- **機器可讀產物**：產出 `out/<app>/release_gate.json`，遵循嚴格 Schema 並落實零 raw user / installation UUID 隱私政策。
- **CI Quality Gate 整合**：
  - 單獨執行品質判定：
    ```bash
    python3 -m crash_trend.release_gate --app shop_app
    ```
  - 搭配 `--fail-on-regression` 進行 CI 自動阻擋：
    ```bash
    python3 crash_trend/pipeline_run.py --fail-on-regression
    ```
  - **Exit Codes 契約**：
    - `0`：管線執行成功，且品質閘門為 pass / warn / baseline / insufficient_data（或未加 `--fail-on-regression`）。
    - `1`：管線執行異常（BigQuery 異常、程式崩潰等運行期錯誤）。
    - `2`：品質退化阻擋（啟用 `--fail-on-regression` 且任何 App 之 release gate 判定為 `fail`）。

---

## Google Chat 品質告警傳送 (Google Chat Quality Alerts Delivery - Issue #59)

系統內建 Google Chat Incoming Webhook 品質通知發送機制，將版本品質退化閘門（Release Gate）之 WARN / FAIL 與復原狀態即時送達團隊 Space：

- **嚴格金鑰隔離與脫敏**：Webhook URL 僅從環境變數注入（預設 `GOOGLE_CHAT_WEBHOOK_URL`），嚴禁寫入設定檔、產物或記錄檔。日誌、異常堆疊與審計儲存中出現之 URL 自動脫敏為 `https://chat.googleapis.com/...<redacted>`。
- **確定性去重與冷卻 (Deterministic Dedupe & Cooldown)**：
  - 依 `(app_id, platform, version, gate_status, sorted(reasons), policy_version)` 產生確定性 SHA-256 數位指紋 (`alert_fingerprint`)。
  - 預設冷卻時間 360 分鐘（6 小時），避免重複洗版。
  - **狀態變更（Status Change）** 或 **新增退化原因（New Reason）** 自動忽略冷卻即時重發。
- **指標復原通知 (Recovery Notification)**：
  - 當前次已通知退化之版本在新一次評估中指標全數修復至 `pass`，系統自動於同一 Space/Thread 發送 `✅ Release Gate Recovered` 復原通知。
- **發佈專屬 Thread 收攏**：
  - 支援確定性 `threadKey`（格式：`crash-trend:{app_id}:{platform}:{version}`），同一發佈版本的首次警報、原因變更與復原通知自動收納於同一個討論串。
- **可靠傳送與有限重試**：
  - 連線與讀取逾時限制，避免卡住管線。
  - 針對 429、5xx 與網路暫態錯誤實施有界指數退避重試（最多 3 次），4xx 永久錯誤立即失敗不空轉重試。
- **解耦式失敗語意 (Decoupled Failure Semantics)**：
  - Webhook HTTP 傳送失敗 **絕不改變 Release Gate 評估結果**，亦不會中斷 `release_gate.json` 或後續 `build_dashboard` 產出。
- **CLI 獨立執行與預覽**：
  ```bash
  # 預覽通知內容與 Payload（不實際送出 HTTP POST，不寫入 sent 狀態）
  python3 -m crash_trend.alerts --app shop_app --dry-run

  # 強制略過去重與冷卻送出
  python3 -m crash_trend.alerts --app shop_app --force
  ```
- **SQLite 稽核與狀態儲存**：
  - 於 `out/<app>/alert_delivery.sqlite3` 記錄每次發送嘗試、狀態、HTTP 碼、指紋與錯誤訊息（嚴禁記錄 URL/Token/User IDs）。

---

## 歷史閘門快照與品質趨勢 (Historical Gate Trend - Issue #61)

每次 Release Gate 評估結果均保存為不可變歷史快照，持久化於 SQLite 儲存庫 `out/<app>/release_gate_history.sqlite3`（資料表 `release_gate_snapshots`），作為品質演進趨勢分析之權威來源：

- **不可變快照與冪等重試**：依據 `(app_id, platform, version, policy_identity, metrics_digest)` 產生 SHA-256 `evaluation_key`，以唯一索引搭配 `INSERT OR IGNORE` 保證重複執行或歷史重播完全冪等，絕不產生重複資料。
- **狀態演進與轉移追蹤**：完整記錄版本從 `insufficient_data -> warn -> fail -> pass (recovery)` 的狀態演進軌跡，追蹤指標如何隨修復或新版本發佈逐步收斂。
- **解耦失敗語意**：歷史快照儲存庫寫入異常絕不中斷主要管線，亦不影響 `release_gate.json` 或後續 `build_dashboard` 產出。
- **Dashboard 前端整合**：
  - **Release Detail Modal**：在發布版本詳情中呈現 **Gate Evaluation Timeline**，包含時間戳、Gate 判定狀態標籤、摘要、觸發規則清單與狀態轉移軌跡。
  - **Quality Trend 概覽**：直觀呈現近期發佈版本的品質演進趨勢。
- **CLI 查詢與資料修剪**：
  ```bash
  # 查詢指定版本歷史評估軌跡
  python3 -m crash_trend.release_gate_history --app shop_app --platform android --version 3.2.0

  # 查詢最近版本品質演進趨勢
  python3 -m crash_trend.release_gate_history --app shop_app --platform android --trend --limit 10

  # 修剪超過指定天數之歷史快照
  python3 -m crash_trend.release_gate_history --app shop_app --prune-older-than-days 180
  ```

---

## 告警觀測度與發送審計 (Alert Delivery Observability - Issue #63)

系統提供完整的唯讀投影與查詢層 (`crash_trend/alerts/observability.py`)，將 Google Chat 品質通知之發送狀態、重試、去重冷卻與審計記錄安全呈現於 Dashboard 與 CLI：

- **確定性健康度語意 (Transparent Health Semantics)**：
  - `healthy`：最近一次真實（非 dry-run）投遞嘗試成功。
  - `degraded`：最近一次真實投遞嘗試失敗。
  - `no_data`：尚無真實投遞嘗試紀錄。
  - `unavailable`：資料庫毀損或讀取異常（附帶清洗後之診斷資訊）。
  - 去重冷卻抑制（`suppressed`）視為正常策略決策，**絕不使健康度降級**；`dry_run` 記錄嚴格排除於健康度統計之外。
- **權威對齊之未解決失敗次數 (`unresolved_failures`)**：
  - 連續失敗計數嚴格遵從 `(attempted_at DESC, id DESC)` 權威，計算自最新成功投遞以來的失敗次數，即便非同步 worker 延遲寫入仍保證與健康度狀態 100% 一致。
- **全 Scheme 憑證防禦性清洗 (Zero-Secret Guarantees)**：
  - `sanitize_audit_text()` 徹底抹除 Webhook URL、Token、密鑰、所有 HTTP 認證標頭（包含 `Bearer`, `Basic`, `ApiKey`, `Digest` 與 JSON 鍵值）以及 Raw UUID，確保導出資料與 Dashboard 絕不洩漏機密。
- **唯讀安全模式 (Read-only Safe Observability)**：
  - Dashboard 建置與 Observability 查詢連線嚴格使用 SQLite URI `mode=ro`，完全略過目錄建立、WAL 與 DDL 遷移。
  - 讀取缺少 `is_recovery` 欄位之舊版資料庫或唯讀檔案系統（`chmod 444`）時安全回退，絕不產生 `attempt to write a readonly database`。
- **Dashboard 前端整合**：
  - **Release Detail Modal**：在 Gate Evaluation Timeline 下方呈現 **Alert Delivery Timeline**（發送狀態、嘗試次數、HTTP 碼、抑制原因 / 清洗後錯誤訊息、復原標記）。
  - **Data Pipelines & Notifications View (`#view-notifications`)**：呈現 Google Chat 連線健康度卡片、24 小時發送統計（成功、失敗、抑制次數）與最近通知審計表格。
- **CLI 審計查詢**：
  ```bash
  # 查詢最近 20 筆發送審計記錄
  python3 -m crash_trend.alerts --history --app shop_app --limit 20

  # 依平台與版本篩選並輸出結構化 JSON
  python3 -m crash_trend.alerts --history --app shop_app --platform android --version 3.2.0 --json
  ```

---

## 週同步與通知

`scripts/weekly_sync.sh` 會：

1. 執行 `pipeline_run.py`。
2. 依每個 App 的資料源設定抓取 / enrich / normalize / analyze。
3. 產生最新 Dashboard 與 Pipeline Run Summary。
4. 若設定 `CRASH_REPORT_URL`，每月最多發送一次聊天摘要卡；發送失敗時下週重試。
5. 若 repo 有產生可追蹤的變更，建立 `chore: weekly sync YYYY-MM-DD` commit。
6. macOS 本機執行時可透過 `osascript` 顯示完成 / 失敗通知。

部署與排程細節請參考 [`DEPLOY.md`](DEPLOY.md)。

---

## 專案結構

```text
crash_trend/
  ai_config_service.py   # AI Policy CLI / Admin API
  ai_provider.py         # Gemini / OpenRouter Provider 與 Canonical Schemas
  ai_router.py           # Task Router、Cost Guard、Fallback、Privacy policy
  ai_telemetry.py        # AI request / quota / token telemetry
  analyze_ai.py          # Provider-neutral AI 分析入口
  analyze_gemini.py      # Priority Score、triage gating 與分析核心
  authority_store.py     # SQLite-backed Catalog Authority Store（去重權威儲存）
  build_dashboard.py     # 自包含 Dashboard HTML 產生器
  check_surge.py         # Crash 趨勢暴增偵測
  config.py              # apps.yaml 載入與資料源設定
  fetch_bigquery.py      # Crashlytics BigQuery 資料抓取
  fetch_issue_details.py # Issue detail / stack trace enrichment
  fetch_sessions.py      # Firebase Sessions / Crash-free metrics
  fetch_stacktraces.py   # Firebase MCP stacktrace cache
  gate/                  # Release Regression Gate 核心 (policy, evaluator, artifact)
  lifecycle.py           # Issue & Release Catalog lifecycle / 狀態與比較處理
  normalize.py           # Canonical normalization / 歷史資料整理
  pipeline_health.py     # Run Summary、Stage status、錯誤資訊消毒
  pipeline_run.py        # 端到端 Pipeline Orchestrator
  pm_brief.py            # PM-friendly issue 摘要
  post_report.py         # 每月聊天摘要卡
  release_gate.py        # Release Regression Gate CLI / Standalone runner
  schema_v2.py           # Dashboard V2 TypedDict / schema validation
  versions.py            # App version 比較工具

scripts/
  create_sa.sh
  weekly_sync.sh
  com.crash-trend.weekly-sync.plist.example

docker/
  crontab

docs/
  dashboard_v2_schema.md
  screenshot.png

manual/
  example_app/

tests/
  ...

DEPLOY.md
Dockerfile
docker-compose.yml
apps.example.yaml
pyproject.toml
uv.lock
requirements.txt
```

---

## 主要輸出與儲存總表

執行後的 runtime artifacts 與資料庫預設不進 Git：

```text
dashboard.html
out/
  pipeline_run.json                 # 管線健康度與 stage 審計
  dashboard_v2.json                 # Dashboard V2 Bundle（前端渲染契約，頂層聚合）
  <app>/
    dashboard_v2.json               # 單一 app 前端渲染契約
    historical_catalog.json         # 跨週期版本目錄與 Issue 生命週期累積狀態契約
    release_gate.json               # 單次最新 Release Gate 評估結果產物
    catalog_authority.sqlite3       # SQLite 受影響用戶去重權威儲存（不含 PII 的加鹽雜湊）
    release_gate_history.sqlite3    # SQLite 閘門評估快照不可變歷史序列
    alert_delivery.sqlite3          # SQLite 品質通知發送嘗試、去重冷卻與審計儲存
reports/
logs/
```

`.env`、`apps.yaml`、Service Account JSON、Admin token 與 crash 原始輸出也已透過 `.gitignore` / Docker mount 策略避免直接提交到 repository。

### 儲存架構與 Artifact 生命週期總表

| 儲存項目 | 路徑 | 格式 / 引擎 | 擁有者 (Writer) | 讀取者 (Reader) | 權威性 / 生命週期 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Pipeline Run Summary** | `out/pipeline_run.json` | JSON | `pipeline_run.py` | Dashboard, CI | 單次管線執行之健康度與 stage 審計資訊 |
| **Multi-App Dashboard Bundle** | `out/dashboard_v2.json` | JSON | `pipeline_run.py` / `renderer.py` | Web UI (`dashboard.html`) | 聚合所有 App 當期與多週期快照之唯讀展示容器 |
| **Single-App Dashboard Bundle** | `out/<app>/dashboard_v2.json` | JSON | `pipeline_run.py` / `renderer.py` | Web UI | 單一 App 專屬之完整前端資料容器 |
| **Historical Catalog** | `out/<app>/historical_catalog.json` | JSON | `lifecycle.py` | Release Gate, Dashboard | 跨週期版本累積與 Issue 生命週期狀態（零 raw IDs） |
| **Release Gate Artifact** | `out/<app>/release_gate.json` | JSON | `release_gate.py` | CI, Alerts Dispatcher, Dashboard | 最新一次 Release Gate 評估結果之機器可讀產物 |
| **Catalog Authority Store** | `out/<app>/catalog_authority.sqlite3` | SQLite 3 (WAL) | `authority_store.py` | `lifecycle.py` | 設備級受影響用戶精確去重之唯一事實來源（加鹽 SHA-256） |
| **Release Gate History Store** | `out/<app>/release_gate_history.sqlite3`| SQLite 3 (WAL) | `gate/history.py` | `release_gate.py`, Dashboard | 閘門評估快照不可變歷史序列與品質演進趨勢 |
| **Alert Delivery Audit Store** | `out/<app>/alert_delivery.sqlite3` | SQLite 3 (WAL) | `alerts/state.py` | Observability, Dashboard | 發送嘗試、HTTP 結果、去重冷卻抑制原因與 24h 審計 |
| **自包含靜態儀表板** | `dashboard.html` | Self-contained HTML | `build_dashboard.py` | 瀏覽器, Nginx | 內嵌前端 Bundle 與視覺化樣式之單一 HTML 檔案 |

---

## CLI 指令與 Exit Code 規範 (CLI Usage & Exit Code Contract)

### Exit Code 契約

crash-trend 的 CLI 工具在設計上與 CI/CD 工作流深度整合，嚴格遵循確定性的 Exit Code 語意：

| Exit Code | 狀態定義 | 說明與觸發情境 |
| :--- | :--- | :--- |
| **`0`** | **成功 (Success)** | 管線正常執行完成；品質閘門判定為 `pass` / `warn` / `insufficient_data` / `baseline`（或未加 `--fail-on-regression`）；品質警報正常送出或被策略抑制（或未加 `--fail-on-alert-failure`）。 |
| **`1`** | **運行期異常 (Runtime Error)** | BigQuery 查詢失敗、網路斷線、未捕捉例外或程式崩潰；或啟用 `--fail-on-alert-failure` 且品質通知 HTTP 傳送失敗。 |
| **`2`** | **品質退化阻擋 (Gate Regression)** | 啟用 `--fail-on-regression` 且任何 App 之 Release Gate 判定為 `fail`（由 CI 捕獲以立即阻擋有問題的部署或發佈）。 |

### 核心 CLI 工具速查

| 模組 | 主要用途 | 常用參數範例 |
| :--- | :--- | :--- |
| `crash_trend.pipeline_run` | 端到端管線編排 | `python3 -m crash_trend.pipeline_run --app shop_app --days 30 --fail-on-regression` |
| `crash_trend.release_gate` | 版本品質退化閘門獨立評估 | `python3 -m crash_trend.release_gate --app shop_app --platform android --fail-on-regression` |
| `crash_trend.release_gate_history` | 閘門歷史快照與品質趨勢 | `python3 -m crash_trend.release_gate_history --app shop_app --trend --limit 10` |
| `crash_trend.alerts` | 品質警報發送與審計查詢 | `python3 -m crash_trend.alerts --history --app shop_app --limit 20 --json` |
| `crash_trend.ai_config_service` | AI 治理原則調整與 Admin API | `python3 -m crash_trend.ai_config_service --app shop_app --mode auto` |
| `crash_trend.build_dashboard` | 手動獨立產出 Dashboard HTML | `python3 -m crash_trend.build_dashboard --in out/dashboard_v2.json --out dashboard.html` |

---

## 向下相容與遷移指南 (Backward Compatibility & Migration)

- **V2.6 至 V2.7 零手動遷移 (Zero-Manual-Migration)**：
  - 現有專案升級至 V2.7 完全無痛，不需手動執行任何資料庫遷移指令或 SQL script。
  - 新增之 SQLite 儲存庫（`release_gate_history.sqlite3`, `alert_delivery.sqlite3`）於首次執行管線時自動在寫入路徑建立。
- **唯讀環境安全相容 (Read-only Safe Observability)**：
  - Dashboard 建置與 Observability 查詢在連線 SQLite 時一律使用 `mode=ro` URI 連線，完全略過目錄建立、WAL pragma 與 `CREATE TABLE`。
  - 資料庫檔案尚未建立時，各查詢函式主動檢查 `_table_exists()` 並安全回傳空記錄或零統計值。
  - 在唯讀掛載檔案系統或 `chmod 444` 環境下讀取舊版 SQLite（缺少 `is_recovery` 欄位）時，透過 `PRAGMA table_info` 偵測並安全降級回傳 `is_recovery=False`，**絕不觸發任何 DDL 寫入，徹底杜絕 `attempt to write a readonly database`**。
- **Schema 欄位平滑演進**：
  - 寫入路徑（Dispatcher / Store）在需要時自動以 `ALTER TABLE ADD COLUMN is_recovery` 增量遷移。
  - `historical_catalog.json` 載入舊版含 `installation_ids` 之檔案時，自動遷移入 SQLite 加鹽雜湊庫，持久化 JSON 強制保證零 raw IDs。
  - `schema_v2.py` 之 `SUPPORTED_SCHEMA_VERSIONS` 保持完整相容清單：`{"2.0", "2.3", "2.3.0", "2.6", "2.6.0", "2.7", "2.7.0"}`。

---

## Release Notes: Dashboard V2.7

Dashboard V2.7 標誌著從單純的「事後監控與報表」邁向「主動品質退化守護與即時告警審計」的重大演進：

1. **Issue #57 — Release Regression Gate & Quality Alerts**：
   - 建立確定性版本退化判定核心，嚴格以正規化暴險指標（Crash Rate 變動率、Crash-free Users 降幅、Fatal/ANR 升幅、復發/新引入問題）客觀評估。
   - 產出結構化機器可讀產物 `out/<app>/release_gate.json`。
   - 整合 CI Pipeline 自動阻擋機制（`--fail-on-regression`，Exit code 2）。
2. **Issue #59 — Google Chat Quality Alerts Delivery**：
   - 整合 Google Chat Incoming Webhook，實踐版本退化（WARN / FAIL）與復原通知即時推送。
   - 實作確定性 SHA-256 數位指紋與 6 小時去重冷卻，支援狀態變更與新原因即時重發。
   - 支援版本專屬 `threadKey` 討論串收攏，以及嚴格金鑰隔離與 URL 脫敏。
   - 提供 `--dry-run`、`--force` 與 `--fail-on-alert-failure` 獨立 CLI。
3. **Issue #61 — Historical Gate Trend**：
   - 建立 SQLite 不可變歷史評估快照庫 `out/<app>/release_gate_history.sqlite3`。
   - 以 `evaluation_key` 保證重播與重試完全冪等。
   - 追蹤 `insufficient_data -> warn -> fail -> pass` 之狀態演進軌跡與近期版本品質趨勢。
   - 於 Dashboard Release Detail Modal 呈現 Gate Evaluation Timeline。
4. **Issue #63 — Alert Delivery Observability**：
   - 實作唯讀投影與查詢層（`crash_trend/alerts/observability.py`）。
   - 定義確定性健康度狀態語意（healthy, degraded, no_data, unavailable），連續失敗計數嚴格對齊 `(attempted_at DESC, id DESC)` 權威。
   - 實作全 Scheme 認證憑證防禦性清洗（Basic, ApiKey, Digest, Bearer, JSON）。
   - 於 Dashboard Notifications 視圖呈現連線健康卡片、24h 統計與最近通知審計表；Release Detail 呈現發送軌跡。
   - 支援唯讀模式（`read_only=True`, `mode=ro`）與舊版資料庫安全相容。
5. **Issue #65 — Contract, Documentation & Technical Debt Cleanup**：
   - 全面收斂六大解耦契約、升級主 README 與 Schema Spec。
   - 補齊 apps.example.yaml、CLI 文件與儲存路徑總表。
   - 規劃 V2.8 技術債與未來演進路線。

---

## 開發與品質檢驗 (Development & Quality Gates)

本專案使用 `pyproject.toml` 與 `uv` 進行依賴鎖定與品質檢查。

### 本地環境安裝

```bash
# 使用 uv 同步虛擬環境與開發依賴（推薦）
uv sync

# 或使用標準 pip 安裝 editable package 與 dev 依賴
pip install -e ".[dev]"
```

### 品質檢查指令

```bash
# 驗證 lockfile 與 requirements.txt 一致性（無漂移）
uv lock --check
./scripts/export_requirements.sh  # 或: uv export --no-dev --locked --no-emit-project --no-hashes --no-header -o requirements.txt

# 程式碼 Linting（Ruff）
uv run ruff check .

# 靜態型別檢查基線（Mypy Baseline）
uv run mypy

# 執行全量單元與整合測試
uv run python -m unittest discover -s tests -p "test_*.py" -v
```

### GitHub Actions CI
GitHub Actions (`.github/workflows/ci.yml`) 在每次 push 與 PR 時自動執行四道品質閘門：
1. **Lockfile & Requirements 一致性**：驗證 committed `uv.lock` 與 `pyproject.toml` 一致，且 `requirements.txt` 無漂移。
2. **測試矩陣**：Python 3.11 與 3.12 全量單元測試（使用 `--locked --no-sync` 嚴格確保環境不浮動）。
3. **Linter**：Ruff 程式碼規範與語法檢查。
4. **Type Check**：核心模組 Mypy 靜態型別檢查。

---

## 文件

- [`DEPLOY.md`](DEPLOY.md)：Docker / 排程 / 部署方式
- [`docs/dashboard_v2_schema.md`](docs/dashboard_v2_schema.md)：Dashboard V2.7 資料契約體系規格
- [`apps.example.yaml`](apps.example.yaml)：完整多 App / AI / Data Source / Release Gate & Alerts 設定範例

## License

MIT — 詳見 [`LICENSE`](LICENSE)。
