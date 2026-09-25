"""LLM 客户端：MiniMax 国内版。解析层与解释层共用。

**只做两件事**：把自然语言变成 JSON（parser）、把结构化结果润色成人话（explain）。
中间的打分一律本地算 —— 见 engine.py 的铁律。

踩过的坑，别再踩：

1. **失败也返回 HTTP 200**。MiniMax 在鉴权失败、余额不足、参数非法时都回 200，
   真正的错误码在 body 的 `base_resp.status_code` 里（0 才是成功）。只判断
   `response.ok` 会把错误当成功，拿到空内容还不知道为什么。
2. **国内版域名是 api.minimaxi.com**（minimax + i），海外版是 api.minimax.io。
   写混了会 404/401 而不报"域名错"，很难查。
3. **temperature 不接受 0**（要求 (0,1]），传 0 会被拒。
4. 响应里可能带 thinking 内容，多轮时要保留完整 assistant 消息 —— 本项目单轮调用，
   但解析时仍要能容忍正文前后混入 thinking 段。
5. **密钥不在 config.json，在 `.env`**。`from_dict()` 只读非敏感参数（域名/模型/温度/超时），
   `api_key` 由 `config.load_api_key()` 从环境变量 `MINIMAX_API_KEY` 注入。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

#: 国内版（用户明确指定，不要改成 .io）
DEFAULT_BASE_URL = "https://api.minimaxi.com/v1"
DEFAULT_MODEL = "MiniMax-M3"

#: 剥 thinking 段。**两种闭合都要认**：
#:   1. ASCII `</think>` / `</thinking>`
#:   2. MiniMax 原生 `<｜end▁of▁thinking｜>`
#: 只写第 1 种的话，MiniMax 的输出整段留下（闭合标签匹配不上），extract_json 就会
#: 从思考正文里的 `{` 一直抓到真 JSON 的 `}`，区间抓错、直接解析失败（实测踩过）。
_BAR = "\uff5c"        # ｜ 全角竖线
_SEP = "\u2581"        # ▁ 下八分之一块
_THINK_OPEN = f"(?:<think(?:ing)?>|<{_BAR}begin{_SEP}of{_SEP}thinking{_BAR}>)"
_THINK_CLOSE = f"(?:</think(?:ing)?>|<{_BAR}end{_SEP}of{_SEP}thinking{_BAR}>)"
_THINK_RE = re.compile(f"{_THINK_OPEN}.*?(?:{_THINK_CLOSE}|$)", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class LLMError(RuntimeError):
    """LLM 调用失败。上层必须能降级（解析层退回别名表 + 正则）。"""


@dataclass
class LLMConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    temperature: float = 0.2
    timeout: float = 30.0
    max_retries: int = 2

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "LLMConfig":
        """只认非敏感参数。`api_key` 不从这里读 —— 那是 `.env` 的事（见 config.load_api_key）。"""
        d = data or {}
        try:
            temp = float(d.get("temperature", 0.2))
        except (TypeError, ValueError):
            temp = 0.2
        # MiniMax 拒绝 temperature=0，夹到 (0, 1]
        temp = min(1.0, max(0.01, temp))
        return cls(
            base_url=str(d.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
            model=str(d.get("model") or DEFAULT_MODEL),
            temperature=temp,
            timeout=float(d.get("timeout_seconds", 30)),
            max_retries=int(d.get("max_retries", 2)),
        )


class LLMClient:
    def __init__(self, cfg: LLMConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self._session = session or requests.Session()

    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.cfg.api_key)

    def chat(self, system: str, user: str, model: str | None = None) -> str:
        """一次单轮对话，返回正文（已剥掉 thinking 段）。"""
        if not self.configured:
            raise LLMError("未配置 MiniMax API key：请在项目根目录的 .env 里设置 MINIMAX_API_KEY")

        payload = {
            "model": model or self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature,
            "stream": False,
        }
        url = f"{self.cfg.base_url}/chat/completions"
        last_err: Exception | None = None

        for attempt in range(self.cfg.max_retries + 1):
            try:
                resp = self._session.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self.cfg.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.cfg.timeout,
                )
                return self._extract(resp)
            except LLMError:
                raise
            except Exception as e:  # 网络类错误才重试
                last_err = e
                if attempt < self.cfg.max_retries:
                    time.sleep(1.5 * (attempt + 1))
        raise LLMError(f"MiniMax 调用失败（{self.cfg.max_retries + 1} 次尝试）：{last_err}")

    # ------------------------------------------------------------------
    @staticmethod
    def _extract(resp: requests.Response) -> str:
        """从响应里取正文。**必须先查业务错误码，再看 HTTP 状态。**"""
        try:
            body = resp.json()
        except ValueError:
            raise LLMError(f"MiniMax 返回非 JSON（HTTP {resp.status_code}）：{resp.text[:200]}")

        # 坑 1：失败也是 200，错误码在 base_resp 里
        base_resp = body.get("base_resp") or {}
        code = base_resp.get("status_code")
        if code not in (None, 0):
            msg = base_resp.get("status_msg") or "unknown"
            raise LLMError(f"MiniMax 业务错误 status_code={code}: {msg}")

        if resp.status_code != 200:
            raise LLMError(f"MiniMax HTTP {resp.status_code}: {str(body)[:200]}")

        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"MiniMax 响应无 choices：{str(body)[:200]}")
        content = (choices[0].get("message") or {}).get("content")
        if content is None:
            raise LLMError(f"MiniMax 响应无 content：{str(body)[:200]}")
        return strip_thinking(content)

    def chat_json(self, system: str, user: str, model: str | None = None) -> dict:
        """要 JSON 的调用：剥 thinking → 取 code fence → json.loads。"""
        raw = self.chat(system, user, model)
        return extract_json(raw)


def strip_thinking(text: str) -> str:
    """剥掉 ` thinking...<｜end▁of▁thinking｜>` 段（模型把推理过程写在正文里）。"""
    return _THINK_RE.sub("", text or "").strip()


def extract_json(text: str) -> dict:
    """从模型输出里抠出 JSON。容忍三类常见写法：
    1. 直接是 JSON
    2. 包在 ```json ``` 里
    3. 前后有解释文字 —— 取第一个 `{` 到最后一个 `}`
    """
    if not text:
        raise LLMError("模型返回空内容")
    cleaned = strip_thinking(text)

    fence = _FENCE_RE.search(cleaned)
    if fence:
        cleaned = fence.group(1).strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass

    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(cleaned[start : end + 1])
            if isinstance(data, dict):
                return data
        except ValueError as e:
            raise LLMError(f"模型输出不是合法 JSON：{e}；原文前 200 字：{cleaned[:200]}")
    raise LLMError(f"模型输出里找不到 JSON：{cleaned[:200]}")
