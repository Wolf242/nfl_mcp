"""Where Yahoo page HTML comes from. All sources implement ``async fetch(page, **params)``.

* :class:`DirSource` - saved pages in a folder (tests, and the "snapshot feed" route where
  the Chrome extension or a manual save drops pages in).
* :class:`BrowserSource` - Playwright with a persistent profile you log into yourself once.
  Every URL passes :func:`nfl_mcp.yahoo.guard.check_read_only` first.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from urllib.parse import urlencode

from .guard import check_read_only

BASE = "https://football.fantasysports.yahoo.com"


class YahooLoginRequired(RuntimeError):
    """The saved browser profile is not logged in to Yahoo."""


def page_url(league: str, page: str, **params) -> str:
    """Canonical read-page URL. ``page`` is one of: home, roster, matchup, players, settings,
    standings, transactions. ``params`` become the query string."""
    path = {"home": "", "standings": ""}.get(page)
    if path is None:
        path = "/" + (str(params.pop("mid")) if page == "roster" else page)
    q = ("?" + urlencode(params)) if params else ""
    return f"{BASE}/f1/{league}{path}{q}"


def snapshot_name(page: str, **params) -> str:
    """File name a DirSource looks for, e.g. ``roster_9.html`` / ``matchup_9_w8.html``."""
    if page == "roster":
        return f"roster_{params['mid']}.html"
    if page == "matchup":
        return f"matchup_{params.get('mid1', 'x')}_w{params.get('week', 'x')}.html"
    if page == "players":
        return f"players_{params.get('pos', 'ALL')}_{params.get('status', 'A')}.html"
    if page == "transactions":
        return f"transactions_{params.get('transactionsfilter', 'all')}.html"
    return f"{page}.html"


class DirSource:
    def __init__(self, folder: str | os.PathLike):
        self.folder = Path(folder)

    async def fetch(self, league: str, page: str, **params) -> str:
        p = self.folder / snapshot_name(page, **params)
        if not p.exists():
            raise FileNotFoundError(f"snapshot missing: {p.name}")
        return p.read_text(encoding="utf-8")


class BrowserSource:
    """Playwright persistent-profile reader. Needs ``pip install nfl_mcp[yahoo]`` and a
    one-time ``python -m nfl_mcp.yahoo.login`` (you type your own password; nothing here
    ever sees or stores it)."""

    def __init__(self, profile_dir: str | None = None, min_interval: float = 1.5,
                 ttl: float = 60.0, headless: bool = True, cdp_url: str | None = None):
        self.profile_dir = profile_dir or os.getenv("YAHOO_PROFILE_DIR") or str(
            Path.home() / ".nfl_mcp" / "yahoo-profile")
        self.min_interval, self.ttl, self.headless = min_interval, ttl, headless
        # YAHOO_CDP_URL: attach to a normal Chrome YOU started with --remote-debugging-port and
        # signed in to by hand (works with "Sign in with Google", which blocks automated browsers).
        self.cdp_url = cdp_url or os.getenv("YAHOO_CDP_URL")
        self._ctx = None
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._cache: dict[str, tuple[float, str]] = {}

    async def _context(self):
        if self._ctx is None:
            from playwright.async_api import async_playwright  # optional dependency
            self._pw = await async_playwright().start()
            if self.cdp_url:
                self._browser = await self._pw.chromium.connect_over_cdp(self.cdp_url)
                self._ctx = (self._browser.contexts[0] if self._browser.contexts
                             else await self._browser.new_context())
            else:
                self._ctx = await self._pw.chromium.launch_persistent_context(
                    self.profile_dir, headless=self.headless)
        return self._ctx

    async def fetch(self, league: str, page: str, **params) -> str:
        url = check_read_only(page_url(league, page, **params), league_id=league)
        hit = self._cache.get(url)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        async with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            ctx = await self._context()
            pg = await ctx.new_page()
            try:
                await pg.goto(url, wait_until="domcontentloaded")
                if "login.yahoo.com" in pg.url or "/login" in pg.url:
                    raise YahooLoginRequired(
                        "Yahoo session expired - run `python -m nfl_mcp.yahoo.login` and sign in")
                # a final guard on where we actually landed
                check_read_only(pg.url, league_id=league)
                html = await pg.content()
            finally:
                await pg.close()
                self._last = time.monotonic()
        self._cache[url] = (time.monotonic(), html)
        return html

    async def close(self):
        # An attached (CDP) browser belongs to the user: disconnect, never close it.
        if self._ctx is not None and self._browser is None:
            await self._ctx.close()
        if self._pw is not None:
            await self._pw.stop()
        self._ctx = self._pw = self._browser = None


def default_source():
    """``YAHOO_SNAPSHOT_DIR`` -> DirSource; otherwise the logged-in browser."""
    snap = os.getenv("YAHOO_SNAPSHOT_DIR")
    return DirSource(snap) if snap else BrowserSource()
