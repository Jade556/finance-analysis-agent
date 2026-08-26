"""
Agent 评测脚本 (Evaluation Harness)
===================================
量化 Agent 的两个核心指标，让"改进"变成可对比的数字，而不是拍脑袋：

1. 工具选择准确率 (Tool-Selection Accuracy)
   —— 每条用例声明"必须命中的工具"，跑完后从消息轨迹里抽出实际调用的工具，
      对比是否命中。分 "all"(全命中) 和 "any"(命中其一) 两种判定。

2. Text2SQL 执行成功率
   —— 统计走了 query_erp_by_natural_language 的用例里，SQL 最终成功执行的比例。

用法：
    # 先在 .env 里配好 openrouter_api_key / model_name
    python eval/run_eval.py

没有配 API Key 时脚本会给出提示并退出（不会假装跑过）。
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EVAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval_set.json")


def extract_called_tools(messages) -> list[str]:
    """从 LangGraph 返回的消息列表里，抽出所有被调用过的工具名。"""
    called = []
    for m in messages:
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                if name:
                    called.append(name)
    return called


def extract_tool_outputs(messages) -> list:
    """抽出 ToolMessage 的内容，用于判断 Text2SQL 是否执行成功。"""
    outs = []
    for m in messages:
        if m.__class__.__name__ == "ToolMessage":
            outs.append(getattr(m, "content", ""))
    return outs


def run():
    if not os.getenv("openrouter_api_key"):
        # 尝试从 .env 读取
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass
    if not os.getenv("openrouter_api_key"):
        print("❌ 未检测到 openrouter_api_key，请先在 .env 配置后再评测。")
        sys.exit(1)

    from langchain_core.messages import HumanMessage
    from app.graph import agent_app

    with open(EVAL_PATH, encoding="utf-8") as f:
        suite = json.load(f)
    cases = suite["cases"]

    rows = []
    tool_hits = 0
    t2s_total = 0
    t2s_success = 0

    for i, case in enumerate(cases):
        q = case["question"]
        must = case["must_call"]
        mode = case.get("match", "all")

        config = {"configurable": {"thread_id": f"eval_{i}"}, "recursion_limit": 50}
        try:
            result = agent_app.invoke({"messages": [HumanMessage(content=q)]}, config=config)
            called = extract_called_tools(result["messages"])
            outputs = extract_tool_outputs(result["messages"])
        except Exception as e:
            rows.append((case["id"], "ERROR", str(e)[:40]))
            continue

        called_set = set(called)
        if mode == "any":
            passed = any(t in called_set for t in must)
        else:
            passed = all(t in called_set for t in must)
        tool_hits += int(passed)

        # Text2SQL 成功率
        if "query_erp_by_natural_language" in called_set:
            t2s_total += 1
            ok = any('"ok": true' in str(o).lower() or "'ok': true" in str(o).lower()
                     or '"ok":true' in str(o).lower() for o in outputs)
            t2s_success += int(ok)

        rows.append((case["id"], "PASS" if passed else "FAIL", ",".join(called) or "-"))

    # ---- 报告 ----
    print("\n" + "=" * 70)
    print(f"{'case_id':<18}{'result':<8}{'called_tools'}")
    print("-" * 70)
    for cid, res, called in rows:
        print(f"{cid:<18}{res:<8}{called}")
    print("=" * 70)
    n = len(cases)
    print(f"工具选择准确率: {tool_hits}/{n} = {tool_hits / n * 100:.1f}%")
    if t2s_total:
        print(f"Text2SQL 执行成功率(Agent内): {t2s_success}/{t2s_total} = {t2s_success / t2s_total * 100:.1f}%")
    print("=" * 70)

    # ---- 第二部分：Text2SQL 直测（隔离 Agent 工具选择，纯测 NL->SQL 质量）----
    run_text2sql_eval()


def run_text2sql_eval():
    """直接调用 text2sql() 引擎，度量执行成功率与平均自纠轮次。"""
    from app.text2sql import text2sql

    q_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "text2sql_questions.json")
    with open(q_path, encoding="utf-8") as f:
        questions = json.load(f)["questions"]

    print("\n" + "=" * 70)
    print("Text2SQL 直测（NL -> SQL -> 执行）")
    print("-" * 70)
    ok_cnt = 0
    total_attempts = 0
    for q in questions:
        r = text2sql(q)
        ok = r.get("ok", False)
        ok_cnt += int(ok)
        total_attempts += r.get("attempts", 0)
        flag = "OK  " if ok else "FAIL"
        print(f"  [{flag}] attempts={r.get('attempts')} | {q}")
        if ok:
            print(f"         SQL: {r.get('sql')}")
    m = len(questions)
    print("-" * 70)
    print(f"Text2SQL 执行成功率: {ok_cnt}/{m} = {ok_cnt / m * 100:.1f}%")
    print(f"平均自纠轮次: {total_attempts / m:.2f}")
    print("=" * 70)


if __name__ == "__main__":
    run()
