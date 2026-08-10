"""Conversation-mode utterance routing — handle_utterance with fakes."""

from __future__ import annotations

import time

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
        return self.frontmost


class FakeTTS:
    def __init__(self):
        self.spoken = []

    def speak(self, phrase):
        self.spoken.append(phrase)


class FakeRunner:
    def __init__(self, busy=False, consumes=False, full=False):
        self.busy = busy
        self.consumes = consumes
        self.full = full
        self.submitted = []
        self.fed = []
        self.cancels = 0
        self.resets = 0
        self.notes = []

    def submit(self, instruction):
        if self.full:
            return False
        self.submitted.append(instruction)
        return True

    def feed_user_speech(self, text):
        self.fed.append(text)
        return self.consumes

    def cancel_all(self):
        self.cancels += 1
        self.busy = False

    def reset_history(self):
        self.resets += 1

    def note(self, text):
        self.notes.append(text)


def make_ctx(agent=None, session=None, echo=None, target_app="Terminal"):
    return hfv.DispatchContext(
        adapter=FakeAdapter(), target_app=target_app, tts=FakeTTS(),
        command_backend="mlx", agent=agent, session=session, echo=echo,
    )


def started_session():
    s = hfv.ConversationSession()
    s.start()
    return s


class TestSessionLifecycle:
    def test_micoracle_command_starts_session(self, monkeypatch):
        runner = FakeRunner()
        session = hfv.ConversationSession()
        ctx = make_ctx(agent=runner, session=session)
        monkeypatch.setattr(hfv._control, "route", lambda cmd: None)
        hfv.handle_utterance(ctx, hfv.WakeState(), "micoracle summarize hacker news")
        assert session.active()
        assert runner.submitted == ["summarize hacker news"]

    def test_bare_micoracle_starts_session(self):
        session = hfv.ConversationSession()
        ctx = make_ctx(agent=FakeRunner(), session=session)
        hfv.handle_utterance(ctx, hfv.WakeState(), "micoracle")
        assert session.active()
        assert "listening" in ctx.tts.spoken

    def test_followup_needs_no_wake_word(self, monkeypatch):
        runner = FakeRunner()
        ctx = make_ctx(agent=runner, session=started_session())
        monkeypatch.setattr(hfv._control, "route", lambda cmd: None)
        hfv.handle_utterance(ctx, hfv.WakeState(), "search for python tutorials")
        assert runner.submitted == ["search for python tutorials"]
        assert "Searching." in ctx.tts.spoken

    def test_without_session_bare_text_is_ignored(self):
        runner = FakeRunner()
        ctx = make_ctx(agent=runner, session=hfv.ConversationSession())
        hfv.handle_utterance(ctx, hfv.WakeState(), "search for python tutorials")
        assert runner.submitted == []

    def test_expired_session_reverts_to_classic(self):
        runner = FakeRunner()
        session = hfv.ConversationSession(timeout_secs=0.01)
        session.start()
        time.sleep(0.03)
        ctx = make_ctx(agent=runner, session=session)
        hfv.handle_utterance(ctx, hfv.WakeState(), "search for cats please")
        assert runner.submitted == []


class TestSessionStop:
    def test_stop_ends_session_when_idle(self):
        runner = FakeRunner(busy=False)
        session = started_session()
        ctx = make_ctx(agent=runner, session=session)
        hfv.handle_utterance(ctx, hfv.WakeState(), "stop")
        assert not session.active()
        assert runner.cancels == 1
        assert runner.resets == 1
        assert "Stopped." in ctx.tts.spoken

    def test_stop_micoracle_variant(self):
        runner = FakeRunner()
        session = started_session()
        ctx = make_ctx(agent=runner, session=session)
        hfv.handle_utterance(ctx, hfv.WakeState(), "stop micoracle")
        assert not session.active()

    def test_micoracle_stop_variant(self):
        runner = FakeRunner()
        session = started_session()
        ctx = make_ctx(agent=runner, session=session)
        hfv.handle_utterance(ctx, hfv.WakeState(), "micoracle stop")
        assert not session.active()
        assert runner.cancels == 1

    def test_stop_while_busy_does_not_double_speak(self):
        runner = FakeRunner(busy=True)
        session = started_session()
        ctx = make_ctx(agent=runner, session=session)
        hfv.handle_utterance(ctx, hfv.WakeState(), "stop it")
        assert not session.active()
        assert runner.cancels == 1
        # the aborted task's own report speaks; the worker stays silent
        assert "Stopped." not in ctx.tts.spoken

    def test_stop_wins_over_busy_feed(self):
        # a busy runner that would consume anything must not swallow "stop"
        runner = FakeRunner(busy=True, consumes=True)
        session = started_session()
        ctx = make_ctx(agent=runner, session=session)
        hfv.handle_utterance(ctx, hfv.WakeState(), "stop")
        assert not session.active()
        assert runner.fed == []  # feed_user_speech never reached


class TestSessionAnswers:
    def test_answer_fed_to_pending_question_not_dispatched(self):
        runner = FakeRunner(busy=True, consumes=True)
        session = started_session()
        ctx = make_ctx(agent=runner, session=session)
        hfv.handle_utterance(ctx, hfv.WakeState(), "the login form")
        assert runner.fed == ["the login form"]
        assert runner.submitted == []
        assert session.active()


class TestSessionGuards:
    def test_own_tts_echo_dropped(self):
        guard = hfv.EchoGuard()
        guard.note("Opening the page now")
        runner = FakeRunner()
        ctx = make_ctx(agent=runner, session=started_session(), echo=guard)
        hfv.handle_utterance(ctx, hfv.WakeState(), "Opening the page now")
        assert runner.submitted == []

    def test_single_word_noise_ignored(self):
        runner = FakeRunner()
        ctx = make_ctx(agent=runner, session=started_session())
        hfv.handle_utterance(ctx, hfv.WakeState(), "cough")
        assert runner.submitted == []

    def test_single_word_control_action_executes(self, monkeypatch):
        runner = FakeRunner()
        session = started_session()
        ctx = make_ctx(agent=runner, session=session)
        monkeypatch.setattr(
            hfv._control, "route",
            lambda cmd: control.ActionResult(True, "screenshot", "p", "Saved"),
        )
        hfv.handle_utterance(ctx, hfv.WakeState(), "screenshot")
        assert "Saved" in ctx.tts.spoken
        # the agent's next task learns what happened outside its loop
        assert runner.notes and "screenshot" in runner.notes[0]


class TestSessionDictation:
    def test_claude_dictation_still_works_in_session(self):
        runner = FakeRunner()
        session = started_session()
        ctx = make_ctx(agent=runner, session=session)
        hfv.handle_utterance(ctx, hfv.WakeState(), "claude fix the bug")
        assert ctx.adapter.pasted == [("fix the bug", "Terminal")]
        assert runner.submitted == []
        assert session.active()

    def test_bare_claude_arms_two_step_followup(self):
        runner = FakeRunner()
        ctx = make_ctx(agent=runner, session=started_session())
        ws = hfv.WakeState()
        hfv.handle_utterance(ctx, ws, "claude")
        assert ws.active_backend() == "claude"
        hfv.handle_utterance(ctx, ws, "fix the tests")
        assert ctx.adapter.pasted == [("fix the tests", "Terminal")]
        assert runner.submitted == []
