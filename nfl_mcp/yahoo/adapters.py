"""One interface over every way of reading Yahoo, so the provider does not care which.

``HtmlAdapter`` wraps a page source (saved pages or the logged-in browser) and parses the
HTML; ``ApiAdapter`` (see :mod:`nfl_mcp.yahoo.api`) answers the same methods from Yahoo's
official API. Both return the *same dicts* the HTML parsers emit.
"""
from __future__ import annotations

import time

from . import parsers


class HtmlAdapter:
    def __init__(self, source, league: str, ttl: float = 60.0):
        self.source, self.league, self.ttl = source, str(league), ttl
        self._memo: dict = {}

    async def _page(self, page: str, **params) -> str:
        key = (page, tuple(sorted(params.items())))
        hit = self._memo.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        html = await self.source.fetch(self.league, page, **params)
        self._memo[key] = (time.monotonic(), html)
        return html

    async def standings(self) -> list[dict]:
        return parsers.parse_standings(await self._page("standings"))["teams"]

    async def roster(self, mid: str) -> list[dict]:
        return parsers.parse_roster(await self._page("roster", mid=mid))["slots"]

    async def matchup(self, week: int, mid: str) -> dict:
        return parsers.parse_matchup(await self._page("matchup", week=week, mid1=mid))

    async def players(self, pos: str, status: str) -> list[dict]:
        return parsers.parse_players(await self._page("players", pos=pos, status=status))["players"]

    async def settings_rows(self) -> list[str]:
        return parsers.parse_settings_rows(await self._page("settings"))

    async def transactions(self, kind: str) -> dict:
        return parsers.parse_transactions(await self._page("transactions", transactionsfilter=kind))
