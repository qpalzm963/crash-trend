# 部署到伺服器（內網機器 / Mac mini / Linux 主機）

目標：週同步全自動（BQ 拉取 → normalize → Gemini 月報 → 儀表板 → commit），本機不用開著。

## 前置準備（一次性）

1. **BigQuery 唯讀 SA**：`scripts/create_sa.sh <firebase_project>`（多專案共用金鑰：對其餘專案跑
   `create_sa.sh <專案> --grant-only <sa_email>`）。
2. **Gemini API key**：Google AI Studio 產生。
3. 把你的 instance repo（含 apps.yaml）推到私有 Git。

## 安裝（Docker，建議）

```bash
git clone <你的 instance repo> && cd crash-trend
mkdir -p ~/.config/crash-trend && cp <SA json> ~/.config/crash-trend/sa.json && chmod 600 ~/.config/crash-trend/sa.json
printf 'GEMINI_API_KEY=...\n' > .env
docker compose up -d --build                 # supercronic 每週一 09:37（TZ=Asia/Taipei，compose 可改）
docker compose run --rm crash-trend /bin/bash /app/scripts/weekly_sync.sh   # 手動試跑驗證
tail -30 logs/weekly_sync.log
```

- 改排程：編輯 `docker/crontab` 後 `docker compose restart`
- 憑證以 read-only bind mount 掛入容器（`docker-compose.yml`），**不進 image、不進 git**
- `apps.yaml` 各 app 的 `source_repo` 指到伺服器上的原始碼 clone（沒有可留空，僅少了 AI 的原始碼片段輔助）

## 備選：launchd（macOS 主機直跑）

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp scripts/com.crash-trend.weekly-sync.plist.example ~/Library/LaunchAgents/com.crash-trend.weekly-sync.plist
# 編輯其中兩處 /PATH/TO 後：
launchctl load ~/Library/LaunchAgents/com.crash-trend.weekly-sync.plist
```

## 與聊天系統整合（可選）

`weekly_sync.sh` 產出的 `reports/data/<app>/<月>.json` 就是現成的通知 payload：
在腳本尾端 `curl -X POST` 到你的 bot/webhook（Google Chat、Slack…），
內容建議：KPI 對比上期、新增/惡化 top 3、儀表板連結。

## 驗收清單

- [ ] 手動跑一輪：BQ 有資料、月報 md 生成、儀表板更新、commit 成功
- [ ] 排程到點自動執行（`logs/weekly_sync.log` 時間戳）
- [ ] 儀表板數字與月報一致

## 已知限制

- BigQuery 連結前的 console 歷史無程式化管道（平台限制），需要時人工填 `manual/<app>/console_issues.csv` 一次。
