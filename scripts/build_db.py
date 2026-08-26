"""
构建多表 ERP 数据库 (可重复运行)
================================
把原来的单表 3 行 ERP 扩成 3 张表、多行的小型企业库，让 Text2SQL 能真正处理
**多表 JOIN**、并因多表共有 sku_id 而可能触发"ambiguous column"类错误，
从而让"执行反馈自纠环"有机会真实生效。

三张表（都带 sku_id，join 时不加表前缀就会歧义报错）：
- product_costs : 商品成本与库存 (保留原始 3 个 SKU + 新增若干)
- orders        : 订单流水 (渠道 / 日期 / 金额 / 退款)
- suppliers     : 供应商与供货关系 (交货周期)

用法:  python scripts/build_db.py
"""
import os
import sqlite3

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DATA_DIR, "erp.db")

# ---- product_costs：保留原始 3 行，追加 9 行 ----
PRODUCTS = [
    # sku_id,        product_name,                            prod_cost, logi_cost, stock
    ("sku_cat_001", "Deep Sea Fish Oil Cat Food - Classic",   35.0, 5.0, 50000),
    ("sku_cat_002", "Tofu Cat Litter",                        12.0, 8.0, 20000),
    ("sku_toy_003", "Interactive Cat Toy",                    15.0, 3.0, 10000),
    ("sku_cat_004", "Grain-Free Chicken Cat Food",            42.0, 5.0, 18000),
    ("sku_cat_005", "Kitten Milk Replacer",                   28.0, 4.0,  6500),
    ("sku_dog_001", "Beef Jerky Dog Treats",                  22.0, 6.0, 30000),
    ("sku_dog_002", "Large Dog Chew Bone",                    18.0, 7.0,  4200),
    ("sku_dog_003", "Puppy Training Pads (100pcs)",           33.0, 9.0, 15000),
    ("sku_toy_004", "Feather Teaser Wand",                     8.0, 2.0, 25000),
    ("sku_toy_005", "Automatic Laser Toy",                    46.0, 4.0,  3800),
    ("sku_acc_001", "Adjustable Pet Collar",                  11.0, 3.0, 40000),
    ("sku_acc_002", "Stainless Steel Feeding Bowl",           16.0, 4.0,  9000),
]

# ---- suppliers：供应商 + 供货 SKU + 交货周期(天) ----
SUPPLIERS = [
    # supplier_id, supplier_name,     sku_id,        lead_time_days
    ("sup_01", "Oceanic Pet Foods",   "sku_cat_001", 7),
    ("sup_01", "Oceanic Pet Foods",   "sku_cat_004", 9),
    ("sup_02", "GreenPaw Supplies",   "sku_cat_002", 5),
    ("sup_02", "GreenPaw Supplies",   "sku_acc_001", 4),
    ("sup_03", "HappyToy Mfg",        "sku_toy_003", 12),
    ("sup_03", "HappyToy Mfg",        "sku_toy_004", 10),
    ("sup_03", "HappyToy Mfg",        "sku_toy_005", 21),
    ("sup_04", "CanineCare Co",       "sku_dog_001", 8),
    ("sup_04", "CanineCare Co",       "sku_dog_002", 15),
    ("sup_05", "PetEssentials Ltd",   "sku_cat_005", 6),
    ("sup_05", "PetEssentials Ltd",   "sku_dog_003", 11),
    ("sup_05", "PetEssentials Ltd",   "sku_acc_002", 5),
]


def _build_orders():
    """确定性地生成订单流水（不使用随机数，保证可复现）。"""
    channels = ["douyin", "tmall", "xiaohongshu"]
    rows = []
    oid = 1000
    # 为每个 SKU 在 3 个渠道各造 1 单，金额/退款按规则确定性变化
    for idx, (sku, name, pcost, lcost, stock) in enumerate(PRODUCTS):
        for ci, ch in enumerate(channels):
            oid += 1
            qty = 200 + idx * 30 + ci * 50
            unit_price = round(pcost * 2.4, 2)          # 简单定价
            gmv = round(qty * unit_price, 2)
            refund = round(gmv * (0.05 + (idx % 4) * 0.03), 2)  # 5%~14% 退款
            day = 18 + (idx + ci) % 3                    # 2025-10-18/19/20
            rows.append((oid, sku, ch, f"2025-10-{day:02d}", qty, gmv, refund))
    return rows


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.executescript(
        """
        DROP TABLE IF EXISTS product_costs;
        DROP TABLE IF EXISTS orders;
        DROP TABLE IF EXISTS suppliers;

        CREATE TABLE product_costs (
            sku_id TEXT PRIMARY KEY,
            product_name TEXT,
            production_cost REAL,
            logistics_cost REAL,
            warehouse_stock INTEGER
        );
        CREATE TABLE orders (
            order_id INTEGER PRIMARY KEY,
            sku_id TEXT,
            channel TEXT,
            order_date TEXT,
            quantity INTEGER,
            gmv REAL,
            refund_amount REAL
        );
        CREATE TABLE suppliers (
            supplier_id TEXT,
            supplier_name TEXT,
            sku_id TEXT,
            lead_time_days INTEGER
        );
        """
    )

    cur.executemany("INSERT INTO product_costs VALUES (?,?,?,?,?)", PRODUCTS)
    cur.executemany("INSERT INTO suppliers VALUES (?,?,?,?)", SUPPLIERS)
    cur.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", _build_orders())

    conn.commit()
    for t in ("product_costs", "orders", "suppliers"):
        n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<15} {n} 行")
    conn.close()
    print(f"OK: 已生成多表 ERP -> {DB_PATH}")


if __name__ == "__main__":
    main()
