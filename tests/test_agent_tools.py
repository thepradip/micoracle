"""Tests for the agent tool registry (no browser, no subprocesses, no network)."""

from types import SimpleNamespace

import pytest

import agent_tools
import cli_agents


class FakeSession:
    def __init__(self):
        self.closed = False

    def navigate(self, url):
        return SimpleNamespace(
            ok=True, summary=lambda: f"ok: url={url} | title='T' | http status 200"
        )

    def list_elements(self):
        return "[0] <a> Home"

    def click(self, target):
        ok = target != "missing"
        text = "clicked" if ok else "no element matched"
        return SimpleNamespace(ok=ok, summary=lambda: text)

    def fill(self, target, value):
        return SimpleNamespace(ok=True, summary=lambda: "read-back confirms")

    def read_page(self, max_chars=4000):
        return "title: T\nurl: u\n\nbody"

    def scrape(self, selector, attr=None, limit=50):
        return ["One", "Two"] if selector == ".title" else []

    def screenshot(self, path=None):
        return "/tmp/fake-shot.png"

    def close(self):
        self.closed = True


class FakeAdapter:
    def get_frontmost_app(self):
        return "Safari"


def make_registry(session=None):
    session = session if session is not None else FakeSession()
    return agent_tools.ToolRegistry(
        browser_factory=lambda: session, adapter=FakeAdapter()
    ), session


class TestSpecs:
    def test_all_specs_have_schema(self):
        registry, _ = make_registry()
        for spec in registry.specs():
            assert spec["name"] and spec["description"]
            assert spec["parameters"]["type"] == "object"
            for req in spec["parameters"].get("required", []):
                assert req in spec["parameters"]["properties"]

    def test_expected_tool_names(self):
        names = {s["name"] for s in agent_tools.TOOL_SPECS}
        assert {"browser_open", "browser_click", "browser_scrape", "cli_claude",
                "cli_codex", "ask_user", "task_complete",
                "desktop_screenshot"} <= names


class TestConfirmationGate:
    @pytest.mark.parametrize("text,expected", [
        ("Buy now", True),
        ("delete my account", True),
        ("Send message", True),
        ("Checkout", True),
        ("Read more", False),
        ("next page", False),
        ("search results", False),
    ])
    def test_click_target_patterns(self, text, expected):
        registry, _ = make_registry()
        warning = registry.needs_confirmation("browser_click", {"target": text})
        assert (warning is not None) is expected

    def test_model_flag_forces_confirmation(self):
        registry, _ = make_registry()
        assert registry.needs_confirmation(
            "browser_click", {"target": "innocent", "destructive": True}
        ) is not None

    def test_cli_task_with_delete(self):
        registry, _ = make_registry()
        assert registry.needs_confirmation(
            "cli_claude", {"task": "delete old logs"}
        ) is not None

    def test_read_tools_never_gated(self):
        registry, _ = make_registry()
        assert registry.needs_confirmation("browser_read_page", {}) is None
        assert registry.needs_confirmation("browser_open", {"url": "https://x.com/buy"}) is None


class TestBrowserDispatch:
    def test_open(self):
        registry, _ = make_registry()
        result = registry.execute("browser_open", {"url": "https://x.com"})
        assert result.ok and "http status 200" in result.content

    def test_click_failure_reported(self):
        registry, _ = make_registry()
        result = registry.execute("browser_click", {"target": "missing"})
        assert result.ok is False

    def test_scrape_zero_matches_not_ok(self):
        registry, _ = make_registry()
        result = registry.execute("browser_scrape", {"selector": ".nope"})
        assert result.ok is False
        assert "0 elements" in result.content

    def test_scrape_lists_values(self):
        registry, _ = make_registry()
        result = registry.execute("browser_scrape", {"selector": ".title"})
        assert result.ok and "2 matches" in result.content

    def test_screenshot_sets_image_path(self):
        registry, _ = make_registry()
        result = registry.execute("browser_screenshot", {})
        assert result.image_path == "/tmp/fake-shot.png"

    def test_no_playwright_message(self):
        registry = agent_tools.ToolRegistry(
            browser_factory=lambda: None, adapter=FakeAdapter()
        )
        result = registry.execute("browser_open", {"url": "https://x.com"})
        assert result.ok is False
        assert "playwright" in result.content

    def test_close_closes_session(self):
        registry, session = make_registry()
        registry.execute("browser_open", {"url": "https://x.com"})
        registry.close()
        assert session.closed is True


class TestCliDispatch:
    def test_cli_claude_result_formatting(self, monkeypatch):
        monkeypatch.setattr(
            agent_tools._cli, "run_claude",
            lambda task, cwd=None: cli_agents.CLIResult(True, 0, "answer", 1.2, ""),
        )
        registry, _ = make_registry()
        result = registry.execute("cli_claude", {"task": "do it"})
        assert result.ok and "exit=0" in result.content and "answer" in result.content

    def test_cli_codex_failure(self, monkeypatch):
        monkeypatch.setattr(
            agent_tools._cli, "run_codex",
            lambda task, cwd=None: cli_agents.CLIResult(False, 1, "", 0.5, "stderr: boom"),
        )
        registry, _ = make_registry()
        result = registry.execute("cli_codex", {"task": "do it"})
        assert result.ok is False and "boom" in result.content


class TestMiscDispatch:
    def test_unknown_tool(self):
        registry, _ = make_registry()
        result = registry.execute("teleport", {})
        assert result.ok is False and "unknown tool" in result.content

    def test_loop_tools_rejected(self):
        registry, _ = make_registry()
        assert registry.execute("ask_user", {"question": "?"}).ok is False
        assert registry.execute("task_complete", {"success": True}).ok is False

    def test_exception_becomes_result(self):
        class Exploding(FakeSession):
            def navigate(self, url):
                raise RuntimeError("kaboom")

        registry, _ = make_registry(Exploding())
        result = registry.execute("browser_open", {"url": "https://x.com"})
        assert result.ok is False and "kaboom" in result.content

    def test_desktop_open_app_reports_frontmost(self, monkeypatch):
        monkeypatch.setattr(
            agent_tools._control, "execute",
            lambda intent: SimpleNamespace(ok=True, detail=intent.arg),
        )
        registry, _ = make_registry()
        result = registry.execute("desktop_open_app", {"name": "Notes"})
        assert result.ok
        assert "frontmost app now: Safari" in result.content
