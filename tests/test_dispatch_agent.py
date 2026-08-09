"""Dispatch routing with the agent runner wired in (no audio, no LLM)."""

import control
import hands_free_voice as hfv


class FakeAdapter:
    supported_apps = {"Terminal"}

    def __init__(self):
        self.pasted = []

    def paste_and_return(self, text, target):
        self.pasted.append((text, target))


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


def make_ctx(agent=None, jarvis=None):
    adapter = FakeAdapter()
    tts = FakeTTS()
    ctx = hfv.DispatchContext(
        adapter=adapter, target_app="Terminal", tts=tts,
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


class TestContextDefaults:
    def test_agent_field_defaults_none(self):
        ctx, _, _ = make_ctx()
        assert ctx.agent is None
