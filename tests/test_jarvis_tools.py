"""Tests for the jarvis tool-calling extension (no network — fake clients)."""

import base64
import json
from types import SimpleNamespace

import jarvis

SPECS = [
    {
        "name": "browser_open",
        "description": "Open a URL in the browser.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    }
]

NEUTRAL_HISTORY = [
    {"role": "user", "content": "open example.com"},
    {
        "role": "assistant",
        "content": "Opening it now.",
        "tool_calls": [
            {"id": "c1", "name": "browser_open", "arguments": {"url": "https://example.com"}}
        ],
    },
    {
        "role": "tool",
        "tool_call_id": "c1",
        "name": "browser_open",
        "content": "ok: https://example.com/ (status 200)",
        "image_path": None,
    },
]


class TestToolSpecTranslation:
    def test_openai_shape(self):
        out = jarvis._tools_to_openai(SPECS)
        assert out == [
            {
                "type": "function",
                "function": {
                    "name": "browser_open",
                    "description": "Open a URL in the browser.",
                    "parameters": SPECS[0]["parameters"],
                },
            }
        ]

    def test_anthropic_shape(self):
        out = jarvis._tools_to_anthropic(SPECS)
        assert out == [
            {
                "name": "browser_open",
                "description": "Open a URL in the browser.",
                "input_schema": SPECS[0]["parameters"],
            }
        ]


class TestMessageTranslation:
    def test_openai_messages(self):
        out = jarvis._msgs_to_openai(NEUTRAL_HISTORY)
        assert out[0] == {"role": "user", "content": "open example.com"}
        assert out[1]["role"] == "assistant"
        assert out[1]["content"] == "Opening it now."
        assert out[1]["tool_calls"] == [
            {
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "browser_open",
                    "arguments": json.dumps({"url": "https://example.com"}),
                },
            }
        ]
        assert out[2] == {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "ok: https://example.com/ (status 200)",
        }

    def test_anthropic_messages(self):
        out = jarvis._msgs_to_anthropic(NEUTRAL_HISTORY)
        assert out[0] == {"role": "user", "content": "open example.com"}
        assert out[1] == {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Opening it now."},
                {
                    "type": "tool_use",
                    "id": "c1",
                    "name": "browser_open",
                    "input": {"url": "https://example.com"},
                },
            ],
        }
        assert out[2]["role"] == "user"
        result_block = out[2]["content"][0]
        assert result_block["type"] == "tool_result"
        assert result_block["tool_use_id"] == "c1"
        assert result_block["content"][0]["type"] == "text"

    def test_anthropic_folds_consecutive_tool_results(self):
        history = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "a", "name": "browser_open", "arguments": {}},
                    {"id": "b", "name": "browser_read_page", "arguments": {}},
                ],
            },
            {"role": "tool", "tool_call_id": "a", "name": "browser_open", "content": "ok"},
            {"role": "tool", "tool_call_id": "b", "name": "browser_read_page", "content": "text"},
        ]
        out = jarvis._msgs_to_anthropic(history)
        # both tool results folded into ONE user message
        assert len(out) == 3
        assert [b["tool_use_id"] for b in out[2]["content"]] == ["a", "b"]

    def test_assistant_without_text_has_no_empty_text_block(self):
        history = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "a", "name": "t", "arguments": {}}],
            }
        ]
        out = jarvis._msgs_to_anthropic(history)
        assert all(b["type"] == "tool_use" for b in out[0]["content"])


class TestImageBlocks:
    def _png(self, tmp_path):
        path = tmp_path / "shot.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\nfakepixels")
        return str(path)

    def test_openai_image_block(self, tmp_path):
        path = self._png(tmp_path)
        block = jarvis._image_block_openai(path)
        expected_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfakepixels").decode("ascii")
        assert block == {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{expected_b64}"},
        }

    def test_anthropic_image_block(self, tmp_path):
        path = self._png(tmp_path)
        block = jarvis._image_block_anthropic(path)
        assert block["type"] == "image"
        assert block["source"]["type"] == "base64"
        assert block["source"]["media_type"] == "image/png"

    def test_jpeg_media_type(self):
        assert jarvis._image_media_type("a.JPG") == "image/jpeg"
        assert jarvis._image_media_type("a.png") == "image/png"

    def test_openai_tool_image_becomes_user_message(self, tmp_path):
        path = self._png(tmp_path)
        history = [
            {"role": "tool", "tool_call_id": "c1", "name": "browser_screenshot",
             "content": "saved", "image_path": path},
        ]
        out = jarvis._msgs_to_openai(history)
        assert out[0]["role"] == "tool"
        assert out[1]["role"] == "user"
        assert out[1]["content"][1]["type"] == "image_url"

    def test_anthropic_tool_image_inside_tool_result(self, tmp_path):
        path = self._png(tmp_path)
        history = [
            {"role": "tool", "tool_call_id": "c1", "name": "browser_screenshot",
             "content": "saved", "image_path": path},
        ]
        out = jarvis._msgs_to_anthropic(history)
        blocks = out[0]["content"][0]["content"]
        assert blocks[0]["type"] == "text"
        assert blocks[1]["type"] == "image"


class FakeOpenAIToolClient:
    def __init__(self, message):
        self.calls = []
        self._message = message
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=self._message)])


class TestOpenAICompleteTools:
    def test_normalizes_tool_calls(self):
        message = SimpleNamespace(
            content="On it.",
            tool_calls=[
                SimpleNamespace(
                    id="c9",
                    function=SimpleNamespace(
                        name="browser_open",
                        arguments='{"url": "https://x.com"}',
                    ),
                )
            ],
        )
        client = FakeOpenAIToolClient(message)
        backend = jarvis._OpenAIBackend(client=client, model="gpt-5.3")
        text, calls = backend.complete_tools("sys", NEUTRAL_HISTORY, SPECS, 500)
        assert text == "On it."
        assert calls == [
            {"id": "c9", "name": "browser_open", "arguments": {"url": "https://x.com"}}
        ]
        sent = client.calls[0]
        assert sent["tools"][0]["type"] == "function"
        assert sent["tool_choice"] == "auto"
        assert sent["messages"][0] == {"role": "system", "content": "sys"}

    def test_bad_arguments_json_becomes_empty_dict(self):
        message = SimpleNamespace(
            content=None,
            tool_calls=[
                SimpleNamespace(
                    id="c1",
                    function=SimpleNamespace(name="t", arguments="{not json"),
                )
            ],
        )
        backend = jarvis._OpenAIBackend(
            client=FakeOpenAIToolClient(message), model="m"
        )
        text, calls = backend.complete_tools("s", [], SPECS, 100)
        assert text == ""
        assert calls[0]["arguments"] == {}


class FakeAnthropicToolClient:
    def __init__(self, blocks):
        self.calls = []
        self._blocks = blocks
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=self._blocks)


class TestAnthropicCompleteTools:
    def test_normalizes_tool_use_blocks(self):
        blocks = [
            SimpleNamespace(type="text", text="Opening."),
            SimpleNamespace(
                type="tool_use", id="t1", name="browser_open",
                input={"url": "https://x.com"},
            ),
        ]
        client = FakeAnthropicToolClient(blocks)
        backend = jarvis._AnthropicBackend(client=client, model="claude-opus-4-8")
        text, calls = backend.complete_tools("sys", NEUTRAL_HISTORY, SPECS, 500)
        assert text == "Opening."
        assert calls == [
            {"id": "t1", "name": "browser_open", "arguments": {"url": "https://x.com"}}
        ]
        sent = client.calls[0]
        assert sent["tools"][0]["input_schema"] == SPECS[0]["parameters"]
        assert sent["system"] == "sys"

    def test_text_only_response(self):
        blocks = [SimpleNamespace(type="text", text="Just chatting.")]
        backend = jarvis._AnthropicBackend(
            client=FakeAnthropicToolClient(blocks), model="m"
        )
        text, calls = backend.complete_tools("s", [], SPECS, 100)
        assert text == "Just chatting."
        assert calls == []


class TestMakeToolBackend:
    def test_none_without_keys(self, monkeypatch):
        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                    "JARVIS_PROVIDER", "MICORACLE_PROVIDER"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(jarvis, "_codex_cli_available", lambda: False)
        assert jarvis.make_tool_backend() is None

    def test_codex_backend_without_keys(self, monkeypatch):
        for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "JARVIS_PROVIDER",
                    "MICORACLE_PROVIDER", "MICORACLE_AGENT_MODEL",
                    "MICORACLE_MODEL", "JARVIS_MODEL"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(jarvis, "_codex_cli_available", lambda: True)
        backend = jarvis.make_tool_backend()
        assert backend is not None
        assert backend.name == "codex"

    def test_agent_model_env_wins(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("MICORACLE_PROVIDER", "anthropic")
        monkeypatch.setenv("MICORACLE_AGENT_MODEL", "claude-opus-5")
        monkeypatch.setenv("JARVIS_MODEL", "other-model")

        built = {}

        class FakeBackendCls:
            name = "anthropic"

            def __init__(self, model=None):
                built["model"] = model
                self.model = model

        monkeypatch.setattr(jarvis, "_AnthropicBackend", FakeBackendCls)
        monkeypatch.setattr(jarvis, "is_available", lambda: True)
        backend = jarvis.make_tool_backend()
        assert backend is not None
        assert built["model"] == "claude-opus-5"
