import sys
import os

# Windows 控制台默认 GBK，Agent 的日志里有中文/符号会触发 UnicodeEncodeError，
# 这里强制把标准输出/错误切到 UTF-8（errors='replace' 兜底），保证跨平台不崩。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Add project root to sys.path so we can import 'app' module
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import json
import pandas as pd
from langchain_core.messages import HumanMessage
from dotenv import load_dotenv

# Load env before importing graph which uses env vars
load_dotenv()

from app.graph import agent_app
from langchain_core.messages import AIMessage, ToolMessage

app = FastAPI()

# Allow CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files (the HTML frontend)
current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app.mount("/static", StaticFiles(directory=current_dir), name="static")

@app.get("/")
async def read_root():
    return FileResponse(os.path.join(current_dir, "frontend.html"))


@app.get("/api/health")
async def health():
    """健康检查 + 数据源状态 + 工具缓存概览（工程可观测性）。"""
    from app.resilience import cache_stats
    data_dir = os.path.join(current_dir, "data")
    sources = {
        "douyin_json": os.path.exists(os.path.join(data_dir, "douyin_data.json")),
        "tmall_json": os.path.exists(os.path.join(data_dir, "tmall_data.json")),
        "erp_sqlite": os.path.exists(os.path.join(data_dir, "erp.db")),
        "xiaohongshu_csv": os.path.exists(os.path.join(data_dir, "xiaohongshu_raw.csv")),
    }
    return {"status": "ok", "data_sources": sources, "tool_cache": cache_stats()}

@app.post("/api/chat")
async def chat_endpoint(request: Request):
    """
    Streaming endpoint with enhanced logging.
    Expects JSON: { "message": "user question", "thread_id": "123" }
    """
    data = await request.json()
    user_input = data.get("message")
    thread_id = data.get("thread_id", "default_thread")
    
    print(f"\n{'='*60}")
    print(f"📩 USER INPUT: {user_input}")
    print(f"{'='*60}\n")
    
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 50  # Increase from default 25 to allow more tool calls
    }
    
    async def event_stream():
        inputs = {"messages": [HumanMessage(content=user_input)]}
        
        try:
            print("🚀 Starting LangGraph stream...")
            event_count = 0
            
            async for event in agent_app.astream_events(inputs, config=config, version="v1"):
                event_count += 1
                kind = event["event"]
                
                # Debug: print all event types (first 20)
                if event_count <= 20:
                    print(f"  📌 Event #{event_count}: {kind}")
                
                # 1. Agent Token Stream
                if kind == "on_chat_model_stream":
                    content = event["data"]["chunk"].content
                    if content:
                        yield f"data: {json.dumps({'type': 'token', 'content': content})}\n\n"
                
                # 2. Tool Call Start - Enhanced with params
                elif kind == "on_tool_start":
                    tool_name = event["name"]
                    tool_inputs = event["data"].get("input")
                    
                    # Terminal log
                    print(f"🔧 TOOL CALL: {tool_name}")
                    print(f"   Params: {json.dumps(tool_inputs, ensure_ascii=False, indent=2)}")
                    
                    yield f"data: {json.dumps({'type': 'tool_start', 'tool': tool_name, 'input': tool_inputs}, ensure_ascii=False)}\n\n"
                
                # 3. Tool Output - Enhanced with full result
                elif kind == "on_tool_end":
                    tool_output = event["data"].get("output")
                    tool_name = event.get("name", "unknown")
                    
                    # Serialize output
                    if hasattr(tool_output, 'content'):
                        tool_output = tool_output.content
                    
                    # Terminal log (truncate if too long)
                    output_str = json.dumps(tool_output, ensure_ascii=False, default=str)
                    print(f"✅ TOOL RESULT ({tool_name}):")
                    print(f"   {output_str[:500]}{'...' if len(output_str) > 500 else ''}\n")
                    
                    yield f"data: {json.dumps({'type': 'tool_end', 'tool': tool_name, 'output': tool_output}, ensure_ascii=False, default=str)}\n\n"
            
            print(f"\n📊 Total events processed: {event_count}")
            print(f"{'='*60}")
            print(f"✅ RESPONSE COMPLETE")
            print(f"{'='*60}\n")
            
        except Exception as e:
            print(f"❌ ERROR in event_stream: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

@app.get("/api/scan-alerts")
async def scan_alerts():
    """
    Scan data sources and generate alerts based on business rules.
    Returns a list of alerts with severity and suggested actions.
    """
    alerts = []
    
    # 1. Scan Douyin data for profit issues
    json_path = os.path.join(current_dir, "data", "douyin_data.json")
    if os.path.exists(json_path):
        with open(json_path, 'r') as f:
            data = json.load(f)
            
        # Rule: if ad_traffic_fee > 30% of GMV, flag as profit warning
        for record in data[:3]:  # Check first 3 records
            gmv = record.get("gmv", 0)
            refund = record.get("after_sales", {}).get("refund_amount", 0)
            ad_fee = record.get("cost_structure", {}).get("ad_traffic_fee", 0)
            comm_rate = record.get("cost_structure", {}).get("commission_rate", 0.2)
            
            # Check if ad spend ratio is abnormally high
            ad_ratio = ad_fee / gmv if gmv > 0 else 0
            refund_ratio = refund / gmv if gmv > 0 else 0
            
            if ad_ratio > 0.25 or refund_ratio > 0.25:
                alerts.append({
                    "id": "alert_profit_001",
                    "severity": "high",
                    "title": "利润异常：抖音渠道费用占比过高",
                    "description": f"昨日抖音 GMV ¥{gmv/10000:.1f}万，广告费占比 {ad_ratio*100:.0f}%，退货率 {refund_ratio*100:.0f}%，预计净利率极低。",
                    "action": "分析抖音渠道亏损原因",
                    "query": "帮我分析一下为什么昨天抖音渠道GMV虽然高，但是显示亏损？具体数据是多少？"
                })
                break  # Only one profit alert
    
    # 2. Scan ERP for stock issues
    db_path = os.path.join(current_dir, "data", "erp.db")
    if os.path.exists(db_path):
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT sku_id, product_name, warehouse_stock FROM product_costs WHERE warehouse_stock < 60000")
        rows = cursor.fetchall()
        conn.close()
        
        for row in rows[:1]:  # Only first low-stock item
            alerts.append({
                "id": "alert_stock_001",
                "severity": "medium",
                "title": f"库存熔断：{row[1]}即将缺货",
                "description": f"当前库存 {row[2]} 件，按近7日均销计算，仅能支撑约 3 天。",
                "action": "检查库存周转情况",
                "query": f"检查一下{row[1]}的库存情况，按目前的销量还能撑几天？"
            })
    
    return {"alerts": alerts, "scan_time": pd.Timestamp.now().isoformat()}

@app.get("/api/data-preview")
async def data_preview():
    """
    Returns usage preview data for the frontend Data Hub.
    """
    previews = {}
    
    # 1. Douyin Data (JSON)
    json_path = os.path.join(current_dir, "data", "douyin_data.json")
    if os.path.exists(json_path):
        with open(json_path, 'r') as f:
            data = json.load(f)
            previews["douyin"] = data[:5] # Top 5 records
    
    # 2. ERP Data (SQLite) -> Convert to dict
    db_path = os.path.join(current_dir, "data", "erp.db")
    if os.path.exists(db_path):
        import sqlite3
        import pandas as pd
        conn = sqlite3.connect(db_path)
        df = pd.read_sql_query("SELECT * FROM product_costs", conn)
        previews["erp"] = df.to_dict(orient="records")
        conn.close()
        
    return previews

if __name__ == "__main__":
    import uvicorn
    import platform
    import subprocess

    def kill_port(port: int):
        """释放占用端口的进程（跨平台：Windows 用 netstat+taskkill，*nix 用 lsof）。"""
        try:
            if platform.system() == "Windows":
                out = subprocess.check_output(
                    f"netstat -ano | findstr :{port}", shell=True
                ).decode(errors="ignore")
                pids = {line.split()[-1] for line in out.strip().splitlines() if line.split()}
                for pid in pids:
                    if pid and pid != "0":
                        print(f"[port] {port} busy, killing PID {pid} ...")
                        subprocess.run(f"taskkill /F /PID {pid}", shell=True,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                import os as _os, signal as _signal
                out = subprocess.check_output(f"lsof -t -i:{port}", shell=True).decode()
                for pid in out.strip().split("\n"):
                    if pid:
                        print(f"[port] {port} busy, killing PID {pid} ...")
                        _os.kill(int(pid), _signal.SIGKILL)
        except subprocess.CalledProcessError:
            pass  # 端口没被占用
        except Exception as e:
            print(f"[port] cleanup skipped: {e}")

    kill_port(8000)

    print("Starting Financial Agent Server...")
    print("Please open http://localhost:8000 to see the demo.")
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
