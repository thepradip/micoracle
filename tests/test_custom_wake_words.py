"""Tests for Pro custom wake words (load + register into WAKE_VARIANTS)."""

from __future__ import annotations

import copy
import json

import pytest

import hands_free_voice as hfv


@pytest.fixture(autouse=True)
def restore_wake_variants():
    """Snapshot the global wake table so mutation never leaks across tests."""
    snapshot = copy.deepcopy(hfv.WAKE_VARIANTS)
    yield
    hfv.WAKE_VARIANTS.clear()
    hfv.WAKE_VARIANTS.update(snapshot)


class TestRegister:
    def test_register_adds_detectable_wake_word(self):
        hfv.register_custom_wake_words({"jarvis": ["jervis"]})
        wake, idx = hfv.detect_wake_word("jarvis open the file")
        assert wake == "jarvis"
        assert idx == 0

    def test_register_word_without_variants_matches_itself(self):
        added = hfv.register_custom_wake_words({"friday": []})
        assert added == ["friday"]
        assert hfv.detect_wake_word("friday deploy now")[0] == "friday"

    def test_register_does_not_break_builtin(self):
        hfv.register_custom_wake_words({"jarvis": []})
        assert hfv.detect_wake_word("claude refactor this")[0] == "claude"


class TestLoad:
    def test_missing_file_returns_empty(self, tmp_path):
        assert hfv.load_custom_wake_words(tmp_path / "none.json") == {}

    def test_load_mapping(self, tmp_path):
        path = tmp_path / "wake_words.json"
        path.write_text(json.dumps({"jarvis": ["jervis"], "friday": []}))
        result = hfv.load_custom_wake_words(path)
        assert result == {"jarvis": ["jervis"], "friday": []}

    def test_load_bare_list(self, tmp_path):
        path = tmp_path / "wake_words.json"
        path.write_text(json.dumps(["jarvis", "friday"]))
        result = hfv.load_custom_wake_words(path)
        assert result == {"jarvis": [], "friday": []}

    def test_load_corrupt_returns_empty(self, tmp_path):
        path = tmp_path / "wake_words.json"
        path.write_text("{ not json")
        assert hfv.load_custom_wake_words(path) == {}
