# crash-trend

**Firebase Crashlytics 趨勢分析與 SaaS 儀表板引擎 (Dashboard V2)** — 匯出 BigQuery 崩潰資料 ＋ Firebase Sessions 統計 ＋ MCP 堆疊追蹤 → AI 找 top pattern 與策略摘要 → 產出優先修復清單、月報，與自包含現代 SaaS 儀表板。設定驅動、多 App 共用一套腳本，換公司／換專案 10 分鐘上線。

![dashboard](docs/screenshot.png)

## 它解決什麼問題

Crashlytics console 看得到個別 crash，但「哪些 crash 最該先修？」「整體 Crash-free 率與各版本健康度如何？」「集中在哪些機型/版本/用戶族群？」需要人工整理。crash-trend 把這件事變成一條自動化的現代化管線：

```
BigQuery Crashlytics ──┐
Firebase Sessions ─────┼→ fetch & enrich ─→ analyze_gemini (確定性評分 P0~P3 + AI 策略) ─→ dashboard.html
Crashlytics MCP / BQ ──┘                     └→ weekly_sync / surge alert / chat 卡片
```

### Dashboard V2 核心特色

- **現代 SaaS Analytics 風格**：淺色白底卡片設計、8 大功能分頁（總覽、問題列表、版本健康度、裝置分析、發佈版本、通知管線、AI 分析、系統設定）、響應式側邊欄與深淺色主題切換。
- **Crash-free 率與去重指標**：接入 Firebase Sessions 計算 Crash-free Users 與 Crash-free Sessions；期間去重 Affected Users；Sessions 未開啟時具備明確的 `Unavailable` 狀態，**絕不顯示假 0%**。
- **確定性 Priority Score ＋ Gemini AI 分析**：
  - 程式確定性評分：`P0` / `P1` / `P2` / `P3`（依受影響用戶、Fatal/ANR、惡化趨勢、最新版本影響、核心路徑權重加總）。
  - Gemini AI 策略摘要：首頁 AI Overview、Key Takeaways、推薦行動，以及各問題的 Root Cause 推測與具體修復建議。
- **元兇定位 (Blame Frame)**：解析關鍵元兇程式碼位置（檔案、行號、方法符號）與完整 Stack Trace，一鍵複製修復 Prompt 貼給 Coding Agent。
- **自包含與離線可用**：單一 HTML 檔、Chart.js 內嵌、零外部 CDN 依賴，`file://` 本地直接開啟。
- **多 App 無縫切換**：頂部 Header 支援多 App 下拉切換，URL 帶 `#<app>` 直達指定分頁。

---

## 快速開始

```bash
git clone https://github.com/qpalzm963/crash-trend && cd crash-trend
cp apps.example.yaml apps.yaml                 # 填寫你的 App 設定（Firebase 專案 ID、package 等）
gcloud auth login                              # 具備 GCP/Firebase 專案 IAM 權限的帳號
scripts/create_sa.sh <firebase_project_id>     # 一鍵建立唯讀 SA ＋ 金鑰
# Firebase Console → 專案設定 → Integrations → BigQuery → Link 勾 Crashlytics 與 Sessions
printf 'GEMINI_API_KEY=...\n' > .env           # AI 分析用（Google AI Studio 取得）
docker compose up -d --build                   # 每週三 10:00（Asia/Taipei）自動同步
# 手動試跑整條管線驗證：
docker compose run --rm crash-trend /bin/bash /app/scripts/weekly_sync.sh
open dashboard.html
```

不用 Docker 的話：
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./scripts/weekly_sync.sh
open dashboard.html
```

---

## 設定檔架構

所有環境差異收斂在三處，核心程式碼零改動：

| 檔案 | 內容 | 版控 |
|---|---|---|
| `apps.yaml` | 各 App 的 Firebase 專案、BQ dataset、Sessions dataset、core_paths、custom_keys | 建議放私有 instance repo |
| `.env` | `GEMINI_API_KEY`、`GEMINI_MODEL`（預設 gemini-flash-latest） | ✗ 永不進版控 |
| `~/.config/crash-trend/sa.json` | BigQuery 唯讀 SA 金鑰（`create_sa.sh` 產生） | ✗ 永不進版控（Docker read-only 掛載） |

### 多 App 設定與 Data Source Profile (`apps.yaml`)

系統正式支援 **Crashlytics-only** 與 **Full Sessions** 雙模式。若未開啟 Sessions 匯出，管線完全不打無效 API、不報 404，儀表板以 `未開啟` 清楚標註，其餘 Crash 除錯與 AI 優先級功能 100% 正常。

```yaml
credentials:
  bq_service_account: ~/.config/crash-trend/sa.json

apps:
  # 模式 A：Crashlytics-only 模式（推薦：輕量、零額外 Sessions 儲存成本）
  clock_in_app:
    display_name: MP打卡系統
    firebase_project: mp-clockin-44dee
    data_sources:
      crashlytics_bigquery: true          # 啟用 Crashlytics 匯出
      sessions: false                     # 停用 Sessions 查詢（不需 firebase_sessions 表）
      mcp: optional                       # optional 備援
    bq_dataset: firebase_crashlytics
    package_name: com.mp.clockinapp
    bundle_id: com.mp.clockin
    source_repo: ~/develop/clock_in_app
    platforms: [android, ios]
    core_paths: [auth_repository, punch, clock_in]

  # 模式 B：完整模式 (含 Firebase Sessions 計算 Crash-free %)
  shop_app:
    display_name: 購物商城 App
    firebase_project: shop-prod-12345
    data_sources:
      crashlytics_bigquery: true
      sessions: true                      # 啟用 Sessions 查詢計算 Crash-free 率
    bq_dataset: firebase_crashlytics
    sessions_dataset: firebase_sessions
    package_name: com.example.shop
    source_repo: ~/develop/shop_app
    platforms: [android, ios]
    core_paths: [checkout, payment, CartActivity]
    custom_keys: [user_tier, network_type]
```

---

## 專案結構

```
crash_trend/
  schema_v2.py           # Dashboard V2 資料契約 (TypedDicts) 與嚴格驗證器
  fetch_bigquery.py      # BigQuery V2：Overview 聚合、日曆日每日趨勢、維度分布
  fetch_sessions.py      # Firebase Sessions：Crash-free Users / Sessions 指標與版本健康度
  fetch_issue_details.py # Issue Detail：Stack trace、Blame frame、Breadcrumbs、Logs (含 MCP fallback)
  fetch_stacktraces.py   # Firebase MCP 驅動客戶端 (headless stdio JSON-RPC)
  analyze_gemini.py      # 確定性 Priority Score ＋ Gemini AI 策略摘要
  build_dashboard.py     # 多 App 聚合與自包含 SaaS Dashboard HTML 產生器
  check_surge.py         # 每日趨勢週暴增偵測告警
  pm_brief.py            # 優先 issue → 給 PM 的白話簡報
  post_report.py         # 月度摘要卡發送（聊天整合，可選）
  config.py              # apps.yaml 讀取與路徑工具
scripts/
  weekly_sync.sh         # 週同步主要進入點（Docker/launchd/手動皆呼叫此腳本）
  create_sa.sh           # 一鍵建 BigQuery 唯讀 SA
```

---

## 容錯與優雅降級 (Graceful Degradation)

- **無 Firebase Sessions**：自動將 Sessions 來源標記為 `unavailable`，Crash-free 卡片顯示明確的 "Unavailable" 字樣，**絕不顯示 0%**。
- **無 GEMINI_API_KEY**：AI 分析標為 `disabled`，Priority Score 依然以確定性權重公式精確計算。
- **MCP 未登入**：自動以 BigQuery sample events 或 subtitle 啟發式解析 Blame Frame，流程不中斷。
- **空資料期間**：無 Crash 事件時安全輸出 0 與空列表，不發生除以零或 Null 錯誤。

---

## License

MIT
