"""
工具韧性层 (Resilience Layer)
=============================
给 Agent 的工具函数统一加上"重试 / 超时 / 结果缓存"三件套。

为什么需要它：
- Agent 在一轮对话里可能对同一工具反复调用（比如自纠时重算利润），
  同参数结果做 TTL 缓存能省掉重复 IO / LLM 开销；
- 外部数据源（DB / 文件 / 第三方 API）会偶发抖动，短重试能显著降低
  "一次失败就打断整条推理链"的概率；
- 单个工具卡死会拖垮整条 SSE 流，超时兜底能快速失败、把错误交回给
  reasoner 节点自己决定要不要换条路。

面试点：这套东西本质是把后端「稳定性工程」搬到 Agent 工具层——
重试要幂等、超时要可中断、缓存要有 TTL 且能按 key 命中。
"""
from __future__ import annotations

import functools
import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable

# 一个进程级共享线程池，用于给同步工具函数套超时（跨平台，不依赖 signal）
_EXECUTOR = ThreadPoolExecutor(max_workers=8)

# 极简 TTL 缓存： key -> (到期时间戳, 结果)
_CACHE: dict[str, tuple[float, Any]] = {}


def _make_key(func_name: str, args: tuple, kwargs: dict) -> str:
    """用函数名 + 入参生成缓存 key；入参不可序列化时降级为 repr。"""
    try:
        payload = json.dumps({"a": args, "k": kwargs}, sort_keys=True, default=str)
    except TypeError:
        payload = repr((args, kwargs))
    return f"{func_name}::{payload}"


def resilient(
    retries: int = 2,
    backoff: float = 0.4,
    timeout: float | None = 30.0,
    cache_ttl: float = 0.0,
) -> Callable:
    """
    Args:
        retries:   失败后的最大重试次数（不含首次）。
        backoff:   指数退避基数，第 n 次重试等待 backoff * 2**(n-1) 秒。
        timeout:   单次调用超时秒数；None 表示不限。
        cache_ttl: 结果缓存有效期（秒）；0 表示不缓存。
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # 1) 缓存命中直接返回
            key = _make_key(func.__name__, args, kwargs)
            now = time.time()
            if cache_ttl > 0 and key in _CACHE:
                expire_at, value = _CACHE[key]
                if now < expire_at:
                    return value
                _CACHE.pop(key, None)  # 过期清理

            # 2) 带超时 + 重试地执行
            last_err: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    if timeout is None:
                        result = func(*args, **kwargs)
                    else:
                        fut = _EXECUTOR.submit(func, *args, **kwargs)
                        result = fut.result(timeout=timeout)
                    # 3) 写缓存
                    if cache_ttl > 0:
                        _CACHE[key] = (now + cache_ttl, result)
                    return result
                except FutureTimeout as e:
                    last_err = TimeoutError(
                        f"{func.__name__} 超过 {timeout}s 超时"
                    )
                    print(f"[resilient][timeout] {func.__name__} "
                          f"(attempt {attempt + 1}/{retries + 1})")
                except Exception as e:  # noqa: BLE001 - 工具层需兜底所有异常
                    last_err = e
                    print(f"[resilient][retry] {func.__name__}: {e} "
                          f"(attempt {attempt + 1}/{retries + 1})")

                if attempt < retries:
                    time.sleep(backoff * (2 ** attempt))

            # 4) 重试耗尽：不抛出，返回结构化错误，交回给 reasoner 决策
            return {"error": f"{func.__name__} 调用失败: {last_err}"}

        return wrapper

    return decorator


def cache_stats() -> dict:
    """给 /api/health 或调试用的缓存概览。"""
    now = time.time()
    live = sum(1 for exp, _ in _CACHE.values() if exp > now)
    return {"cached_keys": len(_CACHE), "live_keys": live}
