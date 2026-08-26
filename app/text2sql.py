"""
Text2SQL 引擎（吸收自「案例7 智能问数」，做成本 Agent 的一个高级工具）
=====================================================================
把"自然语言 → SQL → 执行 → 出错自纠"整条链路收进来，让财务 Agent 能对
ERP 库做任意即席查询，而不是只能走预先写死的几个工具函数。

三个工程要点（面试重点）：
1. Schema Linking：不把整库结构盲塞进 prompt，而是自省表结构 + 采样几行样例
   数据一起给模型，降低"字段名猜错 / 无关表干扰"。本项目库小，仍按可扩展方式写。
2. 执行反馈自纠环：生成 SQL 先干跑，捕获 sqlite 报错回灌给模型重生成，
   最多 N 轮；把"一次性生成成功率"从依赖模型运气变成带反馈的收敛过程。
3. 只读安全闸：强制单条 SELECT，拦截 DROP/DELETE/UPDATE/INSERT/ALTER 等写操作
   和多语句注入——数据分析场景绝不允许 Agent 改库。
"""
from __future__ import annotations

import os
import re
import sqlite3
from typing import Any

from app.resilience import resilient
# 注意：get_llm 在 text2sql() 内部按需导入，
# 这样 get_schema / run_sql / is_safe_select 等纯 SQL 逻辑不依赖 langchain，
# 单测和评测里可以脱离大模型直接跑。

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
DB_PATH = os.path.join(DATA_DIR, "erp.db")

# 只允许单条 SELECT / WITH 查询
_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|truncate|attach|pragma)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- schema linking
def get_schema(db_path: str = DB_PATH, sample_rows: int = 3) -> str:
    """自省 SQLite 结构，输出 DDL + 每表若干样例行，喂给模型做 schema linking。"""
    if not os.path.exists(db_path):
        return "(数据库不存在)"

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    tables = [
        r[0]
        for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]

    blocks: list[str] = []
    for t in tables:
        ddl = cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()
        cols = [c[1] for c in cur.execute(f"PRAGMA table_info({t})").fetchall()]
        rows = cur.execute(f"SELECT * FROM {t} LIMIT {sample_rows}").fetchall()
        sample = "\n".join(
            "    " + " | ".join(str(v) for v in row) for row in rows
        )
        blocks.append(
            f"-- 表 {t}\n{(ddl[0] if ddl else '').strip()}\n"
            f"-- 样例数据（列: {', '.join(cols)}）:\n{sample or '    (空)'}"
        )
    conn.close()
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- 安全闸
def is_safe_select(sql: str) -> tuple[bool, str]:
    """只放行单条 SELECT / WITH 查询。返回 (是否安全, 原因)。"""
    s = sql.strip().rstrip(";").strip()
    if not s:
        return False, "空 SQL"
    if ";" in s:
        return False, "禁止多语句（防注入）"
    if _WRITE_KEYWORDS.search(s):
        return False, "检测到写操作关键字，只读查询被拒绝"
    if not re.match(r"^(select|with)\b", s, re.IGNORECASE):
        return False, "只允许 SELECT / WITH 查询"
    return True, "ok"


def _clean_sql(raw: str) -> str:
    """剥掉模型可能带的 ```sql ``` 代码块围栏。"""
    raw = raw.strip()
    raw = re.sub(r"^```(?:sql)?", "", raw, flags=re.IGNORECASE).strip()
    raw = re.sub(r"```$", "", raw).strip()
    return raw


# ---------------------------------------------------------------- 执行
@resilient(retries=1, timeout=15, cache_ttl=30)
def run_sql(sql: str, db_path: str = DB_PATH) -> dict[str, Any]:
    """只读执行 SQL；出错时把 sqlite 的报错原文带回去（供自纠环使用）。"""
    ok, reason = is_safe_select(sql)
    if not ok:
        return {"ok": False, "error": f"安全校验未通过: {reason}", "sql": sql}
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return {"ok": True, "rows": rows, "row_count": len(rows), "sql": sql}
    except sqlite3.Error as e:
        return {"ok": False, "error": f"SQLite 执行错误: {e}", "sql": sql}


# ---------------------------------------------------------------- 生成 + 自纠环
_GEN_PROMPT = """你是一个严谨的 SQLite 数据分析助手。请把用户问题转成**一条**只读 SQL（SELECT/WITH）。

# 可用库结构与样例数据
{schema}

# 规则
- 只输出 SQL 本身，不要解释，不要加代码块围栏。
- 只允许查询，禁止任何写操作。
- 字段/表名必须严格来自上面的 schema。
- 需要聚合就用 SUM/AVG/COUNT/GROUP BY。

# 用户问题
{question}
{feedback}"""


def text2sql(question: str, max_retries: int = 3, llm=None) -> dict[str, Any]:
    """自然语言 → SQL → 执行 → 自纠 的完整闭环。

    Args:
        question: 自然语言问题
        max_retries: 最大自纠轮次
        llm: 可注入的 LLM（需实现 .invoke(prompt).content）；为 None 时用 llm_factory。
             注入能力用于单测：喂"先错后对"的桩 LLM，确定性验证自纠环能恢复失败查询。

    返回：{ok, sql, rows, row_count, attempts, trace}
    """
    if llm is None:
        from app.llm_factory import get_llm  # 按需导入：纯 SQL 逻辑不依赖 langchain
        llm = get_llm(temperature=0)

    schema = get_schema()
    feedback = ""
    trace: list[dict] = []

    for attempt in range(1, max_retries + 1):
        prompt = _GEN_PROMPT.format(schema=schema, question=question, feedback=feedback)
        raw = llm.invoke(prompt).content
        sql = _clean_sql(raw)
        result = run_sql(sql)
        trace.append({"attempt": attempt, "sql": sql, "result_ok": result["ok"]})

        if result["ok"]:
            return {
                "ok": True,
                "sql": sql,
                "rows": result["rows"],
                "row_count": result["row_count"],
                "attempts": attempt,
                "trace": trace,
            }

        # 出错：把报错回灌给模型，进入下一轮自纠
        feedback = (
            f"\n\n# 上一轮生成的 SQL 执行失败，请修正后重试\n"
            f"错误的 SQL:\n{sql}\n错误信息: {result['error']}"
        )

    return {
        "ok": False,
        "error": f"经 {max_retries} 轮自纠仍未生成可执行 SQL",
        "attempts": max_retries,
        "trace": trace,
    }
