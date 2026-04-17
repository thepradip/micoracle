"""Unit tests for wake-word detection and command extraction (pure functions)."""

from __future__ import annotations

from hands_free_voice import detect_wake_word, extract_command


class TestDetectWakeWord:
    def test_exact_match_claude(self):
        assert detect_wake_word("claude write a function") == ("claude", 0)

    def test_exact_match_codex(self):
        assert detect_wake_word("codex explain this code") == ("codex", 0)

    def test_case_insensitive(self):
        assert detect_wake_word("CODEX WRITE HELLO") == ("codex", 0)
        assert detect_wake_word("Claude write a test") == ("claude", 0)

    def test_wake_in_second_word(self):
        wake, idx = detect_wake_word("hey claude please help")
        assert wake == "claude"
        assert idx == 1

    def test_wake_in_third_word(self):
        wake, idx = detect_wake_word("um ok claude help")
        assert wake == "claude"
        assert idx == 2

    def test_wake_beyond_third_word_ignored(self):
        # Only the first 3 words are checked.
        wake, idx = detect_wake_word("one two three claude refactor")
        assert wake is None
        assert idx == -1

    def test_punctuation_stripped(self):
        assert detect_wake_word("Claude, write hello") == ("claude", 0)
        assert detect_wake_word('"codex" write hello') == ("codex", 0)

    def test_no_wake_word(self):
        assert detect_wake_word("hello world how are you") == (None, -1)

    def test_empty_string(self):
        assert detect_wake_word("") == (None, -1)

    def test_whitespace_only(self):
        assert detect_wake_word("     ") == (None, -1)

    def test_exact_variant_cloud_matches_claude(self):
        # "cloud" is in the claude variant set.
        assert detect_wake_word("cloud refactor this") == ("claude", 0)

    def test_exact_variant_codec_matches_codex(self):
        # "codec" is in the codex variant set.
        assert detect_wake_word("codec explain this") == ("codex", 0)

    def test_fuzzy_match_claudee(self):
        # "claudee" is close enough to "claude" to clear the 0.78 threshold.
        wake, _ = detect_wake_word("claudee write hello")
        assert wake == "claude"

    def test_fuzzy_below_threshold(self):
        # "correct" is too far from "codex" (~0.55 ratio) to match.
        assert detect_wake_word("correct answer here") == (None, -1)


class TestExtractCommand:
    def test_basic(self):
        assert extract_command("codex write hello", 0) == "write hello"

    def test_with_comma(self):
        # Trailing punctuation and leading comma handled by strip
        assert extract_command("codex, write hello", 0) == "write hello"

    def test_empty_after_wake(self):
        assert extract_command("codex", 0) == ""

    def test_trailing_period(self):
        assert extract_command("codex write hello.", 0) == "write hello"

    def test_non_zero_idx_drops_prefix(self):
        assert extract_command("hey codex write hello", 1) == "write hello"

    def test_only_wake_with_period(self):
        assert extract_command("codex.", 0) == ""

    def test_multiple_sentences(self):
        cmd = extract_command("codex write hello and then exit", 0)
        assert cmd == "write hello and then exit"
