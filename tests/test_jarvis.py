"""Tests for the Jarvis conversational agent (no network — fake backend)."""

from __future__ import annotations

import jarvis


class FakeBackend:
    """Records calls and returns scripted replies (provider-agnostic shape)."""

    name = "fake"
    model = "fake-model"

    def __init__(self, replies=None, raise_exc=None):
        self.replies = list(replies or ["ok"])
        self.raise_exc = raise_exc
        self.calls = []

    def complete(self, system, messages, max_tokens):
        self.calls.append({"system": system, "messages": list(messages), "max_tokens": max_tokens})
        if self.raise_exc:
            raise self.raise_exc
        return self.replies.pop(0) if self.replies else "ok"


class TestAsk:
    def test_returns_reply_and_keeps_history(self):
        agent = jarvis.JarvisAgent(backend=FakeBackend(["Hello there."]))
        assert agent.ask("hi") == "Hello there."
        assert agent.messages == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "Hello there."},
        ]

    def test_multi_turn_accumulates_and_sends_history(self):
        backend = FakeBackend(["one", "two"])
        agent = jarvis.JarvisAgent(backend=backend)
        agent.ask("first")
        agent.ask("second")
        assert len(agent.messages) == 4
        # second call included the prior turns
        assert backend.calls[1]["messages"][0] == {"role": "user", "content": "first"}

    def test_system_prompt_passed(self):
        backend = FakeBackend(["hi"])
        jarvis.JarvisAgent(backend=backend).ask("yo")
        assert "MicOracle" in backend.calls[0]["system"]

    def test_error_rolls_back_history(self):
        agent = jarvis.JarvisAgent(backend=FakeBackend(raise_exc=RuntimeError("boom")))
        reply = agent.ask("hi")
        assert "error" in reply.lower()
        assert agent.messages == []

    def test_empty_reply_rolls_back(self):
        agent = jarvis.JarvisAgent(backend=FakeBackend([""]))
        reply = agent.ask("hi")
        assert "didn't get a response" in reply.lower()
        assert agent.messages == []

    def test_history_is_capped(self):
        agent = jarvis.JarvisAgent(backend=FakeBackend(["r"] * 50))
        for i in range(50):
            agent.ask(f"msg {i}")
        assert len(agent.messages) <= jarvis.MAX_HISTORY_MESSAGES

    def test_provider_and_model_exposed(self):
        agent = jarvis.JarvisAgent(backend=FakeBackend(["hi"]))
        assert agent.provider == "fake"
        assert agent.model == "fake-model"


class TestProviderResolution:
    def test_prefers_openai_when_both_keys_set(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
        monkeypatch.delenv("JARVIS_PROVIDER", raising=False)
        monkeypatch.delenv("MICORACLE_PROVIDER", raising=False)
        assert jarvis._resolve_provider() == "openai"

    def test_explicit_override_wins(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        monkeypatch.setenv("JARVIS_PROVIDER", "anthropic")
        monkeypatch.delenv("MICORACLE_PROVIDER", raising=False)
        assert jarvis._resolve_provider() == "anthropic"

    def test_micoracle_provider_beats_jarvis_provider(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "x")
        monkeypatch.setenv("JARVIS_PROVIDER", "openai")
        monkeypatch.setenv("MICORACLE_PROVIDER", "anthropic")
        assert jarvis._resolve_provider() == "anthropic"

    def test_none_without_keys(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("JARVIS_PROVIDER", raising=False)
        monkeypatch.delenv("MICORACLE_PROVIDER", raising=False)
        monkeypatch.setattr(jarvis, "_codex_cli_available", lambda: False)
        assert jarvis._resolve_provider() is None
        assert jarvis.is_available() is False
        assert jarvis.make_agent() is None

    def test_codex_fallback_without_keys(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("JARVIS_PROVIDER", raising=False)
        monkeypatch.delenv("MICORACLE_PROVIDER", raising=False)
        monkeypatch.setattr(jarvis, "_codex_cli_available", lambda: True)
        assert jarvis._resolve_provider() == "codex"
        assert jarvis.is_available() is True


class TestOpenAIBackend:
    class _Msg:
        def __init__(self, content):
            self.message = type("M", (), {"content": content})

    class _Resp:
        def __init__(self, content):
            self.choices = [TestOpenAIBackend._Msg(content)]

    class _FakeOpenAI:
        def __init__(self, content="Paris."):
            self._content = content
            self.kwargs = None
            self.chat = type("C", (), {"completions": self})()

        def create(self, **kwargs):
            self.kwargs = kwargs
            return TestOpenAIBackend._Resp(self._content)

    def test_openai_backend_formats_system_into_messages(self):
        fake = self._FakeOpenAI("Paris.")
        backend = jarvis._OpenAIBackend(client=fake, model="gpt-5.3")
        out = backend.complete("SYS", [{"role": "user", "content": "capital of France?"}], 400)
        assert out == "Paris."
        assert fake.kwargs["model"] == "gpt-5.3"
        assert fake.kwargs["messages"][0] == {"role": "system", "content": "SYS"}
        assert fake.kwargs["messages"][1]["content"] == "capital of France?"


class TestDispatchRouting:
    def test_micoracle_wake_speaks_not_pastes(self, tmp_path):
        import analytics
        import hands_free_voice as hfv

        spoken, pasted = [], []

        class FakeAdapter:
            supported_apps = {"Terminal"}
            def paste_and_return(self, text, target):
                pasted.append((text, target))

        class FakeTTS:
            def speak(self, phrase):
                spoken.append(phrase)

        ctx = hfv.DispatchContext(
            adapter=FakeAdapter(), target_app="Terminal", tts=FakeTTS(),
            command_backend="mlx",
            jarvis=jarvis.JarvisAgent(backend=FakeBackend(["The capital is Paris."])),
            tracker=analytics.UsageTracker(tmp_path / "u.jsonl"),
        )
        hfv._dispatch(ctx, "micoracle", "what is the capital of France")
        assert spoken == ["The capital is Paris."]
        assert pasted == []

    def test_micoracle_wake_without_agent_warns(self):
        import hands_free_voice as hfv

        spoken = []

        class FakeTTS:
            def speak(self, phrase):
                spoken.append(phrase)

        class FakeAdapter:
            supported_apps = set()
            def paste_and_return(self, text, target):
                raise AssertionError("should not paste")

        ctx = hfv.DispatchContext(
            adapter=FakeAdapter(), target_app="", tts=FakeTTS(),
            command_backend="mlx", jarvis=None,
        )
        # "tell me a joke" is not a control action, so with no LLM it should
        # warn that a key is needed (control actions are tried first).
        hfv._dispatch(ctx, "micoracle", "tell me a joke")
        assert any("api key" in s.lower() for s in spoken)
