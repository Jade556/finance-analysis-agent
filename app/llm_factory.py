"""
LLM 工厂
========
统一从环境变量构造 ChatOpenAI，避免 graph.py 和 text2sql.py 各写一份配置。
支持任意 OpenAI 兼容网关（OpenRouter / DeepSeek / 通义 / 本地 vLLM 等）。
"""
import os
from langchain_openai import ChatOpenAI
from dotenv import load_dotenv

load_dotenv()


def get_llm(temperature: float = 0.0, timeout: int = 120) -> ChatOpenAI:
    """按环境变量返回一个 ChatOpenAI 实例。

    读取的环境变量：
        model_name          模型名，默认 gpt-4o
        openrouter_api_key  API Key
        OPENAI_API_BASE     兼容网关地址，默认 OpenRouter
    """
    return ChatOpenAI(
        model=os.getenv("model_name", "gpt-4o"),
        temperature=temperature,
        api_key=os.getenv("openrouter_api_key"),
        base_url=os.getenv("OPENAI_API_BASE", "https://openrouter.ai/api/v1"),
        request_timeout=timeout,
    )
