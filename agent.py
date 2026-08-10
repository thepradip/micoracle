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


_FILLER_PREFIXES = ("ok", "okay", "please", "hey", "now", "just")


def is_stop_phrase(text: str) -> bool:
    t = _normalize(text)
    # "okay stop it" / "please stop" — spoken commands often lead with filler
    words = t.split()
    while words and words[0].strip(",.!?;:") in _FILLER_PREFIXES:
        words = words[1:]
    t = " ".join(words)
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

    def run(self, instruction: str, messages: list[dict] | None = None) -> AgentReport:
        # In conversation mode the runner passes `messages` seeded with prior
        # tasks' history; it then already contains `instruction` as its last
        # user message. Without it, behavior is the classic fresh-task run.
        if messages is None:
            messages = [{"role": "user", "content": instruction}]
        self.messages = messages
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


# ─────────────────── conversation history helpers ─────────────────

HISTORY_CAP = 30  # neutral messages kept between tasks in a conversation


def _sanitize_history(messages: list[dict]) -> list[dict]:
    """Close any assistant tool_calls that never got a tool response.

    run() exits via task_complete without appending a tool result, and an
    abort can bail mid-way through a call batch. OpenAI rejects a history
    whose assistant tool_calls lack paired tool messages (and Anthropic
    rejects dangling tool_use), so a synthetic closure is appended right
    after each unanswered call before the history is reused.
    """
    out: list[dict] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        out.append(m)
        i += 1
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        answered: set = set()
        while i < len(messages) and messages[i].get("role") == "tool":
            answered.add(messages[i].get("tool_call_id"))
            out.append(messages[i])
            i += 1
        for c in m["tool_calls"]:
            if c["id"] not in answered:
                out.append({
                    "role": "tool",
                    "tool_call_id": c["id"],
                    "name": c["name"],
                    "content": "(task ended; result was reported to the user)",
                    "image_path": None,
                })
    return out


def _trim_history(messages: list[dict], cap: int = HISTORY_CAP) -> list[dict]:
    """Bound the carried history, cutting only at a user-message boundary.

    A naive front-trim can strand tool responses whose assistant tool_calls
    message was cut (API 400) and can start the history with a non-user role
    (Anthropic requires user-first), so after trimming we advance to the next
    user message.
    """
    if len(messages) <= cap:
        return list(messages)
    trimmed = messages[-cap:]
    for i, m in enumerate(trimmed):
        if m.get("role") == "user":
            return trimmed[i:]
    return []


class AgentRunner:
    """Owns one worker thread; the mic thread talks to it via queues.

    Tasks queue (up to MAX_QUEUED) and run sequentially. In conversation mode
    the runner also carries a shared neutral message history across tasks so
    follow-up commands can say "there" / "it".
    """

    MAX_QUEUED = 5

    def __init__(self, backend, registry, speak, config: AgentConfig | None = None) -> None:
        self.backend = backend
        self.registry = registry
        self.speak = speak
        self.config = config or AgentConfig()
        self.awaiting_input = False
        self._lock = threading.Lock()
        self._pending = 0                 # queued + running tasks
        self._tasks: "queue.Queue[str]" = queue.Queue(maxsize=self.MAX_QUEUED)
        self._answers: "queue.Queue[str]" = queue.Queue()
        self._history: list[dict] = []
        self._history_lock = threading.Lock()
        self._history_epoch = 0           # bumped by reset; stale tasks skip commit
        self._session: AgentSession | None = None
        self._thread = threading.Thread(target=self._loop, daemon=True, name="micoracle-agent")
        self._thread.start()

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._pending > 0

    # ── called from the STT worker thread ─────────────────────

    def submit(self, instruction: str) -> bool:
        """Queue a task. False only when the queue is full (backlog)."""
        with self._lock:
            self._pending += 1            # before put: busy is True on return
        try:
            self._tasks.put_nowait(instruction)
        except queue.Full:
            with self._lock:
                self._pending -= 1
            return False
        return True

    def feed_user_speech(self, text: str) -> bool:
        """True if this utterance was consumed (answer / confirm / stop)."""
        if not self.busy:
            return False
        if is_stop_phrase(text):
            self.cancel_all()
            return True
        if self.awaiting_input:
            self._answers.put(text)
            return True
        return False

    def abort(self) -> None:
        """Abort only the currently running task."""
        if self._session is not None:
            self._session.abort()
        # unblock a pending ask/confirm so the loop can exit promptly
        self._answers.put("stop")

    def cancel_all(self) -> None:
        """Drop every queued task, then abort the running one."""
        while True:
            try:
                self._tasks.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                self._pending -= 1
        self.abort()

    def reset_history(self) -> None:
        """Forget the conversation context (session ended)."""
        with self._history_lock:
            self._history = []
            self._history_epoch += 1

    def note(self, text: str) -> None:
        """Record an action done outside the agent loop (control fast path)."""
        with self._history_lock:
            self._history.append({
                "role": "user",
                "content": f"(voice control action outside the agent: {text})",
            })

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
            # A prior abort() may have parked a "stop" in _answers with nothing
            # awaiting; drain so it can't answer this task's first ask_user.
            while True:
                try:
                    self._answers.get_nowait()
                except queue.Empty:
                    break
            self._done = threading.Event()
            heartbeat = threading.Thread(target=self._heartbeat, daemon=True)
            heartbeat.start()
            try:
                with self._history_lock:
                    seeded = list(self._history)
                    epoch = self._history_epoch
                    base_len = len(self._history)
                seeded.append({"role": "user", "content": instruction})
                self._session = AgentSession(
                    self.backend, self.registry, self.config,
                    speak=self.speak, confirm=self._confirm, ask_user=self._ask,
                )
                report = self._session.run(instruction, messages=seeded)
                self.speak(report.spoken())
                print(f"[agent] success={report.success} iterations={report.iterations} "
                      f"summary={report.summary!r} evidence={report.evidence}", flush=True)
                self._commit_history(seeded, epoch, base_len, report)
            except Exception as exc:
                self.speak("The agent crashed; details are in the log.")
                print(f"[agent error] {exc!r}", flush=True)
            finally:
                self._session = None
                self.awaiting_input = False
                with self._lock:
                    self._pending -= 1
                self._done.set()

    def _commit_history(self, messages: list[dict], epoch: int, base_len: int,
                        report: AgentReport) -> None:
        final = _sanitize_history(messages)
        for m in final:
            # old screenshots would be re-read and re-encoded on every later
            # request; the text description of the result stays
            if m.get("image_path"):
                m["image_path"] = None
        final.append({
            "role": "user",
            "content": f"(previous task ended: success={report.success}; {report.summary})",
        })
        with self._history_lock:
            if self._history_epoch == epoch:
                # keep control-action notes appended while this task was running
                final.extend(self._history[base_len:])
                self._history = _trim_history(final)

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
