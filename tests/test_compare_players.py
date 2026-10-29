"""compare_players: BFS scoring, usage, context, opponent, injuries, roster fit and the verdict.

Offline: tiny hand-built nflverse rows with fictional players (no network, no real stats)."""
import csv
import gzip
import io
from datetime import UTC, date, datetime

import pytest

from nfl_mcp import nflverse_files as nv
from nfl_mcp import player_compare as pc
from nfl_mcp.yahoo import checkin as ck

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
WEEK_START = date(2026, 10, 27)


def R(pid, name, pos, team, opp, week, **k):
    """A parsed weekly stat line (what ``parse_weekly_rows`` returns)."""
    base = dict.fromkeys(nv._WEEKLY_NUMERIC, 0.0)
    return {"player_id": pid, "name": name, "position": pos, "team": team, "opponent": opp, "week": week,
            "target_share": None, "interceptions": 0.0, **base, **k}


def S(week, team, name, pos, off=0.0, dfn=0.0):
    return {"week": week, "team": team, "name": name, "norm": nv.norm_person(name), "position": pos,
            "off_pct": off, "def_pct": dfn}


def Pl(week, team, kind, yl, rusher=None, receiver=None, down=1, sack=False):
    return {"week": week, "posteam": team, "defteam": "XXX", "kind": kind, "yardline_100": yl, "down": down,
            "ydstogo": 5.0, "epa": 0.0, "rusher_id": rusher, "receiver_id": receiver, "sack": sack, "scramble": False}


# ------------------------------------------------------------------ BFS scoring
def test_bfs_points_include_the_cumulative_yardage_bonuses():
    assert pc.bfs_points(R("a", "A", "RB", "ATL", "TB", 1, rushing_yards=99)) == 9.9
    assert pc.bfs_points(R("a", "A", "RB", "ATL", "TB", 1, rushing_yards=100)) == 12.0      # +2 at 100
    assert pc.bfs_points(R("a", "A", "RB", "ATL", "TB", 1, rushing_yards=199)) == 21.9
    assert pc.bfs_points(R("a", "A", "RB", "ATL", "TB", 1, rushing_yards=200)) == 25.0      # +5 in all at 200, not +2+5
    assert pc.bfs_points(R("a", "A", "WR", "ATL", "TB", 1, receptions=7, receiving_yards=100)) == 7 + 10 + 2
    assert pc.bfs_points(R("a", "A", "WR", "ATL", "TB", 1, receptions=10, receiving_yards=200)) == 10 + 20 + 5
    qb = dict(attempts=40, completions=30, passing_yards=334, passing_tds=2)
    assert pc.bfs_points(R("q", "Q", "QB", "ATL", "TB", 1, **qb)) == round(334 * 0.04 + 12 + 2, 2)    # 334 yd: +2
    assert pc.bfs_points(R("q", "Q", "QB", "ATL", "TB", 1, passing_yards=410)) == round(410 * 0.04 + 5, 2)   # 410 yd: +5 total


def test_bfs_points_price_touchdowns_turnovers_and_return_touchdowns():
    row = R("q", "Q", "QB", "ATL", "TB", 1, passing_yards=250, passing_tds=3, interceptions=2, rushing_yards=30,
            rushing_tds=1, rushing_fumbles_lost=1, special_teams_tds=1)
    assert pc.bfs_points(row) == round(250 * 0.04 + 18 - 4 + 3 + 6 - 2 + 6, 2)
    assert pc.bfs_points(R("w", "W", "WR", "ATL", "TB", 1, receptions=5, receiving_yards=60, receiving_tds=1,
                           receiving_fumbles_lost=1)) == 5 + 6 + 6 - 2


# ------------------------------------------------------------------ the nflverse parsers
def _csv(cols, rows):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def test_weekly_rows_keep_regular_season_skill_players_and_read_the_new_column_names():
    cols = ["player_id", "player_display_name", "position", "team", "opponent_team", "week", "season_type",
            "carries", "rushing_yards", "passing_interceptions", "special_teams_tds", "rushing_epa", "target_share"]
    text = _csv(cols, [
        {"player_id": "1", "player_display_name": "Rex Runner", "position": "RB", "team": "ATL", "opponent_team": "TB",
         "week": "3", "season_type": "REG", "carries": "12", "rushing_yards": "55", "passing_interceptions": "",
         "special_teams_tds": "1", "rushing_epa": "1.5", "target_share": "0.12"},
        {"player_id": "2", "player_display_name": "Kay Kicker", "position": "K", "team": "ATL", "opponent_team": "TB",
         "week": "3", "season_type": "REG"},
        {"player_id": "3", "player_display_name": "Pat Post", "position": "WR", "team": "ATL", "opponent_team": "TB",
         "week": "20", "season_type": "POST"},
        {"player_id": "4", "player_display_name": "Quin Quarter", "position": "QB", "team": "ATL", "opponent_team": "TB",
         "week": "3", "season_type": "REG", "passing_interceptions": "2"}])
    rows = nv.parse_weekly_rows(text)
    assert [r["name"] for r in rows] == ["Rex Runner", "Quin Quarter"]
    assert rows[0]["carries"] == 12 and rows[0]["special_teams_tds"] == 1 and rows[0]["target_share"] == 0.12
    assert rows[1]["interceptions"] == 2 and rows[1]["target_share"] is None


def test_snap_counts_pbp_and_schedule_parsers():
    snaps = nv.parse_snap_counts(_csv(
        ["week", "game_type", "player", "position", "team", "offense_pct", "defense_pct"],
        [{"week": "2", "game_type": "REG", "player": "Rex Runner Jr.", "position": "RB", "team": "ATL",
          "offense_pct": "0.64", "defense_pct": "0"},
         {"week": "2", "game_type": "POST", "player": "Skip Me", "position": "RB", "team": "ATL"}]))
    assert len(snaps) == 1 and snaps[0]["norm"] == "rexrunner" and snaps[0]["off_pct"] == 0.64

    cols = ["week", "season_type", "posteam", "defteam", "play_type", "rush_attempt", "pass_attempt", "yardline_100",
            "down", "ydstogo", "epa", "rusher_player_id", "receiver_player_id", "sack", "qb_scramble", "two_point_attempt"]
    base = {c: "" for c in cols} | {"week": "1", "season_type": "REG", "posteam": "ATL", "defteam": "TB"}
    text = _csv(cols, [
        base | {"play_type": "run", "rush_attempt": "1", "yardline_100": "8", "down": "1", "rusher_player_id": "R1"},
        base | {"play_type": "pass", "pass_attempt": "1", "yardline_100": "15", "down": "3", "receiver_player_id": "R2"},
        base | {"play_type": "pass", "pass_attempt": "1", "sack": "1"},
        base | {"play_type": "qb_kneel", "rush_attempt": "1"},
        base | {"play_type": "run", "rush_attempt": "1", "two_point_attempt": "1"},
        base | {"play_type": "kickoff"}])
    for raw in (text, gzip.compress(text.encode())):
        plays = nv.parse_pbp(raw)
        assert [p["kind"] for p in plays] == ["rush", "pass", "pass"]            # kneel, 2-pt try and kickoff are out
        assert plays[0]["yardline_100"] == 8 and plays[0]["rusher_id"] == "R1"
        assert plays[1]["receiver_id"] == "R2" and plays[1]["down"] == 3 and plays[2]["sack"]

    games = nv.parse_games(_csv(
        ["game_id", "season", "game_type", "week", "gameday", "gametime", "away_team", "home_team", "home_score",
         "spread_line", "total_line", "roof", "temp", "wind"],
        [{"game_id": "g1", "season": "2026", "game_type": "REG", "week": "8", "gameday": "2026-10-29", "gametime": "20:15",
          "away_team": "CAR", "home_team": "GB", "home_score": "", "spread_line": "3.5", "total_line": "46.5",
          "roof": "outdoors", "temp": "", "wind": ""},
         {"game_id": "g0", "season": "2025", "game_type": "REG", "week": "8", "gameday": "2025-10-26", "gametime": "13:00",
          "away_team": "CAR", "home_team": "GB", "home_score": "20", "spread_line": "1", "total_line": "40"}]), 2026)
    gb, car = games[(8, "GB")], games[(8, "CAR")]
    assert (gb["team_spread"], car["team_spread"]) == (-3.5, 3.5)           # the home team is favoured by 3.5
    assert (gb["implied_total"], car["implied_total"]) == (25.0, 21.5)
    assert gb["is_home"] and not car["is_home"] and gb["kickoff"] == datetime(2026, 10, 29, 20, 15) and not gb["played"]
    assert len(games) == 2                                                  # the 2025 game is another season


# ------------------------------------------------------------------ finding the players
def _players():
    return pc.index_players([R("1", "Alex Warren", "RB", "PIT", "CLE", 1), R("2", "Sam Warren", "TE", "IND", "TEN", 1),
                             R("3", "Rex Runner", "RB", "ATL", "TB", 1), R("3", "Rex Runner", "RB", "LAR", "TB", 2)])


def test_a_name_that_fits_two_players_is_an_error_that_lists_them():
    idx = _players()
    e, err = pc.resolve_player(idx, "Warren")
    assert e is None and "Alex Warren (PIT RB)" in err and "Sam Warren (IND TE)" in err
    assert pc.resolve_player(idx, "Alex Warren (PIT RB)")[0]["player_id"] == "1"
    assert pc.resolve_player(idx, "Sam Warren (IND)")[0]["player_id"] == "2"
    assert pc.resolve_player(idx, "rex runner")[0]["team"] == "LAR"          # his latest team
    assert pc.resolve_player(idx, "3")[0]["name"] == "Rex Runner"            # a GSIS id
    assert "No QB, RB, WR or TE named" in pc.resolve_player(idx, "Nobody Here")[1]


# ------------------------------------------------------------------ game log
def test_game_log_stats_cover_only_the_weeks_before_the_one_being_decided():
    lines = [R("1", "Rex Runner", "RB", "ATL", "TB", w, rushing_yards=y) for w, y in ((1, 50), (2, 100), (3, 70), (4, 90), (5, 200))]
    log = pc.game_log(pc.index_players(lines)["1"], 5)
    pts = [5.0, 12.0, 7.0, 9.0]
    assert [x["bfs_points"] for x in log["log"]] == pts and log["games"] == 4
    assert (log["mean"], log["median"], log["min"], log["max"]) == (8.2, 8.0, 5.0, 12.0)
    assert [x["week"] for x in log["last_3"]] == [2, 3, 4]
    assert pc.game_log(pc.index_players(lines)["1"], 1)["mean"] is None


# ------------------------------------------------------------------ usage
def _usage_fixture():
    rex = [R("R", "Rex Runner", "RB", "ATL", "TB", 1, carries=10, receptions=2, targets=3, target_share=0.15),
           R("R", "Rex Runner", "RB", "ATL", "TB", 2, carries=14, receptions=1, targets=2)]
    other = [R("W", "Ann Wide", "WR", "ATL", "TB", 1, targets=17, carries=0), R("W", "Ann Wide", "WR", "ATL", "TB", 2, targets=18)]
    rows = rex + other
    snaps = [S(1, "ATL", "Rex Runner", "RB", 0.70), S(2, "ATL", "Rex Runner", "RB", 0.80)]
    plays = [Pl(1, "ATL", "rush", 30, "R"), Pl(1, "ATL", "rush", 15, "R"), Pl(1, "ATL", "rush", 8, "R"), Pl(1, "ATL", "rush", 5, "W"),
             Pl(1, "ATL", "pass", 12, receiver="R", down=2), Pl(1, "ATL", "pass", 40, receiver="R", down=3),
             Pl(1, "ATL", "pass", 10, receiver="R", sack=True),
             Pl(2, "ATL", "rush", 9, "R"), Pl(2, "ATL", "rush", 40, "R"), Pl(2, "ATL", "rush", 3, "W")]
    return pc.index_players(rows)["R"], pc.UsageData(rows, snaps, plays)


def test_usage_by_week_and_in_summary():
    rex, u = _usage_fixture()
    use = pc.usage(rex, 3, u)
    w1, w2 = use["weeks"]
    assert (w1["snap_pct"], w1["carries"], w1["carry_share"], w1["touches"]) == (70.0, 10.0, 100.0, 12.0)
    assert w1["target_share"] == 15.0 and w2["target_share"] == round(100 * 2 / 20, 1)    # his own share, else targets / team's
    assert (w1["carries_inside_20"], w1["carries_inside_10"], w1["red_zone_targets"]) == (2, 1, 1)
    assert (w2["carries_inside_20"], w2["carries_inside_10"], w2["red_zone_targets"]) == (1, 1, 0)
    s = use["season"]
    assert s["games"] == 2 and s["snap_pct"] == 75.0 and s["touches_per_game"] == 13.5
    assert s["inside_20_carry_share"] == 60.0 and s["inside_10_carry_share"] == 50.0       # 3 of 5, 2 of 4
    assert s["red_zone_targets"] == 1 and s["carries_inside_20"] == 3
    assert s["red_zone_opportunities_per_game"] == 2.0                                      # (3 carries + 1 target) / 2 games
    assert use["last_3"]["games"] == 2 and use["trend"]["snap_pct"] == "insufficient_data"
    assert pc.usage(rex, 2, u)["weeks"][-1]["week"] == 1                                    # nothing from week 2 on


def test_usage_without_play_by_play_leaves_those_fields_empty_and_says_so():
    rows = [R("R", "Rex Runner", "RB", "ATL", "TB", 1, carries=10)]
    use = pc.usage(pc.index_players(rows)["R"], 2, pc.UsageData(rows, None, None))
    w = use["weeks"][0]
    assert w["snap_pct"] is None and w["carries_inside_10"] is None and w["red_zone_targets"] is None
    assert "unavailable" in use["note"]


# ------------------------------------------------------------------ teammates taking the work
def _room():
    rows, snaps, plays = [], [], []
    base = {"Sam Sprint": ("S", [0.70, 0.65, 0.62, 0.55]), "Tom Turbo": ("T", [0.15, 0.22, 0.35, 0.45]),
            "Una Upshot": ("U", [0.10, 0.20, 0.30, 0.40]), "Gil Goal": ("G", [0.50, 0.30, 0.20, 0.10])}
    for name, (pid, shares) in base.items():
        for i, sh in enumerate(shares, start=1):
            rows.append(R(pid, name, "RB", "LAR", "LAC", i, carries=5))
            snaps.append(S(i, "LAR", name, "RB", sh))
    for w in range(1, 5):
        plays += [Pl(w, "LAR", "rush", 6, "S"), Pl(w, "LAR", "rush", 4, "T")]        # inside the 10: 4 each
        plays += [Pl(w, "LAR", "pass", 30, receiver="U", down=3), Pl(w, "LAR", "pass", 30, receiver="S", down=1)]
        plays += [Pl(w, "LAR", "rush", 50, "G")]
    return rows, snaps, plays


def test_a_teammate_is_named_only_when_his_snaps_rise_and_he_takes_goal_line_or_passing_down_work():
    rows, snaps, plays = _room()
    u = pc.UsageData(rows, snaps, plays)
    idx = pc.index_players(rows)
    names = {nv.norm_person(e["name"]): e for e in idx.values()}
    out = pc.teammates_taking_work(idx["S"], 5, u, names)
    got = {t["player"]: t for t in out["teammates"]}
    assert set(got) == {"Tom Turbo (LAR RB)", "Una Upshot (LAR RB)"}
    assert got["Tom Turbo (LAR RB)"]["snap_trend"] == "rising" and "goal-line: 4 of the team's 8 carries inside the 10" in got["Tom Turbo (LAR RB)"]["takes"][0]
    assert "passing downs: 4 of the team's 4 third/fourth-down targets" in got["Una Upshot (LAR RB)"]["takes"][0]
    assert "Gil Goal (LAR RB)" not in got                                        # falling snaps, no goal-line share
    assert [r["player"] for r in out["room"]][0] == "Tom Turbo (LAR RB)" and len(out["room"]) == 3
    assert out["weeks"] == [1, 2, 3, 4]
    # rising snaps without the work, or the work without rising snaps: not named
    flat = [s | {"off_pct": 0.3} if s["name"] == "Tom Turbo" else s for s in snaps]
    assert "Tom Turbo (LAR RB)" not in {t["player"] for t in pc.teammates_taking_work(
        idx["S"], 5, pc.UsageData(rows, flat, plays), names)["teammates"]}
    no_work = pc.UsageData(rows, snaps, [p for p in plays if p["rusher_id"] != "T"])
    assert "Tom Turbo (LAR RB)" not in {t["player"] for t in pc.teammates_taking_work(idx["S"], 5, no_work, names)["teammates"]}
    assert pc.teammates_taking_work(idx["S"], 5, pc.UsageData(rows, None, plays), names)["teammates"] == []


# ------------------------------------------------------------------ the game and who locks first
def _games(thu=True):
    return nv.parse_games(_csv(
        ["game_id", "season", "game_type", "week", "gameday", "gametime", "away_team", "home_team", "home_score",
         "spread_line", "total_line", "roof"],
        [{"game_id": "g1", "season": "2026", "game_type": "REG", "week": "8", "gameday": "2026-10-29" if thu else "2026-11-01",
          "gametime": "20:15" if thu else "13:00", "away_team": "TB", "home_team": "ATL", "home_score": "",
          "spread_line": "-2.5", "total_line": "44", "roof": "dome"},
         {"game_id": "g2", "season": "2026", "game_type": "REG", "week": "8", "gameday": "2026-11-01", "gametime": "16:05",
          "away_team": "LAC", "home_team": "LAR", "home_score": "", "spread_line": "6", "total_line": "48", "roof": "outdoors"}]), 2026)


def test_context_gives_the_line_the_implied_total_and_the_kickoff():
    g = _games()
    atl = pc.game_context(g, "ATL", 8)
    assert (atl["home_or_away"], atl["team_spread"], atl["total"], atl["implied_team_total"]) == ("home", 2.5, 44.0, 20.8)
    assert atl["kickoff"] == "Thu 2026-10-29 20:15 ET" and atl["roof"] == "dome"
    lac = pc.game_context(g, "LAC", 8)
    assert lac["home_or_away"] == "away" and lac["team_spread"] == 6.0 and lac["implied_team_total"] == 21.0
    assert pc.game_context(g, "GB", 8) == {"found": False, "bye": True, "note": "GB has no game in week 8 (bye)."}
    assert pc.game_context(None, "GB", 8)["found"] is False


def test_the_thursday_player_locks_first_and_that_limits_the_choice():
    g = _games()
    a = {"name": "Rex Runner", "team": "ATL", "position": "RB"}
    b = {"name": "Sam Sprint", "team": "LAR", "position": "RB"}
    lock = pc.lock_analysis(a, b, pc.game_context(g, "ATL", 8), pc.game_context(g, "LAR", 8))
    assert lock["locks_first"] == "Rex Runner (ATL RB)" and lock["first_is_thursday"] and lock["hours_between"] == 67.8
    assert "cannot inform that choice" in lock["effect"] and "cannot be swapped back in" in lock["effect"]
    lock = pc.lock_analysis(b, a, pc.game_context(g, "LAR", 8), pc.game_context(g, "ATL", 8))
    assert lock["locks_first"] == "Rex Runner (ATL RB)"                      # the order of the arguments does not matter
    sun = _games(thu=False)
    same = pc.lock_analysis(a, {"name": "Tim Twin", "team": "TB", "position": "RB"},
                            pc.game_context(sun, "ATL", 8), pc.game_context(sun, "TB", 8))
    assert same["locks_first"] is None and "lock together" in same["effect"]
    assert pc.lock_analysis(a, b, {"found": False}, pc.game_context(g, "LAR", 8))["locks_first"] is None


# ------------------------------------------------------------------ the opponent
def _def_rows(season, spec):
    """One RB line per (defense, week): spec = {defense: (rushing_yards, rushing_tds)}."""
    return [R(f"{d}{w}", "Rb", "RB", "ZZZ", d, w, carries=10, rushing_yards=y, rushing_tds=t, rushing_epa=e)
            for d, (y, t, e) in spec.items() for w in range(1, 5)]


def test_the_rank_is_marked_yard_driven_or_touchdown_driven():
    # yards-driven: the stingiest on yards is also the stingiest overall (TDs equal everywhere)
    spec = {"DT": (50, 1, -0.2), "D1": (60, 1, 0.0), "D2": (70, 1, 0.0), "D3": (80, 1, 0.1), "D4": (90, 1, 0.1), "D5": (100, 1, 0.2)}
    p = pc.opponent_profile(_def_rows(2026, spec), "RB", "DT", 2026)
    assert p["rank"] == 1 and p["yard_points_per_game"]["rank"] == 1 and p["driver"] == "yards (stable)"
    assert p["bfs_per_game_allowed"] == 11.0 and p["games"] == 4 and p["of"] == 6 and not p["small_sample"]
    assert p["epa_per_rush_allowed"]["rank"] == 1 and p["epa_per_rush_allowed"]["value"] == -0.02
    assert p["splits"]["rushing_yards"] == {"per_game": 50.0, "rank": 1}
    # touchdown-driven: few touchdowns allowed but the most yards, so the rank follows the touchdowns
    spec = {"DT": (90, 0, 0.0), "D1": (50, 1, 0.0), "D2": (60, 1, 0.0), "D3": (70, 1, 0.0), "D4": (80, 1, 0.0), "D5": (85, 1, 0.0)}
    p = pc.opponent_profile(_def_rows(2026, spec), "RB", "DT", 2026)
    assert p["rank"] == 1 and p["yard_points_per_game"]["rank"] == 6 and p["td_points_per_game"]["rank"] == 1
    assert p["driver"] == "touchdowns (noisy)" and p["td_share_of_points"] == 0.0
    assert pc.opponent_profile(None, "RB", "DT", 2025) == {"season": 2025, "available": False}
    assert pc.opponent_profile(_def_rows(2026, spec), "RB", None, 2026)["available"] is False
    short = _def_rows(2026, spec)[:1] + [R("x", "Rb", "RB", "ZZZ", "DT", 9)]
    assert pc.opponent_profile(short, "RB", "DT", 2026)["small_sample"] is True


def test_the_qb_split_is_passing_and_rushing_not_receiving():
    rows = [R(f"q{d}", "Qb", "QB", "ZZZ", d, 1, passing_yards=y, passing_tds=1) for d, y in (("D1", 200), ("D2", 300))]
    p = pc.opponent_profile(rows, "QB", "D1", 2026)
    assert set(p["splits"]) == {"passing_yards", "passing_tds", "rushing_yards", "rushing_tds"}
    assert p["splits"]["passing_yards"] == {"per_game": 200.0, "rank": 1}


# ------------------------------------------------------------------ injuries
def test_injury_records_show_their_dates_and_flag_stale_and_single_source():
    rec = {"nfl_injury_status": "Questionable", "nfl_injury_source": "report", "practice_status": "DNP",
           "nfl_injury_body_part": "Knee", "nfl_injury_report_date": "2026-10-24T12:00:00+00:00",
           "nfl_injury_stored_at": "2026-10-29T00:30:00+00:00"}
    r = pc.injury_record(rec, "Rex Runner (ATL RB)", NOW, WEEK_START)
    assert r["report_date"] == "2026-10-24T12:00:00+00:00" and r["stored_at"].startswith("2026-10-29")
    assert {"stale_injury", "single_source"} <= set(r["flags"]) and len(r["stale_reasons"]) == 2
    assert r["statement"].startswith("stored report (dated 2026-10-24, stale:") and "Questionable, practice DNP, Knee" in r["statement"]
    fresh = pc.injury_record(rec | {"nfl_injury_report_date": "2026-10-28T09:00:00+00:00", "nfl_injury_source": "both"},
                             "Rex Runner (ATL RB)", NOW, WEEK_START)
    assert fresh["flags"] == [] and fresh["statement"].startswith("nfl_mcp report (dated 2026-10-28) says")
    conflict = pc.injury_record(rec | {"injury": "O"}, "Rex Runner (ATL RB)", NOW, WEEK_START)
    assert "injury_conflict" in conflict["flags"]


def test_the_opposing_defenses_key_starters_are_the_injured_ones_with_the_snaps():
    snaps = [S(w, "TB", n, "DE", dfn=d) for w in (5, 6, 7) for n, d in (("Dan Edge", 0.9), ("Bo Backup", 0.2), ("Cy Corner", 0.8))]
    records = [{"player_name": "Dan Edge", "position": "DE", "injury_status": "Out", "injury_type": "Hamstring",
                "date_reported": "2026-10-28T10:00:00+00:00", "updated_at": "2026-10-29T00:00:00+00:00", "sources": ["ESPN", "CBS"]},
               {"player_name": "Bo Backup", "position": "DE", "injury_status": "Out", "date_reported": "2026-10-28"},
               {"player_name": "Cy Corner", "position": "CB", "injury_status": "Questionable", "injury_type": "Ankle",
                "date_reported": "2026-10-20T10:00:00+00:00", "sources": '["ESPN"]'},
               {"player_name": "Hal Healthy", "position": "S", "injury_status": ""}]
    out = pc.defense_injuries(records, snaps, "TB", 8, NOW, WEEK_START)
    by = {r["player"]: r for r in out["players"]}
    assert set(by) == {"Dan Edge (TB DE)", "Cy Corner (TB CB)"}                # the backup and the healthy player are out
    assert by["Dan Edge (TB DE)"]["flags"] == [] and by["Dan Edge (TB DE)"]["defense_snap_pct"] == 90.0
    assert {"stale_injury", "single_source"} <= set(by["Cy Corner (TB CB)"]["flags"])
    assert by["Cy Corner (TB CB)"]["report_date"] == "2026-10-20T10:00:00+00:00"
    no_snaps = pc.defense_injuries(records, None, "TB", 8, NOW, WEEK_START)
    assert len(no_snaps["players"]) == 3 and "starters or not" in no_snaps["note"]


# ------------------------------------------------------------------ roster fit
def _roster():
    def r(slot, name, pos, team, proj=None):
        return {"slot": slot, "name": name, "position": pos, "team": team, "player_id": None, "injury": None,
                "yahoo_projection": proj, "bye_week": None}
    return [r("QB", "Quin Quarter", "QB", "ATL", 20.0), r("RB", "Rex Runner", "RB", "ATL", 14.0), r("RB", "Dee Dash", "RB", "KC", 9.0),
            r("WR", "Ann Wide", "WR", "ATL", 12.0), r("FLEX", "Fay Flex", "WR", "LAC", 8.0), r("TE", "Tia Tight", "TE", "KC", 7.0),
            r("DEF", "Falcons", "DEF", "ATL", 6.0), r("BN", "Ben Bench", "RB", "DAL", 5.0), r("BN", "Sam Sprint", "RB", "LAR", 11.0),
            r("IR", "Ivy Injured", "WR", "ATL")]


def test_roster_fit_names_the_slot_the_alternatives_and_what_he_stacks_or_hedges_with():
    rex = {"name": "Rex Runner", "team": "ATL", "position": "RB"}
    sam = {"name": "Sam Sprint", "team": "LAR", "position": "RB"}
    fit = pc.roster_fit(rex, sam, _roster(), "TB")
    assert fit["on_roster"] and fit["slot"] == "RB"
    assert {e["slot"] for e in fit["eligible_slots"]} == {"RB", "FLEX"}
    assert [a["player"] for a in fit["bench_alternatives_for_slot"]] == ["Ben Bench (DAL RB)"]       # Sam is the one compared
    kinds = {x["player"]: x["kind"] for x in fit["same_game"]}
    assert kinds == {"Quin Quarter (ATL QB)": "stack", "Ann Wide (ATL WR)": "same_team", "Falcons (ATL DEF)": "stack"}
    bench = pc.roster_fit(sam, rex, _roster(), "LAC")
    assert bench["slot"] == "BN" and "whom he would replace" in bench["note"]
    assert {e["slot"] for e in bench["eligible_slots"]} == {"RB", "FLEX"}
    assert {x["player"]: x["kind"] for x in bench["same_game"]} == {"Fay Flex (LAC WR)": "hedge"}      # the opponent's player
    out = pc.roster_fit({"name": "Wes Wide", "team": "TB", "position": "WR"}, rex, _roster(), "ATL")
    assert out["on_roster"] is False and {e["slot"] for e in out["eligible_slots"]} == {"WR", "FLEX"}
    assert {x["kind"] for x in out["same_game"]} == {"hedge"} and len(out["same_game"]) == 3     # QB, WR, DEF on the other side
    assert pc.roster_fit(rex, sam, None, "TB")["on_roster"] is None


# ------------------------------------------------------------------ the verdict
def _side(name, proj, *, rz=3.0, floor=8.0, ceil=25.0, flags=(), status=None, yahoo=None, on_bye=False):
    return {"label": f"{name} (ATL RB)", "projection": {"nfl_mcp": proj, "yahoo": yahoo, "on_bye": on_bye},
            "usage": {"season": {"red_zone_opportunities_per_game": rz}}, "game_log": {"min": floor, "max": ceil},
            "injuries": {"player": {"flags": list(flags), "status": status, "statement": "stored report (dated 2026-10-24, stale: x) says Questionable"}}}


def test_inside_the_tie_band_the_verdict_is_a_toss_up_with_ordered_tiebreakers_never_a_pick():
    v = pc.verdict(_side("Rex Runner", 15.4, rz=4.0, floor=9.0, ceil=22.0), _side("Sam Sprint", 14.6, rz=2.0, floor=6.0, ceil=31.0))
    assert v["call"] == "toss-up" and v["pick"] is None and v["gap"] == 0.8
    assert [t["name"] for t in v["tiebreakers"]] == ["projection", "red_zone_share", "floor_ceiling"]
    t = v["tiebreakers"]
    assert t[0]["favors"] == "Rex Runner (ATL RB)" and t[1]["favors"] == "Rex Runner (ATL RB)"
    assert t[2]["floor_favors"] == "Rex Runner (ATL RB)" and t[2]["ceiling_favors"] == "Sam Sprint (ATL RB)"
    assert "none of them is a pick" in v["summary"] and "inside the 1.5-point tie band" in v["summary"]
    assert pc.verdict(_side("A", 15.0), _side("B", 13.5))["call"] == "edge"                 # exactly 1.5 apart is outside
    assert pc.verdict(_side("A", 15.0), _side("B", 13.6))["call"] == "toss-up"


def test_outside_the_band_it_is_an_edge_and_negligible_differences_favour_nobody():
    v = pc.verdict(_side("Rex Runner", 15.0), _side("Sam Sprint", 18.0, rz=3.3, floor=8.5, ceil=25.0))
    assert v["call"] == "edge" and v["pick"] == "Sam Sprint (ATL RB)" and v["gap"] == -3.0
    t = v["tiebreakers"]
    assert t[1]["favors"] == "even" and t[2]["floor_favors"] == "even" and t[2]["ceiling_favors"] == "even"
    assert pc.verdict(_side("A", 10.0), _side("B", 10.02))["tiebreakers"][0]["favors"] == "even"


def test_unconfirmed_availability_and_missing_projections_are_called_out():
    v = pc.verdict(_side("Rex Runner", 15.0, flags=["stale_injury", "single_source"], status="Questionable"), _side("Sam Sprint", 15.2))
    assert v["call"] == "toss-up" and len(v["caveats"]) == 1 and "availability is not confirmed" in v["caveats"][0]
    none = pc.verdict(_side("A", None, on_bye=True), _side("B", 12.0))
    assert none["call"] == "no call" and none["pick"] is None and "A (ATL RB) is on bye." in none["caveats"]
    yahoo = pc.verdict(_side("A", None, yahoo=14.0), _side("B", None, yahoo=10.0))
    assert yahoo["projection_basis"] == "Yahoo" and yahoo["pick"] == "A (ATL RB)"


# ------------------------------------------------------------------ the whole comparison
def _league_rows():
    rows = []
    for w in range(1, 5):
        rows += [R("A", "Rex Runner", "RB", "ATL", "TB", w, carries=15, rushing_yards=60 + 10 * w, rushing_tds=w % 2, receptions=2,
                   targets=3, receiving_yards=15),
                 R("B", "Sam Sprint", "RB", "LAR", "LAC", w, carries=14, rushing_yards=70, rushing_tds=0, receptions=3, targets=4,
                   receiving_yards=25),
                 R("T", "Tom Turbo", "RB", "LAR", "LAC", w, carries=2 + 2 * w, rushing_yards=20),
                 R("W", "Ann Wide", "WR", "ATL", "TB", w, targets=8, receptions=5, receiving_yards=60, target_share=0.3)]
        rows += [R(f"x{d}{w}", "Other Rb", "RB", "ZZZ", d, w, carries=10, rushing_yards=40 + 10 * i, rushing_epa=0.1 * i)
                 for i, d in enumerate(("TB", "LAC", "D3", "D4"))]
    return rows


def _sources(**over):
    rows = _league_rows()
    snaps = [S(w, "ATL", "Rex Runner", "RB", 0.75) for w in range(1, 5)]
    snaps += [S(w, "LAR", n, "RB", sh) for w in range(1, 5) for n, sh in (("Sam Sprint", 0.7 - 0.05 * w), ("Tom Turbo", 0.1 + 0.1 * w))]
    snaps += [S(w, "TB", "Dan Edge", "DE", dfn=0.9) for w in range(1, 5)]
    plays = [Pl(w, "LAR", "rush", 4, "T") for w in range(1, 5)] + [Pl(w, "LAR", "pass", 20, receiver="T", down=3) for w in range(1, 5)]

    async def weekly(season):
        return rows if season == 2026 else None

    async def snap(season):
        return snaps

    async def pbp(season):
        return plays

    async def games(season):
        return _games()

    async def project(players, **kw):
        assert kw["scoring"] is pc.BFS_SCORER and kw["week"] == 8
        pts = {"Rex Runner": 15.4, "Sam Sprint": 14.6}
        return {"projections": [{"player": p["name"], "projected_points": pts[p["name"]], "projection_source": "sleeper_blend",
                                 "model_projection": 14.0, "sleeper_projection": 16.0, "injury_status": None} for p in players]}

    async def roster():
        return _roster()

    kw = dict(weekly=weekly, snaps=snap, pbp=pbp, games=games, project=project, roster=roster,
              stored_report=lambda name, team: {}, team_injuries=lambda team: [], week_start=WEEK_START)
    kw.update(over)
    return pc.Sources(**kw)


@pytest.mark.asyncio
async def test_two_same_position_players_compared_end_to_end():
    r = await pc.compare_players("Rex Runner", "Sam Sprint (LAR RB)", season=2026, week=8, src=_sources(), now=NOW)
    assert r["success"] and r["read_only"] and r["scoring"] == "BFS" and r["week"] == 8
    a, b = r["a"], r["b"]
    assert a["label"] == "Rex Runner (ATL RB)" and b["label"] == "Sam Sprint (LAR RB)"
    assert a["game_log"]["games"] == 4 and b["game_log"]["mean"] == 12.5                        # 70 rush yds + 3 rec + 25 rec yds
    assert a["context"]["home_or_away"] == "home" and b["context"]["kickoff"] == "Sun 2026-11-01 16:05 ET"
    assert [t["player"] for t in b["teammates"]["teammates"]] == ["Tom Turbo (LAR RB)"]
    assert a["opponent"]["name"] == "TB" and a["opponent"]["this_season"]["available"] and not a["opponent"]["last_season"]["available"]
    assert a["projection"]["nfl_mcp"] == 15.4 and a["projection"]["yahoo"] == 14.0 and a["projection"]["gap_vs_yahoo"] == 1.4
    assert a["projection"]["gap_is_noise"] is True and a["projection"]["scoring"] == "BFS"
    assert b["projection"]["yahoo"] == 11.0 and b["fit"]["slot"] == "BN"
    assert a["fit"]["slot"] == "RB" and a["injuries"]["opposing_defense"]["team"] == "TB"
    assert b["injuries"]["opposing_defense"]["team"] == "LAC"
    assert r["lock"]["locks_first"] == "Rex Runner (ATL RB)" and r["lock"]["first_is_thursday"]
    v = r["verdict"]
    assert v["call"] == "toss-up" and v["pick"] is None and v["gap"] == 0.8
    assert r["warnings"] == ["Last season's weekly stats is not published for 2025."]


@pytest.mark.asyncio
async def test_missing_optional_files_become_warnings_and_unresolvable_names_become_errors():
    async def none(season):
        return None
    r = await pc.compare_players("Rex Runner", "Sam Sprint", season=2026, week=8,
                                 src=_sources(snaps=none, pbp=none, games=none, roster=None), now=NOW)
    assert r["success"] and len(r["warnings"]) == 4 and "Play by play is not published for 2026." in r["warnings"]
    assert r["a"]["context"]["found"] is False and r["a"]["usage"]["weeks"][0]["snap_pct"] is None
    assert r["a"]["fit"]["on_roster"] is None and r["lock"]["locks_first"] is None
    assert r["verdict"]["call"] == "toss-up"                                                   # the projections still decide
    err = await pc.compare_players("Rex Runner", "Rex Runner", season=2026, week=8, src=_sources())
    assert err == {"success": False, "error": "Both names are the same player."}
    err = await pc.compare_players("Rex Runner", "Nobody", season=2026, week=8, src=_sources())
    assert not err["success"] and "No QB, RB, WR or TE named 'Nobody'" in err["error"]
    err = await pc.compare_players("Rex Runner", "Sam Sprint", season=2026, week=8, src=_sources(weekly=none))
    assert not err["success"] and "weekly player stats for 2026 are not available" in err["error"]


@pytest.mark.asyncio
async def test_a_failing_projection_is_a_warning_and_leaves_no_call_without_numbers():
    async def boom(players, **kw):
        raise RuntimeError("engine down")
    r = await pc.compare_players("Rex Runner", "Sam Sprint", season=2026, week=8, src=_sources(project=boom, roster=None), now=NOW)
    assert any("engine down" in w for w in r["warnings"])
    assert r["verdict"]["call"] == "no call" and r["a"]["projection"]["nfl_mcp"] is None


# ------------------------------------------------------------------ the tool
@pytest.mark.asyncio
async def test_the_tool_uses_the_nflverse_files_the_default_league_and_changes_nothing(monkeypatch):
    from nfl_mcp import database, projections, week_context
    from nfl_mcp.tools import yahoo as tools

    async def resolve(season, wk, db=None):
        return 2026, wk or 8, False

    async def weekly(season):
        return _league_rows() if season == 2026 else None

    async def none(season):
        return None

    async def games(season):
        return _games()

    async def project(players, **kw):
        assert kw["scoring"] is pc.BFS_SCORER
        assert kw["db"].record_projections([{"player": "x"}]) == 0          # the engine's log is switched off
        assert kw["db"].find_player_injury("Rex Runner", "ATL") is None     # reads still go through
        return {"projections": [{"player": p["name"], "projected_points": 15.0, "projection_source": "sleeper_blend"} for p in players]}

    class Db:
        def record_projections(self, *a, **k):
            raise AssertionError("compare_players must not write the projection log")

        def find_player_injury(self, *a, **k):
            return None

        def get_team_injuries_from_cache(self, team, max_age_hours=None):
            return []
    monkeypatch.setattr(week_context, "resolve_season_week", resolve)
    monkeypatch.setattr(database, "get_shared_db", lambda: Db())
    monkeypatch.setattr(nv, "fetch_weekly_rows", weekly)
    monkeypatch.setattr(nv, "fetch_snap_counts", none)
    monkeypatch.setattr(nv, "fetch_pbp", none)
    monkeypatch.setattr(nv, "fetch_games", games)
    monkeypatch.setattr(projections, "project_players", project)
    monkeypatch.delenv("NFL_MCP_DEFAULT_LEAGUE", raising=False)
    monkeypatch.delenv("NFL_MCP_MY_ROSTER", raising=False)
    r = await tools.compare_players("Rex Runner", "Sam Sprint")
    assert r["success"] and r["verdict"]["call"] == "toss-up" and r["week"] == 8
    assert r["warnings"][0].startswith("No Yahoo league and team number")
    assert r["a"]["fit"]["on_roster"] is None
    bad = await tools.compare_players("Rex Runner", "Sam Sprint", league_id="1234567")
    assert bad == {"success": False, "error": "Use a Yahoo league id like 'yahoo:1234567'"}


def test_the_tool_is_registered_in_season_and_full_but_not_offseason_and_is_read_only():
    from nfl_mcp import tool_registry
    names = {t.__name__ for t in tool_registry.get_all_tools("season")}
    assert "compare_players" in names and "compare_players" in {t.__name__ for t in tool_registry.get_all_tools("full")}
    assert "compare_players" not in {t.__name__ for t in tool_registry.get_all_tools("offseason")}
    import inspect

    from nfl_mcp import nflverse_files, player_compare
    for mod in (nflverse_files, player_compare):
        src = inspect.getsource(mod)
        assert "client.post" not in src and ".put(" not in src and ".delete(" not in src
