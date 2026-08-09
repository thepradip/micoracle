"""Tests for the agent loop and runner (scripted fake backend — no LLM)."""

import time

import agent
from agent_tools import ToolResult


class FakeToolBackend:
    """Returns scripted (text, tool_calls) tuples in order."""

    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.calls = []

    def complete_tools(self, system, messages, tools, max_tokens):
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        if not self.scripted:
            raise AssertionError("backend called more times than scripted")
        return self.scripted.pop(0)


class FakeRegistry:
    def __init__(self, results=None, confirmations=None):
        self.executed = []
        self.results = results or {}
        self.confirmations = confirmations or {}

    def specs(self):
        return [{"name": "t", "description": "d", "parameters": {"type": "object"}}]

    def needs_confirmation(self, name, args):
        return self.confirmations.get(name)

    def execute(self, name, args):
        self.executed.append((name, args))
        return self.results.get(name, ToolResult(True, "ok"))

    def close(self):
        pass


def call(name, arguments=None, cid="c1"):
    return {"id": cid, "name": name, "arguments": arguments or {}}


def make_session(backend, registry=None, confirm=lambda q: True,
                 ask_user=lambda q: "answer", speak=None):
    spoken = []
    session = agent.AgentSession(
        backend,
        registry or FakeRegistry(),
        agent.AgentConfig(max_iterations=5, answer_timeout_secs=0.1),
        speak=speak if speak is not None else spoken.append,
        confirm=confirm,
        ask_user=ask_user,
    )
    return session, spoken


class TestAgentSession:
    def test_happy_path_with_evidence(self):
        backend = FakeToolBackend([
            ("Opening the site.", [call("browser_open", {"url": "https://x.com"})]),
            ("", [call("task_complete", {
                "success": True, "summary": "Done.",
                "evidence": ["page title was X"],
            }, cid="c2")]),
        ])
        registry = FakeRegistry()
        session, spoken = make_session(backend, registry)
        report = session.run("open x.com")
        assert report.success is True
        assert report.evidence == ["page title was X"]
        assert registry.executed == [("browser_open", {"url": "https://x.com"})]
        assert "Opening the site." in spoken

    def test_success_without_evidence_rejected_then_downgraded(self):
        backend = FakeToolBackend([
            ("", [call("task_complete", {"success": True, "summary": "All done."})]),
            ("", [call("task_complete", {"success": True, "summary": "All done."}, cid="c2")]),
        ])
        session, _ = make_session(backend)
        report = session.run("do something")
        assert report.success is False
        assert "could not verify" in report.summary
        # the rejection message reached the model
        second_call_msgs = backend.calls[1]["messages"]
        assert any("REJECTED" in str(m.get("content")) for m in second_call_msgs)

    def test_failure_report_passes_through(self):
        backend = FakeToolBackend([
            ("", [call("task_complete", {"success": False, "summary": "The button does not exist."})]),
        ])
        session, _ = make_session(backend)
        report = session.run("click the missing button")
        assert report.success is False
        assert "does not exist" in report.summary

    def test_iteration_cap_is_honest(self):
        backend = FakeToolBackend([
            ("step", [call("t", cid=f"c{i}")]) for i in range(10)
        ])
        session, _ = make_session(backend)
        report = session.run("loop forever")
        assert report.success is False
        assert "ran out of steps" in report.summary.lower()

    def test_declined_confirmation_blocks_execution(self):
        backend = FakeToolBackend([
            ("", [call("t", {"destructive": True})]),
            ("", [call("task_complete", {"success": False, "summary": "Declined."}, cid="c2")]),
        ])
        registry = FakeRegistry(confirmations={"t": "This is destructive. Go ahead?"})
        session, _ = make_session(backend, registry, confirm=lambda q: False)
        report = session.run("delete everything")
        assert registry.executed == []          # tool never ran
        msgs = backend.calls[1]["messages"]
        assert any("DECLINED" in str(m.get("content")) for m in msgs)
        assert report.success is False

    def test_accepted_confirmation_executes(self):
        backend = FakeToolBackend([
            ("", [call("t", {"destructive": True})]),
            ("", [call("task_complete", {"success": True, "summary": "s",
                                          "evidence": ["e"]}, cid="c2")]),
        ])
        registry = FakeRegistry(confirmations={"t": "Sure?"})
        session, _ = make_session(backend, registry, confirm=lambda q: True)
        session.run("go")
        assert registry.executed == [("t", {"destructive": True})]

    def test_ask_user_roundtrip(self):
        backend = FakeToolBackend([
            ("", [call("ask_user", {"question": "Which form?"})]),
            ("", [call("task_complete", {"success": False, "summary": "ok"}, cid="c2")]),
        ])
        session, _ = make_session(backend, ask_user=lambda q: "the login form")
        session.run("fill the form")
        msgs = backend.calls[1]["messages"]
        assert any("user said: the login form" in str(m.get("content")) for m in msgs)

    def test_ask_user_timeout_recorded(self):
        backend = FakeToolBackend([
            ("", [call("ask_user", {"question": "Which?"})]),
            ("", [call("task_complete", {"success": False, "summary": "ok"}, cid="c2")]),
        ])
        session, _ = make_session(backend, ask_user=lambda q: None)
        session.run("x")
        msgs = backend.calls[1]["messages"]
        assert any("no answer" in str(m.get("content")) for m in msgs)

    def test_chat_reply_without_tools(self):
        backend = FakeToolBackend([("Paris.", [])])
        session, _ = make_session(backend)
        report = session.run("capital of france?")
        assert report.success is True
        assert report.summary == "Paris."

    def test_abort_before_iteration(self):
        backend = FakeToolBackend([("step", [call("t")])] * 5)
        session, _ = make_session(backend)
        session.abort()
        report = session.run("x")
        assert report.aborted is True

    def test_model_error_is_reported(self):
        class ExplodingBackend:
            def complete_tools(self, *a, **k):
                raise RuntimeError("api down")

        session, _ = make_session(ExplodingBackend())
        report = session.run("x")
        assert report.success is False
        assert "api down" in report.summary

    def test_failed_tool_result_marked(self):
        backend = FakeToolBackend([
            ("", [call("t")]),
            ("", [call("task_complete", {"success": False, "summary": "s"}, cid="c2")]),
        ])
        registry = FakeRegistry(results={"t": ToolResult(False, "no element matched")})
        session, _ = make_session(backend, registry)
        session.run("x")
        msgs = backend.calls[1]["messages"]
        assert any("FAILED: no element matched" in str(m.get("content")) for m in msgs)


class TestSpeechHelpers:
    def test_stop_phrases(self):
        assert agent.is_stop_phrase("Stop.")
        assert agent.is_stop_phrase("cancel")
        assert agent.is_stop_phrase("stop the task")
        assert not agent.is_stop_phrase("open stopwatch")

    def test_yes_no(self):
        assert agent.is_yes("Yes!")
        assert agent.is_yes("go ahead")
        assert agent.is_no("no")
        assert not agent.is_yes("maybe")


class TestAgentRunner:
    def _runner(self, backend, registry=None):
        spoken = []
        runner = agent.AgentRunner(
            backend, registry or FakeRegistry(), spoken.append,
            config=agent.AgentConfig(max_iterations=5, answer_timeout_secs=1.0),
        )
        return runner, spoken

    def _wait_idle(self, runner, timeout=3.0):
        deadline = time.time() + timeout
        while runner.busy and time.time() < deadline:
            time.sleep(0.01)
        assert not runner.busy, "runner never went idle"

    def test_submit_runs_and_speaks_report(self):
        backend = FakeToolBackend([
            ("", [call("task_complete", {"success": True, "summary": "Done.",
                                          "evidence": ["saw it"]})]),
        ])
        runner, spoken = self._runner(backend)
        assert runner.submit("do the thing") is True
        self._wait_idle(runner)
        assert any("Done." in s for s in spoken)

    def test_busy_rejects_second_task(self):
        backend = FakeToolBackend([
            ("", [call("ask_user", {"question": "hold"})]),
            ("", [call("task_complete", {"success": False, "summary": "s"}, cid="c2")]),
        ])
        runner, _ = self._runner(backend)
        runner.submit("first")
        deadline = time.time() + 2.0
        while not runner.awaiting_input and time.time() < deadline:
            time.sleep(0.01)
        assert runner.submit("second") is False
        runner.feed_user_speech("whatever")
        self._wait_idle(runner)

    def test_feed_speech_stop_aborts(self):
        backend = FakeToolBackend([
            ("", [call("ask_user", {"question": "?"})]),
            ("", [call("task_complete", {"success": False, "summary": "s"}, cid="c2")]),
        ])
        runner, spoken = self._runner(backend)
        runner.submit("task")
        deadline = time.time() + 2.0
        while not runner.awaiting_input and time.time() < deadline:
            time.sleep(0.01)
        assert runner.feed_user_speech("stop") is True
        self._wait_idle(runner)
        assert any("Stopped" in s or "cancelled" in s.lower() for s in spoken)

    def test_feed_speech_ignored_when_idle(self):
        backend = FakeToolBackend([])
        runner, _ = self._runner(backend)
        assert runner.feed_user_speech("hello") is False


class TestMakeRunner:
    def test_none_without_backend(self, monkeypatch):
        import jarvis

        monkeypatch.setattr(jarvis, "make_tool_backend", lambda: None)
        assert agent.make_runner(lambda s: None) is None

    def test_none_without_any_capability(self, monkeypatch):
        import browser
        import cli_agents
        import jarvis

        monkeypatch.setattr(jarvis, "make_tool_backend", lambda: object())
        monkeypatch.setattr(browser, "is_available", lambda: False)
        monkeypatch.setattr(cli_agents, "claude_available", lambda: False)
        monkeypatch.setattr(cli_agents, "codex_available", lambda: False)
        assert agent.make_runner(lambda s: None) is None
