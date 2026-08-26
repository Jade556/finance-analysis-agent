"""
异构脏数据接入与清洗 (Ingestion & Cleaning)
==========================================
第三个渠道「小红书」是以**脏 CSV** 交付的（真实业务里第三方对接经常如此）：
表头带空格、日期三四种写法、金额带 ¥ 和千分位、缺失值用 N/A/-/空 混着来、
还有整行重复。这个模块把它清洗成和抖音/天猫一致的规范结构，
让 Agent 能把三个异构源放在同一口径下做跨源分析。

面试点：这一层证明你处理的是"异构 + 脏"数据，而不是喂给你的干净 JSON。
清洗步骤都做了显式记录（clean_report），可解释、可回溯。
"""
from __future__ import annotations

import os
import re

import pandas as pd

from app.resilience import resilient

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
RAW_CSV = os.path.join(DATA_DIR, "xiaohongshu_raw.csv")

# 脏表头 -> 规范字段名
_HEADER_MAP = {
    "日期": "date",
    "sku_id": "sku_id",
    "gmv(元)": "gmv",
    "订单数": "order_count",
    "退款金额": "refund_amount",
    "渠道备注": "note",
}

_MISSING_TOKENS = {"", "n/a", "na", "-", "nan", "null", "none"}


def _norm_header(h: str) -> str:
    return _HEADER_MAP.get(h.strip().lower(), h.strip().lower())


def _parse_date(v: str) -> str | None:
    """把 2025/10/20、2025-10-20、2025.10.20、20251020 统一成 YYYY-MM-DD。"""
    s = str(v).strip()
    digits = re.sub(r"\D", "", s)
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def _parse_money(v) -> float | None:
    """剥掉 ¥、空格、千分位逗号；缺失记号 -> None。"""
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in _MISSING_TOKENS:
        return None
    s = re.sub(r"[¥,\s元]", "", s)
    try:
        return float(s)
    except ValueError:
        return None


@resilient(retries=1, timeout=15, cache_ttl=60)
def load_and_clean(path: str = RAW_CSV) -> dict:
    """读取脏 CSV -> 规范化 records + 一份清洗报告。"""
    if not os.path.exists(path):
        return {"ok": False, "error": f"文件不存在: {path}"}

    # 用 python 引擎 + 容错读取：真实脏 CSV 偶有列数不齐的坏行，
    # 直接跳过并计数，而不是让整个管道崩掉。
    raw_lines = sum(1 for _ in open(path, encoding="utf-8")) - 1  # 减表头
    df = pd.read_csv(path, dtype=str, keep_default_na=False,
                     engine="python", on_bad_lines="skip")
    rows_in = len(df)
    bad_lines_skipped = max(0, raw_lines - rows_in)

    # 1) 表头规范化
    df.columns = [_norm_header(c) for c in df.columns]

    # 2) 逐列清洗
    df["date"] = df["date"].map(_parse_date)
    df["sku_id"] = df["sku_id"].map(lambda x: str(x).strip().lower())
    for col in ("gmv", "order_count", "refund_amount"):
        if col in df.columns:
            df[col] = df[col].map(_parse_money)

    # 3) 去重（整行完全相同）
    before_dedup = len(df)
    df = df.drop_duplicates()
    dropped_dups = before_dedup - len(df)

    # 4) 丢掉关键字段（日期 / sku / gmv）缺失的行，并计数
    key_missing_mask = df["date"].isna() | (df["sku_id"] == "") | df["gmv"].isna()
    dropped_missing = int(key_missing_mask.sum())
    clean_df = df.loc[~key_missing_mask].copy()

    # 5) 退款缺失按 0 兜底（业务默认无退款），并标记补齐了几处
    refund_filled = int(clean_df["refund_amount"].isna().sum())
    clean_df["refund_amount"] = clean_df["refund_amount"].fillna(0.0)
    clean_df["order_count"] = clean_df["order_count"].fillna(0).astype(int)

    records = clean_df.to_dict(orient="records")
    report = {
        "rows_in": rows_in,
        "rows_out": len(records),
        "bad_lines_skipped": bad_lines_skipped,
        "dropped_duplicates": dropped_dups,
        "dropped_key_missing": dropped_missing,
        "refund_filled_zero": refund_filled,
    }
    return {"ok": True, "records": records, "clean_report": report}
