"""Unit tests: the provider abstraction - normalisation, tool schemas, errors, streaming.

No network access: the OpenAI/Anthropic clients are replaced by lightweight fakes
that speak the real SDK object shapes, so message conversion and tool-call
accumulation are exercised for real.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from minicode.providers.base import (
    AssistantMessage,
    AuthenticationError,
    ContextLengthError,
    Provider,
    ProviderAPIError,
    RateLimitError,
    StreamEvent,
    ToolCall,
    Usage,
    format_tool_results,
    tool_schema_to_anthropic,
    tool_schema_to_openai,
)
from minicode.providers.openai_compat import OpenAICompatProvider
from minicode.providers.registry import ProviderRegistry, ProviderSpec, build_registry
from minicode.providers.scripted import ScriptedProvider
from minicode.tools.base import ToolResult

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# schema conversion
# --------------------------------------------------------------------------- #
def test_tool_schema_to_openai():
    schema = {"name": "read", "description": "Read a file", "parameters": {"type": "object", "properties": {}}}
    converted = tool_schema_to_openai(schema)
    assert converted["type"] == "function"
    assert converted["function"]["name"] == "read"


def test_tool_schema_to_anthropic():
    schema = {"name": "read", "description": "Read a file", "parameters": {"type": "object"}}
    converted = tool_schema_to_anthropic(schema)
    assert converted["name"] == "read"
    assert converted["input_schema"] == {"type": "object"}
    assert "type" not in converted


# --------------------------------------------------------------------------- #
# normalized data
# --------------------------------------------------------------------------- #
def test_tool_call_from_mapping_repairs_missing_arguments():
    call = ToolCall.from_mapping({"id": "c1", "name": "read", "raw_arguments": '{"file_path": "a.py"}'})
    assert call.arguments == {"file_path": "a.py"}


def test_tool_call_from_mapping_survives_broken_json():
    call = ToolCall.from_mapping({"id": "c1", "name": "read", "raw_arguments": "{not json"})
    assert call.arguments == {}
    assert call.raw_arguments == "{not json"  # kept so the model can see its own mistake


def test_usage_total():
    usage = Usage(input_tokens=10, output_tokens=5)
    assert usage.total_tokens == 15


def test_assistant_message_to_message_dict_carries_reasoning():
    message = AssistantMessage(content="hi", reasoning="thought", finish_reason="stop")
    data = message.to_message_dict()
    assert data["content"] == "hi"
    assert data["extra"]["reasoning"] == "thought"
    assert data["extra"]["finish_reason"] == "stop"


def test_assistant_message_omits_empty_reasoning():
    assert "reasoning" not in AssistantMessage(content="hi").to_message_dict()["extra"]


# --------------------------------------------------------------------------- #
# tool result formatting
# --------------------------------------------------------------------------- #
def test_format_tool_results_pairs_calls_with_outputs():
    calls = [ToolCall(id="c1", name="read", arguments={"file_path": "a.py"})]
    results = [ToolResult(title="read", output="file body")]
    messages = format_tool_results(calls, results)
    assert messages[0]["role"] == "tool"
    assert messages[0]["tool_call_id"] == "c1"
    assert messages[0]["content"] == "file body"
    assert messages[0]["extra"]["tool_name"] == "read"


def test_format_tool_results_pads_missing_outputs():
    calls = [ToolCall(id="c1", name="read"), ToolCall(id="c2", name="grep")]
    messages = format_tool_results(calls, [ToolResult(title="read", output="ok")])
    assert messages[1]["content"] == "The tool call was not executed."
    assert messages[1]["extra"]["skipped"]


def test_format_tool_results_marks_errors():
    from minicode.tools.base import ToolError

    calls = [ToolCall(id="c1", name="bash")]
    result = ToolResult(title="bash", output="", error=ToolError(code="nonzero_exit", message="boom"))
    message = format_tool_results(calls, [result])[0]
    assert message["extra"]["error"]["code"] == "nonzero_exit"


# --------------------------------------------------------------------------- #
# a fake OpenAI client that mimics the real SDK surface
# --------------------------------------------------------------------------- #
class FakeCompletions:
    def __init__(self, response):
        self.response = response
        self.payloads: list[dict] = []

    def create(self, **payload):
        self.payloads.append(payload)
        return self.response


class FakeClient:
    def __init__(self, response):
        self.chat = SimpleNamespace(completions=FakeCompletions(response))


def _openai_response(*, content="", tool_calls=(), reasoning="", finish_reason="stop"):
    calls = [
        SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=args)) for cid, name, args in tool_calls
    ]
    message = SimpleNamespace(content=content, tool_calls=calls or None, model_extra={})
    if reasoning:
        message.reasoning_content = reasoning
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            prompt_tokens_details=SimpleNamespace(cached_tokens=5),
        ),
        model_dump=lambda: {"choices": []},
    )


def _make_provider(response, **kwargs):
    provider = OpenAICompatProvider(model="fake-model", api_key="k", name="fake", **kwargs)
    provider.client = FakeClient(response)
    return provider


def test_openai_provider_converts_messages_and_tools():
    provider = _make_provider(_openai_response(content="ok"))
    provider.generate(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ],
        [{"name": "read", "description": "d", "parameters": {"type": "object"}}],
    )
    payload = provider.client.chat.completions.payloads[0]
    assert payload["messages"][0] == {"role": "system", "content": "sys"}
    assert payload["tool_choice"] == "auto"
    assert payload["tools"][0]["function"]["name"] == "read"


def test_openai_provider_round_trips_tool_calls_back_into_history():
    provider = _make_provider(_openai_response(content="", tool_calls=[("c1", "read", '{"file_path": "a.py"}')]))
    provider.generate(
        [
            {"role": "assistant", "content": "", "extra": {"tool_calls": []}},
            {"role": "tool", "tool_call_id": "c1", "content": "body", "extra": {}},
        ],
        None,
    )
    payload = provider.client.chat.completions.payloads[0]
    tool_message = [m for m in payload["messages"] if m["role"] == "tool"][0]
    assert tool_message["tool_call_id"] == "c1"
    assert tool_message["content"] == "body"


def test_openai_provider_emits_assistant_tool_calls():
    provider = _make_provider(_openai_response(tool_calls=[("c9", "read", '{"file_path": "a.py"}')]))
    message = provider.generate([{"role": "user", "content": "go"}], None)
    assert message.has_tool_calls
    assert message.tool_calls[0].name == "read"
    assert message.tool_calls[0].arguments == {"file_path": "a.py"}
    assert message.tool_calls[0].raw_arguments == '{"file_path": "a.py"}'


def test_openai_provider_extracts_reasoning():
    provider = _make_provider(_openai_response(content="answer", reasoning="deep thought"))
    message = provider.generate([{"role": "user", "content": "go"}], None)
    assert message.content == "answer"
    assert message.reasoning == "deep thought"


def test_openai_provider_maps_usage():
    provider = _make_provider(_openai_response(content="x"))
    message = provider.generate([{"role": "user", "content": "go"}], None)
    assert message.usage.input_tokens == 100
    assert message.usage.output_tokens == 20
    assert message.usage.cache_read_tokens == 5


def test_streaming_accumulates_fragments():
    deltas = [
        SimpleNamespace(content="Hel", tool_calls=None, model_extra={}),
        SimpleNamespace(content="lo", tool_calls=None, model_extra={}),
        SimpleNamespace(
            content=None,
            model_extra={},
            tool_calls=[
                SimpleNamespace(index=0, id="c1", function=SimpleNamespace(name="read", arguments='{"f')),
                SimpleNamespace(index=0, id=None, function=SimpleNamespace(name=None, arguments='": "a.py"}')),
            ],
        ),
    ]
    chunks = [
        SimpleNamespace(choices=[SimpleNamespace(delta=d, finish_reason=None)], usage=None, model_dump=lambda: {})
        for d in deltas
    ]
    chunks[-1].choices[0].finish_reason = "tool_calls"

    provider = _make_provider(None)
    provider.client.chat.completions.response = iter(chunks)
    events: list[StreamEvent] = []
    message = provider.generate([{"role": "user", "content": "go"}], None, stream=True, on_event=events.append)

    assert message.content == "Hello"
    assert len(message.tool_calls) == 1
    assert message.tool_calls[0].name == "read"
    assert message.tool_calls[0].arguments == {"f": "a.py"}
    assert [e.type for e in events].count("text_delta") == 2
    assert any(e.type == "tool_call_start" for e in events)


def test_streaming_collects_reasoning_separately():
    delta = SimpleNamespace(content=None, tool_calls=None, model_extra={})
    delta.reasoning_content = "thinking..."
    chunk = SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None, model_dump=lambda: {}
    )
    provider = _make_provider(None)
    provider.client.chat.completions.response = iter([chunk])
    events: list[StreamEvent] = []
    message = provider.generate([{"role": "user", "content": "go"}], None, stream=True, on_event=events.append)
    assert message.reasoning == "thinking..."
    assert message.content == ""
    assert [e.type for e in events] == ["reasoning_delta", "usage"]


# --------------------------------------------------------------------------- #
# error mapping
# --------------------------------------------------------------------------- #
def _http_response(status_code: int):
    """A real httpx.Response - the openai SDK requires it to build its errors."""
    import httpx

    request = httpx.Request("POST", "https://api.test/v1/chat/completions")
    return httpx.Response(status_code=status_code, request=request)


def _raise(exc):
    class Boom:
        def create(self, **_):
            raise exc

    provider = OpenAICompatProvider(model="m", api_key="k", name="fake")
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=Boom()))
    provider.options["max_retries"] = 1
    return provider


def test_context_length_errors_are_not_retried(monkeypatch):
    import openai

    provider = _raise(openai.APIStatusError("maximum context length exceeded", response=_http_response(400), body=None))
    provider.options["max_retries"] = 3
    slept = []
    monkeypatch.setattr("time.sleep", lambda *_: slept.append(1))
    with pytest.raises(ContextLengthError):
        provider.generate([{"role": "user", "content": "x"}], None)
    assert not slept  # retrying cannot help; the agent must compact instead


def test_rate_limits_are_retried_then_raised(monkeypatch):
    import openai

    provider = _raise(openai.RateLimitError("slow down", response=_http_response(429), body=None))
    provider.options["max_retries"] = 2
    monkeypatch.setattr("time.sleep", lambda *_: None)
    with pytest.raises(RateLimitError):
        provider.generate([{"role": "user", "content": "x"}], None)


def test_auth_errors_surface_a_useful_message():
    import openai

    provider = _raise(openai.AuthenticationError("nope", response=_http_response(401), body=None))
    with pytest.raises(AuthenticationError) as info:
        provider.generate([{"role": "user", "content": "x"}], None)
    assert "fake" in str(info.value)  # names the offending provider


def test_unknown_errors_become_provider_api_errors():
    provider = _raise(RuntimeError("kaboom"))
    with pytest.raises(ProviderAPIError):
        provider.generate([{"role": "user", "content": "x"}], None)


# --------------------------------------------------------------------------- #
# registry / factory
# --------------------------------------------------------------------------- #
def test_registry_builds_a_provider_from_config():
    registry = build_registry(
        {
            "providers": {
                "sensenova": {
                    "type": "openai_compat",
                    "models": ["deepseek-v4-flash"],
                    "base_url": "https://example.test/v1",
                    "api_key": "sk-test",
                    "kind": "openai_compat",
                }
            }
        }
    )
    provider = registry.create("sensenova")
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.model == "deepseek-v4-flash"
    assert provider.name == "sensenova"


def test_config_type_field_selects_the_wire_protocol():
    """`type:` is the documented key; it must not be silently dropped."""
    registry = build_registry(
        {
            "providers": {
                "anthropic": {"type": "anthropic_compat", "models": ["claude"], "api_key": "k"},
                "openai": {"type": "openai_compat", "models": ["gpt"], "api_key": "k"},
                "implicit": {"models": ["m"], "api_key": "k"},
            }
        }
    )
    assert registry.specs["anthropic"].kind == "anthropic_compat"
    assert registry.specs["openai"].kind == "openai_compat"
    assert registry.specs["implicit"].kind == "openai_compat"  # documented default


def test_registry_builds_the_anthropic_provider_when_configured():
    registry = build_registry(
        {"providers": {"claude": {"type": "anthropic_compat", "models": ["claude-sonnet-4-5"], "api_key": "k"}}}
    )
    from minicode.providers.anthropic_compat import AnthropicCompatProvider

    assert isinstance(registry.create("claude"), AnthropicCompatProvider)


def test_unknown_provider_keys_are_forwarded_not_dropped():
    """Provider-specific knobs (retries, throttling, ...) must reach the provider."""
    spec = ProviderSpec.from_config(
        "x",
        {
            "type": "openai_compat",
            "models": ["m"],
            "max_retries": 6,
            "retry_delay": 10,
            "min_request_interval": 6,
        },
    )
    assert spec.options == {"max_retries": 6, "retry_delay": 10, "min_request_interval": 6}


def test_options_block_merges_with_inline_keys():
    spec = ProviderSpec.from_config("x", {"models": ["m"], "include_usage": False, "options": {"max_retries": 2}})
    assert spec.options == {"include_usage": False, "max_retries": 2}


def test_registry_lists_models():
    registry = ProviderRegistry(
        {
            "a": {"type": "scripted", "models": ["m1"]},
            "b": {"type": "scripted", "models": ["m2"]},
        }
    )
    assert set(registry.provider_names()) == {"a", "b"}
    assert registry.list_models() == ["a/m1", "b/m2"]


def test_registry_unknown_provider_lists_available():
    registry = ProviderRegistry({"a": {"type": "scripted", "models": ["m1"]}})
    with pytest.raises(KeyError, match="a"):
        registry.create("nope")


def test_in_session_model_switching():
    """``/model`` is a pure registry operation: same process, different provider."""
    registry = ProviderRegistry(
        {
            "a": {"type": "scripted", "models": ["m1"]},
            "b": {"type": "scripted", "models": ["m2"]},
        },
        default_provider="a",
    )
    assert registry.create().model_id == "a/m1"
    switched = registry.set_default("b", "m2")
    assert switched.model_id == "b/m2"
    assert registry.create().model_id == "b/m2"


def test_registry_resolves_provider_slash_model_and_bare_model():
    registry = ProviderRegistry(
        {
            "a": {"type": "scripted", "models": ["m1"]},
            "b": {"type": "scripted", "models": ["m2"]},
        },
        default_provider="a",
    )
    assert registry.get("b/m2").model_id == "b/m2"
    assert registry.get("m2").model_id == "b/m2"  # bare model name is searched for
    assert registry.get("b").model_id == "b/m2"


def test_providers_are_cached_per_provider_model_pair():
    registry = ProviderRegistry({"a": {"type": "scripted", "models": ["m1"]}})
    assert registry.create("a", "m1") is registry.create("a", "m1")


def test_registry_reports_missing_api_keys():
    registry = ProviderRegistry({"local": {"type": "scripted", "models": ["m"]}})
    assert registry.available_specs() == []
    with pytest.raises(RuntimeError, match="No provider has an API key"):
        registry.first_available()


# --------------------------------------------------------------------------- #
# the provider protocol itself
# --------------------------------------------------------------------------- #
def test_provider_is_abstract():
    with pytest.raises(TypeError):
        Provider(model="x")  # type: ignore[abstract]


def test_mini_swe_agent_model_protocol_is_implemented():
    provider = ScriptedProvider(responses=["hello"])
    out = provider.query([{"role": "user", "content": "hi"}])
    assert out["role"] == "assistant"
    assert out["content"] == "hello"


def test_max_tokens_override_reaches_the_payload():
    provider = _make_provider(_openai_response(content="x"))
    provider.generate([{"role": "user", "content": "hi"}], None, max_tokens=4321)
    assert provider.client.chat.completions.payloads[0]["max_tokens"] == 4321
