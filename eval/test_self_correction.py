"""
确定性自纠环测试（不依赖真实 LLM，可离线复现）
=============================================
证明 Text2SQL 的"执行反馈自纠环"能真实恢复一次失败的查询：
注入一个桩 LLM，第一轮故意返回会报 "ambiguous column name: sku_id" 的坏 SQL，
第二轮返回加了表前缀的正确 SQL。断言：最终 ok=True 且 attempts==2。

用法:  python eval/test_self_correction.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.text2sql import text2sql


class _StubMsg:
    def __init__(self, content):
        self.content = content


class StubLLM:
    """按顺序吐出预设回复，模拟"先错后对"。"""
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return _StubMsg(self._replies.pop(0))


def main():
    # 第 1 轮：sku_id 在 orders 与 product_costs 中都存在，不加前缀 -> 歧义报错
    bad_sql = (
        "SELECT sku_id, SUM(quantity) AS total_qty "
        "FROM orders JOIN product_costs ON orders.sku_id = product_costs.sku_id "
        "GROUP BY sku_id ORDER BY total_qty DESC LIMIT 1"
    )
    # 第 2 轮：加上表前缀 -> 正确
    good_sql = (
        "SELECT o.sku_id, SUM(o.quantity) AS total_qty "
        "FROM orders o JOIN product_costs p ON o.sku_id = p.sku_id "
        "GROUP BY o.sku_id ORDER BY total_qty DESC LIMIT 1"
    )

    stub = StubLLM([bad_sql, good_sql])
    result = text2sql("哪个商品订单总销量最高", llm=stub)

    print("attempts:", result["attempts"], "| ok:", result["ok"])
    print("trace:")
    for step in result["trace"]:
        print(f"  第{step['attempt']}轮  result_ok={step['result_ok']}")
    print("最终 SQL:", result.get("sql"))
    print("结果:", result.get("rows"))

    assert result["ok"] is True, "自纠后应成功"
    assert result["attempts"] == 2, "应在第 2 轮恢复"
    assert result["trace"][0]["result_ok"] is False, "第 1 轮应失败(歧义列)"
    assert result["trace"][1]["result_ok"] is True, "第 2 轮应成功"
    print("\n✅ PASS: 自纠环成功把第 1 轮失败的查询在第 2 轮恢复。")


if __name__ == "__main__":
    main()
