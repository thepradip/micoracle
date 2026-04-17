"""Unit tests for Whisper hallucination filters."""

from __future__ import annotations

from hands_free_voice import looks_hallucinated, is_silence_hallucination


class TestLooksHallucinated:
    def test_short_text_returns_false(self):
        assert not looks_hallucinated("hello world")

    def test_empty_returns_false(self):
        assert not looks_hallucinated("")

    def test_three_word_chunk_repeated_three_times(self):
        chunk = "foo bar baz"
        assert looks_hallucinated(f"{chunk} {chunk} {chunk}")

    def test_four_word_chunk_repeated_three_times(self):
        chunk = "this is a test"
        assert looks_hallucinated(f"{chunk} {chunk} {chunk}")

    def test_five_word_chunk_repeated_three_times(self):
        chunk = "write a python program now"
        assert looks_hallucinated(f"{chunk} {chunk} {chunk}")

    def test_two_repeats_not_enough(self):
        chunk = "this is a test"
        # Only 2 repeats (8 words); function needs 3x same chunk and ≥9 words total
        assert not looks_hallucinated(f"{chunk} {chunk}")

    def test_normal_long_sentence_not_hallucinated(self):
        text = "can you please write a python program for prime numbers in the terminal"
        assert not looks_hallucinated(text)

    def test_long_run_of_same_word_is_caught(self):
        # A size-3 window of ["right", "right", "right"] repeated 3× matches,
        # so 9+ consecutive identical words register as a hallucination. This
        # is desirable: Whisper emits exactly this pattern on noisy silence
        # (the user's production log showed "right right right..." ×20).
        text = " ".join(["right"] * 15)
        assert looks_hallucinated(text)

    def test_short_run_of_same_word_not_caught(self):
        # Fewer than 9 words never triggers, regardless of repetition.
        text = " ".join(["right"] * 8)
        assert not looks_hallucinated(text)

    def test_repeat_embedded_in_longer_text(self):
        # Chunk repeated 3× somewhere inside a longer utterance still triggers.
        chunk = "thank you for"
        text = f"hello {chunk} {chunk} {chunk} goodbye everyone"
        assert looks_hallucinated(text)

    def test_case_lowered_before_comparison(self):
        chunk = "this is fine"
        text = f"{chunk.upper()} {chunk} {chunk}"
        assert looks_hallucinated(text)


class TestIsSilenceHallucination:
    def test_thank_you_period(self):
        assert is_silence_hallucination("Thank you.")

    def test_thank_you_no_period(self):
        assert is_silence_hallucination("thank you")

    def test_thanks_for_watching(self):
        assert is_silence_hallucination("thanks for watching")

    def test_amen(self):
        assert is_silence_hallucination("Amen.")

    def test_bye(self):
        assert is_silence_hallucination("bye")

    def test_you_alone(self):
        assert is_silence_hallucination("you")

    def test_period_only(self):
        assert is_silence_hallucination(".")

    def test_okay_variants(self):
        assert is_silence_hallucination("OK")
        assert is_silence_hallucination("okay.")

    def test_real_command_not_filtered(self):
        assert not is_silence_hallucination("write a python function")

    def test_empty_string_not_filtered(self):
        assert not is_silence_hallucination("")

    def test_leading_trailing_whitespace_stripped(self):
        assert is_silence_hallucination("  Thank you.  ")

    def test_mixed_case(self):
        assert is_silence_hallucination("THANK YOU.")
