"""Parsers against trimmed copies of real Yahoo pages (tests/fixtures/yahoo).

The fixtures are the tables the parsers read, from a live league, with the league id, team names and
logos replaced by placeholders and every add/drop/watch link (which carries a session token) removed.
"""
from pathlib import Path

from nfl_mcp.yahoo import parsers
from nfl_mcp.yahoo.idmap import IdMap
from nfl_mcp.yahoo.scoring import diff_from_bfs, parse_settings_rows

FIX = Path(__file__).parent / "fixtures" / "yahoo"


def _page(name: str) -> str:
    return (FIX / f"{name}.html").read_text(encoding="utf-8")


def test_roster_reads_every_slot_with_team_and_position():
    slots = parsers.parse_roster(_page("roster"))["slots"]
    assert len(slots) == 16
    assert all(s["name"] and s["team"] and s["position"] for s in slots)
    assert all(s["yahoo_id"] for s in slots if s["position"] != "DEF")  # team defenses carry no player id
    ir = next(s for s in slots if s["slot"] == "IR")
    assert (ir["name"], ir["team"], ir["position"], ir["injury"]) == ("Jordyn Tyson", "NO", "WR", "IR-R")
    assert any(s["slot"] == "DEF" and s["position"] == "DEF" for s in slots)


def test_every_rostered_player_maps_even_without_a_sleeper_yahoo_id():
    slots = parsers.parse_roster(_page("roster"))["slots"]
    dump = {f"S{i}": {"full_name": s["name"], "team": s["team"], "position": s["position"]}
            for i, s in enumerate(slots) if s["position"] != "DEF"}
    ids = IdMap(dump)
    unmapped = [s["name"] for s in slots
                if ids.resolve(s["yahoo_id"], s["name"], s["team"], s["position"])[0] is None]
    assert unmapped == []


def test_standings_have_team_names_and_a_full_waiver_order():
    teams = parsers.parse_standings(_page("standings"))["teams"]
    assert len(teams) == 10 and all(t["name"] and t["mid"] for t in teams)
    assert sorted(t["waiver"] for t in teams) == list(range(1, 11))
    mine = next(t for t in teams if t["mid"] == "9")
    assert (mine["wins"], mine["losses"], mine["waiver"], mine["moves"]) == (6, 1, 6, 26)


def test_players_page_marks_waiver_players_with_their_clear_date():
    players = parsers.parse_players(_page("players_rb"))["players"]
    assert len(players) == 25 and all(p["name"] and p["team"] and p["position"] for p in players)
    assert players[0]["availability"] == {"state": "W", "clears": "Oct 28", "owner": None}


def test_transactions_views_parse_and_waiver_offers_carry_priority():
    allv = parsers.parse_transactions(_page("transactions_all"))["events"]
    wav = parsers.parse_transactions(_page("transactions_waiver"))["events"]
    assert len(allv) == 25 and len(wav) == 21
    assert all(e["team"] and e["moves"] for e in allv)
    assert all(e["winning_priority"] and e["losers"] for e in wav)


def test_settings_page_yields_scoring_that_matches_bfs():
    rows = parsers.parse_settings_rows(_page("settings"))
    assert len(rows) > 50
    live = parse_settings_rows(rows)
    assert live and not diff_from_bfs(live)
