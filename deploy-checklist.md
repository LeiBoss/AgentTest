# 本次变更部署检查清单

适用范围：新增登录、产业链列表与自动生成、研报与 PDF 导出、渠道指标导入。
部署方式：Docker Compose（web + worker），生产路径以 `/root/AgentTest/invest-system` 为例。

## 一、部署前

- [ ] 确认当前分支与目标提交：`git log --oneline -3`
- [ ] 备份数据库（先停 worker，避免写入）
  ```bash
  docker compose stop worker
  docker compose exec -T web python -c "import sqlite3;s=sqlite3.connect('/data/invest.db');d=sqlite3.connect('/data/invest.db.bak');s.backup(d);d.close();s.close()"
  ls -lh data/invest.db.bak
  ```
- [ ] `.env` 已补三项，且权限为 600
  ```bash
  # AUTH_USER=admin
  # AUTH_PASSWORD=<强密码，不要用默认值>
  # SESSION_SECRET=<随机长字符串>
  chmod 600 .env
  ```
- [ ] 确认原有变量仍在：`DEEPSEEK_API_KEY`、`FEISHU_WEBHOOK`、`FEISHU_SECRET`
- [ ] 确认目录可写：web 需要写 `config/chains/`，worker 为只读
  ```bash
  ls -ld config config/chains data
  touch config/chains/.wtest && rm config/chains/.wtest
  ```
- [ ] 确认 `docker-compose.yml` 中 web 的挂载为 `./config:/app/config`（不带 `:ro`）
- [ ] 阿里云安全组已放行 `8000/TCP`
- [ ] 磁盘余量足够（镜像重建 + 新研报数据）：`df -h /`

## 二、部署

- [ ] 拉取代码并重建
  ```bash
  git pull
  docker compose up -d --build
  docker compose ps
  ```
- [ ] web 状态为 `healthy`，worker 为 `Up`
- [ ] 启动日志无异常
  ```bash
  docker compose logs --tail 60 web
  docker compose logs --tail 20 worker
  ```
- [ ] 确认新表已建（启动时自动创建）
  ```bash
  docker compose exec -T web python -c "import sqlite3;c=sqlite3.connect('/data/invest.db');print(sorted(r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")))"
  ```
  期望包含 `report_log`

## 三、冒烟验证

- [ ] 健康检查（公开）
  ```bash
  curl -s http://127.0.0.1:8000/api/health
  ```
- [ ] 未登录访问页面跳登录、接口 401
  ```bash
  curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/            # 302
  curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/chains   # 401
  ```
- [ ] 错误密码返回 401，正确密码返回 200 并写入 Cookie
  ```bash
  BASE=http://127.0.0.1:8000
  curl -s -o /dev/null -w '%{http_code}\n' -H 'Content-Type: application/json' \
    -d '{"username":"admin","password":"wrong"}' $BASE/api/login          # 401
  curl -s -c /tmp/c.txt -H 'Content-Type: application/json' \
    -d '{"username":"admin","password":"<真实密码>"}' $BASE/api/login      # 200
  ```
- [ ] 产业链列表返回明细（`items` 含 `trigger_count`、`data_date`、`report_ts`）
  ```bash
  curl -s -b /tmp/c.txt $BASE/api/chains
  ```
- [ ] 仪表盘触发器接口正常
  ```bash
  curl -s -o /dev/null -w '%{http_code}\n' -b /tmp/c.txt "$BASE/api/chain/%E7%99%BD%E9%85%92/triggers"   # 200
  ```
- [ ] 研报页可打开，首次应提示尚未生成
  ```bash
  curl -s -o /dev/null -w '%{http_code}\n' -b /tmp/c.txt "$BASE/report?chain=%E7%99%BD%E9%85%92"          # 200
  curl -s -o /dev/null -w '%{http_code}\n' -b /tmp/c.txt "$BASE/api/report/%E7%99%BD%E9%85%92"            # 404（无历史研报时）
  ```
- [ ] 生成研报成功并可再次读取（会调用 LLM，产生费用）
  ```bash
  curl -s -b /tmp/c.txt -X POST "$BASE/api/report/%E7%99%BD%E9%85%92" | head -c 200
  curl -s -o /dev/null -w '%{http_code}\n' -b /tmp/c.txt "$BASE/api/report/%E7%99%BD%E9%85%92"            # 200
  ```
- [ ] 退出登录后接口恢复 401
  ```bash
  curl -s -b /tmp/c.txt -X POST $BASE/api/logout
  curl -s -o /dev/null -w '%{http_code}\n' -b /tmp/c.txt $BASE/api/chains                                  # 401
  ```

## 四、新增产业链验证

- [ ] 生成一条真实要用的链（会调用 LLM，约 30 秒）
  ```bash
  curl -s -b /tmp/c.txt -H 'Content-Type: application/json' \
    -d '{"name":"半导体"}' $BASE/api/chains
  ls -l config/chains/半导体.yaml
  ```
- [ ] 同名再次提交返回 409，非法名称返回 400
- [ ] 生成的 YAML 已通过校验：文件可被正常加载，触发器、规则、卡点数量符合预期
- [ ] 新链首次需要采集才有数据（worker 的 `CHAIN_YAML` 仍是 `baijiu.yaml`，不会自动跑新链）
  ```bash
  docker compose exec -T worker python main.py config/chains/半导体.yaml 600519
  ```
- [ ] 采集后列表 `data_count` 与 `data_date` 有值（不再全是 0 / null）

## 五、研报与 PDF

- [ ] 浏览器登录后从仪表盘点「生成研报」，页面各区块正常渲染，无控制台报错
- [ ] 打印预览符合 A4：页边距正常、表格不截断、操作按钮不出现
- [ ] 「导出 PDF」→ 另存为 PDF，文件可打开且中文显示正常
- [ ] 若生成失败（502），检查是否 LLM 返回含违规词或 evidence 未引用真实指标；正常提示后重试

## 六、线上检查

- [ ] 用公网 IP 实际登录一次，确认 Cookie 生效、7 天内免登录
- [ ] 更换 `AUTH_PASSWORD` 后旧会话立即失效（未设 `SESSION_SECRET` 时按密码派生）
- [ ] 日志无持续 5xx、无未捕获异常：`docker compose logs --tail 100 web`
- [ ] worker 下一轮执行时间正确：`docker compose logs --tail 5 worker`
- [ ] 配置每日备份 `data/invest.db`（cron 或运维脚本），并验证备份可打开

## 七、回滚

- [ ] 代码回滚
  ```bash
  docker compose down
  git checkout <上一个可用提交>
  docker compose up -d --build
  ```
- [ ] 数据回滚（如需）：停止 web 与 worker 后，用 `data/invest.db.bak` 覆盖 `data/invest.db`
- [ ] 新增的 `report_log` 表对旧版本无影响，旧代码可正常读取其他表
- [ ] 临时关闭登录（应急）：从 `.env` 删除 `AUTH_PASSWORD` 后 `docker compose up -d web`
