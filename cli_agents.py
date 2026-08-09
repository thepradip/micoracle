"""Headless runners for the Claude Code and Codex CLI agents.

The MicOracle agent delegates coding/terminal tasks to whichever agent CLI is
installed, running it as a plain subprocess and capturing the final answer so
the result can be verified and spoken back — nothing is pasted blindly into a
terminal window.

Safety: permission modes stay at their guarded defaults. This module never
passes bypass flags (claude --permission-mode bypassPermissions or codex
--dangerously-bypass-approvals-and-sandbox).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass

DEFAULT_TIMEOUT_SECS = 600.0
_STDERR_TAIL_CHARS = 500


@dataclass
class CLIResult:
    """Outcome of one headless CLI agent run — evidence, never hidden."""

    ok: bool
    exit_code: int
    output: str
    duration_secs: float
    detail: str = ""


def claude_available() -> bool:
    return shutil.which("claude") is not None


def codex_available() -> bool:
    return shutil.which("codex") is not None


def _default_cwd(cwd: str | None) -> str | None:
    if cwd:
        return os.path.expanduser(cwd)
    env_cwd = os.environ.get("MICORACLE_AGENT_CWD", "").strip()
    return os.path.expanduser(env_cwd) if env_cwd else None


def _run(argv: list[str], cwd: str | None, timeout: float):
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        return None, time.monotonic() - start
    return proc, time.monotonic() - start


def run_claude(
    task: str,
    cwd: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECS,
    permission_mode: str = "acceptEdits",
    allowed_tools: list[str] | None = None,
) -> CLIResult:
    """Run ``claude -p <task>`` headless and return its final answer."""
    if not claude_available():
        return CLIResult(False, -1, "", 0.0, "claude CLI not found on PATH")

    argv = [
        "claude", "-p", task,
        "--output-format", "json",
        "--permission-mode", permission_mode,
    ]
    if allowed_tools:
        argv += ["--allowedTools", ",".join(allowed_tools)]

    proc, elapsed = _run(argv, _default_cwd(cwd), timeout)
    if proc is None:
        return CLIResult(False, -1, "", elapsed, f"claude timed out after {timeout:.0f}s")

    stderr_tail = (proc.stderr or "")[-_STDERR_TAIL_CHARS:].strip()
    output = (proc.stdout or "").strip()
    detail = ""
    try:
        payload = json.loads(output)
        output = str(payload.get("result", output)).strip()
        if payload.get("is_error"):
            detail = "claude reported is_error=true"
    except (ValueError, TypeError):
        detail = "stdout was not JSON; using raw text"

    ok = proc.returncode == 0 and bool(output) and "is_error" not in detail
    if stderr_tail:
        detail = f"{detail}; stderr: {stderr_tail}".strip("; ")
    return CLIResult(ok, proc.returncode, output, elapsed, detail)


def run_codex(
    task: str,
    cwd: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECS,
    sandbox: str = "workspace-write",
    image_paths: list[str] | None = None,
    model: str | None = None,
) -> CLIResult:
    """Run ``codex exec <task>`` headless and return its final answer."""
    if not codex_available():
        return CLIResult(False, -1, "", 0.0, "codex CLI not found on PATH")

    out_file = tempfile.NamedTemporaryFile(
        mode="r", suffix=".txt", prefix="micoracle-codex-", delete=False,
    )
    out_file.close()
    argv = [
        "codex", "exec", task,
        "--skip-git-repo-check",
        "--sandbox", sandbox,
        "-o", out_file.name,
    ]
    if model:
        argv += ["-m", model]
    resolved_cwd = _default_cwd(cwd)
    if resolved_cwd:
        argv += ["-C", resolved_cwd]
    for path in image_paths or []:
        argv += ["-i", path]

    try:
        proc, elapsed = _run(argv, None, timeout)
        if proc is None:
            return CLIResult(False, -1, "", elapsed, f"codex timed out after {timeout:.0f}s")

        try:
            with open(out_file.name, encoding="utf-8") as fh:
                output = fh.read().strip()
        except OSError:
            output = ""

        detail = ""
        if not output:
            output = (proc.stdout or "").strip()
            detail = "final-message file empty; using stdout"
        stderr_tail = (proc.stderr or "")[-_STDERR_TAIL_CHARS:].strip()
        if stderr_tail:
            detail = f"{detail}; stderr: {stderr_tail}".strip("; ")
        ok = proc.returncode == 0 and bool(output)
        return CLIResult(ok, proc.returncode, output, elapsed, detail)
    finally:
        try:
            os.unlink(out_file.name)
        except OSError:
            pass
