"""Real browser automation for the MicOracle agent (Playwright, sync API).

Design rules:
  - Lazy import: playwright is an optional extra (pip install 'micoracle[browser]'
    then `playwright install chromium`). make_session() returns None when it is
    missing so callers degrade gracefully, matching the repo factory pattern.
  - Evidence built in: every mutating call returns a BrowserObservation carrying
    the post-action URL/title plus a read-back detail, so the agent loop always
    has proof of what actually happened — never a bare "ok".
  - Thread affinity: Playwright sync objects must be used from the thread that
    created them. The AgentRunner owns one dedicated thread and keeps the
    session there for its whole life.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

MAX_ELEMENTS = 40
ELEMENT_SELECTOR = "a, button, input, select, textarea, [role=button]"
DEFAULT_NAV_TIMEOUT_MS = 30_000


@dataclass
class BrowserObservation:
    """What the page looks like right after an action — the evidence."""

    ok: bool
    url: str
    title: str
    detail: str = ""

    def summary(self) -> str:
        status = "ok" if self.ok else "FAILED"
        parts = [f"{status}: url={self.url}", f"title={self.title!r}"]
        if self.detail:
            parts.append(self.detail)
        return " | ".join(parts)


def is_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


class BrowserSession:
    """One chromium page, headed by default so the user can watch."""

    def __init__(self, headless: bool = False) -> None:
        self.headless = headless
        self._pw = None
        self._browser = None
        self._page = None

    # ── lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        cdp_url = os.environ.get("MICORACLE_BROWSER_CDP_URL", "").strip()
        if cdp_url:
            # External CDP engine (e.g. Lightpanda `serve`, a remote Chrome):
            # far lighter and faster than launching Chromium, but such engines
            # may not render pixels — screenshot() degrades gracefully.
            self._browser = self._pw.chromium.connect_over_cdp(cdp_url)
            context = self._browser.contexts[0] if self._browser.contexts else None
            pages = context.pages if context is not None else []
            self._page = pages[0] if pages else self._browser.new_page()
        else:
            self._browser = self._pw.chromium.launch(headless=self.headless)
            self._page = self._browser.new_page()
        self._page.set_default_timeout(DEFAULT_NAV_TIMEOUT_MS)

    def close(self) -> None:
        for closer in (
            lambda: self._browser.close() if self._browser else None,
            lambda: self._pw.stop() if self._pw else None,
        ):
            try:
                closer()
            except Exception:
                pass
        self._pw = self._browser = self._page = None

    @property
    def started(self) -> bool:
        return self._page is not None

    def _ensure_started(self) -> None:
        if not self.started:
            self.start()

    def _observe(self, ok: bool, detail: str = "") -> BrowserObservation:
        try:
            return BrowserObservation(ok, self._page.url, self._page.title(), detail)
        except Exception as exc:
            return BrowserObservation(False, "?", "?", f"{detail}; observe failed: {exc}")

    # ── actions (each returns evidence) ───────────────────────

    def navigate(self, url: str) -> BrowserObservation:
        self._ensure_started()
        if "://" not in url:
            url = f"https://{url}"
        try:
            resp = self._page.goto(url)
        except Exception as exc:
            return self._observe(False, f"navigation failed: {exc}")
        status = resp.status if resp is not None else "?"
        ok = resp is None or (200 <= resp.status < 400)
        return self._observe(ok, f"http status {status}")

    def list_elements(self) -> str:
        """Numbered interactive elements, for click/fill targeting by index."""
        self._ensure_started()
        lines = []
        try:
            handles = self._page.query_selector_all(ELEMENT_SELECTOR)
        except Exception as exc:
            return f"could not list elements: {exc}"
        for i, h in enumerate(handles[:MAX_ELEMENTS]):
            try:
                tag = h.evaluate("el => el.tagName.toLowerCase()")
                text = (h.inner_text() or "").strip()[:80]
                if not text:
                    text = (h.get_attribute("placeholder")
                            or h.get_attribute("aria-label")
                            or h.get_attribute("name")
                            or h.get_attribute("value") or "")[:80]
                lines.append(f"[{i}] <{tag}> {text}")
            except Exception:
                lines.append(f"[{i}] <?> (unreadable)")
        extra = len(handles) - MAX_ELEMENTS
        if extra > 0:
            lines.append(f"... and {extra} more not shown")
        return "\n".join(lines) if lines else "no interactive elements found"

    def _resolve(self, target: str):
        """Target = element index from list_elements, CSS selector, or text."""
        target = target.strip()
        if target.isdigit():
            handles = self._page.query_selector_all(ELEMENT_SELECTOR)
            idx = int(target)
            return handles[idx] if idx < len(handles) else None
        el = None
        try:
            el = self._page.query_selector(target)
        except Exception:
            el = None
        if el is None:
            try:
                loc = self._page.get_by_text(target, exact=False).first
                if loc.count() > 0:
                    return loc
            except Exception:
                return None
        return el

    def click(self, target: str) -> BrowserObservation:
        self._ensure_started()
        el = self._resolve(target)
        if el is None:
            return self._observe(False, f"no element matched {target!r}")
        try:
            label = ""
            try:
                label = (el.inner_text() or "").strip()[:60]
            except Exception:
                pass
            el.click()
            self._page.wait_for_load_state("load")
        except Exception as exc:
            return self._observe(False, f"click failed on {target!r}: {exc}")
        return self._observe(True, f"clicked {target!r} ({label!r})")

    def fill(self, target: str, value: str) -> BrowserObservation:
        self._ensure_started()
        el = self._resolve(target)
        if el is None:
            return self._observe(False, f"no element matched {target!r}")
        try:
            el.fill(value)
            readback = el.input_value()
        except Exception as exc:
            return self._observe(False, f"fill failed on {target!r}: {exc}")
        ok = readback == value
        detail = (f"read-back confirms {readback!r}" if ok
                  else f"MISMATCH: field now contains {readback!r}, expected {value!r}")
        return self._observe(ok, detail)

    def read_page(self, max_chars: int = 4000) -> str:
        self._ensure_started()
        try:
            title = self._page.title()
            url = self._page.url
            text = self._page.inner_text("body")
        except Exception as exc:
            return f"could not read page: {exc}"
        text = " ".join(text.split())
        if len(text) > max_chars:
            text = text[:max_chars] + " …[truncated]"
        return f"title: {title}\nurl: {url}\n\n{text}"

    def scrape(self, selector: str, attr: str | None = None, limit: int = 50) -> list[str]:
        self._ensure_started()
        try:
            handles = self._page.query_selector_all(selector)
        except Exception:
            return []
        values: list[str] = []
        for h in handles[:limit]:
            try:
                if attr:
                    v = h.get_attribute(attr)
                else:
                    v = h.inner_text()
            except Exception:
                continue
            v = (v or "").strip()
            if v:
                values.append(v)
        return values

    def screenshot(self, path: str | None = None) -> str:
        """Returns the saved path, or an error message when the engine cannot
        render (e.g. Lightpanda via MICORACLE_BROWSER_CDP_URL) — callers check
        whether the return value equals the requested path."""
        self._ensure_started()
        if path is None:
            fd, path = tempfile.mkstemp(suffix=".png", prefix="micoracle-browser-")
            os.close(fd)
        try:
            self._page.screenshot(path=path)
        except Exception as exc:
            return (f"screenshot unavailable ({exc}); "
                    "verify with browser_read_page instead")
        return path


def make_session(headless: bool | None = None) -> "BrowserSession | None":
    """Factory: None when playwright isn't installed (caller degrades)."""
    if not is_available():
        return None
    if headless is None:
        headless = os.environ.get("MICORACLE_BROWSER_HEADLESS", "").strip().lower() in (
            "1", "true", "yes",
        )
    try:
        return BrowserSession(headless=headless)
    except Exception:
        return None
