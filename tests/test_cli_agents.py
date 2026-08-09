"""Tests for the headless Claude Code / Codex runners (no real subprocesses)."""

import json
import subprocess
from types import SimpleNamespace

import cli_agents


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class RecordingRun:
    def __init__(self, proc=None, raise_timeout=False):
        self.calls = []
        self.proc = proc if proc is not None else _proc()
        self.raise_timeout = raise_timeout

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))
        return self.proc


class TestAvailability:
    def test_missing_binaries(self, monkeypatch):
        monkeypatch.setattr(cli_agents.shutil, "which", lambda name: None)
        assert cli_agents.claude_available() is False
        assert cli_agents.codex_available() is False
        assert cli_agents.run_claude("x").ok is False
        assert cli_agents.run_codex("x").ok is False

    def test_present_binaries(self, monkeypatch):
        monkeypatch.setattr(cli_agents.shutil, "which", lambda name: f"/bin/{name}")
        assert cli_agents.claude_available() is True
        assert cli_agents.codex_available() is True


class TestRunClaude:
    def _patch(self, monkeypatch, run):
        monkeypatch.setattr(cli_agents.shutil, "which", lambda n: "/bin/claude")
        monkeypatch.setattr(cli_agents.subprocess, "run", run)

    def test_argv_and_json_parse(self, monkeypatch):
        payload = json.dumps({"type": "result", "result": "Done. 2 files changed."})
        run = RecordingRun(_proc(stdout=payload))
        self._patch(monkeypatch, run)

        result = cli_agents.run_claude("fix the bug", timeout=30)
        argv = run.calls[0]["argv"]
        assert argv[:3] == ["claude", "-p", "fix the bug"]
        assert "--output-format" in argv and "json" in argv
        assert "--permission-mode" in argv and "acceptEdits" in argv
        assert "bypassPermissions" not in argv
        assert result.ok is True
        assert result.output == "Done. 2 files changed."

    def test_allowed_tools_flag(self, monkeypatch):
        run = RecordingRun(_proc(stdout='{"result": "ok"}'))
        self._patch(monkeypatch, run)
        cli_agents.run_claude("t", allowed_tools=["Read", "Grep"])
        argv = run.calls[0]["argv"]
        idx = argv.index("--allowedTools")
        assert argv[idx + 1] == "Read,Grep"

    def test_non_json_stdout_falls_back(self, monkeypatch):
        run = RecordingRun(_proc(stdout="plain text answer"))
        self._patch(monkeypatch, run)
        result = cli_agents.run_claude("t")
        assert result.ok is True
        assert result.output == "plain text answer"
        assert "not JSON" in result.detail

    def test_is_error_payload(self, monkeypatch):
        run = RecordingRun(_proc(stdout=json.dumps({"result": "x", "is_error": True})))
        self._patch(monkeypatch, run)
        result = cli_agents.run_claude("t")
        assert result.ok is False

    def test_nonzero_exit(self, monkeypatch):
        run = RecordingRun(_proc(returncode=2, stdout="", stderr="boom"))
        self._patch(monkeypatch, run)
        result = cli_agents.run_claude("t")
        assert result.ok is False
        assert result.exit_code == 2
        assert "boom" in result.detail

    def test_timeout(self, monkeypatch):
        run = RecordingRun(raise_timeout=True)
        self._patch(monkeypatch, run)
        result = cli_agents.run_claude("t", timeout=5)
        assert result.ok is False
        assert "timed out" in result.detail


class TestRunCodex:
    def _patch(self, monkeypatch, run):
        monkeypatch.setattr(cli_agents.shutil, "which", lambda n: "/bin/codex")
        monkeypatch.setattr(cli_agents.subprocess, "run", run)

    def test_argv_and_output_file(self, monkeypatch):
        run = RecordingRun(_proc())

        def fake_run(argv, **kwargs):
            # simulate codex writing its final message to the -o file
            out_path = argv[argv.index("-o") + 1]
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write("README summarized.\n")
            return RecordingRun.__call__(run, argv, **kwargs)

        self._patch(monkeypatch, fake_run)
        result = cli_agents.run_codex("summarize README", cwd="/tmp/proj")
        argv = run.calls[0]["argv"]
        assert argv[:3] == ["codex", "exec", "summarize README"]
        assert "--skip-git-repo-check" in argv
        assert argv[argv.index("--sandbox") + 1] == "workspace-write"
        assert argv[argv.index("-C") + 1] == "/tmp/proj"
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv
        assert result.ok is True
        assert result.output == "README summarized."

    def test_image_paths(self, monkeypatch):
        run = RecordingRun(_proc(stdout="fallback"))
        self._patch(monkeypatch, run)
        cli_agents.run_codex("look", image_paths=["/tmp/a.png", "/tmp/b.png"])
        argv = run.calls[0]["argv"]
        assert argv.count("-i") == 2
        assert "/tmp/a.png" in argv and "/tmp/b.png" in argv

    def test_empty_file_falls_back_to_stdout(self, monkeypatch):
        run = RecordingRun(_proc(stdout="stdout answer"))
        self._patch(monkeypatch, run)
        result = cli_agents.run_codex("t")
        assert result.ok is True
        assert result.output == "stdout answer"
        assert "final-message file empty" in result.detail

    def test_nonzero_exit_and_empty_output(self, monkeypatch):
        run = RecordingRun(_proc(returncode=1, stdout="", stderr="sandbox denied"))
        self._patch(monkeypatch, run)
        result = cli_agents.run_codex("t")
        assert result.ok is False
        assert "sandbox denied" in result.detail

    def test_timeout(self, monkeypatch):
        run = RecordingRun(raise_timeout=True)
        self._patch(monkeypatch, run)
        result = cli_agents.run_codex("t", timeout=5)
        assert result.ok is False
        assert "timed out" in result.detail


class TestCwdResolution:
    def test_env_cwd_used_when_none(self, monkeypatch):
        monkeypatch.setenv("MICORACLE_AGENT_CWD", "/tmp/envproj")
        assert cli_agents._default_cwd(None) == "/tmp/envproj"

    def test_explicit_cwd_wins(self, monkeypatch):
        monkeypatch.setenv("MICORACLE_AGENT_CWD", "/tmp/envproj")
        assert cli_agents._default_cwd("/tmp/mine") == "/tmp/mine"

    def test_tilde_expanded(self, monkeypatch):
        monkeypatch.delenv("MICORACLE_AGENT_CWD", raising=False)
        assert cli_agents._default_cwd("~/x").startswith("/")
