# 产业链跟踪系统 — 项目总上下文

## 目标

为个人开发者构建的产业链跟踪系统。用最低成本跑通最小闭环：

```
输入产业链名称 → 加载配置 → 取数 → 触发器判定 → 组合规则 → LLM 解读 → 飞书推送
```

## 技术约束（不可更改）

- Python 3.11+
- 依赖仅限：`pydantic>=2`、`akshare`、`pandas`、`tenacity`、`openai`、`requests`、`pyyaml`、`pytest`
- 数据库：SQLite 单文件 `invest.db`，不用 ORM，手写 SQL
- LLM：DeepSeek，通过 openai SDK，`base_url="https://api.deepseek.com"`
- 配置：YAML 文件，Pydantic 模型校验
- 部署：阿里云轻量 2核2G，systemd timer 调度
- 不引入：LangGraph、LangChain、Neo4j、Redis、Docker、FastAPI、任何 Web 框架（第一阶段）

## 代码风格

- 每个模块单文件，函数短小，类型注解完整
- 纯函数优先，副作用集中在 `main.py`
- 所有外部调用（AkShare、LLM、飞书）都要有 try/except 和结构化日志
- 不写长篇 docstring，一行说明即可
- 测试用 pytest，每个模块配一个 `test_*.py`

## 硬性规则（违反即驳回）

1. LLM 不允许做任何数值计算。比率 / 同比 / 环比必须由 Python 函数算好再传给 LLM。
2. 每条 LLM 输出必须包含 `evidence` 字段（trigger_id 列表），无 `evidence` 的输出直接丢弃。
3. 所有对外输出必须过 `compliance_filter`，出现荐股词直接拦截。
4. 不写"未来可能扩展"的代码，只实现当前提示词要求的功能。
