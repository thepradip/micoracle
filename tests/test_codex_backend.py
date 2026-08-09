"""Tests for the codex-CLI brain backend (no real codex runs — patched runner)."""

import cli_agents
import jarvis


def _patch_codex(monkeypatch, output, ok=True):
    calls = []

    def fake_run_codex(task, cwd=None, timeout=600.0, sandbox="workspace-write",
                       image_paths=None, model=None):
        calls.append({"task": task, "sandbox": sandbox,
                      "image_paths": image_paths, "model": model})
        return cli_agents.CLIResult(ok, 0 if ok else 1, output, 1.0,
                                    "" if ok else "boom")

    monkeypatch.setattr(cli_agents, "run_codex", fake_run_codex)
    return calls


HISTORY = [
    {"role": "user", "content": "open example.com"},
    {"role": "assistant", "content": "Opening.",
     "tool_calls": [{"id": "c1", "name": "browser_open",
                     "arguments": {"url": "https://example.com"}}]},
    {"role": "tool", "tool_call_id": "c1", "name": "browser_open",
     "content": "ok: status 200", "image_path": None},
]

SPECS = [{"name": "browser_open", "description": "d",
          "parameters": {"type": "object", "properties": {}}}]


class TestCompleteTools:
    def test_json_reply_parsed(self, monkeypatch):
        calls = _patch_codex(monkeypatch,
            '{"say": "Opening the site.", "tool_calls": '
            '[{"name": "browser_open", "arguments": {"url": "https://x.com"}}]}')
        backend = jarvis._CodexBackend()
        say, tool_calls = backend.complete_tools("SYS", HISTORY, SPECS, 1000)
        assert say == "Opening the site."
        assert tool_calls[0]["name"] == "browser_open"
        assert tool_calls[0]["arguments"] == {"url": "https://x.com"}
        assert tool_calls[0]["id"].startswith("codex_call_")
        # brain runs read-only, and the prompt carries system + tools + transcript
        assert calls[0]["sandbox"] == "read-only"
        assert "SYS" in calls[0]["task"]
        assert "browser_open" in calls[0]["task"]
        assert "USER: open example.com" in calls[0]["task"]

    def test_fenced_json_accepted(self, monkeypatch):
        _patch_codex(monkeypatch,
            '```json\n{"say": "", "tool_calls": [{"name": "t", "arguments": {}}]}\n```')
        backend = jarvis._CodexBackend()
        say, tool_calls = backend.complete_tools("s", [], SPECS, 100)
        assert say == ""
        assert tool_calls[0]["name"] == "t"

    def test_non_json_becomes_spoken_answer(self, monkeypatch):
        _patch_codex(monkeypatch, "The capital of France is Paris.")
        backend = jarvis._CodexBackend()
        say, tool_calls = backend.complete_tools("s", [], SPECS, 100)
        assert say == "The capital of France is Paris."
        assert tool_calls == []

    def test_screenshots_attached(self, monkeypatch):
        calls = _patch_codex(monkeypatch, '{"say": "", "tool_calls": []}')
        history = HISTORY + [
            {"role": "tool", "tool_call_id": "c2", "name": "browser_screenshot",
             "content": "saved", "image_path": "/tmp/a.png"},
            {"role": "tool", "tool_call_id": "c3", "name": "desktop_screenshot",
             "content": "saved", "image_path": "/tmp/b.png"},
        ]
        backend = jarvis._CodexBackend()
        backend.complete_tools("s", history, SPECS, 100)
        assert calls[0]["image_paths"] == ["/tmp/a.png", "/tmp/b.png"]

    def test_failure_raises(self, monkeypatch):
        _patch_codex(monkeypatch, "", ok=False)
        backend = jarvis._CodexBackend()
        try:
            backend.complete_tools("s", [], SPECS, 100)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert "codex exec failed" in str(exc)

    def test_ids_unique_across_iterations(self, monkeypatch):
        _patch_codex(monkeypatch,
            '{"say": "", "tool_calls": [{"name": "t", "arguments": {}}]}')
        backend = jarvis._CodexBackend()
        _, first = backend.complete_tools("s", [], SPECS, 100)
        _, second = backend.complete_tools("s", [], SPECS, 100)
        assert first[0]["id"] != second[0]["id"]

    def test_model_default_not_passed(self, monkeypatch):
        for var in ("MICORACLE_MODEL", "JARVIS_MODEL"):
            monkeypatch.delenv(var, raising=False)
        calls = _patch_codex(monkeypatch, '{"say": "hi", "tool_calls": []}')
        jarvis._CodexBackend().complete_tools("s", [], SPECS, 100)
        assert calls[0]["model"] is None

    def test_explicit_model_passed(self, monkeypatch):
        calls = _patch_codex(monkeypatch, '{"say": "hi", "tool_calls": []}')
        jarvis._CodexBackend(model="gpt-5.6-sol").complete_tools("s", [], SPECS, 100)
        assert calls[0]["model"] == "gpt-5.6-sol"


class TestComplete:
    def test_chat_roundtrip(self, monkeypatch):
        calls = _patch_codex(monkeypatch, "Paris.")
        backend = jarvis._CodexBackend()
        out = backend.complete("SYS", [{"role": "user", "content": "capital?"}], 400)
        assert out == "Paris."
        assert "USER: capital?" in calls[0]["task"]

    def test_agent_wraps_codex_backend(self, monkeypatch):
        _patch_codex(monkeypatch, "Hello from GPT.")
        agent = jarvis.JarvisAgent(backend=jarvis._CodexBackend())
        assert agent.ask("hi") == "Hello from GPT."
        assert agent.provider == "codex"


class TestParseJson:
    def test_plain(self):
        assert jarvis._CodexBackend._parse_json('{"a": 1}') == {"a": 1}

    def test_with_surrounding_prose(self):
        parsed = jarvis._CodexBackend._parse_json('Sure: {"say": "x", "tool_calls": []} done')
        assert parsed == {"say": "x", "tool_calls": []}

    def test_garbage_returns_none(self):
        assert jarvis._CodexBackend._parse_json("no json here") is None
        assert jarvis._CodexBackend._parse_json("{broken") is None
