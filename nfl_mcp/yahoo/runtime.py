"""Process-wide wiring: one provider per Yahoo league, plus the stub response that lets the
Sleeper fetch paths consume Yahoo data without changing their downstream code."""
from __future__ import annotations

import os

from . import yahoo_league_number
from .provider import YahooProvider
from .sources import default_source

_providers: dict[str, YahooProvider] = {}
_source = None


class StubResponse:
    """Duck-types the bit of ``httpx.Response`` the Sleeper paths read."""

    def __init__(self, data):
        self.status_code = 200
        self._data = data
        self.headers: dict = {}

    def json(self):
        return self._data

    def raise_for_status(self) -> None:
        return None


def set_source(source) -> None:
    """Tests / embedding: replace the page source (e.g. a DirSource)."""
    global _source
    _source = source
    _providers.clear()


async def get_provider(league_id: str) -> YahooProvider:
    num = yahoo_league_number(league_id)
    if num not in _providers:
        global _source
        if _source is None:
            _source = default_source()
        from .. import sleeper_tools
        players = sleeper_tools._PLAYERS_CACHE.get("data")
        if players is None:
            await sleeper_tools.fetch_all_players()
            players = sleeper_tools._PLAYERS_CACHE.get("data")
        source = _source
        if os.getenv("YAHOO_ACCESS", "").lower() == "api" and not os.getenv("YAHOO_SNAPSHOT_DIR"):
            from .api import ApiAdapter, ApiClient  # experimental official-API route
            source = ApiAdapter(ApiClient(num))
        _providers[num] = YahooProvider(num, source, players or {})
    return _providers[num]


async def rosters_response(league_id: str) -> tuple[StubResponse, int]:
    prov = await get_provider(league_id)
    return StubResponse(await prov.get_rosters()), 0


async def matchups_response(league_id: str, week: int) -> tuple[StubResponse, int]:
    prov = await get_provider(league_id)
    return StubResponse(await prov.get_matchups(week)), 0
