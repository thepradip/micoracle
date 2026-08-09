"""MicOracle agent — the tool-calling brain behind "micoracle, <do something>".

AgentSession runs the LLM loop: understand the instruction (asking back out
loud when ambiguous), act through the ToolRegistry (browser, desktop, claude/
codex CLIs), and finish only through task_complete — where success without
evidence is rejected. AgentRunner gives the loop its own long-lived thread so
the mic keeps listening, the Playwright session keeps its thread affinity, and
spoken answers ("yes", "stop", a clarification) can be fed in mid-task.
"""

from __future__ import annotations

import os
import queue
import threading
from dataclasses import dataclass, field

AGENT_SYSTEM_PROMPT = (
    "You are MicOracle, a voice-controlled agent on the user's Mac. You hear "
    "transcribed speech and act through tools: a real browser, the desktop, and "
    "the claude/codex coding CLIs.\n"
    "Rules:\n"
    "1. If the instruction is ambiguous or missing a needed detail, call "
    "ask_user BEFORE acting — never guess.\n"
    "2. After every state-changing action, verify it worked using a read tool "
    "(browser_read_page, a screenshot, a read-back) before moving on.\n"
    "3. Finish with task_complete. success=true requires evidence entries "
    "describing what you actually observed. Never claim success you did not "
    "verify; if something failed, say so plainly.\n"
    "4. Your text replies are spoken aloud by TTS: one short natural sentence, "
    "no markdown, no lists, no URLs spelled out.\n"
    "5. Speech transcription is imperfect — interpret obvious mis-hearings "
    "sensibly (e.g. 'hacker news' not 'hacked her news').\n"
    "6. For ANY task that involves reading, checking, or extracting from a web "
    "page, use the browser_* tools — they drive your own controlled browser "
    "where browser_read_page and browser_scrape give you the content directly. "
    "Never use desktop_open_url plus screenshots to read a page; "
    "desktop_open_url is only for showing the user something without reading it."
)

_STOP_WORDS = {"stop", "cancel", "abort", "never mind", "nevermind"}
_YES_WORDS = {"yes", "yeah", "yep", "sure", "go ahead", "do it", "confirm", "ok", "okay"}
_NO_WORDS = {"no", "nope", "don't", "do not", "cancel", "stop"}


@dataclass
class AgentConfig:
    max_iterations: int = int(os.environ.get("MICORACLE_AGENT_MAX_STEPS", "15"))
    max_tokens: int = 1500
    answer_timeout_secs: float = 30.0


@dataclass
class AgentReport:
    success: bool
    summary: str
    evidence: list[str] = field(default_factory=list)
    iterations: int = 0
    aborted: bool = False

    def spoken(self) -> str:
        if self.aborted:
            return f"Stopped. {self.summary}" if self.summary else "Stopped."
        if self.success:
            text = self.summary
            if self.evidence:
                text += f" {self.evidence[0]}"
            return text
        return self.summary or "I couldn't finish the task."


def _normalize(text: str) -> str:
    return (text or "").strip().lower().rstrip(".!?,")


def is_stop_phrase(text: str) -> bool:
    t = _normalize(text)
    return t in _STOP_WORDS or any(t.startswith(w + " ") for w in ("stop", "cancel"))


def is_yes(text: str) -> bool:
    return _normalize(text) in _YES_WORDS


def is_no(text: str) -> bool:
    return _normalize(text) in _NO_WORDS


class AgentSession:
    """One task = one run(). Holds the neutral message history for that task."""

    def __init__(self, backend, registry, config: AgentConfig,
                 speak, confirm, ask_user) -> None:
        self.backend = backend
        self.registry = registry
        self.config = config
        self.speak = speak
        self.confirm = confirm      # (question: str) -> bool
        self.ask_user = ask_user    # (question: str) -> str | None
        self._abort = threading.Event()

    def abort(self) -> None:
        self._abort.set()

    def run(self, instruction: str) -> AgentReport:
        messages: list[dict] = [{"role": "user", "content": instruction}]
        tools = self.registry.specs()
        evidence_nagged = False

        for iteration in range(1, self.config.max_iterations + 1):
            if self._abort.is_set():
                return AgentReport(False, "Task cancelled.", iterations=iteration, aborted=True)

            try:
                text, calls = self.backend.complete_tools(
                    AGENT_SYSTEM_PROMPT, messages, tools, self.config.max_tokens,
                )
            except Exception as exc:
                return AgentReport(
                    False, f"I hit an error reaching the model: {exc}",
                    iterations=iteration,
                )

            messages.append(
                {"role": "assistant", "content": text, "tool_calls": calls}
            )

            if text and calls:
                self.speak(text)
            if not calls:
                # Chat-style answer: nothing to verify, nothing was done.
                return AgentReport(
                    True, text or "I have nothing to add.", iterations=iteration,
                )

            for call in calls:
                if self._abort.is_set():
                    return AgentReport(
                        False, "Task cancelled.", iterations=iteration, aborted=True,
                    )
                name, args = call["name"], call["arguments"]

                if name == "task_complete":
                    success = bool(args.get("success"))
                    summary = str(args.get("summary", "")).strip()
                    evidence = [str(e) for e in args.get("evidence") or [] if str(e).strip()]
                    if success and not evidence:
                        if not evidence_nagged:
                            evidence_nagged = True
                            messages.append(_tool_msg(
                                call,
                                "REJECTED: success=true requires evidence of what you "
                                "observed. Verify with a read tool, then call "
                                "task_complete again with evidence.",
                            ))
                            continue
                        return AgentReport(
                            False,
                            f"I did the steps but could not verify the result. {summary}",
                            iterations=iteration,
                        )
                    return AgentReport(success, summary, evidence, iteration)

                if name == "ask_user":
                    question = str(args.get("question", "")).strip() or "Can you clarify?"
                    answer = self.ask_user(question)
                    messages.append(_tool_msg(
                        call,
                        f"user said: {answer}" if answer
                        else "no answer within the timeout; proceed sensibly or finish",
                    ))
                    continue

                warning = self.registry.needs_confirmation(name, args)
                if warning is not None:
                    if not self.confirm(warning):
                        messages.append(_tool_msg(
                            call, "User DECLINED this action. Do not retry it; "
                                  "adjust or finish honestly.",
                        ))
                        continue

                result = self.registry.execute(name, args)
                messages.append(_tool_msg(
                    call,
                    ("ok" if result.ok else "FAILED") + f": {result.content}",
                    image_path=result.image_path,
                ))

        return AgentReport(
            False,
            "I ran out of steps before finishing. "
            "Last state is in the log; nothing was verified complete.",
            iterations=self.config.max_iterations,
        )


def _tool_msg(call: dict, content: str, image_path: str | None = None) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call["id"],
        "name": call["name"],
        "content": content,
        "image_path": image_path,
    }


class AgentRunner:
    """Owns one worker thread; the mic thread talks to it via queues."""

    def __init__(self, backend, registry, speak, config: AgentConfig | None = None) -> None:
        self.backend = backend
        self.registry = registry
        self.speak = speak
        self.config = config or AgentConfig()
        self.busy = False
        self.awaiting_input = False
        self._tasks: "queue.Queue[str]" = queue.Queue()
        self._answers: "queue.Queue[str]" = queue.Queue()
        self._session: AgentSession | None = None
        self._thread = threading.Thread(target=self._loop, daemon=True, name="micoracle-agent")
        self._thread.start()

    # ── called from the STT worker thread ─────────────────────

    def submit(self, instruction: str) -> bool:
        if self.busy:
            return False
        self.busy = True
        self._tasks.put(instruction)
        return True

    def feed_user_speech(self, text: str) -> bool:
        """True if this utterance was consumed (answer / confirm / stop)."""
        if not self.busy:
            return False
        if is_stop_phrase(text):
            self.abort()
            return True
        if self.awaiting_input:
            self._answers.put(text)
            return True
        return False

    def abort(self) -> None:
        if self._session is not None:
            self._session.abort()
        # unblock a pending ask/confirm so the loop can exit promptly
        self._answers.put("stop")

    # ── worker thread ──────────────────────────────────────────

    def _heartbeat(self) -> None:
        # Reassure the user during long steps (e.g. a codex run) without
        # interrupting a pending question.
        while self.busy:
            if self._done.wait(timeout=30.0):
                return
            if self.busy and not self.awaiting_input:
                self.speak("Still working.")

    def _loop(self) -> None:
        while True:
            instruction = self._tasks.get()
            self._done = threading.Event()
            heartbeat = threading.Thread(target=self._heartbeat, daemon=True)
            heartbeat.start()
            try:
                self._session = AgentSession(
                    self.backend, self.registry, self.config,
                    speak=self.speak, confirm=self._confirm, ask_user=self._ask,
                )
                report = self._session.run(instruction)
                self.speak(report.spoken())
                print(f"[agent] success={report.success} iterations={report.iterations} "
                      f"summary={report.summary!r} evidence={report.evidence}", flush=True)
            except Exception as exc:
                self.speak("The agent crashed; details are in the log.")
                print(f"[agent error] {exc!r}", flush=True)
            finally:
                self._session = None
                self.busy = False
                self.awaiting_input = False
                self._done.set()

    def _wait_answer(self, prompt: str) -> str | None:
        self.speak(prompt)
        self.awaiting_input = True
        try:
            return self._answers.get(timeout=self.config.answer_timeout_secs)
        except queue.Empty:
            return None
        finally:
            self.awaiting_input = False

    def _confirm(self, warning: str) -> bool:
        answer = self._wait_answer(warning)
        return answer is not None and is_yes(answer)

    def _ask(self, question: str) -> str | None:
        return self._wait_answer(question)


def make_runner(speak) -> "AgentRunner | None":
    """Build the agent if a brain and at least one capability exist, else None."""
    import jarvis as _jarvis

    backend = _jarvis.make_tool_backend()
    if backend is None:
        return None

    import agent_tools as _tools
    import browser as _browser
    import cli_agents as _cli

    if not (_browser.is_available() or _cli.claude_available() or _cli.codex_available()):
        return None
    try:
        registry = _tools.ToolRegistry()
        return AgentRunner(backend, registry, speak)
    except Exception:
        return None


def capabilities() -> str:
    """Short human-readable capability summary for the startup banner."""
    import browser as _browser
    import cli_agents as _cli

    parts = []
    if _browser.is_available():
        parts.append("browser")
    if _cli.claude_available():
        parts.append("claude")
    if _cli.codex_available():
        parts.append("codex")
    return "+".join(parts) if parts else "none"
