"""The Yahoo-specific MCP tools, called with a real ``yahoo:<number>`` league id."""
import shutil
from pathlib import Path

import pytest

from nfl_mcp import sleeper_tools
from nfl_mcp.tools import yahoo as tools
from nfl_mcp.yahoo import runtime
from nfl_mcp.yahoo.sources import DirSource

FIX = Path(__file__).parent / "fixtures" / "yahoo"
LG = "yahoo:1234567"


@pytest.fixture
def pages(tmp_path, monkeypatch):
    monkeypatch.setitem(sleeper_tools._PLAYERS_CACHE, "data", {})
    for src, dst in (("standings", "standings"), ("settings", "settings"), ("players_rb", "players_RB_A"),
                     ("players_def", "players_DEF_A"), ("transactions_all", "transactions_all"),
                     ("transactions_waiver", "transactions_waiver")):
        shutil.copy(FIX / f"{src}.html", tmp_path / f"{dst}.html")
    runtime.set_source(DirSource(tmp_path))
    yield
    runtime.set_source(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("league", [LG, "YAHOO:1234567"])
async def test_tools_accept_a_yahoo_league_id(pages, league):
    order = await tools.get_yahoo_waiver_order(league)
    assert order["success"] and [t["priority"] for t in order["order"]] == list(range(1, 11))
    assert all(t["team"] for t in order["order"])


@pytest.mark.asyncio
async def test_free_agents_transactions_and_scoring_tools_run(pages):
    fa = await tools.get_yahoo_free_agents(LG, "RB")
    assert fa["success"] and fa["count"] == 25
    assert (await tools.get_yahoo_free_agents(LG, "DEF"))["count"] == 21
    assert (await tools.get_yahoo_transactions(LG, "waiver"))["events"]
    sc = await tools.check_yahoo_scoring(LG)
    assert sc["success"] and sc["differences_from_bfs"] == {}


@pytest.mark.asyncio
async def test_planner_uses_the_current_waiver_position(pages):
    names = [p["name"] for p in (await tools.get_yahoo_free_agents(LG, "RB"))["players"][:2]]
    wish = [{"player": n, "value": 10 - i, "drop": "Someone"} for i, n in enumerate(names)]
    plan = await tools.plan_yahoo_waiver_sequence(LG, "9", wish, [names[0]])
    assert plan["success"] and plan["current_priority"] == 6 and plan["ranked"][0][0] == names[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["1234567", "sleeper:1", ""])
async def test_non_yahoo_ids_get_the_friendly_error(pages, bad):
    r = await tools.get_yahoo_waiver_order(bad)
    assert r["success"] is False and "yahoo:" in r["error"]
