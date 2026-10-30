"""A matchup page read while games are being played, ESPN's comment beside a stored injury, and body parts that are
really statuses. The live pages are scrubbed copies of pages read on 2026-10-29 during a game (tests/fixtures/yahoo)."""
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nfl_mcp import injury_status, projections, sleeper_tools
from nfl_mcp.yahoo import checkin, compare, parsers, runtime
from nfl_mcp.yahoo.sources import BrowserSource, DirSource

FIX = Path(__file__).parent / "fixtures" / "yahoo"
LG = "yahoo:1234567"
WEEK = 8
NOW = datetime(2026, 10, 29, 22, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _nfl_state(monkeypatch):
    monkeypatch.setitem(sleeper_tools._nfl_state_cache, "state",
                        (time.monotonic(), {"week": WEEK, "season": "2026", "season_type": "regular"}))


@pytest.fixture(scope="module")
def live():
    return parsers.parse_matchup((FIX / "matchup_live.html").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 1. the live matchup page
def test_the_live_layout_is_read_both_lineups_totals_and_the_two_teams(live):
    assert live["layout"] == "live"
    home, away = live["home"], live["away"]
    assert [home["team"]["mid"], away["team"]["mid"]] == ["9", "5"]
    assert (len(home["starters"]), len(home["bench"]), len(away["starters"]), len(away["bench"])) == (9, 7, 9, 7)
    assert (home["total_proj"], home["total_pts"], away["total_proj"], away["total_pts"]) == (133.09, 30.7, 119.6, 2.0)
    assert [p["slot"] for p in home["starters"]] == ["QB", "RB", "RB", "WR", "WR", "TE", "W/R/T", "K", "DEF"]
    cousins, hubbard = home["starters"][0], home["starters"][2]
    assert (cousins["name"], cousins["yahoo_id"], cousins["team"], cousins["position"]) == ("K. Cousins", "25812", "LV", "QB")
    assert cousins["projected"] == 18.42 and cousins["points"] is None and cousins["started"] is False
    assert cousins["game"] == {"kickoff": "Sun 1:00 PM", "home": False, "opponent": "NYJ", "state": "scheduled"}


def test_a_started_player_has_points_and_both_projections_and_the_game_line(live):
    hubbard = live["home"]["starters"][2]
    assert hubbard["name_abbreviated"] and hubbard["name"] == "C. Hubbard" and hubbard["yahoo_id"] == "33514"
    assert hubbard["started"] is True and hubbard["points"] == 16.8
    assert hubbard["projected"] == 16.49 and hubbard["live_projection"] == 25.05
    assert hubbard["game"]["state"] == "live" and hubbard["game"]["opponent"] == "GB" and hubbard["game"]["home"] is False
    assert hubbard["stats"].startswith("37 Rush Yds, 1 Rush TD")
    mcmillan = live["away"]["starters"][3]
    assert (mcmillan["name"], mcmillan["points"], mcmillan["projected"], mcmillan["live_projection"]) == ("T. McMillan", 2.0, 15.8, 9.9)
    # the pre-game projections still add up to Yahoo's own total
    assert round(sum(p["projected"] for p in live["home"]["starters"]), 2) == live["home"]["total_proj"]
    assert round(sum(p["points"] for p in live["home"]["starters"] if p["started"]), 2) == live["home"]["total_pts"]


def test_injury_badges_byes_and_defenses_are_read_in_the_live_layout(live):
    away = {p["name"]: p for p in live["away"]["starters"]}
    assert away["G. Pickens"]["injury"] == "Q" and away["T. Warren"]["injury"] == "Q"
    bench = {p["name"]: p for p in live["home"]["bench"]}
    assert bench["G. Kittle"]["game"]["bye"] is True and bench["J. Tyson"]["injury"] == "IR-R"
    assert bench["G. Kittle"]["slot"] == "BN" and bench["J. Tyson"]["slot"] == "IR"
    defense = live["home"]["starters"][-1]
    assert (defense["name"], defense["team"], defense["position"], defense["name_abbreviated"]) == ("Commanders", "WAS", "DEF", False)


def test_the_classic_layout_still_parses_and_a_finished_week_has_everyone_started():
    m = parsers.parse_matchup((FIX / "matchup.html").read_text(encoding="utf-8"))
    assert m["layout"] == "classic" and len(m["home"]["starters"]) == 9 and all(p["started"] for p in m["home"]["starters"])
    assert m["home"]["starters"][0]["points"] == 15.82 and m["home"]["total_pts"] == 99.52
    assert parsers.parse_matchup("<html><body><table class='Tst-table'><tr><td>x</td></tr></table></body></html>")["layout"] is None


def test_the_opponent_totals_are_points_so_far_plus_projections_for_players_yet_to_play(live):
    opp, warn = checkin._opponent(live, "9", 12.0, 8)
    assert warn == [] and opp["mid"] == "5"
    assert opp["basis"] == "points so far plus Yahoo's projections for players yet to play"
    assert opp["my_projected_points"] == 132.15 and opp["projected_points"] == 105.8        # 30.70 + 101.45 / 2.00 + 103.80
    assert opp["margin"] == 26.35 and opp["read"].startswith("good matchup")
    assert opp["pre_game_projection"] == {"mine": 133.09, "theirs": 119.6}
    assert opp["games_started"] == {"mine": 2, "theirs": 1} and opp["left_out"] is None
    assert [(x["label"], x["points"]) for x in opp["counted_as_played"]["mine"]] == [
        ("C. Hubbard (CAR RB)", 16.8), ("M. Golden (GB WR)", 13.9)]
    assert [x["label"] for x in opp["counted_as_played"]["theirs"]] == ["T. McMillan (CAR WR)"]
    assert opp["counted_as_played"]["mine"][0]["game"] == "live"
    assert opp["points_so_far"] == {"mine": 30.7, "theirs": 2.0}
    assert [x["label"] for x in opp["injured_starters"]] == ["G. Pickens (DAL WR)", "T. Warren (IND TE)"]
    # before anyone has played, Yahoo's own totals are used as before
    fresh = {"home": {"team": {"mid": "9", "name": "x"}, "total_proj": 131.08, "starters": []},
             "away": {"team": {"mid": "5", "name": "y"}, "total_proj": 124.0, "starters": []}}
    pre, _ = checkin._opponent(fresh, "9", 12.0, 8)
    assert pre["basis"] == "Yahoo's pre-game projections" and pre["my_projected_points"] == 131.08 and pre["margin"] == 7.08
    # a finished week is the points scored, not the projections
    done = parsers.parse_matchup((FIX / "matchup.html").read_text(encoding="utf-8"))
    final, _ = checkin._opponent(done, done["home"]["team"]["mid"], 12.0, 8)
    assert final["my_projected_points"] == 99.52 and final["projected_points"] == 120.66 and final["margin"] == -21.14


def test_a_starter_with_no_number_is_left_out_and_named_never_silently_dropped(live):
    for p in live["away"]["starters"][:2]:
        p["projected"] = p["points"] = None
    opp, _ = checkin._opponent(live, "9", 12.0, 8)
    assert opp["left_out"] == {"mine": [], "theirs": ["K. Murray (MIN QB)", "D. Henry (BAL RB)"]}
    assert opp["projected_points"] == round(105.8 - 20.2 - 15.91, 2)


def test_a_page_that_gives_no_opponent_names_the_page_and_does_not_claim_a_bye():
    empty = parsers.parse_matchup("<html><body><table class='Tst-table'><tr><td>Level</td></tr></table></body></html>")
    opp, warn = checkin._opponent(empty, "9", 12.0, 8)
    assert opp is None and len(warn) == 1
    text = warn[0]
    assert "Yahoo matchup page (/matchup?week=8&mid1=9)" in text and "page layout may have changed" in text
    assert "no lineup table could be read" in text and "That is not a bye week" in text
    assert "a bye week," not in text and "or a bye" not in text
    lineups_only = {"home": {"starters": [{"name": "A"}], "team": None}, "away": {"starters": [], "team": None}}
    assert "could not be identified" in checkin._opponent(lineups_only, "9", 12.0, 8)[1][0]
    other = {"home": {"starters": [{"name": "A"}], "team": {"mid": "3", "name": "x"}},
             "away": {"starters": [], "team": {"mid": "4", "name": "y"}}}
    assert "your team was not one of the two teams" in checkin._opponent(other, "9", 12.0, 8)[1][0]


# ------------------------------------------------------------------ 1b. the team totals while games are played
def _starter(label, slot="WR", *, yahoo=None, ours=None, points=None, bye=False, status=None):
    return {"label": label, "slot": slot, "yahoo_projection": yahoo, "our_projection": ours, "started": points is not None,
            "points_so_far": points, "on_bye": bye, "game_status": status,
            "gap": round(ours - yahoo, 1) if ours is not None and yahoo is not None else None}


def test_starter_totals_count_points_so_far_for_started_players_on_both_sides_and_say_which():
    rows = [_starter("Qb (AA QB)", "QB", yahoo=18.0, ours=20.0), _starter("Rb (BB RB)", "RB", points=16.8, status="Half, 10-20"),
            _starter("Wr (CC WR)", "WR", points=13.9), _starter("Te (DD TE)", "TE", yahoo=10.0, ours=9.0)]
    t = compare.starter_totals(rows)
    assert (t["yahoo"], t["ours"], t["gap"]) == (58.7, 59.7, 1.0)                     # 30.7 + 28.0 / 30.7 + 29.0
    assert (t["players_compared"], t["starters"]) == (4, 4)
    assert t["scope"].startswith("all 4 starters") and "Rb (BB RB) 16.8" in t["scope"] and "2 yet to play" in t["scope"]
    assert [x["label"] for x in t["counted_as_played"]] == ["Rb (BB RB)", "Wr (CC WR)"]
    assert t["counted_as_played"][0] == {"label": "Rb (BB RB)", "points": 16.8, "status": "Half, 10-20"}
    assert t["counted_as_projected"] == ["Qb (AA QB)", "Te (DD TE)"] and t["not_compared"] == []
    assert "points so far" in t["basis"]


def test_a_total_over_part_of_the_starters_says_which_are_left_out_and_why():
    rows = [_starter("Qb (AA QB)", "QB", yahoo=18.0, ours=20.0), _starter("Rb (BB RB)", "RB", yahoo=None, ours=9.0),
            _starter("Wr (CC WR)", "WR", ours=None, yahoo=8.0), _starter("Te (DD TE)", "TE", bye=True),
            _starter("K (EE K)", "K", points=0.0)]
    t = compare.starter_totals(rows)
    assert (t["players_compared"], t["starters"]) == (2, 5) and (t["yahoo"], t["ours"]) == (18.0, 20.0)
    assert t["not_compared"] == [{"label": "Rb (BB RB)", "reason": "no Yahoo projection"},
                                 {"label": "Wr (CC WR)", "reason": "no nfl_mcp projection"},
                                 {"label": "Te (DD TE)", "reason": "on bye"}]
    assert t["scope"].startswith("2 of 5 starters; left out of both totals: Rb (BB RB) (no Yahoo projection)")
    assert "Te (DD TE) (on bye)" in t["scope"]


@pytest.fixture
def live_pages(tmp_path, monkeypatch):
    players = {"1": {"yahoo_id": 34007, "full_name": "George Pickens", "team": "DAL", "position": "WR"},
               "2": {"yahoo_id": 41799, "full_name": "Tyler Warren", "team": "IND", "position": "TE"},
               "3": {"yahoo_id": 33514, "full_name": "Chuba Hubbard", "team": "CAR", "position": "RB"}}
    monkeypatch.setitem(sleeper_tools._PLAYERS_CACHE, "data", players)
    for src, dst in (("standings", "standings"), ("settings", "settings"), ("roster_live", "roster_9"),
                     ("matchup_live", "matchup_9_w8")):
        shutil.copy(FIX / f"{src}.html", tmp_path / f"{dst}.html")

    async def project(players, **kw):
        return {"success": True, "week": WEEK, "projections": [
            {"player": p["name"], "player_id": p.get("player_id"), "projected_points": 10.0, "model_projection": 10.0,
             "sleeper_projection": 10.0, "projection_source": "sleeper_blend"} for p in players]}
    monkeypatch.setattr(projections, "project_players", project)
    runtime.set_source(DirSource(tmp_path))
    yield
    runtime.set_source(None)


@pytest.mark.asyncio
async def test_a_check_in_during_a_game_compares_totals_on_the_same_basis_and_lists_who_was_counted(live_pages):
    prov = await runtime.get_provider(LG)
    r = await checkin.build_checkin(prov, LG, "9", WEEK, compare=compare.compare_projections, now=NOW)
    t = r["roster"]["starter_totals"]
    assert (t["players_compared"], t["starters"]) == (9, 9) and t["scope"].startswith("all 9 starters")
    assert (t["yahoo"], t["ours"]) == (132.2, 100.7)                                   # 30.7 + 101.45 / 30.7 + 7 * 10.0
    assert [x["label"] for x in t["counted_as_played"]] == ["Chuba Hubbard (CAR RB)", "Matthew Golden (GB WR)"]
    hubbard = next(p for p in r["roster"]["slots"] if p["name"] == "Chuba Hubbard")
    assert hubbard["started"] and hubbard["points_so_far"] == 16.8 and hubbard["yahoo_projection"] is None
    w = next(x for x in r["warnings"] if x.startswith("Team projection gap"))
    assert "all 9 starters" in w and "Chuba Hubbard (CAR RB) 16.8" in w and "yet to play" in w
    o = r["opponent"]
    assert o is not None and o["mid"] == "5" and o["games_started"] == {"mine": 2, "theirs": 1}
    assert o["my_projected_points"] == 132.15 and o["projected_points"] == 105.8
    assert o["my_total_nfl_mcp"]["total"] == 100.7 and o["my_total_nfl_mcp"]["scope"].startswith("all 9 starters")
    assert [x["label"] for x in o["injured_starters"]] == ["George Pickens (DAL WR)", "Tyler Warren (IND TE)"]   # full names by id
    assert [x["label"] for x in o["counted_as_played"]["mine"]] == ["Chuba Hubbard (CAR RB)", "M. Golden (GB WR)"]
    assert any("partly played" in x for x in r["warnings"])


@pytest.mark.asyncio
async def test_a_check_in_with_an_unreadable_matchup_page_says_so_and_does_not_claim_a_bye(live_pages, tmp_path):
    (tmp_path / "matchup_9_w8.html").write_text("<html><body><table class='Tst-table'><tr><td>x</td></tr></table></body></html>")
    runtime.set_source(DirSource(tmp_path))
    prov = await runtime.get_provider(LG)
    r = await checkin.build_checkin(prov, LG, "9", WEEK, compare=compare.compare_projections, now=NOW)
    assert r["opponent"] is None
    text = next(x for x in r["warnings"] if x.startswith("No opponent could be found"))
    assert "/matchup?week=8&mid1=9" in text and "page layout may have changed" in text and "not a bye" in text


@pytest.mark.asyncio
async def test_the_browser_waits_for_the_script_drawn_lineup_but_only_for_the_matchup_page():
    class Page:
        def __init__(self, fail=False):
            self.fail, self.calls = fail, []

        async def wait_for_function(self, js, timeout):
            self.calls.append((js, timeout))
            if self.fail:
                raise TimeoutError("not drawn")
    src = BrowserSource(cdp_url="http://localhost:9222")
    ok = Page()
    assert await src._await_lineup(ok) is True
    js, timeout = ok.calls[0]
    assert "statTable1" in js and "Fan Pts" in js and timeout == 15000
    assert await src._await_lineup(Page(fail=True)) is False                         # never fails the fetch itself


# ------------------------------------------------------------------ 2. ESPN's comment beside a stored injury
def P(name, nfl, comment, **kw):
    return {"slot": "BN", "name": name, "team": "KC", "position": "WR", "injury": kw.pop("injury", None), "bye_week": 12,
            "nfl_injury_status": nfl, "nfl_injury_source": "report", "practice_status": None, "gameday_status": None,
            "nfl_injury_comment": comment, "nfl_injury_report_date": "2026-10-20T10:00Z", **kw}


@pytest.mark.parametrize("comment,says", [
    ("The team activated Jones from injured reserve on Wednesday.", "activated"),
    ("Smith (knee) was a limited practice participant Thursday.", "practicing"),
    ("Doe (hamstring) practiced fully Wednesday.", "practicing"),
    ("Roe (ankle) is practicing again.", "practicing"),
    ("Poe (foot) returned to practice Monday.", "practicing"),
    ("Smith (knee) did not practice Wednesday.", None),
    ("Doe (knee) won't return to practice this week.", None),
    ("The coach said Parsons has a chance to resume practicing next week.", None),
    ("Jones could be activated Sunday.", None),
    ("The Rams will activate Jones from IR.", None),
    ("He broke his foot in practice last week and will have surgery.", None),
    ("Signed to the practice squad.", None),
    ("", None), (None, None)])
def test_a_comment_that_says_activated_or_practicing_is_recognised_and_negated_or_hypothetical_ones_are_not(comment, says):
    assert injury_status.comment_contradicts_out(comment) == says


def test_the_comment_is_shown_with_the_record_and_an_out_that_says_activated_or_practicing_is_flagged():
    stale = P("Una Back", "Out", "Back (knee) was a limited practice participant Thursday.")
    other = P("Ned Down", "Out", "Down (knee) will miss the rest of the season.")
    q = P("Quin Q", "Questionable", "Q (knee) practiced fully Thursday.")
    v = {r["name"]: r for r in checkin.verify_roster([stale, other, q], 8, {}, now=NOW)["injuries"]}
    assert 'ESPN\'s comment: "Back (knee) was a limited practice participant Thursday."' in v["Una Back"]["statement"]
    assert v["Una Back"]["statement"].endswith("(status may be stale despite its date)")
    assert "status_may_be_stale" in v["Una Back"]["flags"] and v["Una Back"]["comment"].startswith("Back (knee)")
    assert 'ESPN\'s comment: "Down (knee) will miss the rest of the season."' in v["Ned Down"]["statement"]
    assert "stale despite" not in v["Ned Down"]["statement"] and "status_may_be_stale" not in v["Ned Down"]["flags"]
    assert "practiced fully" in v["Quin Q"]["statement"] and "status_may_be_stale" not in v["Quin Q"]["flags"]   # only an Out
    assert any("status may be stale despite its date" in w for w in checkin.verification_warnings(
        {"injuries": [v["Una Back"]], "byes": [], "summary": {"injuries": {}, "byes": {}}}))


def test_a_long_comment_is_cut_in_the_statement_but_kept_whole_in_the_row():
    text = "Back (knee) is expected to miss time. " * 30
    p = P("Una Back", "Out", text)
    assert len(checkin.injury_statement(p, [])) < len(text) and checkin.injury_statement(p, []).count("...") == 1
    assert checkin.verify_roster([p], 8, {}, now=NOW)["injuries"][0]["comment"] == text


def test_an_out_the_comment_contradicts_does_not_price_a_zero_as_a_fact():
    p = P("Una Back", "Out", "The team activated Back from injured reserve.", our_projection=0.0)
    p["injury_flags"] = checkin.injury_flags(p, False, NOW)
    assert "status_may_be_stale" in p["injury_flags"]
    assert "status may be stale" in checkin.projection_unknown(p)


def test_the_stored_comment_comes_from_the_report_but_not_from_a_record_built_from_the_roster_page(tmp_path):
    from nfl_mcp.database import NFLDatabase
    db = NFLDatabase(str(tmp_path / "t.db"))
    base = {"team_id": "KC", "position": "WR", "injury_status": "Out", "severity": 5, "confidence": 90,
            "date_reported": "2026-10-20T10:00Z"}
    db.upsert_injuries([
        {**base, "player_id": "1", "player_name": "Una Back", "injury_type": "Knee", "sources": ["ESPN"],
         "injury_description": "Back (knee) was a limited practice participant Thursday."},
        {**base, "player_id": "2", "player_name": "Ned Down", "injury_type": "unknown", "sources": ["espn_roster"],
         "injury_description": "ESPN's roster page lists Out (entry dated 2026-10-20); it gives no body part or comment"}])
    a = compare._stored_report(db, "Una Back", "KC")
    assert a["comment"].startswith("Back (knee) was a limited") and a["body_part"] == "Knee" and a["body_part_unknown"] is False
    b = compare._stored_report(db, "Ned Down", "KC")
    assert b["comment"] is None and b["body_part"] is None and b["body_part_unknown"] is True


# ------------------------------------------------------------------ 3. a body part that is a status word
@pytest.mark.parametrize("raw,part,unknown", [
    ("Knee", "Knee", False), ("Knee - ACL", "Knee - ACL", False), ("active", None, True), ("Active", None, True),
    ("inactive", None, True), ("Questionable", None, True), ("out", None, True), ("Out", None, True),
    ("Suspension", None, True), ("unknown", None, True), ("  ", None, False), ("", None, False), (None, None, False)])
def test_a_status_word_is_not_a_body_part(raw, part, unknown):
    assert injury_status.body_part(raw) == (part, unknown)


def test_a_status_word_as_body_part_is_ignored_and_the_statement_says_body_part_unknown():
    p = P("Golden", "Questionable", None, nfl_injury_body_part="active", practice_status="DNP", nfl_practice_date="2026-10-27")
    s = checkin.injury_statement(p, [])
    assert s == "nfl_mcp report (dated 2026-10-20) says Questionable, practice DNP (dated 2026-10-27), body part unknown"
    assert "active" not in s
    row = checkin.verify_roster([p], 8, {}, now=NOW)["injuries"][0]
    assert row["body_part"] is None and row["body_part_unknown"] is True and "body part unknown" in row["detail"]
    assert "body part: active" not in row["detail"]
    ok = P("Kneed", "Questionable", None, nfl_injury_body_part="Knee")
    assert "Knee" in checkin.injury_statement(ok, []) and "body part unknown" not in checkin.injury_statement(ok, [])
    none = P("Nopart", "Questionable", None)
    assert "body part" not in checkin.injury_statement(none, [])                    # no value at all: nothing to say


def test_the_stored_body_part_is_cleaned_where_it_is_read(tmp_path):
    from nfl_mcp.database import NFLDatabase
    db = NFLDatabase(str(tmp_path / "t.db"))
    db.upsert_injuries([{"player_id": "1", "player_name": "Una Back", "team_id": "KC", "position": "WR",
                         "injury_status": "Questionable", "injury_type": "active", "severity": 2, "confidence": 65,
                         "sources": ["ESPN"], "date_reported": "2026-10-29T10:00Z"}])
    r = compare._stored_report(db, "Una Back", "KC")
    assert r["body_part"] is None and r["body_part_unknown"] is True


def test_compare_players_reports_a_status_word_body_part_as_unknown():
    from nfl_mcp import player_compare as pc
    p = {"nfl_injury_status": "Questionable", "nfl_injury_source": "report", "nfl_injury_body_part": "Active",
         "nfl_injury_report_date": "2026-10-29T10:00Z", "slot": "BN", "injury": "Q"}
    rec = pc.injury_record(p, "Una Back (KC WR)", NOW, None)
    assert rec["body_part"] is None and rec["body_part_unknown"] is True and "body part unknown" in rec["statement"]


def test_an_abbreviated_name_is_expanded_by_initial_surname_team_and_position_only_when_one_player_fits():
    from nfl_mcp.yahoo.idmap import IdMap
    ids = IdMap({"1": {"full_name": "George Pickens", "team": "DAL", "position": "WR"},
                 "2": {"full_name": "Gerald Pickens", "team": "DAL", "position": "TE"},
                 "3": {"full_name": "Chuba Hubbard", "team": "CAR", "position": "RB"},
                 "4": {"full_name": "Ollie Gordon II", "team": "MIA", "position": "RB"},
                 "5": {"full_name": "Gary Pickens", "team": "DAL", "position": "WR"}})
    assert ids.expand("C. Hubbard", "Car", "RB") == "Chuba Hubbard"
    assert ids.expand("O. Gordon II", "MIA", "RB") == "Ollie Gordon II"
    assert ids.expand("G. Pickens", "DAL", "TE") == "Gerald Pickens"                  # position tells them apart
    assert ids.expand("G. Pickens", "DAL", "WR") is None                              # two fit: no guess
    assert ids.expand("Chuba Hubbard", "CAR", "RB") is None and ids.expand(None, "CAR", "RB") is None
    assert ids.expand("C. Hubbard", "DAL", "RB") is None                              # wrong team
