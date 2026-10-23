# 部署（Docker）

Web 展示与每日任务分别跑在两个容器里，共用 `./data/invest.db`：

- `invest-web`：FastAPI + uvicorn，常驻，暴露 8000
- `invest-worker`：每天 07:30（Asia/Shanghai）执行一次 `main.py`

## 1. 安装 Docker

```bash
sudo dnf install -y docker
sudo systemctl enable --now docker

# Compose 插件优先用系统源；没有则装独立二进制
sudo dnf install -y docker-compose-plugin || {
  sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" \
    -o /usr/local/bin/docker-compose
  sudo chmod +x /usr/local/bin/docker-compose
}
```

国内拉取基础镜像慢时，可在 `/etc/docker/daemon.json` 配置阿里云镜像加速器后重启 Docker。

## 2. 准备环境变量与数据目录

```bash
cd /root/new-invest/AgentTest/invest-system

cat > .env <<'EOF'
DEEPSEEK_API_KEY=sk-xxx
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
FEISHU_SECRET=xxx
EOF
chmod 600 .env

mkdir -p data
# 如已有 SQLite 数据，迁移进来（可选）
cp -a invest.db data/invest.db 2>/dev/null || true
```

## 3. 构建并启动

```bash
docker compose build
docker compose up -d
docker compose ps
```

首次没有数据时，手动跑一次采集：

```bash
docker compose run --rm worker python main.py config/chains/baijiu.yaml 600519
```

## 4. 验证

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/
docker compose logs -f web
docker compose logs -f worker
```

外网访问前，在阿里云安全组放行 `8000/TCP`，然后打开 `http://公网IP:8000`。

## 5. 日常运维

```bash
# 更新代码后重建
git pull
docker compose up -d --build

# 手动触发一次任务
docker compose exec worker python main.py config/chains/baijiu.yaml 600519

# 停止 / 启动
docker compose down
docker compose up -d

# 查看定时任务下一次执行时间（容器内逻辑）
docker compose logs --tail 20 worker
```

## 6. 从 systemd 迁移

先停掉旧服务，避免两个进程同时写 SQLite：

```bash
sudo systemctl disable --now invest.timer invest-web.service
sudo rm -f /etc/systemd/system/invest.service \
           /etc/systemd/system/invest.timer \
           /etc/systemd/system/invest-web.service
sudo systemctl daemon-reload
```

## 7. 手工导入渠道指标

渠道调研、研报类指标没有公开接口，用 CSV 导入。文件放在宿主机 `data/manual/`，容器内对应 `/data/manual/`。

```bash
mkdir -p data/manual
cat > data/manual/2026-10-20.csv <<'EOF'
metric_date,metric_id,value
2026-10-20,wholesale_price,2250
2026-10-20,channel_profit,85
2026-10-20,channel_inv,2.1
EOF

docker compose exec -T web python import_metrics.py \
  config/chains/baijiu.yaml 600519 /data/manual/2026-10-20.csv
```

规则：

- `metric_id` 必须是 `config/chains/baijiu.yaml` 中已定义的 trigger id
- `metric_date` 为 `YYYY-MM-DD`，不能晚于今天
- `value` 按该触发器配置的 `unit` 填写，只写数字
- 同一文件内同一日期 + 指标不能重复
- 校验失败会按行号指出具体字段，且整批不写入

导入只写 `daily_metric`，不触发判定与推送。需要立即生效时再跑一次主流程：

```bash
docker compose exec -T worker python main.py config/chains/baijiu.yaml 600519
```

## 说明

- 数据库路径由 `INVEST_DB=/data/invest.db` 指定，`data/` 挂载到容器 `/data`，重建容器不丢数据。
- `config/` 以只读方式挂载，改 YAML 后下次任务执行自动生效。
- 容器内已设置 `TZ=Asia/Shanghai`，07:30 为北京时间。
- 定时逻辑在 `deploy/worker.sh`，只依赖 `date` 和 `sleep`，没有引入额外调度组件。
