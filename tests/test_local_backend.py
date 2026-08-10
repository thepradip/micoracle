"""Tests for the local OpenAI-compatible brain backend (no network — fake client)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import jarvis

TOOLS = [{"name": "browser_goto", "description": "go", "parameters": {"type": "object"}}]


class FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=arguments)


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeClient:
    """Mimics the OpenAI SDK surface _LocalBackend touches."""

    def __init__(self, responses=(), models=("served-model",), fail_on_tools=False):
        self._responses = list(responses)
        self.calls = []
        self.fail_on_tools = fail_on_tools
        self.models = SimpleNamespace(
            list=lambda: [SimpleNamespace(id=m) for m in models]
        )
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_on_tools and "tools" in kwargs:
            raise RuntimeError("this server does not support tools")
        msg = self._responses.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("MICORACLE_PROVIDER", "JARVIS_PROVIDER", "MICORACLE_BASE_URL",
                "MICORACLE_MODEL", "JARVIS_MODEL", "MICORACLE_AGENT_MODEL",
                "MICORACLE_TOOL_STYLE", "MICORACLE_API_KEY",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)


class TestProviderResolution:
    def test_base_url_selects_local(self, monkeypatch):
        monkeypatch.setenv("MICORACLE_BASE_URL", "http://localhost:8080/v1")
        assert jarvis._resolve_provider() == "local"

    def test_base_url_beats_openai_key(self, monkeypatch):
        monkeypatch.setenv("MICORACLE_BASE_URL", "http://localhost:8080/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        assert jarvis._resolve_provider() == "local"

    def test_explicit_local_override(self, monkeypatch):
        monkeypatch.setenv("MICORACLE_PROVIDER", "local")
        assert jarvis._resolve_provider() == "local"

    def test_no_base_url_keeps_cloud_resolution(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        assert jarvis._resolve_provider() == "openai"


class TestModelSelection:
    def test_auto_picks_first_served_model(self):
        backend = jarvis._LocalBackend(client=FakeClient(models=("llama3.3", "phi4")))
        assert backend.model == "llama3.3"

    def test_env_model_wins_over_served(self, monkeypatch):
        monkeypatch.setenv("MICORACLE_MODEL", "qwen2.5")
        backend = jarvis._LocalBackend(client=FakeClient())
        assert backend.model == "qwen2.5"

    def test_no_served_models_is_clear_error(self):
        with pytest.raises(RuntimeError, match="advertises no models"):
            jarvis._LocalBackend(client=FakeClient(models=()))

    def test_unreachable_server_is_clear_error(self):
        client = FakeClient()
        def boom():
            raise ConnectionError("refused")
        client.models = SimpleNamespace(list=boom)
        with pytest.raises(RuntimeError, match="Could not reach"):
            jarvis._LocalBackend(client=client)


class TestNativeTools:
    def test_tool_calls_parsed(self):
        msg = FakeMessage(
            content="opening",
            tool_calls=[FakeToolCall("c1", "browser_goto", '{"url": "https://x.com"}')],
        )
        backend = jarvis._LocalBackend(client=FakeClient([msg]), model="m")
        text, calls = backend.complete_tools(
            "sys", [{"role": "user", "content": "go"}], TOOLS, 100,
        )
        assert text == "opening"
        assert calls == [
            {"id": "c1", "name": "browser_goto", "arguments": {"url": "https://x.com"}}
        ]


class TestJsonFallback:
    _JSON_REPLY = FakeMessage(content=json.dumps({
        "say": "on it",
        "tool_calls": [{"name": "browser_goto", "arguments": {"url": "https://x.com"}}],
    }))

    def test_native_failure_falls_back_to_json(self):
        client = FakeClient([self._JSON_REPLY], fail_on_tools=True)
        backend = jarvis._LocalBackend(client=client, model="m")
        text, calls = backend.complete_tools(
            "sys", [{"role": "user", "content": "go"}], TOOLS, 100,
        )
        assert text == "on it"
        assert calls == [
            {"id": "local_call_1", "name": "browser_goto",
             "arguments": {"url": "https://x.com"}}
        ]
        # second turn goes straight to JSON mode — no tools kwarg again
        client._responses.append(self._JSON_REPLY)
        backend.complete_tools("sys", [{"role": "user", "content": "go"}], TOOLS, 100)
        assert "tools" not in client.calls[-1]

    def test_forced_json_style_never_sends_tools(self, monkeypatch):
        monkeypatch.setenv("MICORACLE_TOOL_STYLE", "json")
        client = FakeClient([self._JSON_REPLY])
        backend = jarvis._LocalBackend(client=client, model="m")
        backend.complete_tools("sys", [{"role": "user", "content": "go"}], TOOLS, 100)
        assert all("tools" not in c for c in client.calls)

    def test_forced_native_style_raises_instead_of_falling_back(self, monkeypatch):
        monkeypatch.setenv("MICORACLE_TOOL_STYLE", "native")
        client = FakeClient(fail_on_tools=True)
        backend = jarvis._LocalBackend(client=client, model="m")
        with pytest.raises(RuntimeError, match="does not support tools"):
            backend.complete_tools("sys", [{"role": "user", "content": "go"}], TOOLS, 100)

    def test_non_json_reply_is_spoken_answer(self, monkeypatch):
        monkeypatch.setenv("MICORACLE_TOOL_STYLE", "json")
        client = FakeClient([FakeMessage(content="just chatting")])
        backend = jarvis._LocalBackend(client=client, model="m")
        text, calls = backend.complete_tools(
            "sys", [{"role": "user", "content": "hi"}], TOOLS, 100,
        )
        assert text == "just chatting"
        assert calls == []


class TestFactories:
    def test_build_backend_local(self, monkeypatch):
        pytest.importorskip("openai")
        monkeypatch.setenv("MICORACLE_PROVIDER", "local")
        monkeypatch.setenv("MICORACLE_MODEL", "llama3.3")
        backend = jarvis._build_backend("local")
        assert backend.name == "local"
        assert backend.model == "llama3.3"
        assert backend.base_url == jarvis.DEFAULT_LOCAL_BASE_URL

    def test_make_tool_backend_local(self, monkeypatch):
        pytest.importorskip("openai")
        monkeypatch.setenv("MICORACLE_BASE_URL", "http://localhost:8080/v1")
        monkeypatch.setenv("MICORACLE_MODEL", "llama3.3")
        backend = jarvis.make_tool_backend()
        assert backend is not None
        assert backend.name == "local"
        assert backend.base_url == "http://localhost:8080/v1"
        assert callable(backend.complete_tools)
