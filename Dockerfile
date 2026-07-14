# crash-trend 排程容器：內建 supercronic 依 docker/crontab 每週執行同步
# 憑證「不」烘進 image —— runtime 以 read-only bind mount 掛入（見 docker-compose.yml）
FROM python:3.12-slim

ARG TARGETARCH
ADD https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-${TARGETARCH} /usr/local/bin/supercronic

RUN apt-get update && apt-get install -y --no-install-recommends git tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && chmod +x /usr/local/bin/supercronic \
    # 程式碼以 bind mount 掛在 /app（host 端 repo），須信任該目錄
    && git config --global safe.directory /app

# firebase-tools（fetch_stacktraces.py 用）需 Node ≥20 —— 從官方 image 抄 node+npm，不裝整套 apt nodejs
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g firebase-tools@latest \
    && npm cache clean --force
# 認證：host 的 ~/.config/configstore/firebase-tools.json（user token）由 compose 掛入，不進 image

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 程式碼與資料由 compose bind mount 提供；此處僅為 image 可獨立檢視
COPY . .

ENV TZ=Asia/Taipei
# 絕對路徑必要：supercronic 為 PID 1 時以 argv[0] re-exec 自身（raw syscall 不查 PATH）
CMD ["/usr/local/bin/supercronic", "-passthrough-logs", "/app/docker/crontab"]
