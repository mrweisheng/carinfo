"""搜索模块的配置读取。

分工跟项目其余部分一致：

- **非敏感的调参**（域名、模型名、temperature、超时）→ `config.json`
- **凭证** → `.env` / 进程环境变量，**config.json 里不放任何 key**

`MINIMAX_API_KEY` 缺失时不报错：解析层自动降级为规则解析（别名表 + 正则），功能不瘫。
"""

from __future__ import annotations

import json
import os
from typing import Any

from carinfo.search.llm import LLMConfig

CONFIG_FILE = "config.json"
ENV_KEY = "MINIMAX_API_KEY"

_dotenv_loaded = False


def load_dotenv_once(force: bool = False) -> None:
    """把 cwd 及上层目录的 `.env` 灌进 `os.environ`。幂等，已存在的环境变量优先。

    允许 python-dotenv 缺席（只靠系统环境变量也能跑）。
    """
    global _dotenv_loaded
    if _dotenv_loaded and not force:
        return
    _dotenv_loaded = True
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def load_raw(path: str | None = None) -> dict[str, Any]:
    path = path or os.environ.get("CARINFO_CONFIG") or CONFIG_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} 不是合法 JSON：{e}") from e


def load_api_key() -> str:
    """唯一的密钥来源：环境变量 / `.env`。config.json 不参与。"""
    load_dotenv_once()
    return (os.environ.get(ENV_KEY) or "").strip()


def load_llm_config(path: str | None = None) -> LLMConfig:
    cfg = LLMConfig.from_dict(load_raw(path).get("llm"))
    cfg.api_key = load_api_key()
    return cfg


def load_search_config(path: str | None = None) -> dict[str, Any]:
    """只返回**真有人读**的键。条数口径由 spec.DEFAULT_LIMIT / MAX_LIMIT 单点定义，
    不在这里再开一套（曾经有过 default_limit/max_limit，全项目零消费，已删）。"""
    s = load_raw(path).get("search") or {}
    return {
        "use_llm": bool(s.get("use_llm", True)),
        "polish_summary": bool(s.get("polish_summary", False)),
    }


# 导入即加载 .env —— api.py 直接读 os.environ 拿 DB 凭证，不能等到第一次调 load_llm_config 才灌
load_dotenv_once()


__all__ = [
    "CONFIG_FILE",
    "ENV_KEY",
    "load_api_key",
    "load_dotenv_once",
    "load_raw",
    "load_llm_config",
    "load_search_config",
]
