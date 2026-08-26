# 企业级财务分析 Agent（多源异构数据整合）

> 一个基于 **LangGraph** 的单 Agent 应用：面向电商企业"跨系统财务分析"场景，
> 让业务人员用自然语言完成 GMV / 净利润 / 退货率的**跨数据源归因分析**，
> Agent 自主规划并调用工具，全程流式展示思维链，关键动作走人机协同确认。

---

## 1. 它能做什么

- **跨源分析**：整合抖音（JSON）、天猫（JSON）、小红书（脏 CSV）三个渠道 + ERP 成本（SQLite），在同一口径下算账。
- **自然语言问数**：内置 Text2SQL 工具，可对 ERP 库做任意即席查询（"库存低于 2 万的商品有哪些"）。
- **去幻觉核算**：利润一律走计算器工具，禁止大模型心算。
- **人机协同**：发现严重亏损/库存熔断时，Agent 产出工单建议 → 用户确认 → 才真正执行。
- **流式思维链**：FastAPI + SSE 实时推送推理过程与每一步工具调用。

## 2. 架构

```
用户提问
  │
  ▼
reasoner（推理节点，绑定 8 个工具）──┐
  │  有 tool_calls                  │ 无 tool_calls
  ▼                                 ▼
tools（ToolNode 执行）──回传──►    END（结构化回答）
```

- 状态图：`app/graph.py`（`StateGraph` + `add_messages` + `tools_condition` 条件边，ReAct 闭环）
- 工具集：`app/tools.py`（8 个工具）
- 服务层：`app/main.py`（`/api/chat` 流式、`/api/scan-alerts` 预警、`/api/health` 健康检查）
- 前端：`frontend.html`（单文件仪表盘）

## 3. 目录

```
finance-analysis-agent/
├── app/
│   ├── graph.py         # LangGraph Agent 定义 + 工具绑定
│   ├── tools.py         # 8 个工具（含 2 个本人新增）
│   ├── text2sql.py      # ★本人新增：Text2SQL 引擎（schema linking + 执行自纠）
│   ├── ingest.py        # ★本人新增：异构脏 CSV 接入与清洗
│   ├── resilience.py    # ★本人新增：工具韧性层（重试/超时/缓存）
│   ├── llm_factory.py   # ★本人新增：统一 LLM 构造
│   └── main.py          # FastAPI 服务（已做 Windows 兼容 + 健康检查）
├── data/
│   ├── douyin_data.json / tmall_data.json   # 渠道数据
│   ├── erp.db                               # ERP 成本（SQLite）
│   └── xiaohongshu_raw.csv                  # ★本人新增：故意做脏的第三渠道
├── eval/
│   ├── eval_set.json            # ★本人新增：Agent 工具选择评测集
│   ├── text2sql_questions.json  # ★本人新增：Text2SQL 直测题库（含多表 JOIN）
│   ├── run_eval.py              # ★本人新增：工具选择准确率 + Text2SQL 成功率
│   └── test_self_correction.py  # ★本人新增：自纠环确定性测试（离线）
├── scripts/
│   └── build_db.py              # ★本人新增：一键重建多表 ERP 库
├── requirements.txt
└── .env.example
```

## 4. 快速开始

```bash
# 1) 装依赖
pip install -r requirements.txt

# 2) 配 Key（复制 .env.example 为 .env 后填入真实 Key）
cp .env.example .env

# 3) （可选）重建多表 ERP 示例库
python scripts/build_db.py

# 4) 启动（Windows / Mac / Linux 通用）
python -m app.main
# 打开 http://localhost:8000

# 5) 跑评测（需已配 Key）
python eval/run_eval.py
# 自纠环离线测试（不需要 Key）
python eval/test_self_correction.py
```

---

