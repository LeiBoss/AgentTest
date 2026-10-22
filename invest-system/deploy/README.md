# 部署说明

目标环境：阿里云轻量应用服务器（Ubuntu，2 核 2G），路径统一为 `/home/ubuntu/invest-system`。

## 1. 创建虚拟环境并安装依赖

```bash
cd /home/ubuntu/invest-system
python3.11 -m venv .venv
source .venv/bin/activate
pip install -U pip -i https://mirrors.aliyun.com/pypi/simple/
pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/
```

阿里云镜像不可用时，去掉 `-i` 参数改走默认源。

## 2. 配置环境变量

创建 `.env`（systemd 通过 `EnvironmentFile` 读取，shell 手动运行时也可 `source`）：

```bash
cat > /home/ubuntu/invest-system/.env <<'EOF'
DEEPSEEK_API_KEY=sk-xxx
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
FEISHU_SECRET=xxx
EOF
chmod 600 /home/ubuntu/invest-system/.env
```

三个变量缺任意一个，LLM 解读或推送都会失败并在日志中报错。

## 3. 安装并启用定时器

```bash
cd /home/ubuntu/invest-system
sudo cp deploy/invest.service deploy/invest.timer /etc/systemd/system/
sudo timedatectl set-timezone Asia/Shanghai
sudo systemctl daemon-reload
sudo systemctl enable --now invest.timer
```

`OnCalendar` 走系统本地时区，务必先设为 `Asia/Shanghai`，否则 07:30 会按 UTC 触发。

## 4. 验证

```bash
systemctl list-timers invest.timer
sudo systemctl start invest.service
journalctl -u invest.service -f
```

手动跑一次完整流程（不依赖 systemd）：

```bash
cd /home/ubuntu/invest-system
.venv/bin/python main.py config/chains/baijiu.yaml 600519.SH
```

## 5. 启用 swap

```bash
chmod +x deploy/swap.sh
sudo ./deploy/swap.sh
free -h
```

脚本创建 2GB `/swapfile`、写入 `/etc/fstab`、并把 `vm.swappiness` 持久化为 10，重复执行会跳过已启用的 swap。

## 常用运维命令

```bash
sudo systemctl status invest.timer      # 定时器状态
sudo systemctl status invest.service    # 最近一次执行状态
journalctl -u invest.service -n 100     # 最近 100 行日志
sudo systemctl disable --now invest.timer  # 停止调度
```
