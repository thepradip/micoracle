"""Tests for the voice macro engine."""

from __future__ import annotations

import json

import pytest

import macros


@pytest.fixture()
def store():
    return macros.MacroStore([
        macros.Macro("refactor this", "Refactor the selected code. {args}"),
        macros.Macro("write tests for", "Write unit tests for {args}."),
        macros.Macro("ship it", "Commit all changes.", wake="codex"),
        macros.Macro("status", "git status"),
    ])


class TestExpansion:
    def test_exact_trigger_with_args(self, store):
        exp = store.expand("write tests for the parser")
        assert exp.matched
        assert exp.text == "Write unit tests for the parser."

    def test_trigger_no_args_slot_keeps_template(self, store):
        exp = store.expand("status")
        assert exp.matched
        assert exp.text == "git status"

    def test_no_args_slot_appends_extra_words(self, store):
        exp = store.expand("ship it now please", wake="codex")
        assert exp.matched
        assert exp.text == "Commit all changes. now please"

    def test_empty_args_slot_left_clean(self, store):
        exp = store.expand("refactor this")
        assert exp.matched
        assert exp.text == "Refactor the selected code."

    def test_no_match_returns_unchanged(self, store):
        exp = store.expand("please summarize this article")
        assert not exp.matched
        assert exp.text == "please summarize this article"

    def test_fuzzy_trigger_still_matches(self, store):
        # "refactor these" is close enough to "refactor this".
        exp = store.expand("refactor these for clarity")
        assert exp.matched
        assert exp.macro.trigger == "refactor this"

    def test_wake_scoped_macro_ignored_for_other_wake(self, store):
        exp = store.expand("ship it", wake="claude")
        assert not exp.matched

    def test_wake_scoped_macro_matches_when_wake_unknown(self, store):
        # No wake context provided -> scope is not enforced.
        exp = store.expand("ship it")
        assert exp.matched

    def test_empty_command(self, store):
        assert not store.expand("").matched


class TestPersistence:
    def test_load_missing_file_is_empty(self, tmp_path):
        assert len(macros.load_macros(tmp_path / "none.json")) == 0

    def test_write_and_load_defaults(self, tmp_path):
        path = tmp_path / "macros.json"
        macros.write_default_macros(path)
        store = macros.load_macros(path)
        assert len(store) == len(macros.default_macros())
        assert store.expand("explain this").matched

    def test_write_does_not_clobber(self, tmp_path):
        path = tmp_path / "macros.json"
        path.write_text(json.dumps({"macros": [
            {"trigger": "hello", "template": "Hi there"}]}))
        macros.write_default_macros(path)
        store = macros.load_macros(path)
        assert len(store) == 1  # untouched

    def test_load_skips_invalid_entries(self, tmp_path):
        path = tmp_path / "macros.json"
        path.write_text(json.dumps({"macros": [
            {"trigger": "ok", "template": "fine"},
            {"trigger": "", "template": "missing trigger"},
            {"template": "no trigger key"},
            "not a dict",
        ]}))
        store = macros.load_macros(path)
        assert len(store) == 1

    def test_load_corrupt_json_is_empty(self, tmp_path):
        path = tmp_path / "macros.json"
        path.write_text("{ this is not json")
        assert len(macros.load_macros(path)) == 0

    def test_load_yaml_when_available(self, tmp_path):
        pytest.importorskip("yaml")
        path = tmp_path / "macros.yaml"
        path.write_text(
            "macros:\n"
            "  - trigger: deploy\n"
            "    template: Deploy to production. {args}\n"
        )
        store = macros.load_macros(path)
        assert store.expand("deploy the api").text == "Deploy to production. the api"
