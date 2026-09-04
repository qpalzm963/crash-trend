# crash-trend

**Firebase Crashlytics 趨勢分析與 Dashboard V2.5** — 將 Crashlytics BigQuery、Firebase Sessions 與可選的 Crashlytics MCP 資料整合成可排序的修復優先級、AI 分析、Pipeline Health、週期報表與自包含式 Dashboard。

![dashboard](docs/screenshot.png)

## 為什麼需要 crash-trend

Firebase Crashlytics 很適合查看單一 crash，但要回答下面這些問題，通常仍需要人工整理：

- 哪些 crash / ANR 最值得先修？
- 最新版本是否正在惡化？
- Crash-free Users / Sessions 是否下降？
- 問題集中在哪些版本、裝置或使用者族群？
- 哪些 issue 需要進一步做 Root Cause 分析？
- 本週資料源、AI 與同步流程是否正常？

crash-trend 將這些工作整理成一條可重複執行的資料管線：

```text
Crashlytics BigQuery ──┐
Firebase Sessions ─────┼─→ fetch / enrich ─→ normalize ─→ deterministic priority (P0~P3)
Crashlytics MCP ───────┘                                  │
                                                         ├─→ AI Task Router
                                                         │    ├─ lightweight triage
                                                         │    └─ deep analysis
                                                         │
                                                         ├─→ dashboard.html
                                                         ├─→ pipeline health
                                                         ├─→ surge detection
                                                         └─→ monthly chat report
```

---

## Dashboard V2.5 核心能力

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
  build_dashboard.py     # 自包含 Dashboard HTML 產生器
  check_surge.py         # Crash 趨勢暴增偵測
  config.py              # apps.yaml 載入與資料源設定
  fetch_bigquery.py      # Crashlytics BigQuery 資料抓取
  fetch_issue_details.py # Issue detail / stack trace enrichment
  fetch_sessions.py      # Firebase Sessions / Crash-free metrics
  fetch_stacktraces.py   # Firebase MCP stacktrace cache
  lifecycle.py           # Issue lifecycle / 狀態處理
  normalize.py           # Canonical normalization / 歷史資料整理
  pipeline_health.py     # Run Summary、Stage status、錯誤資訊消毒
  pipeline_run.py        # 端到端 Pipeline Orchestrator
  pm_brief.py            # PM-friendly issue 摘要
  post_report.py         # 每月聊天摘要卡
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
requirements.txt
```

---

## 主要輸出

執行後的 runtime artifacts 預設不進 Git：

```text
dashboard.html
out/
  pipeline_run.json
  dashboard_v2.json
  <app>/...
reports/
logs/
```

`.env`、`apps.yaml`、Service Account JSON、Admin token 與 crash 原始輸出也已透過 `.gitignore` / Docker mount 策略避免直接提交到 repository。

---

## 測試

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

GitHub Actions 會在 `main`、`feature/**` push 與對 `main` 的 Pull Request 上執行測試，matrix 為 Python 3.11 / 3.12。

---

## 文件

- [`DEPLOY.md`](DEPLOY.md)：Docker / 排程 / 部署方式
- [`docs/dashboard_v2_schema.md`](docs/dashboard_v2_schema.md)：Dashboard V2 資料契約
- [`apps.example.yaml`](apps.example.yaml)：完整多 App / AI / Data Source 設定範例

## License

MIT — 詳見 [`LICENSE`](LICENSE)。
