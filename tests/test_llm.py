"""LLM 接入层单测：降级是一等公民，且全程不联网。

这里用 monkeypatch 顶掉 urlopen，验证的是"模型不可用时系统必须照常工作"，
以及"任何一次模型故障都记埋点"。测试里绝不出现真实 Key 或真实请求。
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from app.agent import llm as llm_mod
from app.agent.llm import LlmClient, _parse_json


class _Resp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _completion(content: str) -> _Resp:
    return _Resp({"choices": [{"message": {"content": content}}]})


@pytest.fixture
def patch_urlopen(monkeypatch):
    def _patch(handler):
        calls: list[dict] = []

        def fake(req, timeout=None):
            calls.append({"url": req.full_url, "body": json.loads(req.data.decode("utf-8")),
                          "auth": req.get_header("Authorization"), "timeout": timeout})
            return handler(req)

        monkeypatch.setattr(llm_mod.urllib.request, "urlopen", fake)
        return calls

    return _patch


class TestAvailability:
    def test_without_key_the_client_is_explicitly_unavailable(self, settings):
        settings.LLM_API_KEY = ""
        c = LlmClient()
        assert c.available() is False

    def test_chat_json_short_circuits_to_rule_engine(self, settings, no_network):
        """没 Key 时连 socket 都不碰，直接给出"走规则引擎"的埋点。"""
        settings.LLM_API_KEY = ""
        obj, meta = LlmClient().chat_json("sys", "user", purpose="plan")
        assert obj is None
        assert meta["outcome"] == "unavailable" and meta["attempts"] == 0
        assert "未配置 LLM_API_KEY" in meta["reason"]
        assert no_network is not None

    def test_missing_base_url_also_disables(self, settings):
        settings.LLM_API_KEY = "sk-test"
        settings.LLM_BASE_URL = ""
        assert LlmClient().available() is False

    def test_settings_are_read_per_call_not_cached(self, settings, monkeypatch):
        """LLMClient 每次新建都读当前 settings —— 测试里 monkeypatch 才生效。"""
        settings.LLM_API_KEY = ""
        assert LlmClient().available() is False
        settings.LLM_API_KEY = "sk-test"
        assert LlmClient().available() is True

    def test_counters_start_at_zero(self):
        c = LlmClient()
        assert c.calls == 0 and c.latencies_ms == [] and c.failures == 0


class TestRequestShape:
    def test_payload_and_headers(self, settings, patch_urlopen):
        settings.LLM_API_KEY = "sk-test-123456"
        settings.LLM_BASE_URL = "https://gateway.local/v1/"
        settings.LLM_MODEL = "unit-model"
        settings.LLM_TIMEOUT_SEC = 3.5
        calls = patch_urlopen(lambda req: _completion('{"steps": []}'))
        LlmClient().chat_json("S", "U", purpose="plan", temperature=0.1)
        call = calls[0]
        assert call["url"] == "https://gateway.local/v1/chat/completions"
        assert call["auth"] == "Bearer sk-test-123456"
        assert call["timeout"] == 3.5
        assert call["body"]["model"] == "unit-model"
        assert call["body"]["temperature"] == 0.1
        assert call["body"]["response_format"] == {"type": "json_object"}
        assert [m["role"] for m in call["body"]["messages"]] == ["system", "user"]

    def test_trailing_slash_is_normalised(self, settings, patch_urlopen):
        settings.LLM_API_KEY = "sk-x"
        settings.LLM_BASE_URL = "https://a.local/v1//"
        calls = patch_urlopen(lambda req: _completion("{}"))
        LlmClient().chat_json("S", "U")
        assert calls[0]["url"] == "https://a.local/v1/chat/completions"

    def test_success_records_model_and_latency(self, settings, patch_urlopen):
        settings.LLM_API_KEY = "sk-x"
        patch_urlopen(lambda req: _completion('{"answer": "好"}'))
        obj, meta = LlmClient().chat_json("S", "U", purpose="answer:diagnose")
        assert obj == {"answer": "好"}
        assert meta["outcome"] == "ok" and meta["model"] and meta["ms"] is not None


def _raiser(exc: BaseException):
    def _handler(_req):
        raise exc

    return _handler


class TestDegradation:
    def test_network_error_is_retried_then_degrades(self, settings, patch_urlopen, monkeypatch):
        settings.LLM_API_KEY = "sk-x"
        settings.LLM_MAX_RETRY = 1
        monkeypatch.setattr(llm_mod.time, "sleep", lambda _s: None)
        calls = patch_urlopen(_raiser(urllib.error.URLError("boom")))
        obj, meta = LlmClient().chat_json("S", "U")
        assert obj is None
        assert meta["outcome"] == "failed" and meta["attempts"] == 2
        assert len(calls) == 2, "重试有上限，不能无限打下去"

    def test_timeout_error_also_degrades(self, settings, patch_urlopen, monkeypatch):
        settings.LLM_API_KEY = "sk-x"
        settings.LLM_MAX_RETRY = 0
        monkeypatch.setattr(llm_mod.time, "sleep", lambda _s: None)
        patch_urlopen(_raiser(TimeoutError("timed out")))
        obj, meta = LlmClient().chat_json("S", "U")
        assert obj is None and meta["outcome"] == "failed"

    def test_malformed_response_body_degrades(self, settings, patch_urlopen, monkeypatch):
        settings.LLM_API_KEY = "sk-x"
        settings.LLM_MAX_RETRY = 0
        monkeypatch.setattr(llm_mod.time, "sleep", lambda _s: None)
        patch_urlopen(lambda req: _Resp({"nope": 1}))
        obj, meta = LlmClient().chat_json("S", "U")
        assert obj is None and meta["outcome"] == "failed"

    def test_non_json_content_is_not_used_as_answer(self, settings, patch_urlopen, monkeypatch):
        """绝不让模型的自然语言直接进结论——解析失败就等于不可用。"""
        settings.LLM_API_KEY = "sk-x"
        settings.LLM_MAX_RETRY = 0
        monkeypatch.setattr(llm_mod.time, "sleep", lambda _s: None)
        patch_urlopen(lambda req: _completion("根据分析，建议立即扩容三倍。"))
        obj, meta = LlmClient().chat_json("S", "U")
        assert obj is None and meta["outcome"] == "failed"
        assert "不是合法 JSON" in meta["reason"]

    def test_failure_counter_is_exposed_for_observability(self, settings, patch_urlopen, monkeypatch):
        settings.LLM_API_KEY = "sk-x"
        settings.LLM_MAX_RETRY = 0
        monkeypatch.setattr(llm_mod.time, "sleep", lambda _s: None)
        patch_urlopen(_raiser(urllib.error.URLError("x")))
        c = LlmClient()
        c.chat_json("S", "U")
        assert c.failures >= 1


class TestJsonParsing:
    def test_plain_json(self):
        assert _parse_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fenced_without_language_tag(self):
        assert _parse_json('```\n{"a": [1, 2]}\n```') == {"a": [1, 2]}

    def test_json_wrapped_in_prose(self):
        assert _parse_json('好的，计划如下：{"steps": [{"tool": "kb_search"}]} 以上。') == \
            {"steps": [{"tool": "kb_search"}]}

    def test_nested_braces_are_balanced(self):
        assert _parse_json('前缀 {"a": {"b": {"c": 1}}} 后缀') == {"a": {"b": {"c": 1}}}

    @pytest.mark.parametrize("content", ["", "纯文字", '{"unclosed": ', "[1,2] trailing"])
    def test_unparseable_returns_none(self, content):
        assert _parse_json(content) is None

    def test_top_level_array_is_accepted_when_bare(self):
        assert _parse_json('[{"tool": "x"}]') == [{"tool": "x"}]
