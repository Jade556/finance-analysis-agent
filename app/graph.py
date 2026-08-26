from typing import Annotated, Literal, TypedDict
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.message import add_messages
import os

from app.llm_factory import get_llm  # 统一的 LLM 构造，和 text2sql 共用配置

# Import our custom tools
from app.tools import (
    get_tmall_sales_data,
    get_douyin_campaign_data,
    get_product_cost_structure,
    list_available_skus,
    calculate_sku_net_profit,
    create_feishu_ticket,
    query_erp_by_natural_language,   # 【新增】Text2SQL 智能问数
    get_xiaohongshu_sales_data,      # 【新增】小红书异构脏数据渠道
)

# 1. 定义状态 (State) - LangGraph 1.0 核心
# 相比旧版，这里强制要求定义 Reduction 策略 (add_messages)
# 这使得状态管理更加严谨，避免了消息历史被意外覆盖的问题
class AgentState(TypedDict):
    # messages: 保存对话历史的列表
    # Annotated[list, add_messages]: 表示当新消息进入时，会自动 append 到列表中
    messages: Annotated[list, add_messages]

# 2. 注册工具与模型 (LLM & Tools)
# 这些工具对应我们在 tools.py 中定义的"业务抓手"
tools = [
    get_tmall_sales_data,           # 场景: 查天猫销量
    get_douyin_campaign_data,       # 场景: 查抖音投流/退货
    get_xiaohongshu_sales_data,     # 场景: 查小红书(异构脏数据清洗后)
    list_available_skus,            # 场景: 列出所有可用 SKU
    get_product_cost_structure,     # 场景: 查ERP成本
    query_erp_by_natural_language,  # 场景: 自然语言即席查询 ERP (Text2SQL)
    calculate_sku_net_profit,       # 场景: 严谨计算利润 (去幻觉)
    create_feishu_ticket            # 场景: 动作执行 (人机协同)
]

from dotenv import load_dotenv

# 加载 .env 环境变量
load_dotenv()

# 绑定工具到 LLM（配置统一走 llm_factory，支持 OpenRouter / DeepSeek / 通义 等兼容接口）
llm = get_llm(temperature=0, timeout=120)
llm_with_tools = llm.bind_tools(tools)

# 3. 定义图节点 (Nodes)
def reasoner(state: AgentState):
    """
    [大脑节点]：负责推理。
    它接收当前的状态（历史消息），决定是直接回复用户，还是发起工具调用。
    """
    system_prompt = (
        "你是一家宠物电商公司的【高级财务分析师 Agent】。\n"
        "你的目标是：发现经营异常、精确核算利润、保卫公司现金流。\n"
        "\n"
        "【重要：数据日期】:\n"
        "当前系统日期是 2025-10-21。当用户提到'昨天'时，请使用日期 2025-10-20。\n"
        "数据库中只有 2025-10-20 的数据可用。请不要猜测其他日期！\n"
        "\n"
        "【数据源说明】:\n"
        "- 抖音数据: get_douyin_campaign_data(start_date='2025-10-20', end_date='2025-10-20')\n"
        "- 天猫数据: get_tmall_sales_data\n"
        "- ERP成本: get_product_cost_structure，SKU格式为 'sku_cat_001', 'sku_cat_002', 'sku_toy_003'\n"
        "- 如果不确定 SKU，请先调用 list_available_skus\n"
        "\n"
        "【执行原则】:\n"
        "1. 真实性: 严禁编造数字。必须调用工具获取真实数据。\n"
        "2. 准确性: 禁止心算利润。必须调用 calculate_sku_net_profit 工具进行核算。\n"
        "3. 主动性: 发现亏损时，必须分析原因（如退货率高、投流贵）。\n"
        "4. 清晰性: 汇报时使用 Markdown 表格。\n"
        "\n"
        "【重要：人机协同格式】:\n"
        "当你发现严重问题（如净利润 < 0 或库存告急）需要发送工单时，\n"
        "**必须**使用以下精确格式（否则系统无法解析）：\n"
        "\n"
        "[CONFIRM_ACTION]\n"
        "action: send_ticket\n"
        "department: 运营部\n"
        "title: 【紧急】抖音渠道亏损56.7万，建议暂停投放\n"
        "reason: 退货率29.9%导致净利率-39.65%，建议立即暂停投流并排查退货原因\n"
        "[/CONFIRM_ACTION]\n"
        "\n"
        "注意：\n"
        "- 必须包含开始标签 [CONFIRM_ACTION] 和结束标签 [/CONFIRM_ACTION]\n"
        "- 必须填写 department, title, reason 三个字段\n"
        "- 不要用普通文本询问'是否需要发送工单'，必须用上述格式\n"
        "- 只有用户点击确认后，才能调用 create_feishu_ticket 工具"
    )
    
    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        MessagesPlaceholder(variable_name="messages"),
    ])
    
    chain = prompt | llm_with_tools
    response = chain.invoke({"messages": state["messages"]})
    return {"messages": [response]}

# 4. 构建图 (Graph Builder)
builder = StateGraph(AgentState)

# 添加节点
builder.add_node("reasoner", reasoner)
builder.add_node("tools", ToolNode(tools)) # 官方封装的工具执行节点

# 定义边 (Edges)
builder.add_edge(START, "reasoner")

# 条件边 (Conditional Edge):
# reasoner 执行后，LangGraph 会自动检查 message 中是否有 tool_calls
# - 有 -> 流转到 "tools" 节点
# - 无 -> 流转到 END (直接回复用户)
builder.add_conditional_edges(
    "reasoner",
    tools_condition,
)

# 工具执行完，必须回传给大脑 (reasoner) 进行结果解读
builder.add_edge("tools", "reasoner")

# 5. 编译图 (Compile) & 持久化 (Persistence)
memory = MemorySaver()

# [关键配置] Human-in-the-loop (人机协同)
# 如果需要开启"发送工单前的人工确认"，可以取消下行的注释：
# interrupt_before = ["tools"] 
# 这样当 Agent 试图调用工具时，会暂停运行，等待人工 review。

graph = builder.compile(
    checkpointer=memory,
    # Human-in-the-loop: 在执行 tools 前暂停（可选，目前通过 prompt 控制）
    # interrupt_before=["tools"]
)

# 导出给 main.py 使用
agent_app = graph
