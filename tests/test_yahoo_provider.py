"""YahooProvider end to end on synthetic pages, including through nfl_mcp's own Sleeper paths."""
import pytest

from nfl_mcp import sleeper_tools
from nfl_mcp.yahoo import runtime
from nfl_mcp.yahoo.idmap import IdMap
from nfl_mcp.yahoo.provider import YahooProvider
from nfl_mcp.yahoo.sources import DirSource

from . import yahoo_fixtures as fx

SLEEPER = {
    "S_BIJAN": {"yahoo_id": 40055, "full_name": "Bijan Robinson", "team": "ATL", "position": "RB"},
    "S_GOLD": {"yahoo_id": 41001, "full_name": "Matthew Golden", "team": "GB", "position": "WR"},
    "S_WARR": {"yahoo_id": 32800, "full_name": "Jaylen Warren", "team": "PIT", "position": "RB"},
    "S_COUS": {"yahoo_id": 25812, "full_name": "Kirk Cousins", "team": "LV", "position": "QB"},
    "S_MURR": {"yahoo_id": 31833, "full_name": "Kyler Murray", "team": "MIN", "position": "QB"},
    "S_HENRY": {"full_name": "Derrick Henry", "team": "BAL", "position": "RB"},  # no yahoo_id: name fallback
    "HOU": {"full_name": "Houston Texans", "team": "HOU", "position": "DEF"},
}


@pytest.fixture
def snap(tmp_path):
    (tmp_path / "standings.html").write_text(fx.standings_html([
        (1, "Team Nine", 9, "6-1-0", "949.28", "706.82", 6, 26),
        (2, "Team Five", 5, "5-2-0", "900.00", "800.00", 4, 9)]))
    (tmp_path / "settings.html").write_text(fx.settings_html())
    (tmp_path / "roster_9.html").write_text(fx.roster_html([
        ("QB", "Kirk Cousins", "25812", "LV - QB", "18.42", None),
        ("RB", "Bijan Robinson", "40055", "Atl - RB", "20.16", None),
        ("WR", "Matthew Golden", "41001", "GB - WR", "11.30", "Q"),
        ("DEF", "Houston", "99001", "Hou - DEF", "0.00", None),
        ("BN", "Jaylen Warren", "32800", "Pit - RB", "9.10", None),
        ("IR", "Mystery Rookie", "55555", "Nyj - WR", "0.00", "IR"),
    ]))
    (tmp_path / "roster_5.html").write_text(fx.roster_html([
        ("QB", "Kyler Murray", "31833", "Min - QB", "20.22", None),
        ("RB", "Derrick Henry", "28000", "Bal - RB", "15.83", None)]))
    pair = [("QB", ("Kirk Cousins", "25812", "LV - QB", "18.42"), ("Kyler Murray", "31833", "Min - QB", "20.22")),
            ("RB", ("Bijan Robinson", "40055", "Atl - RB", "20.16"), ("Derrick Henry", "28000", "Bal - RB", "15.83"))]
    (tmp_path / "matchup_9_w8.html").write_text(
        fx.matchup_html(("9", "Team Nine"), ("5", "Team Five"), pair, ("125.41", "114.70")))
    (tmp_path / "matchup_5_w8.html").write_text(
        fx.matchup_html(("5", "Team Five"), ("9", "Team Nine"),
                        [(p, r, l) for p, l, r in pair], ("114.70", "125.41")))
    (tmp_path / "players_RB_A.html").write_text(fx.players_html([
        ("Rico Dowdle", "34001", "Pit - RB", "W (Oct 28)"), ("Jaylen Warren", "32800", "Pit - RB", "FA")]))
    (tmp_path / "transactions_all.html").write_text(fx.transactions_all_html([
        ("Team Nine", 9, "Oct 21,3:57 am", [("Texans", "99001", "Hou - DEF", "Waiver")])]))
    return DirSource(tmp_path)


def test_id_resolution_prefers_yahoo_id_then_name_then_team_defense():
    ids = IdMap(SLEEPER)
    assert ids.resolve("40055", "Whoever", "ATL", "RB") == ("S_BIJAN", "yahoo_id")
    assert ids.resolve("28000", "Derrick Henry", "Bal", "RB") == ("S_HENRY", "name")
    assert ids.resolve("99001", "Houston", "Hou", "DEF") == ("HOU", "dst")
    assert ids.resolve("55555", "Mystery Rookie", "NYJ", "WR") == (None, "unmapped")


@pytest.mark.asyncio
async def test_league_uses_live_settings_and_flags_no_difference(snap):
    lg = await YahooProvider("1234567", snap, SLEEPER).get_league()
    assert lg["league_id"] == "yahoo:1234567" and lg["total_rosters"] == 2
    assert lg["metadata"]["scoring_source"] == "yahoo_settings_page"
    assert lg["metadata"]["scoring_diff_from_bfs"] == {}
    assert lg["scoring_settings"]["pass_td"] == 6.0 and lg["scoring_settings"]["bonus_pass_yd_400"] == 5.0
    assert lg["roster_positions"].count("BN") == 6 and "FLEX" in lg["roster_positions"]
    assert lg["settings"]["reserve_slots"] == 2


@pytest.mark.asyncio
async def test_roster_is_sleeper_shaped_and_keeps_unmapped_visible(snap):
    r = (await YahooProvider("1234567", snap, SLEEPER).get_rosters())[0]
    assert r["roster_id"] == 9 and r["owner_id"] == "9"
    assert r["starters"] == ["S_COUS", "S_BIJAN", "S_GOLD", "HOU"]
    assert r["players"] == ["S_COUS", "S_BIJAN", "S_GOLD", "HOU", "S_WARR"]   # bench included, IR unmapped
    assert r["reserve"] == []
    assert r["unmapped"][0]["name"] == "Mystery Rookie"
    assert r["settings"]["wins"] == 6 and r["settings"]["waiver_position"] == 6
    assert r["metadata"]["team_name"] == "Team Nine"
    assert any(s["injury"] == "Q" for s in r["yahoo_slots"])


@pytest.mark.asyncio
async def test_matchups_dedupe_the_two_views_of_one_game(snap):
    m = await YahooProvider("1234567", snap, SLEEPER).get_matchups(8)
    assert len(m) == 2 and m[0]["matchup_id"] == m[1]["matchup_id"] == 1
    assert {x["roster_id"] for x in m} == {9, 5}
    mine = next(x for x in m if x["roster_id"] == 9)
    assert mine["starters"] == ["S_COUS", "S_BIJAN"] and mine["projected_points"] == 125.41


@pytest.mark.asyncio
async def test_waiver_order_free_agent_tags_and_transactions(snap):
    p = YahooProvider("1234567", snap, SLEEPER)
    assert [t["name"] for t in await p.get_waiver_order()] == ["Team Five", "Team Nine"]
    fa = await p.get_free_agents("RB")
    assert fa[0]["availability"] == {"state": "W", "clears": "Oct 28", "owner": None}
    assert fa[1]["availability"]["state"] == "FA" and fa[1]["player_id"] == "S_WARR"
    tx = await p.get_transactions("all")
    assert tx["events"][0]["moves"][0]["label"] == "Waiver"


@pytest.mark.asyncio
async def test_existing_sleeper_paths_accept_a_yahoo_league_id(snap, monkeypatch):
    monkeypatch.setitem(sleeper_tools._PLAYERS_CACHE, "data", SLEEPER)
    runtime.set_source(snap)
    try:
        lg = await sleeper_tools.get_league("yahoo:1234567")
        assert lg["success"] and lg["league"]["metadata"]["platform"] == "yahoo"
        users = await sleeper_tools.get_league_users("yahoo:1234567")
        assert users["count"] == 2 and users["users"][0]["display_name"] == "Team Five"
        ro = await sleeper_tools.get_rosters("yahoo:1234567", force_refresh=True)
        assert ro["success"] is not False and ro["count"] == 2
        mine = next(r for r in ro["rosters"] if r["roster_id"] == 9)
        assert mine["starters"][:2] == ["S_COUS", "S_BIJAN"]
        mu = await sleeper_tools.get_matchups("yahoo:1234567", 8)
        assert mu["count"] == 2
    finally:
        runtime.set_source(None)
