"""ConversationSession, EchoGuard, ack and session-noise helpers."""

from __future__ import annotations

import time

import hands_free_voice as hfv


class TestConversationSession:
    def test_inactive_initially(self):
        assert not hfv.ConversationSession().active()

    def test_start_activates(self):
        s = hfv.ConversationSession()
        s.start()
        assert s.active()

    def test_end_deactivates(self):
        s = hfv.ConversationSession()
        s.start()
        s.end()
        assert not s.active()

    def test_expires_after_timeout(self):
        s = hfv.ConversationSession(timeout_secs=0.03)
        s.start()
        time.sleep(0.05)
        assert not s.active()

    def test_touch_extends(self):
        s = hfv.ConversationSession(timeout_secs=0.15)
        s.start()
        time.sleep(0.08)
        s.touch()
        time.sleep(0.09)  # past the original expiry, inside the touched one
        assert s.active()

    def test_touch_on_expired_is_noop(self):
        s = hfv.ConversationSession(timeout_secs=0.01)
        s.start()
        time.sleep(0.03)
        s.touch()
        assert not s.active()

    def test_restart_after_expiry(self):
        s = hfv.ConversationSession(timeout_secs=0.01)
        s.start()
        time.sleep(0.03)
        s.start()
        assert s.active()


class TestEchoGuard:
    def test_exact_echo_detected(self):
        g = hfv.EchoGuard()
        g.note("Opening the page")
        assert g.is_echo("opening the page")

    def test_fuzzy_echo_detected(self):
        g = hfv.EchoGuard()
        g.note("Searching for cats on Google.")
        assert g.is_echo("searching for cats on google")

    def test_unrelated_text_passes(self):
        g = hfv.EchoGuard()
        g.note("Searching for cats on Google.")
        assert not g.is_echo("open the terminal and run the tests")

    def test_window_expiry(self):
        g = hfv.EchoGuard(window_secs=0.03)
        g.note("hello there my friend")
        time.sleep(0.05)
        assert not g.is_echo("hello there my friend")

    def test_empty_never_echo(self):
        g = hfv.EchoGuard()
        g.note("something spoken")
        assert not g.is_echo("")
        assert not g.is_echo("   ")


class TestAckFor:
    def test_verb_acks(self):
        assert hfv.ack_for("search for cats") == "Searching."
        assert hfv.ack_for("open the browser") == "Opening."
        assert hfv.ack_for("go to github.com") == "Opening."
        assert hfv.ack_for("type hello world") == "Typing."
        assert hfv.ack_for("check the weather") == "Checking."
        assert hfv.ack_for("read me the headlines") == "Checking."

    def test_fallback_rotates_through_generic_acks(self):
        acks = {hfv.ack_for("do the thing") for _ in range(6)}
        assert acks <= {"On it.", "Got it.", "Okay."}
        assert len(acks) == 3

    def test_empty_command_still_acks(self):
        assert hfv.ack_for("") in {"On it.", "Got it.", "Okay."}


class TestSessionNoise:
    def test_single_word_is_noise(self):
        assert hfv.is_session_noise("cough")
        assert hfv.is_session_noise("hello")

    def test_single_word_control_action_is_not_noise(self):
        assert not hfv.is_session_noise("screenshot")

    def test_multi_word_is_not_noise(self):
        assert not hfv.is_session_noise("open the browser")
        assert not hfv.is_session_noise("search cats")
