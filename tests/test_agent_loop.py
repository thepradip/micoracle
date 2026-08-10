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

    def test_stop_with_filler_prefix(self):
        assert agent.is_stop_phrase("Ok stop it.")
        assert agent.is_stop_phrase("okay, stop")
        assert agent.is_stop_phrase("please stop micoracle")
        assert not agent.is_stop_phrase("okay")            # bare filler is not stop
        assert not agent.is_stop_phrase("okay open safari")

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

    def _wait_awaiting(self, runner, timeout=2.0):
        deadline = time.time() + timeout
        while not runner.awaiting_input and time.time() < deadline:
            time.sleep(0.01)
        assert runner.awaiting_input, "runner never reached ask_user"

    def test_second_task_queues_and_runs_in_order(self):
        backend = FakeToolBackend([
            ("", [call("task_complete", {"success": True, "summary": "first done",
                                          "evidence": ["e1"]})]),
            ("", [call("task_complete", {"success": True, "summary": "second done",
                                          "evidence": ["e2"]}, cid="c2")]),
        ])
        runner, spoken = self._runner(backend)
        assert runner.submit("first") is True
        assert runner.submit("second") is True      # queued, not rejected
        self._wait_idle(runner)
        i1 = next(i for i, s in enumerate(spoken) if "first done" in s)
        i2 = next(i for i, s in enumerate(spoken) if "second done" in s)
        assert i1 < i2

    def test_submit_false_only_when_queue_full(self):
        backend = FakeToolBackend([
            ("", [call("ask_user", {"question": "hold"})]),
            ("", [call("task_complete", {"success": False, "summary": "s"}, cid="c2")]),
        ])
        runner, _ = self._runner(backend)
        runner.submit("running")
        self._wait_awaiting(runner)                 # loop is blocked in ask_user
        for i in range(agent.AgentRunner.MAX_QUEUED):
            assert runner.submit(f"queued {i}") is True
        assert runner.submit("overflow") is False   # 1 running + 5 queued
        runner.cancel_all()
        self._wait_idle(runner)

    def test_cancel_all_drains_queue_and_aborts(self):
        backend = FakeToolBackend([
            ("", [call("ask_user", {"question": "hold"})]),
            ("", [call("task_complete", {"success": False, "summary": "s"}, cid="c2")]),
        ])
        runner, _ = self._runner(backend)
        runner.submit("running")
        self._wait_awaiting(runner)
        runner.submit("queued")
        runner.cancel_all()
        self._wait_idle(runner)
        assert len(backend.calls) == 1              # queued task never started

    def test_feed_stop_drains_queue_too(self):
        backend = FakeToolBackend([
            ("", [call("ask_user", {"question": "hold"})]),
            ("", [call("task_complete", {"success": False, "summary": "s"}, cid="c2")]),
        ])
        runner, _ = self._runner(backend)
        runner.submit("running")
        self._wait_awaiting(runner)
        runner.submit("queued")
        assert runner.feed_user_speech("stop") is True
        self._wait_idle(runner)
        assert len(backend.calls) == 1

    def test_stale_stop_answer_is_drained_before_next_task(self):
        # regression: an idle abort() parks "stop" in _answers; the next
        # task's first ask_user must not instantly receive it
        backend = FakeToolBackend([
            ("", [call("ask_user", {"question": "which?"})]),
            ("", [call("task_complete", {"success": False, "summary": "s"}, cid="c2")]),
        ])
        spoken = []
        runner = agent.AgentRunner(
            backend, FakeRegistry(), spoken.append,
            config=agent.AgentConfig(max_iterations=5, answer_timeout_secs=0.2),
        )
        runner.abort()                              # idle abort — stale "stop"
        runner.submit("task")
        self._wait_idle(runner)
        msgs = backend.calls[1]["messages"]
        assert not any("user said: stop" in str(m.get("content")) for m in msgs)
        assert any("no answer" in str(m.get("content")) for m in msgs)

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


def _no_unpaired_tool_calls(messages) -> bool:
    """Every assistant tool_call must be answered by a following tool message."""
    i = 0
    while i < len(messages):
        m = messages[i]
        i += 1
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        answered = set()
        while i < len(messages) and messages[i].get("role") == "tool":
            answered.add(messages[i].get("tool_call_id"))
            i += 1
        if {c["id"] for c in m["tool_calls"]} - answered:
            return False
    return True


class TestSharedHistory:
    def _runner(self, backend):
        spoken = []
        runner = agent.AgentRunner(
            backend, FakeRegistry(), spoken.append,
            config=agent.AgentConfig(max_iterations=5, answer_timeout_secs=0.5),
        )
        return runner, spoken

    def _wait_idle(self, runner, timeout=3.0):
        deadline = time.time() + timeout
        while runner.busy and time.time() < deadline:
            time.sleep(0.01)
        assert not runner.busy

    def _two_task_backend(self):
        return FakeToolBackend([
            ("", [call("task_complete", {"success": True, "summary": "opened example",
                                          "evidence": ["url"]})]),
            ("", [call("task_complete", {"success": True, "summary": "done",
                                          "evidence": ["e"]}, cid="c2")]),
        ])

    def test_second_task_sees_first_and_boundary_note(self):
        backend = self._two_task_backend()
        runner, _ = self._runner(backend)
        runner.submit("open example.com")
        self._wait_idle(runner)
        runner.submit("read it")
        self._wait_idle(runner)
        msgs = backend.calls[1]["messages"]
        joined = str(msgs)
        assert "open example.com" in joined
        assert "previous task ended: success=True" in joined
        assert _no_unpaired_tool_calls(msgs)        # no API-400 histories
        assert msgs[0]["role"] == "user"

    def test_reset_history_forgets_prior_task(self):
        backend = self._two_task_backend()
        runner, _ = self._runner(backend)
        runner.submit("open example.com")
        self._wait_idle(runner)
        runner.reset_history()
        runner.submit("read it")
        self._wait_idle(runner)
        msgs = backend.calls[1]["messages"]
        assert "open example.com" not in str(msgs)

    def test_note_reaches_next_task(self):
        backend = FakeToolBackend([
            ("", [call("task_complete", {"success": True, "summary": "s",
                                          "evidence": ["e"]})]),
        ])
        runner, _ = self._runner(backend)
        runner.note("open_app Safari ok=True")
        runner.submit("what did I just open?")
        self._wait_idle(runner)
        msgs = backend.calls[0]["messages"]
        assert any("open_app Safari" in str(m.get("content")) for m in msgs)

    def test_reset_during_task_skips_commit(self):
        backend = FakeToolBackend([
            ("", [call("ask_user", {"question": "hold"})]),
            ("", [call("task_complete", {"success": True, "summary": "s",
                                          "evidence": ["e"]}, cid="c2")]),
        ])
        runner, _ = self._runner(backend)
        runner.submit("task")
        deadline = time.time() + 2.0
        while not runner.awaiting_input and time.time() < deadline:
            time.sleep(0.01)
        runner.reset_history()                      # session ended mid-task
        runner.feed_user_speech("go ahead")
        self._wait_idle(runner)
        assert runner._history == []                # stale-epoch commit skipped


class TestHistoryHelpers:
    def test_sanitize_appends_closure_for_unanswered_call(self):
        msgs = [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "name": "task_complete", "arguments": {}}]},
        ]
        out = agent._sanitize_history(msgs)
        assert out[-1]["role"] == "tool"
        assert out[-1]["tool_call_id"] == "c1"

    def test_sanitize_keeps_answered_pairs_untouched(self):
        msgs = [
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "name": "t", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "t", "content": "ok"},
        ]
        assert agent._sanitize_history(msgs) == msgs

    def test_trim_cuts_at_user_boundary(self):
        msgs = [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "name": "t", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "t", "content": "ok"},
            {"role": "user", "content": "recent"},
            {"role": "assistant", "content": "reply"},
        ]
        out = agent._trim_history(msgs, cap=3)
        assert out[0] == {"role": "user", "content": "recent"}
        assert all(m["role"] != "tool" or i > 0 for i, m in enumerate(out))

    def test_trim_noop_under_cap(self):
        msgs = [{"role": "user", "content": "u"}]
        assert agent._trim_history(msgs, cap=30) == msgs


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
