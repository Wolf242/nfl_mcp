"""Guard, scoring, waiver model and parsers for the Yahoo provider (offline)."""
import random

import pytest

from nfl_mcp.scoring import ScoringModel
from nfl_mcp.yahoo import guard, parsers
from nfl_mcp.yahoo.scoring import BFS_SCORING, diff_from_bfs, parse_settings_rows
from nfl_mcp.yahoo.waiver_model import Claim, plan_sequence, process_run, replay_log

from . import yahoo_fixtures as fx

L = "1234567"


# ---------------------------------------------------------------- guard
@pytest.mark.parametrize("url", [
    f"https://football.fantasysports.yahoo.com/f1/{L}",
    f"https://football.fantasysports.yahoo.com/f1/{L}/9",
    f"https://football.fantasysports.yahoo.com/f1/{L}/matchup?week=8&mid1=9",
    f"https://football.fantasysports.yahoo.com/f1/{L}/players?status=A&pos=RB",
    f"https://football.fantasysports.yahoo.com/f1/{L}/transactions?transactionsfilter=waiver",
    f"https://football.fantasysports.yahoo.com/f1/{L}/settings",
    "https://fantasysports.yahooapis.com/fantasy/v2/league/nfl.l.1234567/standings",
])
def test_guard_allows_reads(url):
    assert guard.check_read_only(url, league_id=L) == url


@pytest.mark.parametrize("url", [
    f"https://football.fantasysports.yahoo.com/f1/{L}/addplayer?apid=34107",
    f"https://football.fantasysports.yahoo.com/f1/{L}/dropplayer?apid=1",
    f"https://football.fantasysports.yahoo.com/f1/{L}/proposetrade",
    f"https://football.fantasysports.yahoo.com/f1/{L}/editroster",
    f"https://football.fantasysports.yahoo.com/f1/{L}/players?status=A&crumb=abc",
    "https://football.fantasysports.yahoo.com/f1/999/9",
    "https://evil.example.com/f1/1234567/9",
    "http://football.fantasysports.yahoo.com/f1/1234567/9",
    "https://fantasysports.yahooapis.com/fantasy/v2/admin/league",
])
def test_guard_blocks_everything_else(url):
    with pytest.raises(guard.WriteBlocked):
        guard.check_read_only(url, league_id=L)


def test_guard_blocks_non_get():
    for m in ("POST", "PUT", "DELETE"):
        with pytest.raises(guard.WriteBlocked):
            guard.check_method(m)
    guard.check_method("get")


# ---------------------------------------------------------------- scoring
def test_settings_page_rebuilds_bfs_constants():
    parsed = parse_settings_rows(fx.SETTINGS_ROWS)
    assert diff_from_bfs(parsed) == {}
    assert parsed["bonus_pass_yd_300"] == 2 and parsed["bonus_pass_yd_400"] == 5  # cumulative totals
    assert parsed["pass_yd"] == 0.04 and parsed["rush_yd"] == 0.1


def test_a_commissioner_change_is_reported():
    rows = [r.replace("= 6", "= 4", 1) if r.startswith("Passing Touchdowns") else r
            for r in fx.SETTINGS_ROWS]
    assert diff_from_bfs(parse_settings_rows(rows)) == {"pass_td": (6.0, 4.0)}


def test_bfs_yardage_bonuses_match_checked_box_scores():
    m = ScoringModel.from_settings(BFS_SCORING)
    base = lambda yds: m.game_points({"passing_yards": yds, "attempts": 0, "completions": 0}, "QB")  # noqa: E731
    # Week 1 2026 checks recorded in the project: 410 yds = +5, 334 yds = +2 on top of 0.04/yd.
    assert base(410) == pytest.approx(410 * 0.04 + 5)
    assert base(334) == pytest.approx(334 * 0.04 + 2)
    assert base(299) == pytest.approx(299 * 0.04)
    rush = lambda y: m.game_points({"rushing_yards": y, "carries": 0}, "RB")  # noqa: E731
    assert rush(100) == pytest.approx(10 + 2) and rush(200) == pytest.approx(20 + 5)


def test_bfs_td_and_ppr_values():
    m = ScoringModel.from_settings(BFS_SCORING)
    assert m.game_points({"passing_tds": 1}, "QB") == pytest.approx(6)
    assert m.game_points({"interceptions": 1}, "QB") == pytest.approx(-2)
    assert m.game_points({"receptions": 5, "receiving_yards": 50, "receiving_tds": 1}, "WR") == pytest.approx(5 + 5 + 6)


# ---------------------------------------------------------------- waiver model
ORDER = ["M1", "L", "St", "W", "GO", "LI", "SS", "U", "FG", "G"]


def test_win_goes_to_bottom_failed_claim_is_free():
    r = process_run(ORDER, {"L": [Claim("L", "a")], "W": [Claim("W", "a"), Claim("W", "b")],
                            "G": [Claim("G", "b")]})
    got = [(o.team, o.player, o.won, o.priority_at_processing) for o in r.outcomes]
    assert got == [("L", "a", True, 2), ("W", "a", False, 3), ("W", "b", True, 3), ("G", "b", False, 8)]
    assert r.final_order == ["M1", "St", "GO", "LI", "SS", "U", "FG", "G", "L", "W"]


def test_uncontested_wins_also_drop_you():
    r = process_run(ORDER, {"W": [Claim("W", "solo")]})
    assert r.final_order[-1] == "W" and r.final_order[3] == "GO"


def test_free_agent_activity_is_not_modelled_as_movement():
    r = process_run(ORDER, {})
    assert r.final_order == ORDER


def test_replay_log_reproduces_simulated_history_and_rejects_wrong_start():
    rng = random.Random(7)
    start = ORDER[:]
    cur = start[:]
    wins = []
    for _ in range(12):
        teams = rng.sample(cur, 5)
        players = [f"p{rng.randint(0, 3)}" for _ in teams]
        claims = {t: [Claim(t, p)] for t, p in zip(teams, players, strict=True)}
        res = process_run(cur, claims)
        by_player = {}
        for o in res.outcomes:
            by_player.setdefault(o.player, []).append(o)
        order = cur[:]
        for o in res.outcomes:
            if not o.won:
                continue
            # Yahoo shows each loser at their position when the WINNER's claim was processed.
            losers = [(x.team, order.index(x.team) + 1) for x in by_player[o.player] if not x.won]
            wins.append((o.team, order.index(o.team) + 1 if losers else None, losers))
            order.remove(o.team)
            order.append(o.team)
        cur = res.final_order
    assert replay_log(start, wins) == cur
    wrong = start[3:] + start[:3]  # a rotated start must violate some observed priority
    assert any(w[1] is not None for w in wins)
    assert replay_log(wrong, wins) is None


def test_plan_puts_contested_value_first_and_flags_shared_drop():
    plan = plan_sequence(ORDER, "W", [("Cheap", 1, "D1"), ("Dowdle", 5, "D1"), ("Star", 9, "D2")],
                         contested={"Dowdle"})
    assert [p[0] for p in plan["ranked"]] == ["Dowdle", "Star", "Cheap"]
    assert plan["current_priority"] == 4
    assert any("share one drop" in n for n in plan["notes"])


# ---------------------------------------------------------------- parsers
def test_roster_parser_reads_slot_player_id_injury_and_projection():
    html = fx.roster_html([
        ("RB", "Bijan Robinson", "40055", "Atl - RB", "20.16", None),
        ("WR", "Matthew Golden", "41001", "GB - WR", "11.30", "Q"),
        ("BN", "Jaylen Warren", "32800", "Pit - RB", "9.10", None),
    ])
    slots = parsers.parse_roster(html)["slots"]
    assert [s["slot"] for s in slots] == ["RB", "WR", "BN"]
    b, g, _ = slots
    assert (b["name"], b["yahoo_id"], b["team"], b["position"]) == ("Bijan Robinson", "40055", "ATL", "RB")
    assert b["projected"] == 20.16 and b["pct_start"] == 100.0 and b["bye"] == 11.0
    assert g["injury"] == "Q" and b["injury"] is None
    assert b["game"] == {"kickoff": "Sun 1:00 pm", "home": False, "opponent": "TB"}


def test_players_parser_distinguishes_fa_waiver_and_owned():
    html = fx.players_html([("Rico Dowdle", "34001", "Pit - RB", "W (Oct 28)"),
                            ("Free Guy", "34002", "Den - RB", "FA"),
                            ("Taken Guy", "34003", "Den - WR", "Team Two")])
    a = [p["availability"] for p in parsers.parse_players(html)["players"]]
    assert a[0] == {"state": "W", "clears": "Oct 28", "owner": None}
    assert a[1]["state"] == "FA" and a[2] == {"state": "OWNED", "clears": None, "owner": "Team Two"}


def test_standings_parser_reads_waiver_order():
    html = fx.standings_html([(1, "Team Nine", 9, "6-1-0", "949.28", "706.82", 6, 26),
                              (2, "Team Two", 2, "6-1-0", "948.36", "827.54", 10, 11)])
    t = parsers.parse_standings(html)["teams"]
    assert t[0]["name"] == "Team Nine" and t[0]["mid"] == "9" and t[0]["waiver"] == 6
    assert t[0]["wins"] == 6 and t[0]["moves"] == 26 and t[1]["waiver"] == 10


def test_matchup_parser_pairs_sides_and_totals():
    html = fx.matchup_html(("9", "Team Nine"), ("5", "Team Five"), [
        ("QB", ("Kirk Cousins", "25812", "LV - QB", "18.42"), ("Kyler Murray", "31833", "Min - QB", "20.22")),
        ("RB", ("Bijan Robinson", "40055", "Atl - RB", "20.16"), ("Derrick Henry", "28000", "Bal - RB", "15.83")),
    ], ("125.41", "114.70"))
    m = parsers.parse_matchup(html)
    assert m["home"]["team"] == {"mid": "9", "name": "Team Nine"}
    assert m["away"]["team"] == {"mid": "5", "name": "Team Five"}
    assert [p["name"] for p in m["home"]["starters"]] == ["Kirk Cousins", "Bijan Robinson"]
    assert m["away"]["starters"][1]["projected"] == 15.83
    assert (m["home"]["total_proj"], m["away"]["total_proj"]) == (125.41, 114.70)


def test_transactions_all_and_waiver_offer_views():
    allv = parsers.parse_transactions(fx.transactions_all_html([
        ("Team Four", 4, "Oct 25, 9:13 pm", [("Jakobi Meyers", "29000", "Jax - WR", "Free Agent"),
                                                        ("Juwan Johnson", "30000", "NO - TE", "To Waivers")])]))
    ev = allv["events"][0]
    assert ev["team"] == "Team Four" and ev["mid"] == "4" and ev["when"] == "Oct 25, 9:13 pm"
    assert [(m["player"], m["label"]) for m in ev["moves"]] == [("Jakobi Meyers", "Free Agent"),
                                                                 ("Juwan Johnson", "To Waivers")]
    wv = parsers.parse_transactions(fx.transactions_waiver_html([
        ("Josh Jacobs", "31856", "GB - RB", 9, [("Team Four", 4, 10)], "Team Two", 2,
         "Oct 21,3:57 am")]))
    e = wv["events"][0]
    assert e["winning_priority"] == 9 and e["losers"] == [("Team Four", 10)]
    assert e["team"] == "Team Two" and e["moves"][0]["player"] == "Josh Jacobs"


def test_defense_on_bye_is_flagged_and_has_no_player_id():
    html = ('<table id="statTable2"><thead><tr><th>Pos</th><th>Edit</th><th>Defense</th><th>Bye</th></tr></thead>'
            '<tbody><tr><td>DEF</td><td>DEF</td><td class="player"><a class="Nowrap" href="https://sports.yahoo.com/nfl/houston-texans-1">Texans</a>'
            '<span class="Fz-xxs">Hou - DEF</span><span class="ysf-game-status">Bye</span></td><td>8</td></tr></tbody></table>')
    d = parsers.parse_roster(html)["slots"][0]
    assert (d["slot"], d["name"], d["team"], d["position"], d["yahoo_id"]) == ("DEF", "Texans", "HOU", "DEF", None)
    assert d["game"]["bye"] is True
