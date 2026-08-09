"""Tests for the Playwright wrapper (no real browser — fake page objects)."""

from types import SimpleNamespace

import browser


class FakeElement:
    def __init__(self, tag="a", text="Link text", attrs=None, value=""):
        self.tag = tag
        self.text = text
        self.attrs = attrs or {}
        self.value = value
        self.clicked = False

    def evaluate(self, script):
        return self.tag

    def inner_text(self):
        return self.text

    def get_attribute(self, name):
        return self.attrs.get(name)

    def click(self):
        self.clicked = True

    def fill(self, value):
        self.value = value

    def input_value(self):
        return self.value


class FakePage:
    def __init__(self, elements=None, url="https://example.com/", title_="Example"):
        self.elements = elements if elements is not None else []
        self.url = url
        self._title = title_
        self.goto_calls = []
        self.screenshot_paths = []
        self.body_text = "Hello   world\n\nmore   text"
        self.goto_status = 200

    def title(self):
        return self._title

    def goto(self, url):
        self.goto_calls.append(url)
        self.url = url
        return SimpleNamespace(status=self.goto_status)

    def query_selector_all(self, selector):
        return list(self.elements)

    def query_selector(self, selector):
        for el in self.elements:
            if el.attrs.get("css") == selector:
                return el
        return None

    def get_by_text(self, text, exact=False):
        matches = [el for el in self.elements if text in el.text]

        class Loc:
            first = matches[0] if matches else None

            def count(self_inner):
                return len(matches)

        loc = Loc()
        if matches:
            first = matches[0]
            first.count = lambda: len(matches)
            loc.first = first
        return loc

    def wait_for_load_state(self, state):
        pass

    def inner_text(self, selector):
        return self.body_text

    def screenshot(self, path):
        self.screenshot_paths.append(path)
        with open(path, "wb") as fh:
            fh.write(b"\x89PNGfake")

    def set_default_timeout(self, ms):
        pass


def make_started_session(page=None):
    s = browser.BrowserSession()
    s._page = page if page is not None else FakePage()
    s._browser = SimpleNamespace(close=lambda: None)
    s._pw = SimpleNamespace(stop=lambda: None)
    return s


class TestNavigate:
    def test_ok_with_status(self):
        page = FakePage()
        s = make_started_session(page)
        obs = s.navigate("https://news.ycombinator.com")
        assert obs.ok is True
        assert "http status 200" in obs.detail
        assert page.goto_calls == ["https://news.ycombinator.com"]

    def test_scheme_added(self):
        page = FakePage()
        s = make_started_session(page)
        s.navigate("example.com")
        assert page.goto_calls == ["https://example.com"]

    def test_http_error_status(self):
        page = FakePage()
        page.goto_status = 404
        s = make_started_session(page)
        obs = s.navigate("https://x.com/missing")
        assert obs.ok is False
        assert "404" in obs.detail

    def test_navigation_exception(self):
        page = FakePage()

        def boom(url):
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

        page.goto = boom
        s = make_started_session(page)
        obs = s.navigate("https://doesnotexist.example")
        assert obs.ok is False
        assert "navigation failed" in obs.detail


class TestElements:
    def test_list_elements_numbered(self):
        page = FakePage(elements=[
            FakeElement("a", "Home"),
            FakeElement("input", "", attrs={"placeholder": "Search"}),
        ])
        s = make_started_session(page)
        listing = s.list_elements()
        assert "[0] <a> Home" in listing
        assert "[1] <input> Search" in listing

    def test_click_by_index(self):
        el = FakeElement("button", "Submit")
        s = make_started_session(FakePage(elements=[el]))
        obs = s.click("0")
        assert obs.ok is True
        assert el.clicked is True

    def test_click_no_match(self):
        s = make_started_session(FakePage(elements=[]))
        obs = s.click("99")
        assert obs.ok is False
        assert "no element matched" in obs.detail

    def test_click_by_css(self):
        el = FakeElement("button", "Go", attrs={"css": "#go"})
        s = make_started_session(FakePage(elements=[el]))
        obs = s.click("#go")
        assert obs.ok is True
        assert el.clicked is True


class TestFill:
    def test_fill_readback_confirms(self):
        el = FakeElement("input", "", attrs={"css": "#q"})
        s = make_started_session(FakePage(elements=[el]))
        obs = s.fill("#q", "weather pune")
        assert obs.ok is True
        assert "read-back confirms" in obs.detail

    def test_fill_mismatch_reported(self):
        el = FakeElement("input", "", attrs={"css": "#q"})
        el.input_value = lambda: "something else"
        s = make_started_session(FakePage(elements=[el]))
        obs = s.fill("#q", "expected")
        assert obs.ok is False
        assert "MISMATCH" in obs.detail


class TestReadAndScrape:
    def test_read_page_collapses_whitespace_and_truncates(self):
        page = FakePage()
        page.body_text = "word " * 5000
        s = make_started_session(page)
        out = s.read_page(max_chars=100)
        assert "…[truncated]" in out
        assert "title: Example" in out

    def test_scrape_text(self):
        page = FakePage(elements=[FakeElement("a", "One"), FakeElement("a", "Two")])
        s = make_started_session(page)
        assert s.scrape("a") == ["One", "Two"]

    def test_scrape_attr_and_empty_skipped(self):
        page = FakePage(elements=[
            FakeElement("a", "x", attrs={"href": "/a"}),
            FakeElement("a", "y", attrs={}),
        ])
        s = make_started_session(page)
        assert s.scrape("a", attr="href") == ["/a"]

    def test_scrape_limit(self):
        page = FakePage(elements=[FakeElement("a", f"t{i}") for i in range(10)])
        s = make_started_session(page)
        assert len(s.scrape("a", limit=3)) == 3


class TestScreenshotAndFactory:
    def test_screenshot_default_path(self, tmp_path, monkeypatch):
        page = FakePage()
        s = make_started_session(page)
        path = s.screenshot(str(tmp_path / "s.png"))
        assert path.endswith("s.png")
        assert page.screenshot_paths == [path]

    def test_make_session_none_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(browser, "is_available", lambda: False)
        assert browser.make_session() is None

    def test_make_session_headless_env(self, monkeypatch):
        monkeypatch.setattr(browser, "is_available", lambda: True)
        monkeypatch.setenv("MICORACLE_BROWSER_HEADLESS", "1")
        s = browser.make_session()
        assert s is not None
        assert s.headless is True

    def test_close_idempotent(self):
        s = make_started_session()
        s.close()
        s.close()
        assert s.started is False
