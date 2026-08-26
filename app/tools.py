import pandas as pd
import sqlite3
import json
import os
from typing import Dict, List, Any
from langchain_core.tools import tool

from app.resilience import resilient

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

@tool
def get_tmall_sales_data(start_date: str, end_date: str, sku_id: str = None) -> List[Dict]:
    """
    [工具：查天猫销量] 获取天猫平台的销售数据。
    
    Args:
        start_date (str): 开始日期 (YYYY-MM-DD)
        end_date (str): 结束日期 (YYYY-MM-DD)
        sku_id (str, optional): 指定 SKU ID 进行筛选
    
    Returns:
        List[Dict]: 销售记录列表，包含日期、SKU、销售额(GMV)、订单量等
    """
    file_path = os.path.join(DATA_DIR, "tmall_data.json")
    if not os.path.exists(file_path):
        return [{"error": "Tmall data file not found"}]
        
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["date"])
    
    mask = (df["date"] >= start_date) & (df["date"] <= end_date)
    if sku_id:
        mask &= (df["sku_id"] == sku_id)
        
    filtered_df = df.loc[mask]
    return filtered_df.to_dict(orient="records")

@tool
@resilient(cache_ttl=30)
def get_douyin_campaign_data(start_date: str, end_date: str) -> Dict:
    """
    [工具：查抖音数据] 获取抖音渠道的投流与销售数据汇总。
    返回指定日期范围内的汇总数据，包含 GMV、广告费、退款等关键财务指标。

    Args:
        start_date (str): 开始日期 (YYYY-MM-DD)
        end_date (str): 结束日期 (YYYY-MM-DD)
    
    Returns:
        Dict: 汇总数据，包含：
            - total_gmv: 总交易额
            - total_ad_fee: 总广告费
            - total_refund: 总退款金额
            - total_orders: 总订单数
            - avg_commission_rate: 平均佣金率
            - top_sku: 销量最高的 SKU
    """
    file_path = os.path.join(DATA_DIR, "douyin_data.json")
    if not os.path.exists(file_path):
        return {"error": "Douyin data file not found"}
        
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    df = pd.DataFrame(data)
    df["date"] = pd.to_datetime(df["date"])
    
    mask = (df["date"] >= start_date) & (df["date"] <= end_date)
    filtered_df = df.loc[mask]
    
    if filtered_df.empty:
        return {"error": f"No data found for date range {start_date} to {end_date}"}
    
    # Aggregate summary
    total_gmv = filtered_df["gmv"].sum()
    total_orders = filtered_df["order_count"].sum()
    
    # Extract nested fields
    total_ad_fee = sum(r.get("ad_traffic_fee", 0) for r in filtered_df["cost_structure"])
    total_refund = sum(r.get("refund_amount", 0) for r in filtered_df["after_sales"])
    total_return_orders = sum(r.get("return_order_count", 0) for r in filtered_df["after_sales"])
    avg_commission_rate = sum(r.get("commission_rate", 0.2) for r in filtered_df["cost_structure"]) / len(filtered_df)
    
    # Top SKU by GMV
    top_sku = filtered_df.groupby("sku_id")["gmv"].sum().idxmax()
    
    return {
        "date_range": f"{start_date} to {end_date}",
        "total_gmv": round(total_gmv, 2),
        "total_orders": int(total_orders),
        "total_ad_fee": round(total_ad_fee, 2),
        "total_refund": round(total_refund, 2),
        "total_return_orders": int(total_return_orders),
        "return_rate": round(total_return_orders / total_orders * 100, 1) if total_orders > 0 else 0,
        "ad_fee_ratio": round(total_ad_fee / total_gmv * 100, 1) if total_gmv > 0 else 0,
        "avg_commission_rate": round(avg_commission_rate, 2),
        "top_sku_by_gmv": top_sku,
        "record_count": len(filtered_df)
    }


@tool
def get_product_cost_structure(sku_id: str) -> Dict:
    """
    [工具：查ERP成本] 连接内部 ERP 数据库，查询商品的【生产成本】和【即时库存】。
    这是计算净利润必不可少的环节。
    
    注意：调用此工具前，请先调用 list_available_skus 获取可用的 SKU 列表。

    Args:
        sku_id (str): 商品唯一编码 (格式: 'sku_cat_001', 'sku_cat_002' 等)
    
    Returns:
        Dict: {
            "production_cost": 生产成本 (COGS),
            "logistics_cost": 单均履约费,
            "current_stock": 当前库存量
        }
    """
    db_path = os.path.join(DATA_DIR, "erp.db")
    if not os.path.exists(db_path):
        return {"error": "ERP database not found"}
        
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT production_cost, logistics_cost, warehouse_stock FROM product_costs WHERE sku_id = ?", (sku_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return {
            "sku_id": sku_id,
            "production_cost": row[0],
            "logistics_cost": row[1],
            "current_stock": row[2]
        }
    else:
        return {"error": f"SKU {sku_id} not found in ERP"}

@tool
def list_available_skus() -> List[Dict]:
    """
    [工具：列出所有SKU] 查询 ERP 数据库中所有可用的商品 SKU 及其基本信息。
    在调用 get_product_cost_structure 之前，请先调用此工具获取正确的 SKU ID。

    Returns:
        List[Dict]: 包含所有 SKU 的列表，每个 SKU 包含 sku_id, sku_name, category 等字段
    """
    db_path = os.path.join(DATA_DIR, "erp.db")
    if not os.path.exists(db_path):
        return [{"error": "ERP database not found"}]
        
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT sku_id, product_name, production_cost, warehouse_stock FROM product_costs")
    rows = cursor.fetchall()
    conn.close()
    
    return [
        {
            "sku_id": row[0],
            "product_name": row[1],
            "unit_cost": row[2],
            "current_stock": row[3]
        }
        for row in rows
    ]

@tool
def calculate_sku_net_profit(gmv: float, ad_spend: float, commission: float, 
                             platform_fee: float, production_cost: float, 
                             logistics_cost: float, refund_amount: float, sales_count: int) -> Dict:
    """
    [工具：利润核算器] 执行严谨的财务计算。
    警告：不要尝试让 LLM 自己做加减法，必须调用此工具进行核算，以防止幻觉。
    
    Args:
        gmv (float): 销售收入 (Gross Merchandise Value)
        ad_spend (float): 广告投流费用
        commission (float): 达人/主播佣金
        platform_fee (float): 平台技术服务费
        production_cost (float): 单件生产成本 (Unit Cost)
        logistics_cost (float): 单件物流成本 (Unit Logistics)
        refund_amount (float): 仅退款 + 退货退款总额
        sales_count (int): 总销量 (用于计算总 COGS)
        
    Returns:
        Dict: 包含净利润(net_profit)、利润率(margin)及各项成本明细。
    """
    
    # 1. 计算商品销售成本 (COGS)
    total_cogs = sales_count * production_cost
    
    # 2. 计算总履约成本
    total_logistics = sales_count * logistics_cost
    
    # 3. 汇总所有支出
    total_costs = ad_spend + commission + platform_fee + total_cogs + total_logistics + refund_amount
    
    # 4. 计算净利
    net_profit = gmv - total_costs
    net_profit_margin = (net_profit / gmv) if gmv > 0 else 0
    
    return {
        "gmv": gmv,
        "total_revenue": gmv - refund_amount, # 净销售额
        "breakdown": {
            "cogs (生产成本)": total_cogs,
            "logistics (物流费)": total_logistics,
            "ads (广告费)": ad_spend,
            "commission (佣金)": commission,
            "platform_fee (平台费)": platform_fee,
            "refunds (退款)": refund_amount
        },
        "net_profit (净利润)": round(net_profit, 2),
        "net_profit_margin (净利率)": round(net_profit_margin, 4)
    }

@tool
def create_feishu_ticket(priority: str, department: str, title: str, content: str) -> str:
    """
    [工具：发送工单] 当发现严重问题（如严重亏损、库存熔断）时，调用此工具通知业务/管理部门。
    
    Args:
        priority (str): 优先级 ('High'/'Medium'/'Low')
        department (str): 接收部门 (e.g., '运营部', '供应链')
        title (str): 标题
        content (str): 详细描述与数据支撑
    """
    # 调用企业微信/飞书/钉钉 API 发送消息
    print(f"!!! [System] Enterprise Ticket Created !!! Priority: {priority} | To: {department} | {title}")
    return f"Success: Ticket created. ID: TICKET-{pd.Timestamp.now().strftime('%Y%m%d%H%M%S')}"


# ============================================================================
# 【本人新增工具 1】智能问数 —— 把「案例7 Text2SQL」收进来做成 Agent 的一个工具
# ============================================================================
@tool
def query_erp_by_natural_language(question: str) -> Dict:
    """
    [工具：智能问数 / Text2SQL] 用自然语言对 ERP 库做**任意即席查询**。

    当已有的固定工具（如 get_product_cost_structure）无法满足灵活查询时使用，例如：
      - "库存低于 2 万的商品有哪些"
      - "把所有 SKU 按库存从高到低排序"
      - "生产成本最高的商品是哪个"
    内部自动完成：自省表结构做 schema linking → 生成 SQL → 只读执行 → 出错自纠（最多3轮）。

    Args:
        question (str): 自然语言查询问题

    Returns:
        Dict: {ok, sql, rows, row_count, attempts}
    """
    from app.text2sql import text2sql  # 延迟导入，避免无 LLM 环境下的导入开销
    res = text2sql(question)
    if res.get("ok"):
        return {
            "ok": True,
            "sql": res["sql"],
            "rows": res["rows"],
            "row_count": res["row_count"],
            "attempts": res["attempts"],
        }
    return {"ok": False, "error": res.get("error"), "attempts": res.get("attempts")}


# ============================================================================
# 【本人新增工具 2】小红书渠道 —— 异构脏 CSV 的接入与清洗
# ============================================================================
@tool
@resilient(cache_ttl=60)
def get_xiaohongshu_sales_data(start_date: str, end_date: str) -> Dict:
    """
    [工具：查小红书数据] 读取并清洗小红书渠道数据后返回销售汇总。

    小红书渠道以**脏 CSV** 交付（日期格式不统一、金额带 ¥ 和千分位、含缺失值与重复行），
    本工具内部完成清洗，输出与抖音/天猫一致口径的汇总，并附带清洗报告。

    Args:
        start_date (str): 开始日期 (YYYY-MM-DD)
        end_date (str): 结束日期 (YYYY-MM-DD)

    Returns:
        Dict: {ok, channel, date_range, summary, clean_report}
    """
    from app.ingest import load_and_clean
    res = load_and_clean()
    if not res.get("ok"):
        return res

    recs = [r for r in res["records"] if start_date <= r["date"] <= end_date]
    total_gmv = round(sum(r["gmv"] for r in recs), 2)
    total_orders = int(sum(r["order_count"] for r in recs))
    total_refund = round(sum(r["refund_amount"] for r in recs), 2)
    return {
        "ok": True,
        "channel": "xiaohongshu",
        "date_range": f"{start_date} to {end_date}",
        "summary": {
            "total_gmv": total_gmv,
            "total_orders": total_orders,
            "total_refund": total_refund,
            "return_rate": round(total_refund / total_gmv * 100, 1) if total_gmv else 0,
            "record_count": len(recs),
        },
        "clean_report": res["clean_report"],
    }
