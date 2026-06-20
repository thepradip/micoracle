"""Jarvis — a conversational voice assistant brain (OpenAI or Anthropic).

micoracle gives the ears (wake word + speech-to-text). Jarvis adds the brain:
when you say "Jarvis, ...", the transcribed prompt goes to an LLM instead of
being pasted into an app, and the reply is spoken back through the TTS backend.

Provider-agnostic via a small backend abstraction:
  - OpenAI   (default) — Chat Completions, model from JARVIS_MODEL or "gpt-5.3"
  - Anthropic          — Messages API, model from JARVIS_MODEL or "claude-opus-4-8"

Provider is chosen by JARVIS_PROVIDER, else inferred from whichever API key is
set (OpenAI preferred). Thinking/extended reasoning is left at defaults for low
voice latency; the system prompt keeps replies short and speech-friendly.
"""

from __future__ import annotations

import os

DEFAULT_OPENAI_MODEL = "gpt-5.3"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"
DEFAULT_MAX_TOKENS = 400
MAX_HISTORY_MESSAGES = 20  # ~10 turns; keeps latency and token use bounded

SYSTEM_PROMPT = (
    "You are Jarvis, a hands-free voice assistant running on the user's Mac. "
    "Your replies are spoken aloud by text-to-speech, so keep them short, "
    "natural, and conversational — usually one or two sentences. Never use "
    "markdown, bullet points, code blocks, headings, or emoji. If a full answer "
    "would be long, give a brief spoken summary and offer to go deeper. Answer "
    "directly, without preamble like 'Sure' or 'Here is'."
)


# ─────────────────────────── backends ─────────────────────────────


class _OpenAIBackend:
    name = "openai"

    def __init__(self, client=None, model: str | None = None) -> None:
        self.model = model or os.environ.get("JARVIS_MODEL", DEFAULT_OPENAI_MODEL)
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
        self.model = model or os.environ.get("JARVIS_MODEL", DEFAULT_ANTHROPIC_MODEL)
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


def _resolve_provider() -> str | None:
    """Pick a provider from env override, else from whichever key is present."""
    override = os.environ.get("JARVIS_PROVIDER", "").strip().lower()
    if override in ("openai", "anthropic"):
        return override
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def _build_backend(provider: str):
    if provider == "openai":
        return _OpenAIBackend()
    if provider == "anthropic":
        return _AnthropicBackend()
    raise ValueError(f"Unknown Jarvis provider: {provider!r}")


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
    return False


def make_agent() -> "JarvisAgent | None":
    """Build a JarvisAgent if configured, else None (caller degrades gracefully)."""
    if not is_available():
        return None
    try:
        return JarvisAgent()
    except Exception:
        return None
