"""Tool registry for the MicOracle agent — the provider-neutral action layer.

Each tool returns a ToolResult whose ``content`` is textual evidence of what
actually happened (post-action state, read-backs, exit codes) so the agent
loop can verify instead of assume. Screenshots set ``image_path`` and are fed
into the model as vision content.

Safety: destructive-sounding actions (buy, delete, send, …) require a spoken
confirmation. The model is asked to flag them via a ``destructive`` argument,
and a regex gate runs regardless — belt and braces.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass

import browser as _browser
import cli_agents as _cli
import control as _control
import platform_adapter as _pa

DESTRUCTIVE_PATTERNS = re.compile(
    r"\b(buy|purchase|order|pay|checkout|delete|remove|send|submit|post|publish|confirm)\b",
    re.I,
)

def _schema(props: dict, required: list[str] | None = None) -> dict:
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema

_STR = {"type": "string"}
_BOOL_DESTRUCTIVE = {
    "type": "boolean",
    "description": "Set true if this action buys, deletes, sends, submits, or otherwise cannot be undone.",
}

TOOL_SPECS: list[dict] = [
    {
        "name": "browser_open",
        "description": "Open a URL in the controlled browser. Returns final URL, title, and HTTP status.",
        "parameters": _schema({"url": _STR}, ["url"]),
    },
    {
        "name": "browser_list_elements",
        "description": "List the page's interactive elements (links, buttons, inputs) numbered for use as click/fill targets.",
        "parameters": _schema({}),
    },
    {
        "name": "browser_click",
        "description": "Click an element: pass an index from browser_list_elements, a CSS selector, or visible text. Returns post-click URL and title.",
        "parameters": _schema({"target": _STR, "destructive": _BOOL_DESTRUCTIVE}, ["target"]),
    },
    {
        "name": "browser_fill",
        "description": "Fill an input field and read the value back to confirm it stuck.",
        "parameters": _schema({"target": _STR, "value": _STR}, ["target", "value"]),
    },
    {
        "name": "browser_read_page",
        "description": "Read the current page's visible text (title, URL, body).",
        "parameters": _schema({"max_chars": {"type": "integer"}}),
    },
    {
        "name": "browser_scrape",
        "description": "Extract text (or an attribute) from all elements matching a CSS selector.",
        "parameters": _schema(
            {"selector": _STR, "attr": _STR, "limit": {"type": "integer"}}, ["selector"]
        ),
    },
    {
        "name": "browser_screenshot",
        "description": "Screenshot the browser page. The image is returned to you — look at it to verify state.",
        "parameters": _schema({}),
    },
    {
        "name": "desktop_screenshot",
        "description": "Screenshot the whole screen. The image is returned to you — look at it to verify state.",
        "parameters": _schema({}),
    },
    {
        "name": "desktop_open_app",
        "description": "Open a macOS application by name.",
        "parameters": _schema({"name": _STR}, ["name"]),
    },
    {
        "name": "desktop_focus_app",
        "description": "Bring a running macOS application to the front.",
        "parameters": _schema({"name": _STR}, ["name"]),
    },
    {
        "name": "desktop_open_url",
        "description": "Open a URL in the user's default browser (fire-and-forget; prefer browser_open when you need to read or interact with the page).",
        "parameters": _schema({"url": _STR}, ["url"]),
    },
    {
        "name": "desktop_type_text",
        "description": "Type text into the frontmost application and press Return.",
        "parameters": _schema({"text": _STR, "destructive": _BOOL_DESTRUCTIVE}, ["text"]),
    },
    {
        "name": "cli_claude",
        "description": "Delegate a coding/terminal task to the Claude Code CLI (headless). It can read/edit files and run commands in the working directory; mention a skill by name in the task if one should be used. Returns its final answer, exit code, and duration.",
        "parameters": _schema({"task": _STR, "cwd": _STR}, ["task"]),
    },
    {
        "name": "cli_codex",
        "description": "Delegate a coding/terminal task to the Codex CLI (headless, sandboxed). Returns its final answer, exit code, and duration.",
        "parameters": _schema({"task": _STR, "cwd": _STR}, ["task"]),
    },
    {
        "name": "ask_user",
        "description": "Ask the user a clarifying question out loud and wait for their spoken answer. Use when the instruction is ambiguous.",
        "parameters": _schema({"question": _STR}, ["question"]),
    },
    {
        "name": "task_complete",
        "description": "Finish the task. success=true REQUIRES concrete evidence entries describing what you observed; never claim success you did not verify.",
        "parameters": _schema(
            {
                "success": {"type": "boolean"},
                "summary": {"type": "string", "description": "One or two spoken-friendly sentences."},
                "evidence": {
                    "type": "array",
                    "items": _STR,
                    "description": "Observed facts proving the outcome (URLs seen, read-backs, exit codes).",
                },
            },
            ["success", "summary"],
        ),
    },
]


@dataclass
class ToolResult:
    ok: bool
    content: str
    image_path: str | None = None


class ToolRegistry:
    """Executes tools; owns the browser session (created lazily, closed once)."""

    def __init__(self, browser_factory=_browser.make_session, adapter=None) -> None:
        self._browser_factory = browser_factory
        self._session = None
        self._adapter = adapter

    # ── plumbing ───────────────────────────────────────────────

    def specs(self) -> list[dict]:
        return TOOL_SPECS

    def _get_session(self):
        if self._session is None:
            self._session = self._browser_factory()
        return self._session

    def _get_adapter(self):
        if self._adapter is None:
            self._adapter = _pa.get_platform_adapter()
        return self._adapter

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    # ── safety gate ────────────────────────────────────────────

    def needs_confirmation(self, name: str, args: dict) -> str | None:
        """Spoken warning text if this call must be confirmed first, else None."""
        if args.get("destructive"):
            return f"This will run a destructive action: {name}. Should I go ahead?"
        risky_text = ""
        if name == "browser_click":
            risky_text = str(args.get("target", ""))
        elif name == "desktop_type_text":
            risky_text = str(args.get("text", ""))
        elif name in ("cli_claude", "cli_codex"):
            risky_text = str(args.get("task", ""))
        match = DESTRUCTIVE_PATTERNS.search(risky_text)
        if match:
            return (
                f"That involves '{match.group(0)}' which I can't undo: "
                f"{risky_text[:120]}. Should I go ahead?"
            )
        return None

    # ── execution ──────────────────────────────────────────────

    def execute(self, name: str, args: dict) -> ToolResult:
        try:
            return self._execute(name, args)
        except Exception as exc:
            return ToolResult(False, f"{name} raised {type(exc).__name__}: {exc}")

    def _execute(self, name: str, args: dict) -> ToolResult:
        if name.startswith("browser_"):
            session = self._get_session()
            if session is None:
                return ToolResult(
                    False,
                    "browser automation unavailable — playwright is not installed "
                    "(pip install 'micoracle[browser]' && playwright install chromium)",
                )
            return self._execute_browser(session, name, args)

        if name == "desktop_screenshot":
            fd, path = tempfile.mkstemp(suffix=".png", prefix="micoracle-screen-")
            os.close(fd)
            proc = subprocess.run(
                ["screencapture", "-x", path], capture_output=True, text=True,
            )
            if proc.returncode != 0 or not os.path.getsize(path):
                return ToolResult(False, f"screencapture failed (exit {proc.returncode})")
            return ToolResult(True, f"screenshot saved: {path}", image_path=path)

        if name in ("desktop_open_app", "desktop_focus_app", "desktop_open_url"):
            kind = {
                "desktop_open_app": "open_app",
                "desktop_focus_app": "focus_app",
                "desktop_open_url": "open_url",
            }[name]
            arg = args.get("name") or args.get("url") or ""
            result = _control.execute(_control.Intent(kind, arg))
            frontmost = self._frontmost()
            return ToolResult(
                result.ok, f"{result.detail}; frontmost app now: {frontmost}"
            )

        if name == "desktop_type_text":
            text = args.get("text", "")
            result = _control.execute(_control.Intent("type_text", text))
            frontmost = self._frontmost()
            detail = result.detail if not result.ok else f"typed {text!r}"
            return ToolResult(result.ok, f"{detail}; frontmost app: {frontmost}")

        if name == "cli_claude":
            r = _cli.run_claude(args["task"], cwd=args.get("cwd"))
            return self._cli_result("claude", r)

        if name == "cli_codex":
            r = _cli.run_codex(args["task"], cwd=args.get("cwd"))
            return self._cli_result("codex", r)

        if name in ("ask_user", "task_complete"):
            return ToolResult(False, f"{name} is handled by the agent loop, not the registry")

        return ToolResult(False, f"unknown tool: {name}")

    def _execute_browser(self, session, name: str, args: dict) -> ToolResult:
        if name == "browser_open":
            obs = session.navigate(args["url"])
            return ToolResult(obs.ok, obs.summary())
        if name == "browser_list_elements":
            return ToolResult(True, session.list_elements())
        if name == "browser_click":
            obs = session.click(args["target"])
            return ToolResult(obs.ok, obs.summary())
        if name == "browser_fill":
            obs = session.fill(args["target"], args["value"])
            return ToolResult(obs.ok, obs.summary())
        if name == "browser_read_page":
            return ToolResult(True, session.read_page(int(args.get("max_chars") or 4000)))
        if name == "browser_scrape":
            values = session.scrape(
                args["selector"], attr=args.get("attr"), limit=int(args.get("limit") or 50),
            )
            if not values:
                return ToolResult(False, f"0 elements matched {args['selector']!r}")
            listing = "\n".join(f"- {v}" for v in values[:50])
            return ToolResult(True, f"{len(values)} matches:\n{listing}")
        if name == "browser_screenshot":
            path = session.screenshot()
            return ToolResult(True, f"screenshot saved: {path}", image_path=path)
        return ToolResult(False, f"unknown browser tool: {name}")

    def _cli_result(self, label: str, r) -> ToolResult:
        content = (
            f"{label}: exit={r.exit_code} ok={r.ok} duration={r.duration_secs:.1f}s\n"
            f"{r.output or '(no output)'}"
        )
        if r.detail:
            content += f"\n[{r.detail}]"
        return ToolResult(r.ok, content)

    def _frontmost(self) -> str:
        try:
            return self._get_adapter().get_frontmost_app()
        except Exception:
            return "unknown"
