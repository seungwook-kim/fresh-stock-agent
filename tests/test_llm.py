"""LLM 백엔드 테스트. 실제 API는 부르지 않고 가짜 SDK 클라이언트로 요청 형태와 응답 처리를 확인한다."""

import json
from types import SimpleNamespace

import pytest

from fresh_stock.llm import AnthropicClient, ClaudeCodeClient, ImageInput, LLMError, make_llm

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


def _response(stop_reason="end_turn", text='{"ok": true}'):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, cache_creation_input_tokens=3, cache_read_input_tokens=None, output_tokens=5),
    )


class FakeMessages:
    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _fake_client(response):
    return SimpleNamespace(messages=FakeMessages(response), beta=SimpleNamespace(messages=FakeMessages(response)))


def test_opus_uses_structured_output_and_default_fallback():
    client = _fake_client(_response())
    llm = AnthropicClient(client=client)
    out = llm.generate_json("시스템", "질문", SCHEMA, purpose="t", images=(ImageInput(b"\x89PNG", "image/png"),))
    assert out == {"ok": True}
    kw = client.beta.messages.kwargs
    assert kw["model"] == "claude-opus-5"
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "low"}
    content = kw["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/png"
    assert content[-1] == {"type": "text", "text": "질문"}
    assert llm.calls[0].input_tokens == 13 and llm.calls[0].images == 1


def test_other_models_use_plain_messages_api():
    client = _fake_client(_response())
    AnthropicClient(model="claude-sonnet-5", client=client).generate_json("s", "p", SCHEMA)
    assert client.messages.kwargs["model"] == "claude-sonnet-5"
    assert "fallbacks" not in client.messages.kwargs
    assert client.messages.kwargs["messages"][0]["content"] == "p"


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_refusal_and_truncation_raise(stop_reason):
    llm = AnthropicClient(client=_fake_client(_response(stop_reason=stop_reason)))
    with pytest.raises(LLMError):
        llm.generate_json("s", "p", SCHEMA)
    assert llm.calls[0].error


def test_invalid_json_raises():
    llm = AnthropicClient(client=_fake_client(_response(text="not json")))
    with pytest.raises(LLMError):
        llm.generate_json("s", "p", SCHEMA)


def test_make_llm_backends():
    assert isinstance(make_llm("claude-code", None, "low", None), ClaudeCodeClient)
    assert make_llm("claude-code", None, "low", None).model == "sonnet"
    api = make_llm("anthropic", None, "low", None)
    assert isinstance(api, AnthropicClient) and api.model == "claude-opus-5"
    assert make_llm("anthropic", "sonnet", "low", None).model == "claude-sonnet-5"
    assert make_llm("anthropic", "claude-haiku-4-5", None, None).model == "claude-haiku-4-5"
    with pytest.raises(ValueError):
        make_llm("openai", None, None, None)


def test_claude_code_result_parsing():
    stream = "\n".join(json.dumps(e) for e in [{"type": "system"}, {"type": "result", "structured_output": {"ok": 1}}])
    assert ClaudeCodeClient._result(stream)["structured_output"] == {"ok": 1}
    with pytest.raises(LLMError):
        ClaudeCodeClient._result(json.dumps({"type": "assistant"}))
