import sqlite3
import json
import pandas as pd
import os

# Connect to ERP Database
db_path = "data/erp.db"
if os.path.exists(db_path):
    print(f"\n======== [Internal ERP System] (SQLite: {db_path}) ========")
    conn = sqlite3.connect(db_path)
    df_erp = pd.read_sql_query("SELECT * FROM product_costs", conn)
    print(df_erp.to_markdown(index=False))
    conn.close()
else:
    print(f"ERP Database not found at {db_path}")

# Read Douyin Data
json_path = "data/douyin_data.json"
if os.path.exists(json_path):
    print(f"\n======== [External Channel: Douyin] (JSON: {json_path}) ========")
    with open(json_path, 'r') as f:
        data = json.load(f)
    if data:
        # Show first record detailed structure
        print("Sample Record Structure (JSON):")
        print(json.dumps(data[0], indent=2, ensure_ascii=False))
        print(f"\nTotal Records: {len(data)}")
else:
    print(f"Douyin Data not found at {json_path}")
