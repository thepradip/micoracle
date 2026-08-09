"""MicOracle assistant brain — conversational LLM layer (OpenAI or Anthropic).

micoracle gives the ears (wake word + speech-to-text). This module adds the
brain: when you say "MicOracle, ...", the transcribed prompt goes to an LLM
instead of being pasted into an app, and the reply is spoken back via TTS.

Provider-agnostic via a small backend abstraction:
  - OpenAI   (default) — Chat Completions, model from MICORACLE_MODEL or "gpt-5.3"
  - Anthropic          — Messages API, model from MICORACLE_MODEL or "claude-opus-4-8"

Provider is chosen by MICORACLE_PROVIDER, else inferred from whichever API key
is set (OpenAI preferred). The legacy JARVIS_PROVIDER / JARVIS_MODEL variables
are still honored as fallbacks. Thinking/extended reasoning is left at defaults
for low voice latency; the system prompt keeps replies short and speech-friendly.
"""

from __future__ import annotations

import os

DEFAULT_OPENAI_MODEL = "gpt-5.3"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"
DEFAULT_MAX_TOKENS = 400
MAX_HISTORY_MESSAGES = 20  # ~10 turns; keeps latency and token use bounded

SYSTEM_PROMPT = (
    "You are MicOracle, a hands-free voice assistant running on the user's Mac. "
    "Your replies are spoken aloud by text-to-speech, so keep them short, "
    "natural, and conversational — usually one or two sentences. Never use "
    "markdown, bullet points, code blocks, headings, or emoji. If a full answer "
    "would be long, give a brief spoken summary and offer to go deeper. Answer "
    "directly, without preamble like 'Sure' or 'Here is'."
)


# ─────────────────────────── backends ─────────────────────────────


def _model_env(default: str) -> str:
    return (
        os.environ.get("MICORACLE_MODEL", "").strip()
        or os.environ.get("JARVIS_MODEL", "").strip()
        or default
    )


class _OpenAIBackend:
    name = "openai"

    def __init__(self, client=None, model: str | None = None) -> None:
        self.model = model or _model_env(DEFAULT_OPENAI_MODEL)
        if client is not None:
            self.client = client
        else:
            from openai import OpenAI  # lazy import

            self.client = OpenAI()  # reads OPENAI_API_KEY

    def complete(self, system: str, messages: list[dict], max_tokens: int) -> str:
        msgs = [{"role": "system", "content": system}, *messages]
        try:
            resp = self.client.chat.completions.create(
                model=self.model, messages=msgs, max_completion_tokens=max_tokens,
            )
        except Exception as exc:
            # Older models want `max_tokens`; some reasoning models reject any
            # cap. Retry once without the token cap before giving up.
            if "max_completion_tokens" in str(exc) or "max_tokens" in str(exc):
                resp = self.client.chat.completions.create(model=self.model, messages=msgs)
            else:
                raise
        return (resp.choices[0].message.content or "").strip()


class _AnthropicBackend:
    name = "anthropic"

    def __init__(self, client=None, model: str | None = None) -> None:
        self.model = model or _model_env(DEFAULT_ANTHROPIC_MODEL)
        if client is not None:
            self.client = client
        else:
            import anthropic  # lazy import

            self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    def complete(self, system: str, messages: list[dict], max_tokens: int) -> str:
        resp = self.client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system, messages=messages,
        )
        return next(
            (b.text for b in resp.content if getattr(b, "type", None) == "text"), ""
        ).strip()


class _CodexBackend:
    """Brain via the codex CLI — GPT through the user's ChatGPT login, no API key.

    ``complete()`` serializes the conversation into one prompt. ``complete_tools()``
    emulates function calling: tool specs and the transcript go into the prompt,
    the model must reply with a single JSON object, and screenshots are attached
    with ``codex exec -i`` so the model can see them. Each call is a fresh
    ``codex exec`` run (read-only sandbox); state lives in our message history.
    """

    name = "codex"
    _PROMPT_TAIL = (
        "Respond with ONLY one JSON object and nothing else (no markdown fences):\n"
        '{"say": "<one short spoken sentence, or empty string>", '
        '"tool_calls": [{"name": "<tool name>", "arguments": {<args>}}]}\n'
        "Use an empty tool_calls list only when you are answering conversationally "
        "with no action needed; otherwise act, verify, and finish via task_complete."
    )

    def __init__(self, model: str | None = None) -> None:
        # Empty model → whatever ~/.codex/config.toml sets (e.g. gpt-5.6).
        self.model = model or _model_env("") or "codex-config-default"
        self._call_seq = 0

    def _exec(self, prompt: str, image_paths: list[str] | None = None) -> str:
        import cli_agents as _cli

        result = _cli.run_codex(
            prompt,
            sandbox="read-only",
            timeout=300.0,
            image_paths=image_paths,
            model=None if self.model == "codex-config-default" else self.model,
        )
        if not result.ok:
            raise RuntimeError(f"codex exec failed: {result.detail or result.exit_code}")
        return result.output

    @staticmethod
    def _transcript(messages: list[dict]) -> str:
        import json as _json

        lines: list[str] = []
        for m in messages:
            role = m["role"]
            if role == "user":
                lines.append(f"USER: {m.get('content', '')}")
            elif role == "assistant":
                if m.get("content"):
                    lines.append(f"ASSISTANT said: {m['content']}")
                for c in m.get("tool_calls") or []:
                    lines.append(
                        f"ASSISTANT called {c['name']}({_json.dumps(c['arguments'])})"
                    )
            elif role == "tool":
                content = str(m.get("content", ""))[:3000]
                suffix = " (screenshot attached as an image)" if m.get("image_path") else ""
                lines.append(f"TOOL RESULT [{m.get('name', '?')}]: {content}{suffix}")
        return "\n".join(lines)

    def complete(self, system: str, messages: list[dict], max_tokens: int) -> str:
        prompt = (
            f"{system}\n\nConversation so far:\n{self._transcript(messages)}\n\n"
            "Reply with only the assistant's next spoken message, plain text."
        )
        return self._exec(prompt).strip()

    def complete_tools(self, system, messages, tools, max_tokens):
        import json as _json

        # Attach the most recent screenshots (if any) so the model sees them.
        images = [m["image_path"] for m in messages
                  if m.get("role") == "tool" and m.get("image_path")][-2:]
        prompt = (
            f"{system}\n\nAvailable tools (JSON Schema):\n{_json.dumps(tools)}\n\n"
            f"Conversation so far:\n{self._transcript(messages)}\n\n{self._PROMPT_TAIL}"
        )
        raw = self._exec(prompt, image_paths=images).strip()
        payload = self._parse_json(raw)
        if payload is None:
            # Not JSON — treat the whole reply as a spoken answer, no actions.
            return raw, []
        say = str(payload.get("say", "") or "").strip()
        calls: list[dict] = []
        for c in payload.get("tool_calls") or []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            self._call_seq += 1
            args = c.get("arguments")
            calls.append({
                "id": f"codex_call_{self._call_seq}",
                "name": str(c["name"]),
                "arguments": args if isinstance(args, dict) else {},
            })
        return say, calls

    @staticmethod
    def _parse_json(raw: str):
        import json as _json

        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:]
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            payload = _json.loads(text[start:end + 1])
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None


def _codex_cli_available() -> bool:
    import shutil

    return shutil.which("codex") is not None


def _resolve_provider() -> str | None:
    """Pick a provider from env override, else from whichever key is present.

    With no API key at all, fall back to the codex CLI when installed: it is
    authenticated via its own login (ChatGPT subscription) and gives the brain
    GPT access with zero keys in the environment — fully local setup.
    """
    override = (
        os.environ.get("MICORACLE_PROVIDER", "").strip().lower()
        or os.environ.get("JARVIS_PROVIDER", "").strip().lower()
    )
    if override in ("openai", "anthropic", "codex"):
        return override
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if _codex_cli_available():
        return "codex"
    return None


def _build_backend(provider: str):
    if provider == "openai":
        return _OpenAIBackend()
    if provider == "anthropic":
        return _AnthropicBackend()
    if provider == "codex":
        return _CodexBackend()
    raise ValueError(f"Unknown MicOracle provider: {provider!r}")


# ─────────────────────────── agent ────────────────────────────────


class JarvisAgent:
    """Multi-turn conversational agent. Stateless API; history kept locally."""

    def __init__(
        self,
        backend=None,
        system: str = SYSTEM_PROMPT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.backend = backend if backend is not None else _build_backend(_resolve_provider() or "openai")
        self.system = system
        self.max_tokens = max_tokens
        self.messages: list[dict] = []

    @property
    def provider(self) -> str:
        return getattr(self.backend, "name", "unknown")

    @property
    def model(self) -> str:
        return getattr(self.backend, "model", "unknown")

    def ask(self, prompt: str) -> str:
        """Send ``prompt`` with prior history and return the spoken reply."""
        self.messages.append({"role": "user", "content": prompt})
        try:
            reply = self.backend.complete(self.system, self.messages, self.max_tokens)
        except Exception as exc:
            self.messages.pop()  # roll back the unanswered turn
            return f"Sorry, I hit an error reaching the model: {exc}"

        reply = (reply or "").strip()
        if not reply:
            self.messages.pop()
            return "Sorry, I didn't get a response."
        self.messages.append({"role": "assistant", "content": reply})
        if len(self.messages) > MAX_HISTORY_MESSAGES:
            self.messages = self.messages[-MAX_HISTORY_MESSAGES:]
        return reply

    def reset(self) -> None:
        self.messages.clear()


def is_available() -> bool:
    """True if Jarvis can run: a provider key is set and its SDK is importable."""
    provider = _resolve_provider()
    if provider == "openai":
        try:
            import openai  # noqa: F401
        except ImportError:
            return False
        return True
    if provider == "anthropic":
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True
    if provider == "codex":
        return _codex_cli_available()
    return False


def make_agent() -> "JarvisAgent | None":
    """Build a JarvisAgent if configured, else None (caller degrades gracefully)."""
    if not is_available():
        return None
    try:
        return JarvisAgent()
    except Exception:
        return None


# ──────────────────── tool-calling extension ──────────────────────
#
# Provider-neutral formats used by the agent loop (agent.py):
#
# Tool spec:   {"name": str, "description": str, "parameters": <JSON schema>}
# Messages:
#   {"role": "user", "content": str}
#   {"role": "assistant", "content": str,
#    "tool_calls": [{"id": str, "name": str, "arguments": dict}]}
#   {"role": "tool", "tool_call_id": str, "name": str, "content": str,
#    "image_path": str | None}
#
# complete_tools() on either backend returns (text, tool_calls) where
# tool_calls is a list of {"id", "name", "arguments": dict}.


def _image_media_type(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "image/jpeg"
    return "image/png"


def _read_image_b64(path: str) -> str:
    import base64

    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def _image_block_openai(path: str) -> dict:
    """OpenAI chat-completions image content part (base64 data URL)."""
    data = _read_image_b64(path)
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{_image_media_type(path)};base64,{data}"},
    }


def _image_block_anthropic(path: str) -> dict:
    """Anthropic messages image content block (base64 source)."""
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": _image_media_type(path),
            "data": _read_image_b64(path),
        },
    }


def _tools_to_openai(specs: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": s["name"],
                "description": s["description"],
                "parameters": s["parameters"],
            },
        }
        for s in specs
    ]


def _tools_to_anthropic(specs: list[dict]) -> list[dict]:
    return [
        {
            "name": s["name"],
            "description": s["description"],
            "input_schema": s["parameters"],
        }
        for s in specs
    ]


def _msgs_to_openai(messages: list[dict]) -> list[dict]:
    """Neutral history → OpenAI chat messages.

    Tool results with an image become a tool message (text) followed by a
    user message carrying the image part — chat completions does not accept
    image content inside role:"tool" messages.
    """
    import json

    out: list[dict] = []
    for m in messages:
        role = m["role"]
        if role == "assistant":
            entry: dict = {"role": "assistant", "content": m.get("content") or None}
            calls = m.get("tool_calls") or []
            if calls:
                entry["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["arguments"]),
                        },
                    }
                    for c in calls
                ]
            out.append(entry)
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m["tool_call_id"],
                    "content": m.get("content") or "",
                }
            )
            image_path = m.get("image_path")
            if image_path:
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"Image returned by {m.get('name', 'tool')}:",
                            },
                            _image_block_openai(image_path),
                        ],
                    }
                )
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out


def _msgs_to_anthropic(messages: list[dict]) -> list[dict]:
    """Neutral history → Anthropic messages.

    Assistant tool calls become tool_use blocks; consecutive tool results
    fold into a single user message of tool_result blocks (API requirement).
    """
    out: list[dict] = []
    pending_results: list[dict] = []

    def flush_results() -> None:
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for m in messages:
        role = m["role"]
        if role == "tool":
            content: list[dict] = [{"type": "text", "text": m.get("content") or ""}]
            image_path = m.get("image_path")
            if image_path:
                content.append(_image_block_anthropic(image_path))
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": content,
                }
            )
            continue
        flush_results()
        if role == "assistant":
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for c in m.get("tool_calls") or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": c["id"],
                        "name": c["name"],
                        "input": c["arguments"],
                    }
                )
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    flush_results()
    return out


def _openai_complete_tools(self, system, messages, tools, max_tokens):
    import json

    msgs = [{"role": "system", "content": system}, *_msgs_to_openai(messages)]
    kwargs = dict(
        model=self.model,
        messages=msgs,
        tools=_tools_to_openai(tools),
        tool_choice="auto",
    )
    try:
        resp = self.client.chat.completions.create(
            max_completion_tokens=max_tokens, **kwargs
        )
    except Exception as exc:
        if "max_completion_tokens" in str(exc) or "max_tokens" in str(exc):
            resp = self.client.chat.completions.create(**kwargs)
        else:
            raise
    msg = resp.choices[0].message
    text = (msg.content or "").strip()
    calls: list[dict] = []
    for c in msg.tool_calls or []:
        try:
            arguments = json.loads(c.function.arguments or "{}")
        except (ValueError, TypeError):
            arguments = {}
        calls.append({"id": c.id, "name": c.function.name, "arguments": arguments})
    return text, calls


def _anthropic_complete_tools(self, system, messages, tools, max_tokens):
    resp = self.client.messages.create(
        model=self.model,
        max_tokens=max_tokens,
        system=system,
        messages=_msgs_to_anthropic(messages),
        tools=_tools_to_anthropic(tools),
    )
    texts: list[str] = []
    calls: list[dict] = []
    for b in resp.content:
        btype = getattr(b, "type", None)
        if btype == "text":
            texts.append(b.text)
        elif btype == "tool_use":
            calls.append({"id": b.id, "name": b.name, "arguments": dict(b.input)})
    return "\n".join(texts).strip(), calls


_OpenAIBackend.complete_tools = _openai_complete_tools
_AnthropicBackend.complete_tools = _anthropic_complete_tools


def make_tool_backend():
    """Build a tool-capable backend for the agent loop, else None.

    Model precedence: MICORACLE_AGENT_MODEL > JARVIS_MODEL > provider default.
    Both provider defaults are vision-capable, which the agent needs for
    screenshot verification.
    """
    if not is_available():
        return None
    provider = _resolve_provider()
    model = os.environ.get("MICORACLE_AGENT_MODEL", "").strip() or None
    try:
        if provider == "openai":
            return _OpenAIBackend(model=model)
        if provider == "anthropic":
            return _AnthropicBackend(model=model)
        if provider == "codex":
            return _CodexBackend(model=model)
    except Exception:
        return None
    return None
