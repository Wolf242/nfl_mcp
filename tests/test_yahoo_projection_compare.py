"""Yahoo's projections are shown beside nfl_mcp's own and never fed into them."""
import shutil
from pathlib import Path

import pytest

from nfl_mcp import projections, sleeper_tools
from nfl_mcp.tools import yahoo as tools
from nfl_mcp.yahoo import runtime
from nfl_mcp.yahoo.sources import DirSource

FIX = Path(__file__).parent / "fixtures" / "yahoo"
LG = "yahoo:1234567"
ROOT = Path(__file__).parent.parent / "nfl_mcp"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setitem(sleeper_tools._PLAYERS_CACHE, "data", {})
    shutil.copy(FIX / "roster.html", tmp_path / "roster_9.html")
    runtime.set_source(DirSource(tmp_path))
    seen = {}

    async def fake_project_players(players, **kw):
        seen["players"], seen["kw"] = players, kw
        return {"success": True, "week": 8, "projections": [
            {"player": p["name"], "projected_points": 10.0, "model_projection": 9.0,
             "sleeper_projection": 11.0, "projection_source": "sleeper_blend"} for p in players]}

    monkeypatch.setattr(projections, "project_players", fake_project_players)
    yield seen
    runtime.set_source(None)


@pytest.mark.asyncio
async def test_comparison_lines_up_both_projections_and_skips_bye_players(setup):
    r = await tools.get_yahoo_projection_comparison(LG, "9")
    assert r["success"] and r["week"] == 8 and len(r["players"]) == 16
    by = {p["name"]: p for p in r["players"]}
    assert by["Kirk Cousins"]["our_projection"] == 10.0 and by["Kirk Cousins"]["yahoo_projection"] > 10
    assert by["Kirk Cousins"]["gap"] == round(10.0 - by["Kirk Cousins"]["yahoo_projection"], 1)
    for name in ("George Kittle", "Texans"):  # on bye in week 8 per Yahoo
        assert by[name]["on_bye"] and by[name]["gap"] is None and by[name]["yahoo_projection"] is None
    assert by["Jordyn Tyson"]["our_projection"] is None  # IR is not projected
    t = r["starter_totals"]
    assert t["players_compared"] == 8 and t["starters"] == 9


@pytest.mark.asyncio
async def test_yahoo_numbers_are_never_passed_into_our_projections(setup):
    await tools.get_yahoo_projection_comparison(LG, "9")
    assert setup["players"] and all("yahoo_projection" not in p for p in setup["players"])
    assert set(setup["kw"]) <= {"league_id", "week", "db"}  # a database handle is not Yahoo data


@pytest.mark.asyncio
async def test_bad_inputs_get_clear_errors(setup):
    assert (await tools.get_yahoo_projection_comparison("1234567", "9"))["success"] is False
    assert (await tools.get_yahoo_projection_comparison(LG, "Wolf"))["success"] is False


def test_the_optimizer_and_projection_engine_do_not_read_yahoo_projections():
    for rel in ("lineup_optimizer_tools.py", "projections.py", "sleeper_projections.py", "tools/lineup.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "yahoo_projection" not in src and "yahoo_slots" not in src, rel
