"""Tests for the local usage analytics tracker."""

from __future__ import annotations

import json

import analytics


class TestEstimates:
    def test_local_backend_is_free(self):
        assert analytics.estimate_cost(100, "mlx") == 0.0
        assert analytics.estimate_cost(100, "faster") == 0.0

    def test_cloud_backend_costs_something(self):
        assert analytics.estimate_cost(150, "openai") > 0.0

    def test_unknown_backend_uses_fallback(self):
        # Falls back to the default per-minute rate rather than zero.
        assert analytics.estimate_cost(150, "made-up") > 0.0


class TestTracker:
    def test_record_writes_event(self, tmp_path):
        log = tmp_path / "usage.jsonl"
        tracker = analytics.UsageTracker(log)
        ev = tracker.record(wake="claude", text="write a hello world function",
                            backend="groq", now=100.0)
        assert ev is not None
        assert ev["words"] == 5
        assert ev["wake"] == "claude"
        assert log.exists()
        line = json.loads(log.read_text().splitlines()[0])
        assert line["backend"] == "groq"

    def test_disabled_tracker_writes_nothing(self, tmp_path):
        log = tmp_path / "usage.jsonl"
        tracker = analytics.UsageTracker(log, enabled=False)
        assert tracker.record(wake="claude", text="hi", backend="mlx") is None
        assert not log.exists()

    def test_macro_flag_recorded(self, tmp_path):
        log = tmp_path / "usage.jsonl"
        tracker = analytics.UsageTracker(log)
        tracker.record(wake="codex", text="commit", backend="mlx",
                       macro="ship it", now=1.0)
        ev = json.loads(log.read_text().splitlines()[0])
        assert ev["macro"] == "ship it"


class TestSummary:
    def _log(self, tmp_path):
        log = tmp_path / "usage.jsonl"
        tracker = analytics.UsageTracker(log)
        tracker.record(wake="claude", text="aaa bbb ccc", backend="mlx", now=10.0)
        tracker.record(wake="codex", text="ddd eee", backend="groq",
                       macro="ship it", now=20.0)
        return log

    def test_summarize_counts(self, tmp_path):
        log = self._log(tmp_path)
        s = analytics.summarize(log)
        assert s.commands == 2
        assert s.words == 5
        assert s.macro_uses == 1
        assert s.by_backend == {"mlx": 1, "groq": 1}
        assert s.by_wake == {"claude": 1, "codex": 1}
        assert s.first_ts == 10.0
        assert s.last_ts == 20.0

    def test_summarize_missing_file(self, tmp_path):
        s = analytics.summarize(tmp_path / "nope.jsonl")
        assert s.commands == 0

    def test_summarize_tolerates_corrupt_line(self, tmp_path):
        log = tmp_path / "usage.jsonl"
        log.write_text(
            json.dumps({"ts": 1.0, "wake": "claude", "backend": "mlx",
                        "words": 2, "chars": 5, "time_saved_secs": 1.0,
                        "cost_usd": 0.0}) + "\n"
            "{ partial line not yet flushed"
        )
        s = analytics.summarize(log)
        assert s.commands == 1

    def test_time_saved_human_format(self):
        s = analytics.Summary(time_saved_secs=3725)
        assert s.time_saved_human == "1h 2m"


class TestExport:
    def _log(self, tmp_path):
        log = tmp_path / "usage.jsonl"
        analytics.UsageTracker(log).record(
            wake="claude", text="aaa bbb", backend="mlx", now=10.0)
        return log

    def test_export_json(self, tmp_path):
        out = analytics.export("json", self._log(tmp_path))
        data = json.loads(out)
        assert len(data) == 1
        assert data[0]["wake"] == "claude"

    def test_export_csv_has_header(self, tmp_path):
        out = analytics.export("csv", self._log(tmp_path))
        lines = out.strip().splitlines()
        assert lines[0].startswith("ts,wake,backend")
        assert len(lines) == 2

    def test_export_bad_format(self, tmp_path):
        import pytest
        with pytest.raises(ValueError):
            analytics.export("xml", self._log(tmp_path))

    def test_render_summary_empty(self):
        assert "No usage" in analytics.render_summary(analytics.Summary())

    def test_render_summary_populated(self, tmp_path):
        log = tmp_path / "usage.jsonl"
        analytics.UsageTracker(log).record(
            wake="claude", text="aaa bbb ccc", backend="groq", now=1.0)
        out = analytics.render_summary(analytics.summarize(log))
        assert "Commands dispatched" in out
        assert "groq" in out
