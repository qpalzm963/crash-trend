# Dashboard V2.6 Data Schema & Contracts Specification

本文檔定義 **Crashlytics Engineering Dashboard V2.6** 的完整資料契約體系（Data Contracts）。
本契約旨在徹底解耦資料擷取（BigQuery、Firebase Sessions、MCP、Console）、去重權威儲存（SQLite）、跨視窗歷史目錄（Historical Catalog）、AI 策略分析與前端 UI 呈現層，使系統各層級能以標準化、強型別、具備確定性與隱私保護的介面獨立演進。

---

## 1. 設計原則與核心概念

1. **三大契約職責分離（Three Distinct Decoupled Contracts）**：
   系統嚴格劃分三種獨立且單向流動的資料契約：
   - **Dashboard JSON Contract** (`out/dashboard_v2.json`)：專供前端 Web UI 呈現的聚合容器（Bundle）。
   - **Historical Catalog JSON Contract** (`out/historical_catalog.json`)：跨視窗版本演進與 Issue 生命週期累積狀態。
   - **SQLite Authority Store Contract** (`out/catalog_authority.sqlite3`)：設備級精確去重的唯一事實來源（Single Source of Truth）。
2. **前端與 Raw BigQuery 完全解耦**：
   UI 僅消費本 Schema 定義的聚合資料與結構化分析，不得直接解析 BigQuery raw JSON 或自行在前端執行重度聚合。
3. **精確去重與隱私防護（Privacy-Preserving Deterministic Salted Hashing & Zero PII）**：
   - 設備識別碼（`installation_uuid`）在進入權威資料庫前，一律以 `app_id` 作為 Salt 進行 SHA-256 確定性加鹽雜湊（`SHA-256(f"{app_id}:{raw_uuid}")`），資料庫內不儲存任何明文 PII。
   - **JSON 持久化禁止條款**：`historical_catalog.json` 與 `dashboard_v2.json` **嚴禁儲存任何 raw `installation_ids` 或 `user_ids` 清單**，徹底避免 JSON 檔案膨脹與記憶體外溢。
4. **多 App 與跨平台（Multi-App & Cross-Platform 一等支援）**：
   原生支援包含 iOS / Android 雙平台或多 App 切換，版本與問題追蹤嚴格依平台隔離，禁止不同平台的同名版本相互污染。
5. **正確的聚合語意與資料一致性（Accurate Aggregations & Consistency）**：
   - `crash_events`：期間內所有崩潰事件全量統計（`COUNT(*)`）。
   - `affected_users`：期間內去重受影響用戶（`COUNT(DISTINCT installation_uuid)`），**禁止跨 Issue 加總造成重複計數**。
   - `daily_trend` 一致性：`sum(daily_trend[i].crash_events) == kpi.crash_events.value`；`daily_trend[i].fatal_events + daily_trend[i].anr_events + daily_trend[i].non_fatal_events == daily_trend[i].crash_events`。
   - `affected_users` 在 daily trend 中為當日 distinct users，各日相加可能大於期間總 distinct users（正常現象）。
6. **顯式狀態與空值語意（Explicit Availability Semantics）**：
   對外部依賴（如 Firebase Sessions 導出、AI 策略分析）提供顯式的 `status`（`available` / `unavailable` / `disabled` / `error` / `stale` / `insufficient_data`）。當 Sessions 未啟用時，Crash-free 指標呈現 `unavailable`，**嚴禁顯示假 0% 或假 0**。
7. **嚴格時間戳與版本號規範（Strict ISO 8601 UTC Timestamps & Authoritative Dates）**：
   所有時間戳一律使用標準 **ISO 8601 UTC** 字串（例：`2026-09-02T14:30:00Z`），必須以 `Z` 或 `+00:00` 結尾。版本發布日期 `release_date` 為權威日期，**嚴禁 fallback 至 `first_seen` 時間戳**。
8. **冪等性與增量水線（Idempotency & Watermark Synchronization）**：
   透過事件時間戳水線（`watermark`）與 SQLite `INSERT OR IGNORE` 實現冪等追加寫入，保證重複執行管線不產生資料偏差。

---

## 2. 資料契約架構與系統邊界

```mermaid
flowchart TD
    subgraph RawDataSources [外部資料源]
        BQ[BigQuery Crashlytics]
        Sess[Firebase Sessions]
        MCP[Firebase MCP / Crashlytics Console]
        AI[Gemini / OpenRouter AI]
    end

    subgraph PipelineCore [資料管線核心處理 pipeline_run / lifecycle]
        Norm[Normalize & Triage]
        Prio[Deterministic Priority]
    end

    subgraph AuthorityContract [Contract 3: SQLite Authority Store]
        DB[(catalog_authority.sqlite3)]
        DB_Inst[release_installations\nSalted SHA-256 Hashes]
        DB_Stat[version_authority_status\nBootstrap Status]
        DB_Meta[authority_metadata\nState Version]
        DB --- DB_Inst
        DB --- DB_Stat
        DB --- DB_Meta
    end

    subgraph HistoricalContract [Contract 2: Historical Catalog JSON]
        CatJSON[historical_catalog.json\nHistoricalCatalogData]
        Cat_Ver[app_versions: Lifetime Metrics & Recent Health]
        Cat_Iss[issues: Issue History & Versions Seen]
        Cat_Meta[authority metadata & watermark]
        CatJSON --- Cat_Ver
        CatJSON --- Cat_Iss
        CatJSON --- Cat_Meta
    end

    subgraph DashboardContract [Contract 1: Dashboard JSON]
        DashJSON[dashboard_v2.json\nDashboardV2Bundle]
        Dash_Apps[apps: AppDashboardV2Data]
        Dash_Rel[release_catalog: ReleaseCatalogItem]
        Dash_KPI[kpi / daily_trend / top_issues / ai_summary]
        DashJSON --- Dash_Apps
        Dash_Apps --- Dash_Rel
        Dash_Apps --- Dash_KPI
    end

    subgraph PresentationLayer [前端呈現層]
        UI[dashboard.html\nVue / Tailwind UI]
    end

    BQ -->|Raw Events / installation_uuid| Norm
    Sess -->|Crash-free Metrics| Norm
    MCP -->|Stacktrace / Logs| Norm
    Norm -->|Salted SHA-256 Hashes| DB
    DB -->|Exact Deduplicated Counts| CatJSON
    Norm -->|Version & Issue States| CatJSON
    CatJSON -->|Decoupled Release Catalog & Lifecycles| DashJSON
    AI -->|AI Summary & Analysis| DashJSON
    Prio -->|P0~P3 Priority Scores| DashJSON
    DashJSON --> UI
```

### 三大契約特性對比矩陣

| 特性維度 | 1. Dashboard JSON Contract | 2. Historical Catalog JSON Contract | 3. SQLite Authority Store Contract |
| :--- | :--- | :--- | :--- |
| **檔案路徑** | `out/dashboard_v2.json` | `out/historical_catalog.json` | `out/catalog_authority.sqlite3` |
| **主要定位** | 前端呈現容器（Bundle） | 跨視窗版本演進與狀態累積 | 精確去重唯一事實來源（SSOT） |
| **儲存引擎** | 純 JSON 檔案 | 純 JSON 檔案 | 本機 SQLite 3（WAL 模式） |
| **Schema 版本** | `2.6.0`（相容 `2.0` ~ `2.6.0`） | `2.3.0` / `2.6.0` | `state_version: 1` |
| **PII / 裝置 ID** | **完全零 IDs**（無 installation IDs） | **完全零 IDs**（無 installation IDs） | **加鹽雜湊集合**（SHA-256，零明文） |
| **讀寫模式** | 每次 Pipeline Run 全量產出 | 每次 Pipeline Run 增量讀取並寫回 | 依水線批次追加（`INSERT OR IGNORE`） |
| **生命週期行為** | 單次消費 / UI 渲染 | 跨週期持久化、水線推進 | 跨週期持久化、去重計算、權威標記 |

---

## 3. 詳細欄位規格定義

### 3.1 容器與 Metadata

#### `DashboardV2Bundle`
前端載入或 static dashboard 內嵌之頂層容器：
| 欄位名稱 | 型別 | 必填 | 說明 | 範例 |
| :--- | :--- | :--- | :--- | :--- |
| `schema_version` | string | 是 | Schema 版本號，當前為 `"2.6.0"`（相容 `"2.0"`, `"2.3"`, `"2.3.0"`, `"2.6"`, `"2.6.0"`） | `"2.6.0"` |
| `generated_at` | string (ISO 8601 UTC) | 是 | 報表產生時間（UTC，結尾必須為 `Z`） | `"2026-09-02T14:00:00Z"` |
| `default_app` | string | 是 | 預設開啟的 App key（必須存在於 `apps` 鍵值中） | `"my_app"` |
| `apps` | object | 是 | Key 為 app ID，Value 為 `AppDashboardV2Data` | `{ "my_app": { ... } }` |
| `pipeline_run` | object \| null | 否 | 最近一次管線執行健康度與 stage 審計資訊 | 見 `out/pipeline_run.json` |
| `global_ai_policy` | object \| null | 否 | 全域 AI 治理原則（routing mode, primary/lightweight 模型） | `{ "mode": "auto" }` |
| `ai_usage` | object \| null | 否 | AI Token 與配額審計資訊 | `{ "total_tokens": 12500 }` |

#### `AppDashboardV2Data`
單一 App 完整資料核心：
| 欄位名稱 | 型別 | 必填 | 說明 | 範例 |
| :--- | :--- | :--- | :--- | :--- |
| `metadata` | AppMetadata | 是 | 應用程式基礎資訊 | 見下方 `AppMetadata` |
| `period` | PeriodInfo | 是 | 當前主視窗統計時間區間（如 30 天） | 見下方 `PeriodInfo` |
| `sources` | SourcesAvailability | 是 | 外部資料源抓取健康度與狀態 | 見下方 `SourcesAvailability` |
| `kpi` | OverviewKPI | 是 | 核心 KPI 匯總 | 見下方 `OverviewKPI` |
| `daily_trend` | array[DailyTrendPoint] | 是 | 每日趨勢序列（依日期升冪排序） | 見下方 `DailyTrendPoint` |
| `version_health` | array[VersionHealthItem]| 是 | 當期活躍版本之健康度列表 | 見下方 `VersionHealthItem` |
| `distributions` | Distributions | 是 | 維度分布統計（平台、裝置、OS、版本） | 見下方 `Distributions` |
| `top_issues` | array[IssueSummary] | 是 | Top Issue 清單（含優先級與 AI 分析） | 見下方 `IssueSummary` |
| `ai_summary` | AISummary | 是 | 整體健康度 AI 策略摘要報告 | 見下方 `AISummary` |
| `limitations` | array[string] | 是 | 資料限制或邊界說明字串陣列 | `["Sessions 僅收集近 30 天"]` |
| `periods` | object \| null | 否 | 多週期權威快照字典（Key 為天數 `"7"`, `"30"`, `"90"`） | 見下方 `AppPeriodSnapshot` |
| `ai_policy` | object \| null | 否 | 該 App 專屬之 AI Policy 覆寫設定 | `{ "mode": "gemini_only" }` |
| `release_catalog` | array[ReleaseCatalogItem] \| null | 否 | 獨立於單期快照之長期持續版本目錄 | 見第 3.9 節 `ReleaseCatalogItem` |

#### `AppMetadata`
| 欄位名稱 | 型別 | 必填 | 說明 | 範例 |
| :--- | :--- | :--- | :--- | :--- |
| `app_id` | string | 是 | 系統識別用唯一代號 | `"my_app"` |
| `display_name` | string | 是 | UI 顯示名稱 | `"My App (Taiwan)"` |
| `firebase_project_id` | string | 是 | Firebase 專案代碼 | `"my-app-prod-1234"` |
| `platforms` | array[string] | 是 | 支援之平台列表（`"ios"`, `"android"`） | `["ios", "android"]` |
| `source_repo` | string \| null | 是 (可為 null) | 本地/遠端原始碼路徑或 Git 網址 | `"~/projects/my_app"` |
| `custom_keys_monitored`| array[string] | 是 | 監控中自訂鍵值列表 | `["user_tier", "screen_name"]` |

#### `PeriodInfo`
| 欄位名稱 | 型別 | 必填 | 說明 | 範例 |
| :--- | :--- | :--- | :--- | :--- |
| `days` | integer | 是 | 統計回溯天數 | `30` |
| `start_time` | string (ISO 8601 UTC) | 是 | 區間開始時間（UTC，結尾必須為 `Z`） | `"2026-08-03T14:00:00Z"` |
| `end_time` | string (ISO 8601 UTC) | 是 | 區間結束時間（UTC，結尾必須為 `Z`） | `"2026-09-02T14:00:00Z"` |
| `comparison_period` | object \| null | 是 (可為 null) | 上期對比時間範圍資訊 | `{ "days": 30, "start_time": "...", "end_time": "..." }` |

---

### 3.2 資料來源狀態（Sources & Availability）

#### `SourcesAvailability`
記錄各外部數據管道的健康度與抓取狀態：
```json
{
  "crashlytics_bq": {
    "status": "available",
    "tables_queried": ["com_example_app_IOS", "com_example_app_ANDROID"],
    "last_sync_timestamp": "2026-09-02T14:00:00Z",
    "error_message": null
  },
  "firebase_sessions": {
    "status": "unavailable",
    "last_sync_timestamp": null,
    "error_message": "Firebase Sessions export table not found in dataset"
  },
  "mcp_crashlytics": {
    "status": "available",
    "last_sync_timestamp": "2026-09-02T14:05:00Z",
    "error_message": null
  },
  "ai": {
    "status": "available",
    "provider": "gemini",
    "model": "gemini-3.8-flash",
    "requested_mode": "auto",
    "task_type": "deep_analysis",
    "selected_provider": "gemini",
    "selected_model": "gemini-3.8-flash",
    "routing_reason": "high_priority_issue",
    "fallback_used": false,
    "fallback_reason": null,
    "paid_model_allowed": false,
    "last_sync_timestamp": "2026-09-02T14:10:00Z",
    "error_message": null
  },
  "historical_catalog": {
    "status": "available",
    "last_sync_timestamp": "2026-09-02T14:00:00Z",
    "error_message": null
  }
}
```
- `status` 取值：`"available"`（正常可用）、`"unavailable"`（未啟用/無數據）、`"disabled"`（手動關閉/未開啟）、`"error"`（查詢失敗）、`"stale"`（快取過期/使用備用快取中）、`"insufficient_data"`（資料量不足以計算）。
- 支援相容欄位：`gemini_ai`、`manual_console`。
- 可選全域欄位：`bundle.pipeline_run` 記錄管線健康狀態 (`out/pipeline_run.json`)。

---

### 3.3 總覽 KPI（Overview KPI）

#### `KPIMetric`（通用數值指標）
| 欄位名稱 | 型別 | 必填 | 說明 | 範例 |
| :--- | :--- | :--- | :--- | :--- |
| `value` | integer | 是 | 當期計數 | `12450` |
| `previous_value` | integer \| null | 是 (可為 null) | 上期計數（無基期則為 `null`） | `15600` |
| `change_pct` | float \| null | 是 (可為 null) | 變化百分比（%）（例：-20.19 代表下降 20.19%） | `-20.19` |
| `status` | string | 是 | 狀態（`"available"`, `"insufficient_data"`, `"error"`） | `"available"` |

#### `CrashFreeMetric`（Crash-free 率指標，由 Firebase Sessions 提供）
| 欄位名稱 | 型別 | 必填 | 說明 | 範例 |
| :--- | :--- | :--- | :--- | :--- |
| `rate` | float \| null | 是 (可為 null) | Crash-free 比率（**0.0 ～ 1.0**，例如 0.9985 代表 99.85%） | `0.9985` |
| `total` | integer \| null | 是 (可為 null) | 總數（總用戶數或總 Session 數） | `500000` |
| `crashed` | integer \| null | 是 (可為 null) | 崩潰數（崩潰用戶數或崩潰 Session 數） | `750` |
| `previous_rate` | float \| null | 是 (可為 null) | 上期 Crash-free 比率 | `0.9972` |
| `change_pct_points`| float \| null | 是 (可為 null) | 百分點變化（例：`+0.13` 代表提高 0.13%） | `0.13` |
| `status` | string | 是 | `"available"` \| `"unavailable"` \| `"insufficient_data"` \| `"error"` | `"available"` |
| `unavailable_reason` | string \| null | 是 (可為 null) | 若為 `unavailable` 時的原因說明 | `"Firebase Sessions export 未開啟"` |

#### `EventsByErrorType`
| 欄位名稱 | 型別 | 必填 | 說明 | 範例 |
| :--- | :--- | :--- | :--- | :--- |
| `fatal` | integer | 是 | 致命閃退（FATAL）事件數 | `8200` |
| `anr` | integer | 是 | 應用程式無回應（ANR）事件數 | `1350` |
| `non_fatal` | integer | 是 | 捕捉記錄之非致命錯誤事件數 | `2900` |

---

### 3.4 每日趨勢（Daily Trend）

`daily_trend` 為陣列，依日期由舊至新升冪排序（`date: YYYY-MM-DD`），天數長度應涵蓋 `period.days`。
```json
{
  "date": "2026-09-01",
  "crash_events": 412,
  "affected_users": 285,
  "fatal_events": 270,
  "anr_events": 42,
  "non_fatal_events": 100,
  "sessions_total": 24500,
  "crashed_sessions": 38,
  "crash_free_sessions_rate": 0.9984,
  "by_platform": {
    "ios": { "events": 180, "users": 120 },
    "android": { "events": 232, "users": 165 }
  }
}
```

---

### 3.5 版本健康度（Version Health）

`version_health` 陣列呈現各主要活躍版本的指標，按版本號降冪或活躍度排序：
| 欄位名稱 | 型別 | 必填 | 說明 | 範例 |
| :--- | :--- | :--- | :--- | :--- |
| `version` | string | 是 | 應用程式版本字串 | `"3.2.0"` |
| `platform` | string | 是 | `"ios"` \| `"android"` \| `"all"` | `"all"` |
| `release_date` | string \| null | 是 (可為 null) | 發布日期（YYYY-MM-DD） | `"2026-08-20"` |
| `crash_events` | integer | 是 | 該版本在此期間的崩潰事件數 | `1250` |
| `affected_users` | integer | 是 | 該版本在此期間受影響用戶數 | `820` |
| `crash_free_users_rate` | float \| null | 是 (可為 null) | 該版本 Crash-free 用戶率（若有 Sessions） | `0.9991` |
| `crash_free_sessions_rate`| float \| null | 是 (可為 null) | 該版本 Crash-free Sessions 率 | `0.9994` |
| `adoption_rate` | float \| null | 是 (可為 null) | 該版本佔總活躍 Session / User 佔比（0.0～1.0） | `0.65` |
| `status` | string | 是 | `"latest"` \| `"active"` \| `"maintenance"` \| `"deprecated"` | `"latest"` |
| `trend` | string | 是 | `"improving"` \| `"degrading"` \| `"stable"` \| `"new"` | `"stable"` |

---

### 3.6 維度分布（Distributions）

```json
{
  "platform": [
    { "name": "android", "events": 7200, "users": 4800, "share": 0.578 },
    { "name": "ios", "events": 5250, "users": 3500, "share": 0.422 }
  ],
  "device_models": [
    { "model": "iPhone 15 Pro", "platform": "ios", "events": 1200, "users": 800, "share": 0.096 },
    { "model": "Samsung Galaxy S24", "platform": "android", "events": 980, "users": 650, "share": 0.078 }
  ],
  "os_versions": [
    { "os_version": "iOS 18.0.1", "platform": "ios", "events": 3100, "users": 2100, "share": 0.249 },
    { "os_version": "Android 14", "platform": "android", "events": 4500, "users": 3000, "share": 0.361 }
  ],
  "app_versions": [
    { "app_version": "3.2.0", "platform": "all", "events": 6800, "users": 4300, "share": 0.546 },
    { "app_version": "3.1.2", "platform": "all", "events": 4100, "users": 2800, "share": 0.329 }
  ],
  "custom_keys": [
    { "key": "user_tier", "value": "vip", "platform": "all", "events": 3200 }
  ]
}
```

---

### 3.7 Top Issues 與 Issue Detail

#### `IssueSummary`
| 欄位名稱 | 型別 | 必填 | 說明 | 範例 |
| :--- | :--- | :--- | :--- | :--- |
| `issue_id` | string | 是 | Crashlytics Issue ID | `"8a7f1b2c"` |
| `platform` | string | 是 | `"ios"` \| `"android"` | `"android"` |
| `title` | string | 是 | 錯誤類型或崩潰主標題 | `"NullPointerException"` |
| `subtitle` | string | 是 | 發生位置描述或函式 | `"CheckoutActivity.kt:142"` |
| `error_type` | string | 是 | `"FATAL"` \| `"ANR"` \| `"NON_FATAL"` | `"FATAL"` |
| `priority` | PriorityInfo | 是 | 程式計算之優先級分數與等級 | 見下方 `PriorityInfo` |
| `events` | integer | 是 | 該 Issue 在期間內之事件總數 | `3420` |
| `affected_users` | integer | 是 | 該 Issue 在期間內受影響之用戶數 | `2100` |
| `first_seen_timestamp` | string (ISO 8601 UTC) | 是 | 該 Issue 首次出現時間戳（結尾 `Z`） | `"2026-08-12T09:15:00Z"` |
| `last_seen_timestamp` | string (ISO 8601 UTC) | 是 | 該 Issue 最近出現時間戳（結尾 `Z`） | `"2026-09-02T13:40:00Z"` |
| `first_seen_version` | string | 是 | 該 Issue 最早出現之版本 | `"3.1.0"` |
| `last_seen_version` | string | 是 | 該 Issue 最新出現之版本 | `"3.2.0"` |
| `version_distribution` | array[object] | 是 | 依版本細分之 events/users 列表 | `[{"version":"3.2.0","events":2800,"users":1700}]` |
| `blame_frame` | BlameFrame \| null | 是 (可為 null) | 元兇 Stack Frame 資訊 | 見下方 `BlameFrame` |
| `ai_analysis` | AIIssueAnalysis | 是 | AI 針對該 Issue 之分析摘要 | 見下方 `AIIssueAnalysis` |
| `detail` | IssueDetail \| null | 是 (可為 null) | 深度診斷資料（若有抓取） | 見下方 `IssueDetail` |
| `lifecycle` | IssueLifecycle \| null | 否 (V2.3+ 規範) | 跨版本生命週期狀態 | 見下方 `IssueLifecycle` |

#### `PriorityInfo`
```json
{
  "score": 88,
  "level": "P0",
  "trend": "worsening",
  "score_breakdown": {
    "users_normalized": 10.0,
    "events_normalized": 8.5,
    "fatal_anr_boost": 2,
    "worsening_boost": 2,
    "latest_version_boost": 2,
    "core_path_boost": 3
  }
}
```

#### `BlameFrame`
```json
{
  "file": "app/src/main/java/com/example/CheckoutActivity.kt",
  "line": 142,
  "symbol": "com.example.CheckoutActivity.processPayment",
  "class_name": "com.example.CheckoutActivity",
  "method_name": "processPayment",
  "is_blame": true,
  "source_available": true
}
```

#### `AIIssueAnalysis`
```json
{
  "status": "available",
  "root_cause": "結帳流程中 userProfile 在特定網路延遲條件下尚未初始化即被呼叫導致 NPE。",
  "suggested_fix": "在呼叫 processPayment 前加入 profile 空值防護並補齊 fallback 提示。",
  "effort": "S",
  "confidence": "high",
  "reasoning_sources": ["stack_trace", "blame_frame", "version_concentration"]
}
```

#### `IssueDetail`
```json
{
  "stack_trace": "java.lang.NullPointerException: Attempt to invoke virtual method on a null object reference\n\tat com.example.CheckoutActivity.processPayment(CheckoutActivity.kt:142)\n\tat ...",
  "breadcrumbs": [
    {
      "timestamp": "2026-09-02T13:39:58Z",
      "category": "navigation",
      "message": "User entered /checkout",
      "level": "info"
    }
  ],
  "logs": [
    { "timestamp": "2026-09-02T13:39:59Z", "message": "[PaymentService] Initiating payment request" }
  ],
  "custom_keys": {
    "user_tier": "gold",
    "cart_items_count": 3
  },
  "top_devices": [
    { "model": "Pixel 8", "events": 1400 }
  ],
  "top_os": [
    { "os_version": "Android 14", "events": 2300 }
  ]
}
```

#### `IssueLifecycle`
跨版本生命週期追蹤資訊（V2.3+ 支援）：
```json
{
  "status": "regressed",
  "latest_version": "3.2.0",
  "first_seen_version": "3.0.0",
  "last_seen_version": "3.2.0",
  "versions_seen": 2,
  "confidence": "high",
  "previously_absent_since": "3.1.0",
  "reappeared_version": "3.2.0",
  "reason": "Absent in 3.1.0 (sample sufficient) and reappeared in 3.2.0"
}
```
| 欄位名稱 | 型別 | 必填 | 說明 | 範例 |
| :--- | :--- | :--- | :--- | :--- |
| `status` | string | 是 | `"new_in_latest"` \| `"persistent"` \| `"regressed"` \| `"resolved"` \| `"not_observed_latest"` | `"regressed"` |
| `latest_version` | string | 是 | 當前最新發布版本 | `"3.2.0"` |
| `first_seen_version` | string | 是 | 該 Issue 最早出現之版本 | `"3.0.0"` |
| `last_seen_version` | string | 是 | 該 Issue 最新出現之版本 | `"3.2.0"` |
| `versions_seen` | integer | 是 | 該 Issue 在歷史上活躍之版本總數（非負整數） | `2` |
| `confidence` | string | 是 | 判定信賴等級：`"high"` \| `"medium"` \| `"low"` | `"high"` |
| `previously_absent_since`| string \| null | 否 | 若為 `regressed`，前次消失之版本 | `"3.1.0"` |
| `reappeared_version` | string \| null | 否 | 若為 `regressed`，再次復發之版本 | `"3.2.0"` |
| `reason` | string \| null | 否 | 生命週期狀態判定之依據說明 | `"Absent in 3.1.0..."` |

---

### 3.8 AI 策略摘要（AI Summary）

```json
{
  "status": "available",
  "provider": "gemini",
  "model": "gemini-3.8-flash",
  "generated_at": "2026-09-02T14:10:00Z",
  "overview": "本期整體閃退事件較上月下降 20.2%，然而 3.2.0 新版在結帳流程中引入 1 個高頻 P0 NPE 崩潰，影響約 2,100 位用戶。",
  "key_takeaways": [
    "P0: CheckoutActivity.kt:142 佔本期總閃退量 27.5%，建議立即發布 3.2.1 Hotfix",
    "Android 14 上的 ANR 集中在啟動階段 Database init，需評估非同步載入",
    "舊版本（< 3.1.0）崩潰持續收斂，整體 Crash-free rate 提升至 99.85%"
  ],
  "distribution_insights": "崩潰高度集中於 Android 平台（佔 58%）及 3.2.0 版本。iOS 平台表現穩定。",
  "recommended_actions": [
    { "priority": "P0", "issue_id": "8a7f1b2c", "action": "修復 CheckoutActivity 空值指標保護", "effort": "S" }
  ],
  "data_limitations": "Firebase Sessions 僅收集最近 30 天數據；部分 MCP stack trace 缺去符號化資訊。"
}
```

---

### 3.9 持續版本目錄契約（Release Catalog Specification）

Release Catalog（`release_catalog`）是 Dashboard V2.6 核心功能，將版本狀態由單期視窗提升為獨立發布實體。

```mermaid
classDiagram
    class ReleaseCatalogItem {
        +string version
        +string platform
        +string first_seen
        +string last_seen
        +string release_date
        +string status
        +int lifetime_crashes
        +int lifetime_issues
        +int lifetime_affected_users
        +int lifetime_fatal
        +int lifetime_anr
        +Map~string, ReleaseRecentHealth~ recent_health
        +string stability_status
        +ReleaseIssueLifecycle issue_lifecycle
        +PreviousReleaseComparison vs_previous
    }
    class ReleaseRecentHealth {
        +int crash_events
        +int affected_users
        +int sessions_total
        +float crash_free_users_rate
        +float crash_free_sessions_rate
        +float adoption_rate
        +int fatal_events
        +int anr_events
        +int new_issues_count
        +bool sample_sufficient
        +string status
        +string trend
    }
    class PreviousReleaseComparison {
        +string previous_version
        +float crash_rate_change_pct
        +float crash_free_users_diff
        +float fatal_change_pct
        +float anr_change_pct
        +int new_issues_diff
        +string stability
    }
    class ReleaseIssueLifecycle {
        +int introduced_count
        +int persistent_count
        +int regressed_count
        +int resolved_count
        +List~string~ introduced
        +List~string~ persistent
        +List~string~ regressed
        +List~string~ resolved
    }
    ReleaseCatalogItem *-- ReleaseRecentHealth
    ReleaseCatalogItem *-- PreviousReleaseComparison
    ReleaseCatalogItem *-- ReleaseIssueLifecycle
```

#### `ReleaseCatalogItem` 欄位定義
| 欄位名稱 | 型別 | 必填 | 說明 | 範例 |
| :--- | :--- | :--- | :--- | :--- |
| `version` | string | 是 | 應用程式版本識別碼（非空字串） | `"3.2.0"` |
| `platform` | string | 是 | `"ios"` \| `"android"`（平台嚴格隔離） | `"android"` |
| `first_seen` | string (ISO 8601 UTC) \| null | 是 (可為 null) | 該版本最早崩潰事件時間（結尾必須為 `Z`） | `"2026-08-01T10:00:00Z"` |
| `last_seen` | string (ISO 8601 UTC) \| null | 是 (可為 null) | 該版本最新崩潰事件時間（結尾必須為 `Z`） | `"2026-09-02T13:45:00Z"` |
| `release_date` | string (YYYY-MM-DD) \| null | 是 (可為 null) | **權威發布日期**（若無設定則為 null，**嚴禁 fallback 至 first_seen**） | `"2026-08-01"` |
| `status` | string | 是 | `"latest"` \| `"active"` \| `"legacy"` | `"latest"` |
| `lifetime_crashes` | integer | 是 | 該版本生命週期累積崩潰總數（非負整數） | `4520` |
| `lifetime_issues` | integer | 是 | 該版本生命週期累積關聯 Issue 總數 | `28` |
| `lifetime_affected_users`| integer | 是 | **精確去重受影響用戶總數**（由 SQLite Authority Store 精確計算提供） | `1850` |
| `lifetime_fatal` | integer | 是 | 該版本生命週期累積 FATAL 事件數 | `3100` |
| `lifetime_anr` | integer | 是 | 該版本生命週期累積 ANR 事件數 | `420` |
| `recent_health` | object | 是 | 依視窗聚合之近期健康度字典（見下方） | 見 `ReleaseRecentHealth` |
| `stability_status`| string \| null | 否 | 穩定度文字標籤 | `"degrading"` |
| `issue_lifecycle` | ReleaseIssueLifecycle | 否 | 該版本 Issue 生命週期分類計數與清單 | 見 `ReleaseIssueLifecycle` |
| `vs_previous` | PreviousReleaseComparison \| null | 否 | 與前一相鄰版本之指標對比 | 見 `PreviousReleaseComparison` |

#### `ReleaseRecentHealth`
`recent_health` 字典支援以天數 `"7"`, `"30"`, `"90"`（或 `"7d"`, `"30d"`, `"90d"`）為鍵值：
```json
{
  "30": {
    "crash_events": 1250,
    "affected_users": 820,
    "sessions_total": 450000,
    "crash_free_users_rate": 0.9982,
    "crash_free_sessions_rate": 0.9989,
    "adoption_rate": 0.65,
    "fatal_events": 850,
    "anr_events": 120,
    "new_issues_count": 3,
    "sample_sufficient": true,
    "status": "latest",
    "trend": "stable"
  }
}
```
- `sample_sufficient`：樣本充足度判定布林值（通常門檻為 `sessions_total >= 1000` 或 `affected_users >= 10`），樣本不足時避免誤判 degradation。

#### `PreviousReleaseComparison` (`vs_previous`)
以相鄰前一版本為基準進行之標準化對比：
```json
{
  "previous_version": "3.1.2",
  "crash_rate_change_pct": 0.125,
  "crash_free_users_diff": -0.0015,
  "fatal_change_pct": 0.082,
  "anr_change_pct": -0.041,
  "new_issues_diff": 1,
  "stability": "degrading"
}
```
- `crash_rate_change_pct`：**歸一化暴險變化率**，公式為 `(Rate_curr - Rate_prev) / Rate_prev`，其中 `Rate = crash_events / sessions_total`。
- `stability` 取值：`"improving"`、`"stable"`、`"degrading"`、`"baseline"`（無前一版本時為 baseline）。

#### `ReleaseIssueLifecycle`
```json
{
  "introduced_count": 3,
  "persistent_count": 12,
  "regressed_count": 1,
  "resolved_count": 5,
  "introduced": ["8a7f1b2c", "9b8e2c3d", "0c1d2e3f"],
  "persistent": ["..."],
  "regressed": ["..."],
  "resolved": ["..."]
}
```

---

## 4. 契約二：Historical Catalog JSON 規格（`historical_catalog.json`）

`historical_catalog.json` 是管線長期狀態持久化實體，負責記錄跨視窗版本指標、生命週期分類與水線資訊。

### 4.1 結構與欄位定義

#### `HistoricalCatalogData`
```json
{
  "schema_version": "2.3.0",
  "updated_at": "2026-09-02T14:00:00Z",
  "watermark": "2026-09-02T13:45:00Z",
  "app_id": "shop_app",
  "authority": {
    "backend": "sqlite",
    "state_version": 1,
    "bootstrap_complete": true
  },
  "authority_state_version": 1,
  "bootstrap_complete": true,
  "issues": {
    "android:8a7f1b2c": {
      "issue_id": "8a7f1b2c",
      "platform": "android",
      "title": "NullPointerException",
      "subtitle": "CheckoutActivity.kt:142",
      "error_type": "FATAL",
      "first_seen_version": "3.1.0",
      "last_seen_version": "3.2.0",
      "first_seen_timestamp": "2026-08-12T09:15:00Z",
      "last_seen_timestamp": "2026-09-02T13:40:00Z",
      "versions_seen": ["3.1.0", "3.2.0"],
      "last_updated": "2026-09-02T14:00:00Z"
    }
  },
  "app_versions": {
    "android": {
      "3.2.0": {
        "version": "3.2.0",
        "platform": "android",
        "status": "latest",
        "adoption_rate": 0.65,
        "sessions_total": 450000,
        "crash_events": 4520,
        "sample_sufficient": true,
        "last_updated": "2026-09-02T14:00:00Z",
        "first_seen": "2026-08-01T10:00:00Z",
        "last_seen": "2026-09-02T13:45:00Z",
        "release_date": "2026-08-01",
        "lifetime_crashes": 4520,
        "lifetime_issues": 28,
        "lifetime_affected_users": 1850,
        "lifetime_fatal": 3100,
        "lifetime_anr": 420,
        "recent_health": { ... }
      }
    },
    "ios": { ... }
  }
}
```

### 4.2 絕對禁止事項：Zero Raw IDs Persistence
> [!CAUTION]
> **嚴禁在 `historical_catalog.json` 寫入 `installation_ids` 或 `user_ids` 欄位**。
> - 歷史版本的 catalog 中若有 legacy `installation_ids`，在 `IssueHistoricalCatalog.load()` 時會自動遷移入 SQLite Authority Store，並在記憶體中立即執行 `pop("installation_ids", None)` 清除。
> - `save()` 寫回時只允許序列化 `lifetime_affected_users` 去重數值與 `authority` metadata。

---

## 5. 契約三：SQLite Authority Store 規格（`catalog_authority.sqlite3`）

### 5.1 儲存配置與連線規範

- **檔案命名與路徑**：預設位於 `out/catalog_authority.sqlite3` 或 `out/<app>/catalog_authority.sqlite3`。
- **PRAGMA 規範**：
  ```sql
  PRAGMA journal_mode = WAL;
  PRAGMA synchronous = NORMAL;
  PRAGMA busy_timeout = 5000;
  ```
  WAL 模式確保多執行緒讀取不阻塞寫入，`busy_timeout = 5000` 保障鎖競爭時的優雅等待。

### 5.2 資料表結構（DDL）

```sql
-- 1. 裝置加鹽雜湊權威表：儲存每個版本觀察到的受影響裝置
CREATE TABLE IF NOT EXISTS release_installations (
    app_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    app_version TEXT NOT NULL,
    installation_hash TEXT NOT NULL,
    first_seen TEXT,
    last_seen TEXT,
    PRIMARY KEY (app_id, platform, app_version, installation_hash)
);

CREATE INDEX IF NOT EXISTS idx_release_installations_version
ON release_installations(app_id, platform, app_version);

-- 2. 版本權威狀態表：紀錄各版本的 Bootstrap 完成度與更新時間
CREATE TABLE IF NOT EXISTS version_authority_status (
    app_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    app_version TEXT NOT NULL,
    bootstrap_complete INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT,
    PRIMARY KEY (app_id, platform, app_version)
);

-- 3. 權威全域 Metadata 表：儲存 state_version 與全域 bootstrap 標記
CREATE TABLE IF NOT EXISTS authority_metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

### 5.3 確定性加鹽雜湊算法（Privacy Guard）

為徹底隔絕使用者個資（PII），所有 `installation_uuid` 在進入 SQLite 前必須經過確定性加鹽雜湊運算：
$$\text{installation\_hash} = \text{SHA256}\Big(\text{utf8}\big(\text{app\_id} + \text{":"} + \text{raw\_uuid}\big)\Big)$$
- **確定性（Deterministic）**：同一 App 內的相同 raw UUID 在任何時刻運算結果完全相同，保障精確去重能力。
- **跨 App 隔離（App Salted）**：以 `app_id` 為 Salt，防止跨專案 Rainbow Table 碰撞。
- **不可逆（Non-reversible）**：無法由 hash 還原真實設備 UUID。

### 5.4 查詢與計數語意

1. **精確去重用戶計數**：
   ```sql
   SELECT COUNT(*) FROM release_installations
   WHERE app_id = ? AND platform = ? AND app_version = ?;
   ```
2. **版本權威完整性檢查**：
   ```sql
   SELECT bootstrap_complete FROM version_authority_status
   WHERE app_id = ? AND platform = ? AND app_version = ?;
   ```
   若 `bootstrap_complete == 1`，該版本的 `lifetime_affected_users` 以 SQLite 精確計數為準；若 `bootstrap_complete == 0`，則以 `max(existing_aggregate, exact_count)` 保底。

---

## 6. 資料管線三大生命週期行為

```mermaid
stateDiagram-v2
    [*] --> DetectMode

    state DetectMode <<choice>>
    DetectMode --> Migration : Legacy JSON 偵測到 installation_ids
    DetectMode --> Bootstrap : authority.bootstrap_complete == False
    DetectMode --> Incremental : authority.bootstrap_complete == True 且具備 watermark

    state Migration {
        LoadLegacy: 讀取舊版 JSON
        ImportSQLite: 寫入 SQLite Authority
        PurgeIDs: 記憶體與 JSON 永久刪除 raw IDs
        CheckSufficiency: 比對 exact_count 與 existing_aggregate
        LoadLegacy --> ImportSQLite --> CheckSufficiency --> PurgeIDs
    }

    state Bootstrap {
        FullScan: BigQuery version_catalog_bootstrap 全量掃描
        BatchInsert: 批次寫入 SQLite release_installations
        HandleZeroUser: 顯式標記 0 用戶歷史版本 bootstrap_complete=1
        MarkGlobalComplete: 設定 authority_metadata bootstrap_complete=1
        FullScan --> BatchInsert --> HandleZeroUser --> MarkGlobalComplete
    }

    state Incremental {
        ReadWatermark: 讀取 catalog.watermark
        QueryDelta: BigQuery 查詢 event_timestamp > watermark
        IdempotentInsert: INSERT OR IGNORE 寫入 SQLite
        UpdateMetrics: 更新 last_seen 與 lifetime_affected_users
        AdvanceWatermark: 更新 watermark 為最新事件時間
        ReadWatermark --> QueryDelta --> IdempotentInsert --> UpdateMetrics --> AdvanceWatermark
    }

    Migration --> Bootstrap : incomplete authority (IDs < aggregate)
    Migration --> Incremental : complete authority
    Bootstrap --> Incremental : 下次排程
    Incremental --> [*]
```

### 6.1 Migration（舊版相容升級）
- **情境**：載入帶有 legacy `installation_ids` 的舊版 `historical_catalog.json`。
- **行為**：
  1. `IssueHistoricalCatalog.load()` 解析出待遷移清單 `(platform, version, ids)`。
  2. 呼叫 `authority_store.add_installations()` 匯入 SQLite。
  3. 比較匯入筆數與既有 `lifetime_affected_users`：
     - 若 `exact_count >= existing_users`：標記該版本 `bootstrap_complete = 1`。
     - 若 `exact_count < existing_users`（代表舊 JSON 曾發生截斷或不完整）：標記 `bootstrap_complete = 0`，以 `existing_users` 保底，全域 `bootstrap_complete = False`。
  4. 從記憶體中 `pop("installation_ids")` 與 `pop("user_ids")`。
  5. `save()` 寫回乾淨無 raw IDs 的 JSON。

### 6.2 Bootstrap（全量初始化）
- **情境**：全新專案部署，或 `authority.bootstrap_complete == False`，或特定活躍版本缺乏權威標記。
- **行為**：
  1. 執行 BigQuery 全量版本查詢模板（`version_catalog_bootstrap`），無觀測視窗限制（或回溯全量歷史）。
  2. 收集歷史所有版本的 `installation_uuid` 進行批次加鹽雜湊寫入。
  3. **Zero-user Bootstrap 處理**：對歷史上存在但無任何崩潰事件（0 崩潰、0 用戶）之版本，顯式呼叫 `mark_version_bootstrapped(..., complete=True)` 標記完整，避免日後 incremental 誤判缺失權威而反覆觸發全量掃描。
  4. 全量寫入完成後，於 `authority_metadata` 寫入 `bootstrap_complete = 1`。

### 6.3 Incremental（增量同步）
- **情境**：管線日常排程執行（如每週定期執行）。
- **行為**：
  1. 讀取 `historical_catalog.json` 之 `watermark`（例如 `"2026-09-01T12:00:00Z"`）。
  2. BigQuery 增量查詢附加 `event_timestamp > watermark` 條件，僅抓取最新產生之崩潰事件與裝置。
  3. 透過 SQLite `INSERT OR IGNORE` 進行冪等追加，天然去重已存在的裝置雜湊。
  4. 呼叫 `authority_store.count_installations()` 更新最新 `lifetime_affected_users`。
  5. 推進 `watermark` 為當前最新事件之 ISO 時間戳，儲存更新後的 catalog。

---

## 7. 空值、缺漏與停用欄位語意指引（Semantics Guide）

| 狀態名稱 | 指標數值表現 | UI 呈現規範 | 適用情境 |
| :--- | :--- | :--- | :--- |
| **`available`** | 具有合法數值（如 `0.9985`、`1250`） | 正常渲染數值、對比變化與趨勢圖 | 數據管道正常且數據充足 |
| **`unavailable`** | 數值為 `null`，附帶 `unavailable_reason` | 顯示 `"Unavailable"` 或 `"—"`（附 Tooltip 說明原因），**嚴格禁止顯示 `0` 或 `0%`** | Firebase Sessions 未開啟、該版本無 Sessions |
| **`disabled`** | 數值為 `null` | 隱藏該模組或呈現停用設定卡片 | 未設定 AI Key、使用者主動關閉該資料源 |
| **`insufficient_data`** | 數值為 `null` 或預設值 | 顯示 `"資料收集未滿"` 或 `"無基準"`，不作 degradation 警告 | 統計時間過短、版本初發布樣本作收斂中 |
| **`stale`** | 具有合法數值 | 顯示警告徽章，標註快取時間 | 使用歷史快取備援資料（MCP 離線或查詢逾時） |
| **`error`** | 數值為 `null`，附帶 `error_message` | 顯示錯誤警告標籤，不阻斷整體儀表板其他正常區塊 | 單一查詢逾時、權限不足或網路中斷 |

---

## 8. 驗證規範與相容性（Validation & Compliance）

`crash_trend/schema_v2.py` 提供全套執行階段驗證工具，可在 CI 與管線結尾執行強型別合規檢查：

```python
from crash_trend.schema_v2 import (
    validate_dashboard_v2,
    validate_historical_catalog,
    validate_release_catalog,
    validate_issue_summary,
    validate_issue_lifecycle,
)

# 1. 驗證完整前端 Dashboard Bundle
errors = validate_dashboard_v2(dashboard_bundle_dict)
assert len(errors) == 0, f"Dashboard schema errors: {errors}"

# 2. 驗證歷史目錄契約（保證零 raw IDs）
cat_errors = validate_historical_catalog(historical_catalog_dict)
assert len(cat_errors) == 0, f"Catalog schema errors: {cat_errors}"
```

- **相容性保證**：
  - `schema_v2.py` 之 `SUPPORTED_SCHEMA_VERSIONS` 同時相容 `{"2.0", "2.3", "2.3.0", "2.6", "2.6.0"}`。
  - 舊版消費端若僅需要單期快照，直接讀取 `AppDashboardV2Data` 之 `kpi`、`top_issues` 依然完全相容；若需要長週期版本演進分析，可消費新增之 `release_catalog` 欄位。
