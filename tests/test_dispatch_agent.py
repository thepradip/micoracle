"""Dispatch routing with the agent runner wired in (no audio, no LLM)."""

import control
import hands_free_voice as hfv


class FakeAdapter:
    supported_apps = {"Terminal"}

    def __init__(self, frontmost="Terminal"):
        self.pasted = []
        self.frontmost = frontmost

    def paste_and_return(self, text, target):
        self.pasted.append((text, target))

    def get_frontmost_app(self):
        if isinstance(self.frontmost, Exception):
            raise self.frontmost
        return self.frontmost


class FakeTTS:
    def __init__(self):
        self.spoken = []

    def speak(self, phrase):
        self.spoken.append(phrase)


class FakeRunner:
    def __init__(self, busy=False, consumes=False):
        self.busy = busy
        self.consumes = consumes
        self.submitted = []
        self.fed = []

    def submit(self, instruction):
        if self.busy:
            return False
        self.submitted.append(instruction)
        return True

    def feed_user_speech(self, text):
        self.fed.append(text)
        return self.consumes


class FakeJarvis:
    def __init__(self):
        self.asked = []

    def ask(self, prompt):
        self.asked.append(prompt)
        return "chat reply"


def make_ctx(agent=None, jarvis=None, target_app="Terminal", frontmost="Terminal"):
    adapter = FakeAdapter(frontmost=frontmost)
    tts = FakeTTS()
    ctx = hfv.DispatchContext(
        adapter=adapter, target_app=target_app, tts=tts,
        command_backend="mlx", jarvis=jarvis, agent=agent,
    )
    return ctx, adapter, tts


class TestMicoracleRouting:
    def test_control_fast_path_wins_over_agent(self, monkeypatch):
        runner = FakeRunner()
        ctx, _, tts = make_ctx(agent=runner)
        monkeypatch.setattr(
            control, "route",
            lambda cmd: control.ActionResult(True, "screenshot", "p", "Saved"),
        )
        monkeypatch.setattr(hfv, "_control", control)
        hfv._dispatch(ctx, "micoracle", "take a screenshot")
        assert runner.submitted == []
        assert "Saved" in tts.spoken

    def test_agent_gets_non_control_commands(self, monkeypatch):
        runner = FakeRunner()
        ctx, adapter, _ = make_ctx(agent=runner, jarvis=FakeJarvis())
        monkeypatch.setattr(hfv._control, "route", lambda cmd: None)
        hfv._dispatch(ctx, "micoracle", "open hacker news and read the headlines")
        assert runner.submitted == ["open hacker news and read the headlines"]
        assert ctx.jarvis.asked == []       # agent replaces plain chat
        assert adapter.pasted == []

    def test_busy_agent_speaks_still_working(self, monkeypatch):
        runner = FakeRunner(busy=True)
        ctx, _, tts = make_ctx(agent=runner)
        monkeypatch.setattr(hfv._control, "route", lambda cmd: None)
        hfv._dispatch(ctx, "micoracle", "another task")
        assert any("Still working" in s for s in tts.spoken)

    def test_compound_command_skips_control_and_goes_to_agent(self, monkeypatch):
        runner = FakeRunner()
        ctx, _, _ = make_ctx(agent=runner)
        routed = []
        monkeypatch.setattr(hfv._control, "route", lambda cmd: routed.append(cmd))
        hfv._dispatch(ctx, "micoracle", "open safari and type hello world")
        assert routed == []                 # control layer never consulted
        assert runner.submitted == ["open safari and type hello world"]

    def test_compound_command_without_agent_uses_control(self, monkeypatch):
        ctx, _, tts = make_ctx(agent=None, jarvis=FakeJarvis())
        monkeypatch.setattr(
            hfv._control, "route",
            lambda cmd: control.ActionResult(True, "open_app", "Safari", "Opening Safari"),
        )
        hfv._dispatch(ctx, "micoracle", "open safari and type hello world")
        assert "Opening Safari" in tts.spoken
        assert ctx.jarvis.asked == []

    def test_failed_control_action_falls_back_to_agent(self, monkeypatch):
        runner = FakeRunner()
        ctx, _, tts = make_ctx(agent=runner)
        monkeypatch.setattr(
            hfv._control, "route",
            lambda cmd: control.ActionResult(
                False, "open_app", "Hacker News And Read", "I couldn't open that",
            ),
        )
        hfv._dispatch(ctx, "micoracle", "open hacker news and read the headlines")
        assert runner.submitted == ["open hacker news and read the headlines"]
        assert "I couldn't open that" not in tts.spoken

    def test_failed_action_without_agent_still_reported(self, monkeypatch):
        fake_jarvis = FakeJarvis()
        ctx, _, tts = make_ctx(agent=None, jarvis=fake_jarvis)
        monkeypatch.setattr(
            hfv._control, "route",
            lambda cmd: control.ActionResult(False, "open_app", "Slack", "I couldn't open Slack"),
        )
        hfv._dispatch(ctx, "micoracle", "open slack")
        assert "I couldn't open Slack" in tts.spoken
        assert fake_jarvis.asked == []

    def test_no_agent_falls_back_to_jarvis(self, monkeypatch):
        fake_jarvis = FakeJarvis()
        ctx, _, tts = make_ctx(agent=None, jarvis=fake_jarvis)
        monkeypatch.setattr(hfv._control, "route", lambda cmd: None)
        hfv._dispatch(ctx, "micoracle", "tell me a joke")
        assert fake_jarvis.asked == ["tell me a joke"]
        assert "chat reply" in tts.spoken


class TestDictationRegression:
    def test_claude_wake_still_pastes(self):
        runner = FakeRunner()
        ctx, adapter, tts = make_ctx(agent=runner)
        hfv._dispatch(ctx, "claude", "hello world")
        assert adapter.pasted == [("hello world", "Terminal")]
        assert runner.submitted == []
        assert "sent" in tts.spoken

    def test_codex_wake_still_pastes(self):
        ctx, adapter, _ = make_ctx(agent=FakeRunner())
        hfv._dispatch(ctx, "codex", "run the tests")
        assert adapter.pasted == [("run the tests", "Terminal")]


class TestSmartClaudeCodexRouting:
    def test_terminal_focused_dictates(self):
        runner = FakeRunner()
        ctx, adapter, _ = make_ctx(agent=runner, target_app="", frontmost="Terminal")
        hfv._dispatch(ctx, "claude", "fix the bug")
        assert adapter.pasted == [("fix the bug", "")]
        assert runner.submitted == []

    def test_non_terminal_focused_goes_to_agent(self):
        runner = FakeRunner()
        ctx, adapter, _ = make_ctx(agent=runner, target_app="", frontmost="Safari")
        hfv._dispatch(ctx, "claude", "summarize the readme")
        assert adapter.pasted == []
        assert len(runner.submitted) == 1
        assert runner.submitted[0].startswith("summarize the readme")
        assert "cli_claude" in runner.submitted[0]

    def test_codex_task_prefers_codex_cli(self):
        runner = FakeRunner()
        ctx, _, _ = make_ctx(agent=runner, target_app="", frontmost="Finder")
        hfv._dispatch(ctx, "codex", "run the test suite")
        assert "cli_codex" in runner.submitted[0]

    def test_pinned_target_always_dictates(self):
        runner = FakeRunner()
        ctx, adapter, _ = make_ctx(agent=runner, target_app="Terminal", frontmost="Safari")
        hfv._dispatch(ctx, "claude", "hello")
        assert adapter.pasted == [("hello", "Terminal")]
        assert runner.submitted == []

    def test_no_agent_falls_back_to_dictation(self):
        ctx, adapter, _ = make_ctx(agent=None, target_app="", frontmost="Safari")
        hfv._dispatch(ctx, "claude", "hello")
        assert adapter.pasted == [("hello", "")]

    def test_frontmost_error_falls_back_to_dictation(self):
        runner = FakeRunner()
        ctx, adapter, _ = make_ctx(
            agent=runner, target_app="", frontmost=RuntimeError("wayland"),
        )
        hfv._dispatch(ctx, "claude", "hello")
        assert adapter.pasted == [("hello", "")]
        assert runner.submitted == []

    def test_busy_agent_speaks_still_working(self):
        runner = FakeRunner(busy=True)
        ctx, adapter, tts = make_ctx(agent=runner, target_app="", frontmost="Safari")
        hfv._dispatch(ctx, "codex", "another task")
        assert adapter.pasted == []
        assert any("Still working" in s for s in tts.spoken)


class TestContextDefaults:
    def test_agent_field_defaults_none(self):
        ctx, _, _ = make_ctx()
        assert ctx.agent is None
