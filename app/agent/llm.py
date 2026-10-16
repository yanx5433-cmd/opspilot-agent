"""LLM 接入层：OpenAI 兼容 chat/completions，纯标准库实现（无第三方依赖）。

三个工程取舍：
1. **降级是一等公民**。available()=False 或调用失败/超时/JSON 解析失败，一律返回 None，
   由调用方回落到规则引擎。任何一次 LLM 故障都不能让巡检任务失败——
   这是无人值守场景的硬要求，不是"优雅一点"。
2. **只信 JSON**。所有 LLM 调用都要求返回 JSON，解析失败即视为不可用；
   绝不让模型的自然语言直接进答复（防止幻觉混入结论）。
3. **重试有上限，且有埋点**。每次调用都计数、计时、留 outcome，供预算与评测使用。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from ..config import get_settings

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", re.S)


class LlmClient:
    """按调用新建（settings 可能在测试里被 monkeypatch，不做进程级缓存）。"""

    def __init__(self) -> None:
        s = get_settings()
        self.api_key = s.LLM_API_KEY
        self.base_url = (s.LLM_BASE_URL or "").rstrip("/")
        self.model = s.LLM_MODEL
        self.timeout = s.LLM_TIMEOUT_SEC
        self.max_retry = s.LLM_MAX_RETRY
        self.calls = 0
        self.latencies_ms: list[int] = []
        self.failures = 0

    def available(self) -> bool:
        return bool(self.api_key) and bool(self.base_url)

    # ------------------------------------------------------------------
    def _post(self, payload: dict[str, Any]) -> str | None:
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            self.latencies_ms.append(int((time.monotonic() - t0) * 1000))
            return content
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError):
            self.failures += 1
            return None

    # ------------------------------------------------------------------
    def chat_json(
        self,
        system: str,
        user: str,
        purpose: str = "",
        temperature: float = 0.2,
    ) -> tuple[Any | None, dict[str, Any]]:
        """要求模型返回 JSON 并解析。返回 (对象或 None, 埋点信息)。

        埋点信息一定非空：调用成功/失败、次数、耗时、失败原因，
        评测集靠它区分"LLM 判对"和"LLM 挂了所以规则兜底"。
        """
        meta: dict[str, Any] = {"purpose": purpose, "outcome": "unavailable", "attempts": 0}
        if not self.available():
            meta["reason"] = "未配置 LLM_API_KEY，走规则引擎"
            return None, meta

        payload = {
            "model": self.model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        last_err = ""
        for attempt in range(1, self.max_retry + 2):
            meta["attempts"] = attempt
            content = self._post(payload)
            if content is None:
                last_err = "请求失败或超时"
                time.sleep(min(0.5 * attempt, 2.0))
                continue
            self.calls += 1
            parsed = _parse_json(content)
            if parsed is not None:
                meta.update(outcome="ok", model=self.model, ms=self.latencies_ms[-1] if self.latencies_ms else None)
                return parsed, meta
            last_err = "返回内容不是合法 JSON"
        meta.update(outcome="failed", reason=last_err)
        self.failures += 1
        return None, meta


def _parse_json(content: str) -> Any | None:
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE.search(content)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
    # 容忍模型在 JSON 前后多说两句：抓第一个平衡的花括号块
    start = content.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(content)):
            if content[i] == "{":
                depth += 1
            elif content[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(content[start : i + 1])
                    except json.JSONDecodeError:
                        return None
    return None
