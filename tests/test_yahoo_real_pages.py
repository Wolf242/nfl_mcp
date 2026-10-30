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


def test_matchup_totals_come_from_the_starters_table_not_the_bench_sum():
    m = parsers.parse_matchup(_page("matchup"))
    home, away = m["home"], m["away"]
    assert home["team"]["name"].startswith("Team ") and away["team"]["name"].startswith("Team ")
    assert (home["total_pts"], home["total_proj"]) == (99.52, 141.60)
    assert (away["total_pts"], away["total_proj"]) == (120.66, 124.65)
    assert len(home["starters"]) == 9 and len(home["bench"]) == 8
    # starters' points add up to the displayed total
    assert round(sum(p["points"] for p in home["starters"]), 2) == home["total_pts"]


def test_matchup_empty_bench_slot_is_flagged_not_an_unnamed_player():
    m = parsers.parse_matchup(_page("matchup"))
    empties = [p for p in m["home"]["bench"] if p.get("empty")]
    assert len(empties) == 1 and empties[0]["name"] is None
    assert all(p["name"] for p in m["home"]["starters"] + m["away"]["starters"])


def test_te_availability_is_read_past_the_extra_highlight_column():
    players = parsers.parse_players(_page("players_te"))["players"]
    assert len(players) == 25 and all(p["position"] == "TE" for p in players)
    assert all(p["availability"]["state"] == "W" and p["availability"]["clears"] for p in players)


def test_team_defenses_on_the_wire_are_listed_with_their_team_code():
    players = parsers.parse_players(_page("players_def"))["players"]
    assert len(players) == 21 and all(p["position"] == "DEF" and p["team"] for p in players)
    assert all(p["yahoo_id"] is None for p in players)  # ids are never taken from add links
    assert all(p["availability"]["state"] == "W" for p in players)


def test_bye_week_rows_do_not_shift_columns_into_the_projection():
    slots = {s["name"]: s for s in parsers.parse_roster(_page("roster"))["slots"] if not s.get("empty")}
    for name in ("George Kittle", "Tyler Shough", "Texans"):
        s = slots[name]
        assert s["projected"] is None and s["pct_start"] is None and s["fantasy_points"] is None
        assert s["pct_rostered"] and s["bye"]
    played = [s for s in slots.values() if s["bye"] != slots["George Kittle"]["bye"]]
    assert played and all(0 < s["projected"] < 40 for s in played)


# ------------------------------------------------------------------ the QB, K and DEF free-agent pages, read live on 2026-10-29
def _wire(name: str) -> list[dict]:
    return parsers.parse_players(_page(name))["players"]


def _states(rows):
    out = {}
    for r in rows:
        out[r["availability"]["state"]] = out.get(r["availability"]["state"], 0) + 1
    return out


def test_the_qb_free_agent_page_parses_every_row_with_id_team_position_and_tag():
    rows = _wire("players_qb")
    assert len(rows) == 25 and all(r["position"] == "QB" for r in rows)
    assert all(r["yahoo_id"] and r["name"] and r["team"] for r in rows) and len({r["yahoo_id"] for r in rows}) == 25
    assert (rows[0]["name"], rows[0]["team"]) == ("Jacoby Brissett", "ARI")
    assert _states(rows) == {"FA": 24, "W": 1}
    rodgers = next(r for r in rows if r["name"] == "Aaron Rodgers")
    assert rodgers["availability"] == {"state": "W", "clears": "Nov 1", "owner": None}
    assert {r["injury"] for r in rows} == {None, "IR", "NA"} and all(r["game"] for r in rows)


def test_the_k_free_agent_page_parses_every_row_and_its_waiver_dates():
    rows = _wire("players_k")
    assert len(rows) == 25 and all(r["position"] == "K" for r in rows)
    assert all(r["yahoo_id"] and r["name"] and r["team"] for r in rows) and len({r["yahoo_id"] for r in rows}) == 25
    assert _states(rows) == {"FA": 22, "W": 3}
    assert {(r["name"], r["availability"]["clears"]) for r in rows if r["availability"]["state"] == "W"} == {
        ("Trey Smack", "Nov 4"), ("Matt Gay", "Oct 31"), ("Ryan Fitzgerald", "Nov 4")}
    assert {r["injury"] for r in rows} == {None, "NA"}


def test_the_def_free_agent_page_lists_team_defenses_by_team_code_without_a_player_id():
    rows = _wire("players_def_live")
    assert len(rows) == 23 and all(r["position"] == "DEF" and r["yahoo_id"] is None for r in rows)
    assert len({r["team"] for r in rows}) == 23 and (rows[0]["name"], rows[0]["team"]) == ("Jaguars", "JAX")
    assert _states(rows) == {"FA": 15, "W": 8}
    assert {r["availability"]["clears"] for r in rows if r["availability"]["state"] == "W"} >= {"Oct 31", "Nov 1", "Nov 4"}
    ids = IdMap({})
    assert all(ids.resolve(r["yahoo_id"], r["name"], r["team"], r["position"]) == (r["team"], "dst") for r in rows)


def test_the_wire_rows_for_qb_k_and_def_carry_yahoos_own_tag_from_the_real_pages():
    from nfl_mcp.yahoo import checkin
    for name, pos in (("players_qb", "QB"), ("players_k", "K"), ("players_def_live", "DEF")):
        rows = [checkin._wire_row(dict(r, player_id=r["yahoo_id"])) for r in _wire(name)]
        assert all(r["tag"] == "FA" or r["tag"].startswith("W (") for r in rows), pos
        assert all(r["position"] == pos and r["label"].endswith(f"({r['team']} {pos})") for r in rows), pos
    assert next(r for r in map(checkin._wire_row, _wire("players_def_live")) if r["name"] == "Jaguars")["tag"] == "W (Oct 31)"


def test_the_live_fixtures_carry_no_league_id_token_guid_email_or_write_link():
    import re
    for name in ("players_qb", "players_k", "players_def_live", "matchup_live", "roster_live"):
        text = _page(name)
        assert "1459067" not in text, name
        assert not re.search(r"(?i)crumb|bcookie|csrf|addplayer|dropplayer", text), name
        assert not re.search(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", text), name
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-(?!000000000000)[0-9a-f]{12}", text), name
