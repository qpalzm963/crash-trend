# 部署到伺服器（Dashboard V2 自動化排程）

目標：週同步全自動（BigQuery V2 → Firebase Sessions → Issue Detail → Gemini AI → Dashboard V2 儀表板 → git commit），本機不用開著。

## 前置準備（一次性）

1. **BigQuery 唯讀 SA**：
   執行 `scripts/create_sa.sh <firebase_project>` 建立服務帳號並下載金鑰（若有多個 Firebase 專案，可對其餘專案執行 `scripts/create_sa.sh <專案> --grant-only <sa_email>`）。
2. **Gemini API key**：至 Google AI Studio 產生。
3. **推到私有 Git**：將你的 instance repo（含 `apps.yaml`）推到私有 Git。
4. **（可選）MCP 補強與真實 stack trace 登入**：
   預設 `mcp.mode: manual`（伺服器排程預設不自動呼叫 MCP，零 quota 消耗）。若希望 weekly 自動刷新，可在 `apps.yaml` 設為 `mcp.mode: weekly` 並在主機上執行一次 `firebase login`（user token，**非** service account——SA 打 Crashlytics 會 404），token 存於 `~/.config/configstore/firebase-tools.json`，由 compose 掛入容器。若未登入或 MCP 失敗，管線會自動以 BigQuery 頂層 sample events 或 subtitle 啟發式解析 Blame Frame，完全不中斷管線。若要完全關閉 MCP 亦可設定 `mcp.mode: off`。

---

## 安裝方式（Docker，建議）

```bash
git clone <你的 instance repo> && cd crash-trend
mkdir -p ~/.config/crash-trend && cp <SA json> ~/.config/crash-trend/sa.json && chmod 600 ~/.config/crash-trend/sa.json
printf 'GEMINI_API_KEY=...\n' > .env
docker compose up -d --build                 # supercronic 每週三 10:00（TZ=Asia/Taipei）自動同步
# 手動試跑整條管線驗證：
docker compose run --rm crash-trend /bin/bash /app/scripts/weekly_sync.sh
tail -30 logs/weekly_sync.log
```

- **修改排程**：編輯 `docker/crontab` 後執行 `docker compose restart`。
- **憑證安全**：憑證以 read-only bind mount 掛入容器（`docker-compose.yml`），**不進 image、不進 git**。
- **靜態儀表板 Web 伺服器**：`docker-compose.yml` 內建 nginx 容器，可於 `http://<主機>:8787` 檢視最新產出之 `dashboard.html`。

---

## 備選：launchd（macOS 主機直跑）

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp scripts/com.crash-trend.weekly-sync.plist.example ~/Library/LaunchAgents/com.crash-trend.weekly-sync.plist
# 編輯其中兩處 /PATH/TO 後載入排程：
launchctl load ~/Library/LaunchAgents/com.crash-trend.weekly-sync.plist
```

---

## 與聊天系統整合（可選）

在 `.env` 設定以下變數即啟用（未設定則自動跳過）：

```env
CRASH_REPORT_URL=http://host.docker.internal:3000/api/crash-report
INTERNAL_API_TOKEN=<與接收端共享的 service-to-service token>
DASHBOARD_URL=http://<主機>:8787
```

- **月度摘要卡** `post_report.py`：KPI 對比上期、Crash-free 率、優先修復 TOP 3、儀表板連結（帶 `#<app>` 錨點）。每月只發一張（`out/.card_sent_month` gate，失敗下週自動補發）。
- **暴增告警** `check_surge.py`：每週偵測，最近完整週事件 ≥`SURGE_RATIO` 倍（預設 2）且 ≥`SURGE_MIN_EVENTS` 件（預設 500）→ 立即發 `type=surge_alert` 告警（同週去重）。

---

## Migration 指引（從 V1 升級至 V2.3）

1. **設定檔**：檢查 `apps.yaml`，若有使用 Sessions export，可補充 `sessions_dataset: firebase_sessions`（預設值通常為 `firebase_sessions`）。
2. **多 App 過濾**：若多 App 共享同一個 BigQuery dataset，可在 `apps.yaml` 指定 `package_name` 或 `bundle_id` 確保資料隔離。
3. **歷史目錄冷啟動（Historical Catalog Bootstrap）**：
   Dashboard V2.3 具備跨版本生命週期與回歸偵測能力（`IssueHistoricalCatalog`）。
   BigQuery Crashlytics Export 預設保留 90 天，管線首次查詢時會自動回溯 90 天完整 retention。
   若專案具備超過 90 天之歷史月報檔案（`reports/data/<app>/*.json`）或歷史 `unified.json`，建議於初次升級時手動執行一次離線冷啟動：
   ```bash
   python3 -m crash_trend.lifecycle --app <app_id> --bootstrap
   ```
4. **全新 Dashboard**：執行 `./scripts/weekly_sync.sh` 即可自動產出全新 Dashboard V2.3 `dashboard.html` 與 `reports/dashboard_v2.json`。

---

## 驗收清單

- [ ] 手動執行 `./scripts/weekly_sync.sh`：BQ V2、Sessions、Issue Detail、Gemini AI 與 Dashboard V2 均成功產生
- [ ] 產生之 `dashboard.html` 離線可開啟，SaaS 風格 UI、多 App 下拉切換、排序與圖表互動正常
- [ ] 若 Firebase Sessions 未開，儀表板 Crash-free 卡片顯示 "Unavailable" 且不顯示 0%
- [ ] 若無 GEMINI_API_KEY，確定性 Priority 評分正常計算，AI 區塊優雅標記 "disabled"
