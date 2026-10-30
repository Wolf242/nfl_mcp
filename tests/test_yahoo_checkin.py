"""fantasy_checkin, the env defaults and the Yahoo block handling. Offline: fixtures and fakes only."""
import asyncio
import shutil
import time
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from nfl_mcp import defaults, injury_service, projections, ros, sleeper_tools, waiver_target_tools, week_context
from nfl_mcp.tools import league as league_tools
from nfl_mcp.tools import waiver as waiver_tools
from nfl_mcp.tools import yahoo as tools
from nfl_mcp.yahoo import checkin, compare, news_social, runtime
from nfl_mcp.yahoo.sources import BrowserSource, DirSource, YahooBlocked

FIX = Path(__file__).parent / "fixtures" / "yahoo"
LG = "yahoo:1234567"
WEEK = 8


@pytest.fixture(autouse=True)
def _nfl_state(monkeypatch):
    """League reads ask for the NFL state; seed it so nothing reaches for the network."""
    monkeypatch.setitem(sleeper_tools._nfl_state_cache, "state",
                        (time.monotonic(), {"week": WEEK, "season": "2026", "season_type": "regular"}))


def S(slot, name=None, pos=None, injury=None, bye=None, empty=False):
    return {"slot": slot, "name": name, "position": pos, "injury": injury, "bye": bye, "empty": empty}


# ------------------------------------------------------------------ pure helpers
def kinds(problems):
    return [(p["type"], p["player"]) for p in problems]


def test_problems_cover_bye_empty_out_doubtful_ir_and_full_bench():
    slots = [
        S("QB", "A QB", "QB", bye=WEEK),                       # starter on bye
        S("RB", "B RB", "RB", injury="O"),                      # starter out
        S("RB", "C RB", "RB", injury="D"),                      # starter doubtful
        S("WR", empty=True),                                    # empty starting slot
        S("W/R/T", "D Flex", "WR", injury="Q"),                 # questionable: not a problem
        S("BN", "E Bench", "RB"), S("BN", "F Bench", "WR"),
        S("IR", "G Healthy", "TE"),                             # no tag in IR
        S("IR", "H Quest", "WR", injury="Q"),                   # Q is not IR-eligible
        S("IR", "I Return", "WR", injury="IR-R"),               # eligible
        S("IR", "J Out", "WR", injury="O"),                     # eligible
    ]
    got = kinds(checkin.find_problems(slots, WEEK))
    assert ("starter_on_bye", "A QB") in got and ("starter_out", "B RB") in got
    assert ("starter_doubtful", "C RB") in got and ("empty_starting_slot", None) in got
    assert ("ir_slot_not_eligible", "G Healthy") in got and ("ir_slot_not_eligible", "H Quest") in got
    assert ("ir_slot_not_eligible", "I Return") not in got and ("ir_slot_not_eligible", "J Out") not in got
    assert not any(p == "D Flex" for _, p in got)
    assert ("bench_full", None) in got


def test_bench_with_an_empty_spot_is_not_full_and_bench_bye_is_not_a_starter_problem():
    slots = [S("BN", "A", "RB", bye=WEEK), S("BN", empty=True)]
    got = kinds(checkin.find_problems(slots, WEEK))
    assert ("bench_full", None) not in got and ("starter_on_bye", "A") not in got
    assert ("empty_slot", None) in got


def test_problems_are_ordered_most_urgent_first():
    order = [p["severity"] for p in checkin.find_problems(
        [S("BN", "A", "RB"), S("RB", "B", "RB", injury="D"), S("QB", "C", "QB", injury="O")], WEEK)]
    assert order == sorted(order, key={"high": 0, "medium": 1, "info": 2}.get)


def test_bye_outlook_counts_active_rbs_and_flags_a_hole():
    slots = [S("RB", "R1", "RB", bye=10), S("RB", "R2", "RB", bye=11), S("BN", "R3", "RB", injury="O"),
             S("WR", "W1", "WR", bye=9), S("IR", "R4", "RB", injury="IR")]
    out = {w["week"]: w for w in checkin.bye_outlook(slots, 8, 4)}
    assert [w for w in out] == [8, 9, 10, 11]
    assert out[8]["active_rbs"] == 2 and out[8]["flag"] is None          # R3 is out, IR is ignored
    assert out[10]["active_rbs"] == 1 and "thin at RB: 1 active for 2 slots to fill" in out[10]["flag"]
    assert out[9]["on_bye"] == [{"name": "W1", "label": "W1 (WR)", "position": "WR", "slot": "WR"}]
    assert out[11]["starters_on_bye"] == 1


def test_wire_positions_follow_problems_and_rb_holes():
    probs = checkin.find_problems([S("QB", "A", "QB", bye=WEEK), S("W/R/T", empty=True)], WEEK)
    assert checkin.wire_positions(probs, []) == ["QB", "RB", "WR", "TE"]
    hole = {"flags": ["thin at RB"], "short_positions": ["RB"], "starters_on_bye": 0, "starters_on_bye_positions": []}
    assert checkin.wire_positions([], [hole]) == ["RB"]
    assert checkin.wire_positions([], [{"flags": [], "starters_on_bye": 1, "starters_on_bye_positions": ["WR"]}]) == []
    many = {"flags": ["2 starters on bye"], "starters_on_bye": 2, "starters_on_bye_positions": ["WR", "TE"]}
    assert checkin.wire_positions([], [many]) == ["WR", "TE"]


# ------------------------------------------------------------------ orchestration on fixtures
@pytest.fixture
def pages(tmp_path, monkeypatch):
    monkeypatch.setitem(sleeper_tools._PLAYERS_CACHE, "data", {})
    for src, dst in (("standings", "standings"), ("settings", "settings"), ("roster", "roster_9"),
                     ("matchup", "matchup_9_w8"), ("players_def", "players_DEF_A"),
                     ("players_rb", "players_RB_A"), ("players_rb", "players_WR_A")):
        shutil.copy(FIX / f"{src}.html", tmp_path / f"{dst}.html")
    for mid in range(1, 11):  # every team's roster, for tools that read the whole league
        shutil.copy(FIX / "roster.html", tmp_path / f"roster_{mid}.html")
    runtime.set_source(DirSource(tmp_path))
    yield
    runtime.set_source(None)


def _row(r, week):
    on_bye = r.get("bye_week") is not None and int(r["bye_week"]) == week
    y = None if on_bye else r["yahoo_projection"]
    mine = None if on_bye or y is None else round(y + 1.0, 1)
    return {"slot": r["slot"], "name": r["name"], "team": r["team"], "position": r["position"],
            "player_id": r.get("player_id"), "injury": r["injury"], "bye_week": r.get("bye_week"),
            "on_bye": on_bye, "yahoo_projection": y, "our_projection": mine, "model_projection": mine,
            "sleeper_projection": mine, "projection_source": "bye" if on_bye else "sleeper_blend",
            "nfl_injury_status": None, "nfl_injury_source": None, "practice_status": None,
            "gameday_status": None, "engine_bye_status": None, "gap": 1.0 if mine is not None else None}


def fake_compare(gap=5.0, players=None):
    async def _cmp(league_id, rows, week):
        return {"week": week, "players": players if players is not None else [_row(r, week) for r in rows],
            "starter_totals": {"yahoo": 120.0, "ours": 120.0 + gap, "gap": gap, "players_compared": 8, "starters": 9},
            "biggest_gaps": [], "warnings": []}
    return _cmp


async def fake_project(players, **kw):
    """Descending projections, so the first player asked about is the best."""
    return {"success": True, "projections": [
        {"player": p["name"], "projected_points": round(9.0 - 0.5 * i, 1), "projection_source": "sleeper_blend"}
        for i, p in enumerate(players)]}


NFL = ["ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX",
       "KC", "LAC", "LAR", "LV", "MIA", "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WSH"]


def week_sched(*bye_teams):
    return {t: "OPP" for t in NFL if t not in bye_teams}


async def fake_schedules(weeks):
    byes = {8: ("SF", "NO", "HOU"), 9: ("PIT",), 10: ("CHI",), 11: ("ATL", "GB", "LAR", "SEA")}
    return {w: week_sched(*byes.get(w, ())) for w in weeks}


async def fake_refresh(teams=None, db=None, **kw):
    return [], {"partial": False, "timed_out_sources": [], "stale": False}


async def fake_targets(**kw):
    return {"success": True, "targets": [{"name": "Wire Guy", "position": "DEF", "team": "PIT",
                                          "projected_points": 7.0, "upgrade_points": 6.7, "verdict": "add",
                                          "waiver_strategy": {"recommendation": "add_now"}},
                                         {"name": "WSH", "position": "DEF", "team": "WSH", "upgrade_points": 5.0,
                                          "waiver_strategy": {"recommendation": "add_now"}}]}


async def fake_posts(handles, hours, limit, watch):
    return {"items": [{"author": "rep", "text": "x", "mentions_watched_player": ["Kirk Cousins"]},
                      {"author": "rep", "text": "y", "mentions_watched_player": []}], "errors": 0}


async def build(week=WEEK, **kw):
    prov = await runtime.get_provider(LG)
    return await checkin.build_checkin(prov, LG, "9", week, compare=kw.pop("compare", fake_compare()),
                                       project=kw.pop("project", fake_project),
                                       targets=kw.pop("targets", fake_targets),
                                       posts=kw.pop("posts", fake_posts),
                                       schedules=kw.pop("schedules", fake_schedules),
                                       today=kw.pop("today", date(2026, 10, 28)), **kw)


@pytest.mark.asyncio
async def test_checkin_returns_every_section_and_says_nothing_changed(pages):
    r = await build()
    assert r["success"] and r["read_only"] and "nothing was added, dropped" in r["note"]
    assert set(r) >= {"league", "roster", "problems", "wire", "opponent", "byes", "news", "warnings"}
    assert r["league"]["scoring_source"] == "yahoo_settings_page" and r["league"]["scoring_diff_from_bfs"] == {}
    assert r["league"]["my_waiver_position"] == 6 and len(r["league"]["waiver_order"]) == 10
    assert [b["week"] for b in r["byes"]] == [8, 9, 10, 11]


@pytest.mark.asyncio
async def test_checkin_flags_the_defense_on_bye_and_reads_the_defense_wire(pages):
    r = await build()
    assert ("starter_on_bye", "Texans") in kinds(r["problems"])
    assert next(p for p in r["problems"] if p["player"] == "Texans")["label"] == "Texans (HOU DEF)"
    assert "bench_full" in [p["type"] for p in r["problems"]]
    # DEF for the Texans' bye now; RB and WR for week 11, when three starters are out
    assert list(r["wire"]["positions"]) == ["DEF", "RB", "WR"]
    tags = {p["tag"] for p in r["wire"]["positions"]["DEF"]}
    assert tags and all(t.startswith("W (") for t in tags)
    assert any(w.startswith("Week 11:") and "3 starters on bye" in w for w in r["warnings"])
    by = {t["name"]: t for t in r["wire"]["targets"]}
    # "Wire Guy" is the PIT defense, which is also the best fill for the Texans' bye hole: one row, not two
    assert "Wire Guy" not in by and by["Steelers"]["yahoo_tag"].startswith("W (")
    # nfl_mcp says add now; Yahoo's tag is a waiver claim, so the check-in says Yahoo's tag wins
    assert any("Wire Guy" in w and "waiver claim" in w for w in r["warnings"])
    # Sleeper writes Washington as WSH, Yahoo as WAS; the tag must still be found, and from outside the top few
    assert by["WSH"]["yahoo_tag"].startswith("W (")
    assert r["wire"]["targets"][0]["source"] == "wire_hole"       # the bye hole's best fill comes first


@pytest.mark.asyncio
async def test_checkin_opponent_news_and_single_source_warnings(pages):
    r = await build()
    assert r["opponent"]["team"].startswith("Team ") and r["opponent"]["projected_points"]
    assert "injured_starters" in r["opponent"] and "starters_on_bye" in r["opponent"]
    assert r["news"]["count"] == 1
    assert r["news"]["items"][0]["single_source"] and r["news"]["items"][0]["status"] == "lead"
    text = " ".join(r["warnings"])
    assert "single-source" in text and "rests on Yahoo alone" in text
    assert r["news"]["independent_origins"] == 1 and r["news"]["origins"] == ["rep"]


@pytest.mark.asyncio
@pytest.mark.parametrize("gap,noise", [(11.9, True), (-11.9, True), (12.0, False), (-25.0, False)])
async def test_team_projection_gap_under_twelve_points_is_noise(pages, gap, noise):
    r = await build(compare=fake_compare(gap))
    assert r["roster"]["gap_is_noise"] is noise
    gap_warning = next(w for w in r["warnings"] if w.startswith("Team projection gap"))
    assert ("noise" in gap_warning) is noise


@pytest.mark.asyncio
async def test_a_failing_sub_call_becomes_a_warning_not_a_failure(pages):
    async def boom(**kw):
        raise RuntimeError("no schedule")

    async def boom_posts(*a):
        raise RuntimeError("bluesky down")
    r = await build(targets=boom, posts=boom_posts)
    # the tool failed, but the Texans' bye hole is still filled from the Yahoo wire
    assert r["success"] and [t["source"] for t in r["wire"]["targets"]] == ["wire_hole"] and r["news"]["count"] == 0
    text = " ".join(r["warnings"])
    assert "get_waiver_targets unavailable: no schedule" in text and "News unavailable: bluesky down" in text


# ------------------------------------------------------------------ the tool and the env defaults
@pytest.fixture
def tool_env(pages, monkeypatch):
    async def week(season, wk, db=None):
        return 2026, wk or WEEK, False

    monkeypatch.setattr(week_context, "resolve_season_week", week)
    monkeypatch.setattr(compare, "compare_projections", fake_compare())
    monkeypatch.setattr(projections, "project_players", fake_project)
    monkeypatch.setattr(ros, "schedules_for", lambda db, season, weeks: fake_schedules(weeks))
    monkeypatch.setattr(injury_service, "get_injury_reports_with_retry", fake_refresh)

    async def no_gap_lookup(db, rows):
        return {"looked_up": 0, "records_added": 0}
    monkeypatch.setattr(injury_service, "lookup_gaps", no_gap_lookup)
    monkeypatch.setattr(waiver_target_tools, "get_waiver_targets", fake_targets)
    monkeypatch.setattr(news_social, "get_insider_posts", fake_posts)


@pytest.mark.asyncio
async def test_tool_uses_the_env_defaults(tool_env, monkeypatch):
    monkeypatch.setenv("NFL_MCP_DEFAULT_LEAGUE", LG)
    monkeypatch.setenv("NFL_MCP_MY_ROSTER", "9")
    r = await tools.fantasy_checkin()
    assert r["success"] and r["league_id"] == LG and r["roster_id"] == "9" and r["week"] == WEEK
    assert r["league"]["my_waiver_position"] == 6


@pytest.mark.asyncio
async def test_explicit_arguments_beat_the_env(tool_env, monkeypatch):
    monkeypatch.setenv("NFL_MCP_DEFAULT_LEAGUE", "yahoo:999")
    monkeypatch.setenv("NFL_MCP_MY_ROSTER", "1")
    r = await tools.fantasy_checkin(week=8, league_id=LG, roster_id="9")
    assert r["success"] and r["league_id"] == LG and r["roster_id"] == "9"


@pytest.mark.asyncio
async def test_neither_argument_nor_env_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("NFL_MCP_DEFAULT_LEAGUE", raising=False)
    monkeypatch.delenv("NFL_MCP_MY_ROSTER", raising=False)
    r = await tools.fantasy_checkin()
    assert r["success"] is False and "NFL_MCP_DEFAULT_LEAGUE" in r["error"]
    monkeypatch.setenv("NFL_MCP_DEFAULT_LEAGUE", LG)
    r = await tools.fantasy_checkin()
    assert r["success"] is False and "NFL_MCP_MY_ROSTER" in r["error"]
    assert (await tools.fantasy_checkin(league_id="1234567", roster_id="9"))["error"].startswith("Use a Yahoo")


@pytest.mark.asyncio
async def test_yahoo_tools_default_the_league_from_the_env(pages, monkeypatch):
    monkeypatch.setenv("NFL_MCP_DEFAULT_LEAGUE", LG)
    monkeypatch.setenv("NFL_MCP_MY_ROSTER", "9")
    order = await tools.get_yahoo_waiver_order()
    assert order["success"] and len(order["order"]) == 10
    assert (await tools.check_yahoo_scoring())["success"]
    plan = await tools.plan_yahoo_waiver_sequence(wishlist=[{"player": "A", "value": 5, "drop": "B"}])
    assert plan["success"] and plan["current_priority"] == 6
    assert (await tools.plan_yahoo_waiver_sequence())["success"] is False  # wishlist is still required


@pytest.mark.asyncio
async def test_league_tools_accept_a_yahoo_id_and_the_env_default(pages, monkeypatch):
    # the shared league_id pattern used to reject "yahoo:<n>" at the MCP layer
    r = await league_tools.get_rosters(LG)
    assert r["success"] and r["count"] == 10
    monkeypatch.setenv("NFL_MCP_DEFAULT_LEAGUE", LG)
    assert (await league_tools.get_rosters())["count"] == 10
    assert (await league_tools.get_league())["success"]
    monkeypatch.delenv("NFL_MCP_DEFAULT_LEAGUE")
    assert "NFL_MCP_DEFAULT_LEAGUE" in (await waiver_tools.get_waiver_targets())["error"]


def test_defaults_precedence_and_user_id(monkeypatch):
    monkeypatch.setenv("NFL_MCP_DEFAULT_LEAGUE", "yahoo:1")
    monkeypatch.setenv("NFL_MCP_MY_ROSTER", "9")
    assert defaults.resolve_league(None) == "yahoo:1" and defaults.resolve_league("123") == "123"
    assert defaults.league_and_roster(None, None) == ("yahoo:1", 9)
    assert defaults.league_and_roster(None, 4) == ("yahoo:1", 4)
    assert defaults.league_and_roster("5", None, user_id="u") == ("5", None)  # user_id identifies the team
    monkeypatch.delenv("NFL_MCP_MY_ROSTER")
    assert defaults.league_and_roster(None, None) == ("yahoo:1", None)


# ------------------------------------------------------------------ Yahoo blocking
class _Page:
    url = "https://football.fantasysports.yahoo.com/f1/1234567"
    calls = 0

    async def goto(self, url, **kw):
        type(self).calls += 1
        return SimpleNamespace(status=999)

    async def content(self):
        return "<html><head></head><body>Request denied </body></html>"

    async def close(self):
        pass


class _Ctx:
    async def new_page(self):
        return _Page()


@pytest.mark.asyncio
async def test_a_999_is_an_error_with_a_cooldown_not_an_empty_page():
    _Page.calls = 0
    src = BrowserSource(min_interval=0)

    async def ctx():
        return _Ctx()
    src._context = ctx
    with pytest.raises(YahooBlocked, match="rate-limiting"):
        await src.fetch("1234567", "standings")
    with pytest.raises(YahooBlocked):  # refused without another request to Yahoo
        await src.fetch("1234567", "settings")
    assert _Page.calls == 1


@pytest.mark.asyncio
async def test_tools_report_a_block_as_an_error_message(monkeypatch):
    monkeypatch.setitem(sleeper_tools._PLAYERS_CACHE, "data", {})
    src = BrowserSource(min_interval=0)

    async def ctx():
        return _Ctx()
    src._context = ctx
    runtime.set_source(src)
    try:
        r = await tools.fantasy_checkin(league_id=LG, roster_id="9")
        assert r["success"] is False and "rate-limiting" in r["error"]
        assert (await tools.get_yahoo_waiver_order(LG))["success"] is False
    finally:
        runtime.set_source(None)


# ------------------------------------------------------------------ bye players, wire ranking, drops, timing
@pytest.mark.asyncio
async def test_bye_players_show_no_projection_and_no_model_only_warning(monkeypatch):
    async def fake(players, **kw):
        return {"success": True, "week": 8, "warnings": [
            "no Sleeper projection for George Kittle, Tyler Shough, Matt Real (+2 more) — model only (x).",
            "no Sleeper projection for George Kittle — model only (x)."],
            "projections": [
                {"player": "George Kittle", "projected_points": 16.3, "model_projection": 16.3,
                 "sleeper_projection": None, "projection_source": "model_only", "injury_status": None},
                {"player": "Matt Real", "projected_points": 7.0, "model_projection": 7.0,
                 "sleeper_projection": None, "projection_source": "model_only", "injury_status": "Questionable",
                 "injury_source": "espn", "practice_status": "Limited"}]}
    monkeypatch.setattr(projections, "project_players", fake)
    rows = [{"slot": "BN", "name": "Tyler Shough", "team": "NO", "position": "QB", "player_id": "3",
             "injury": None, "yahoo_projection": None, "bye_week": 8.0},
            {"slot": "BN", "name": "George Kittle", "team": "SF", "position": "TE", "player_id": "1",
             "injury": None, "yahoo_projection": None, "bye_week": 8.0},
            {"slot": "WR", "name": "Matt Real", "team": "KC", "position": "WR", "player_id": "2",
             "injury": "Q", "yahoo_projection": 7.5, "bye_week": 12.0}]
    r = await compare.compare_projections(LG, rows, 8)
    shough, kittle, real = r["players"]
    assert shough["on_bye"] and shough["our_projection"] is None
    assert kittle["on_bye"] and kittle["projection_source"] == "bye"
    assert kittle["our_projection"] is None and kittle["model_projection"] is None
    assert kittle["sleeper_projection"] is None
    assert real["our_projection"] == 7.0 and real["nfl_injury_status"] == "Questionable"
    assert real["nfl_injury_source"] == "espn" and real["practice_status"] == "Limited"
    # the bye player is dropped from the warning; the player who plays stays in it
    assert r["warnings"] == ["no Sleeper projection for Matt Real — model only (x)."]


@pytest.mark.asyncio
async def test_wire_candidates_are_projected_ranked_and_given_an_upgrade_over_the_bye_hole(pages):
    r = await build()
    de = r["wire"]["positions"]["DEF"]
    proj = [p["our_projection"] for p in de]
    assert proj == sorted(proj, reverse=True) and proj[0] == 9.0 and len(de) == 6
    assert de[0]["upgrade"] == 9.0 and "Texans (HOU DEF) is on bye" in de[0]["upgrade_over"]  # replaces a 0 this week


def _starter(slot, pos, mine, **k):
    return {"slot": slot, "position": pos, "our_projection": mine, "on_bye": False, "injury": None,
            "name": slot + pos, **k}


def test_upgrade_baseline_is_the_weakest_starter_or_zero_for_a_hole():
    weakest = checkin._baseline("RB", [_starter("RB", "RB", 15.0), _starter("W/R/T", "RB", 9.0),
                                       _starter("BN", "RB", 1.0)])
    assert weakest[0] == 9.0
    assert checkin._baseline("RB", [_starter("RB", "RB", 15.0, on_bye=True)])[0] == 0.0
    assert checkin._baseline("RB", [_starter("RB", "RB", 15.0, injury="O")])[0] == 0.0
    assert checkin._baseline("TE", [_starter("RB", "RB", 15.0)])[0] is None


def _bench(name, pos, mine, yh=None, bye=False):
    return {"slot": "BN", "name": name, "position": pos, "team": "X", "our_projection": mine,
            "yahoo_projection": yh, "on_bye": bye, "bye_week": 8 if bye else 12, "injury": None}


def test_drop_candidates_rank_the_bench_cheapest_first_and_keep_bye_players_last():
    got = checkin.drop_candidates([_bench("High", "RB", 15.2), _bench("Low", "WR", 8.1, 10.5),
                                   _bench("Mid", "WR", 10.3), _bench("Away", "QB", None, None, bye=True),
                                   {**_bench("Ir", "WR", 3.0), "slot": "IR"}])
    assert [d["name"] for d in got] == ["Low", "Mid", "High", "Away"]
    assert got[0]["players_at_position"] == 2 and got[-1]["note"].startswith("on bye this week")
    assert got[0]["does_a_job"] is False and "does neither job" in got[0]["note"]


@pytest.mark.parametrize("today,phrase", [(date(2026, 10, 28), "clear today"), (date(2026, 10, 27), "clear tomorrow"),
                                          (date(2026, 10, 25), "clear in 3 days"),
                                          (date(2026, 10, 30), "clear 2 day(s) ago")])
def test_claim_timing_says_when_waiver_claims_clear(today, phrase):
    wire = {"positions": {"DEF": [{"tag": "W (Oct 28)"}, {"tag": "FA"}, {"tag": "W (Oct 28)"}]}}
    notes = checkin.claim_timing(wire, today)
    assert len(notes) == 1 and phrase in notes[0] and "not instant adds" in notes[0]
    assert checkin.claim_timing({"positions": {"RB": [{"tag": "FA"}]}}, today) == []


@pytest.mark.asyncio
async def test_checkin_includes_drop_candidates_and_claim_timing(pages):
    r = await build()
    assert r["drop_candidates"] and all(d["position"] for d in r["drop_candidates"])
    assert any("clear today" in t for t in r["wire"]["timing"])


def test_a_week_with_two_starters_on_bye_is_flagged_even_with_enough_backs():
    slots = [S("RB", "R1", "RB", bye=9), S("WR", "W1", "WR", bye=9), S("BN", "R2", "RB"), S("BN", "R3", "RB"),
             S("TE", "T1", "TE", bye=10)]
    out = {w["week"]: w for w in checkin.bye_outlook(slots, 8, 4)}
    assert out[9]["active_rbs"] == 2
    assert "2 starters on bye (R1 (RB) at RB, W1 (WR) at WR)" in out[9]["flags"]
    assert not any("starters on bye" in f for f in out[10]["flags"]) and out[10]["starters_on_bye"] == 1


# ------------------------------------------------------------------ verification
def P(name, slot="WR", team="KC", injury=None, nfl=None, bye=12, **kw):
    return {"slot": slot, "name": name, "team": team, "position": "WR", "injury": injury, "bye_week": bye,
            "nfl_injury_status": nfl, "nfl_injury_source": "espn" if nfl else None, "practice_status": None,
            "gameday_status": None, **kw}


def test_injury_tags_are_checked_against_nfl_mcp_s_data():
    v = checkin.verify_roster([
        P("Agree", injury="Q", nfl="Questionable", practice_status="Limited", yahoo_practice="Limited",
          nfl_injury_body_part="Hamstring", yahoo_body_part="hamstring"),
        P("Partial", injury="Q", nfl="Questionable", practice_status="Limited"),
        P("Differs", injury="Q", nfl="Questionable", practice_status="DNP", yahoo_practice="Limited",
          nfl_injury_body_part="Knee", yahoo_body_part="Knee"),
        P("Conflict", injury="Q", nfl="Out"),
        P("YahooOnly", injury="Q"),
        P("OtherOnly", nfl="Doubtful"),
        P("Healthy"),
        P("Reserve", slot="IR", injury="IR-R", nfl="Injured Reserve"),
        P("IrNoTag", slot="IR"),
    ], 8, {})
    got = {r["name"]: r["verdict"] for r in v["injuries"]}
    assert got == {"Agree": "agree", "Partial": "partial", "Differs": "partial", "Conflict": "conflict",
                   "YahooOnly": "yahoo_only", "OtherOnly": "other_only", "Reserve": "partial"}
    assert "practice: Limited" in next(r for r in v["injuries"] if r["name"] == "Agree")["detail"]
    assert v["summary"]["injuries"] == {"agree": 1, "partial": 3, "conflict": 1, "yahoo_only": 1, "other_only": 1}
    text = " ".join(checkin.verification_warnings(v))
    assert "Injury tag conflict for Conflict" in text and "OtherOnly (KC WR): nfl_mcp has an injury report" in text
    assert "only Yahoo reports" in text and "YahooOnly" in text and "1 injury tag(s) agree" in text


def test_nfl_injury_wording_maps_to_yahoo_tags():
    words = ("Questionable", "Doubtful", "Out", "IR", "Injured Reserve", "PUP", "Probable", "Healthy", None, "")
    assert [checkin.nfl_tag(x) for x in words] == ["Q", "D", "O", "IR", "IR", "PUP-R", None, None, None, None]


def test_bye_weeks_are_checked_against_the_cached_nfl_schedule():
    sched = {8: week_sched("SF"), 9: week_sched("PIT", "DEN"), 10: None, 11: week_sched()}
    v = checkin.verify_roster([
        P("Right", team="SF", bye=8), P("Unknown", team="CHI", bye=10), P("Wrong", team="KC", bye=11),
        P("Missed", team="DEN", bye=14),  # the schedule says DEN is out in week 9, Yahoo says 14
        P("Later", team="BUF", bye=14),   # outside the four-week window: nothing to check
        P("IrPlayer", slot="IR", team="SF", bye=8),
    ], 8, sched)
    got = {(r["name"], r["week"]): r["verdict"] for r in v["byes"]}
    assert got == {("Right", 8): "agree", ("Unknown", 10): "unverified", ("Wrong", 11): "conflict",
                   ("Missed", 9): "conflict"}
    detail = next(r for r in v["byes"] if r["name"] == "Wrong")["detail"]
    assert "Yahoo has the bye in week 11" in detail and "KC playing OPP" in detail
    text = " ".join(checkin.verification_warnings(v))
    assert "Bye conflict for Wrong" in text and "Unknown" in text and "1 bye week(s) confirmed" in text


@pytest.mark.asyncio
async def test_checkin_verifies_byes_against_the_schedule_and_reports_what_is_unverified(pages):
    r = await build()
    v = r["verification"]
    by = {(b["name"], b["week"]): b["verdict"] for b in v["byes"]}
    assert by[("Texans", 8)] == "agree" and by[("George Kittle", 8)] == "agree"
    assert all(b["label"] for b in v["byes"])
    assert by[("Bijan Robinson", 11)] == "agree"
    assert {i["name"]: i["verdict"] for i in v["injuries"]}["Matthew Golden"] == "yahoo_only"
    assert any("only Yahoo reports" in w and "Matthew Golden" in w for w in r["warnings"])


@pytest.mark.asyncio
async def test_without_a_schedule_byes_are_unverified_not_assumed(pages):
    async def none(weeks):
        return {w: None for w in weeks}
    r = await build(schedules=none)
    assert {b["verdict"] for b in r["verification"]["byes"]} == {"unverified"}
    assert any("none cached" in w for w in r["warnings"])

    async def boom(weeks):
        raise RuntimeError("offline")
    r = await build(schedules=boom)
    assert r["success"] and any("NFL schedule unavailable (offline)" in w for w in r["warnings"])


# ------------------------------------------------------------------ injury refresh, wire reasons, db handle
@pytest.mark.asyncio
async def test_injury_reports_are_refreshed_for_the_rosters_teams_before_projecting(pages):
    order = []

    async def refresh(teams):
        order.append(("refresh", teams))
        return [], {"partial": False}

    async def cmp(league_id, rows, week):
        order.append(("compare", None))
        return await fake_compare()(league_id, rows, week)
    r = await build(refresh_injuries=refresh, compare=cmp)
    assert r["success"] and [o[0] for o in order] == ["refresh", "compare"]
    teams = order[0][1]
    assert teams == sorted(teams) and "SF" in teams and "NO" in teams and "HOU" not in teams  # DEF has no injury feed


@pytest.mark.asyncio
async def test_a_slow_or_failing_injury_refresh_is_a_warning_not_a_failure(pages):
    async def slow(teams):
        return [], {"partial": True}

    async def broken(teams):
        raise RuntimeError("espn down")
    r = await build(refresh_injuries=slow)
    assert any("injury refresh timed out" in w for w in r["warnings"])
    r = await build(refresh_injuries=broken)
    assert r["success"] and any("Could not refresh nfl_mcp's injury reports (espn down)" in w for w in r["warnings"])


@pytest.mark.asyncio
async def test_each_wire_position_says_why_it_is_listed(pages):
    r = await build()
    why = r["wire"]["reasons"]
    assert any("Texans (HOU DEF) starts at DEF but is on bye" in x for x in why["DEF"])
    assert any("week 11" in x and "depth for a later week" in x for x in why["RB"])
    assert set(why) == set(r["wire"]["positions"])


def test_wire_reasons_cover_problems_empty_slots_and_future_holes():
    probs = checkin.find_problems([S("QB", "A", "QB", injury="O"), S("W/R/T", empty=True)], 8)
    hole = {"week": 10, "flags": ["thin at RB"], "short_positions": ["RB"], "eligible_active": {"RB": 1},
            "slots_to_fill": {"RB": 3}, "starters_on_bye": 0, "starters_on_bye_positions": []}
    why = checkin.wire_reasons(probs, [{**hole, "week": 8, "short_positions": []}, hole])
    assert "tagged O" in why["QB"][0] and set(why) == {"QB", "RB", "WR", "TE"}
    assert why["RB"][0].startswith("No player in the FLEX") and any("week 10" in x for x in why["RB"])


@pytest.mark.asyncio
async def test_the_comparison_hands_the_projection_engine_a_database(monkeypatch):
    """Without one the engine skips its injury lookup and its cached-schedule bye detection."""
    seen = {}

    async def fake(players, **kw):
        seen.update(kw)
        return {"success": True, "week": 8, "projections": []}
    monkeypatch.setattr(projections, "project_players", fake)
    rows = [{"slot": "WR", "name": "A", "team": "KC", "position": "WR", "player_id": "1", "injury": None,
             "yahoo_projection": 5.0, "bye_week": 12.0}]
    await compare.compare_projections(LG, rows, 8)
    assert seen["db"] is not None
    sentinel = object()
    await compare.compare_projections(LG, rows, 8, db=sentinel)
    assert seen["db"] is sentinel


# ------------------------------------------------------------------ 1. player identity
def N(slot, name, pos, team, bye=None, injury=None):
    return {"slot": slot, "name": name, "position": pos, "team": team, "bye": bye, "injury": injury, "empty": False}


def test_a_label_always_carries_team_and_position_and_the_key_never_the_surname():
    from nfl_mcp.yahoo.idmap import player_key, player_label
    rb = {"name": "Alex Warren", "team": "PIT", "position": "RB", "player_id": "101"}
    te = {"name": "Sam Warren", "team": "CHI", "position": "TE", "player_id": "202"}
    assert player_label("Alex Warren", "PIT", "RB") == "Alex Warren (PIT RB)"
    assert player_label("Sam Warren", "WSH", "te") == "Sam Warren (WAS TE)"
    assert player_label("Texans", "HOU", "DEF") == "Texans (HOU DEF)"
    assert player_key(rb) != player_key(te) and player_key(rb) == "id:101"
    unmapped = {"name": "Alex Warren", "team": "PIT", "position": "RB", "player_id": None}
    assert player_key(unmapped) != player_key({**unmapped, "team": "CHI"})  # same name, other team: other player


def test_two_surnames_never_collide_in_bye_outlook_problems_or_the_opponent():
    slots = [N("RB", "Alex Warren", "RB", "PIT", bye=9), N("TE", "Sam Warren", "TE", "CHI", bye=10),
             N("WR", "Pat Robinson", "WR", "ATL", bye=11, injury="O")]
    out = {w["week"]: w for w in checkin.bye_outlook(slots, 8, 4)}
    assert out[9]["starters_out"] == ["Alex Warren (PIT RB)"] and out[10]["starters_out"] == ["Sam Warren (CHI TE)"]
    probs = checkin.find_problems(slots, 9)
    assert next(p for p in probs if p["type"] == "starter_on_bye")["label"] == "Alex Warren (PIT RB)"
    assert "Pat Robinson (ATL WR) is tagged O" in next(p for p in probs if p["type"] == "starter_out")["detail"]
    side = {"team": {"mid": "5", "name": "Opp"}, "total_proj": 100.0, "starters": [
        {"name": "Sam Warren", "team": "CHI", "position": "TE", "slot": "TE", "injury": "Q", "empty": False}]}
    me = {"team": {"mid": "9", "name": "Me"}, "total_proj": 90.0, "starters": []}
    opp, _ = checkin._opponent({"home": me, "away": side}, "9")
    assert opp["injured_starters"][0]["label"] == "Sam Warren (CHI TE)"


@pytest.mark.asyncio
async def test_projections_are_matched_by_player_id_when_two_players_share_a_name():
    async def project(players, **kw):
        # the engine answers in a different order than asked, and both answers carry the same name
        return {"success": True, "projections": [
            {"player": "Pat Robinson", "player_id": "B", "projected_points": 4.0, "projection_source": "x"},
            {"player": "Pat Robinson", "player_id": "A", "projected_points": 11.0, "projection_source": "x"}]}
    rows = [{"name": "Pat Robinson", "team": "ATL", "position": "WR", "player_id": "A", "tag": "FA", "injury": None,
             "our_projection": None, "upgrade": None, "projection_source": None, "label": "Pat Robinson (ATL WR)"},
            {"name": "Pat Robinson", "team": "TEN", "position": "WR", "player_id": "B", "tag": "FA", "injury": None,
             "our_projection": None, "upgrade": None, "projection_source": None, "label": "Pat Robinson (TEN WR)"}]
    got = await checkin._project_wire(project, LG, 8, "WR", rows, [])
    assert {r["team"]: r["our_projection"] for r in got} == {"ATL": 11.0, "TEN": 4.0}


@pytest.mark.asyncio
async def test_compare_matches_roster_rows_to_projections_by_id(monkeypatch):
    async def fake(players, **kw):
        return {"success": True, "week": 8, "projections": [
            {"player": "Pat Robinson", "player_id": "2", "projected_points": 4.0, "projection_source": "sleeper_blend"},
            {"player": "Pat Robinson", "player_id": "1", "projected_points": 11.0, "projection_source": "sleeper_blend"}]}
    monkeypatch.setattr(projections, "project_players", fake)
    rows = [{"slot": "WR", "name": "Pat Robinson", "team": "ATL", "position": "WR", "player_id": "1",
             "injury": None, "yahoo_projection": 10.0, "bye_week": 11.0},
            {"slot": "BN", "name": "Pat Robinson", "team": "TEN", "position": "WR", "player_id": "2",
             "injury": None, "yahoo_projection": 5.0, "bye_week": 5.0}]
    r = await compare.compare_projections(LG, rows, 8, db=object())
    assert [p["our_projection"] for p in r["players"]] == [11.0, 4.0]
    assert [p["label"] for p in r["players"]] == ["Pat Robinson (ATL WR)", "Pat Robinson (TEN WR)"]
    assert any("both named Pat Robinson" in w for w in r["warnings"])


@pytest.mark.asyncio
async def test_every_player_in_the_report_is_printed_with_team_and_position(pages):
    r = await build()
    assert all(s["label"].endswith(")") for s in r["roster"]["slots"])
    assert all(b["label"] for b in r["verification"]["byes"]) and all(d["label"] for d in r["drop_candidates"])
    assert all(row["label"].endswith(")") for rows in r["wire"]["positions"].values() for row in rows)
    assert all("(" in x for w in r["byes"] for x in w["starters_out"])


# ------------------------------------------------------------------ 2. drop candidates: the job test
def B(slot, name, pos, mine, bye, team="X", injury=None, on_bye=False, **k):
    return {"slot": slot, "name": name, "position": pos, "team": team, "our_projection": mine, "yahoo_projection": mine,
            "bye_week": bye, "on_bye": on_bye, "injury": injury, "nfl_injury_status": None, "player_id": None, **k}


def test_a_bye_week_bench_player_who_covers_a_starters_bye_is_not_free_to_cut():
    players = [B("TE", "Tia Loveland", "TE", 10.0, 10), B("BN", "Kit Kittle", "TE", None, 8, on_bye=True),
               B("QB", "Cal Cousins", "QB", 20.0, 13), B("BN", "Sam Spare", "RB", None, 8, on_bye=True)]
    got = {d["name"]: d for d in checkin.drop_candidates(players, 8)}
    kittle, shough = got["Kit Kittle"], got["Sam Spare"]
    assert kittle["does_a_job"] and "covers the week 10 bye of Tia Loveland (X TE)" in kittle["note"]
    assert "free to cut" not in kittle["note"] and "costs nothing" not in kittle["note"]
    assert not shough["does_a_job"] and "does neither job" in shough["note"]
    assert [d["name"] for d in checkin.drop_candidates(players, 8)][0] == "Sam Spare"


def test_the_job_test_ranks_by_job_not_by_projection():
    players = [B("RB", "Ann Starter", "RB", 15.0, 11), B("RB", "Bea Starter", "RB", 12.0, 5),
               B("BN", "Cy Insurance", "RB", 3.0, 7),      # low projection, but covers Ann's week 11 bye
               B("BN", "Di Nothing", "RB", 5.0, 11),       # beats Cy on projection; shares Ann's bye, so covers nothing
               B("BN", "Ed Riser", "WR", 2.0, 6, role_trend="role_up")]
    names = [d["name"] for d in checkin.drop_candidates(players, 8)]
    assert names[0] == "Di Nothing"                        # does neither job: the drop
    assert names.index("Cy Insurance") > names.index("Di Nothing")
    row = next(d for d in checkin.drop_candidates(players, 8) if d["name"] == "Ed Riser")
    assert row["path_to_role"] == "his role is rising (role_up)"


def test_a_path_to_a_bigger_role_means_projecting_within_a_tie_of_the_weakest_starter():
    players = [B("WR", "Una Starter", "WR", 12.0, 5), B("BN", "Vic Close", "WR", 10.6, 6),
               B("BN", "Wes Far", "WR", 9.0, 6)]
    got = {d["name"]: d for d in checkin.drop_candidates(players, 8)}
    assert got["Vic Close"]["path_to_role"] and not got["Wes Far"]["path_to_role"]
    assert [d["name"] for d in checkin.drop_candidates(players, 8)][0] == "Wes Far"


def test_flagged_starters_get_their_bench_replacements_compared_with_the_wire_first():
    players = [B("WR", "Una Starter", "WR", 12.0, 11, injury="Q"), B("WR", "Zed Steady", "WR", 14.0, 5),
               B("BN", "Vic Bench", "WR", 10.4, 6), B("BN", "Wes Low", "WR", 8.1, 6)]
    wire = {"positions": {"WR": [{"name": "Xan Wire", "team": "TEN", "position": "WR", "tag": "FA",
                                  "our_projection": 10.6}]}}
    row = checkin.bench_vs_wire(players, wire, 8)[0]
    assert row["starter"] == "Una Starter (X WR)" and row["reason"] == "tagged Q"
    assert row["bench_replacements"] == []               # nobody on the bench projects as high as the starter
    assert row["best_bench"]["player"] == "Vic Bench (X WR)"
    assert row["decision"].startswith("no add needed, use Vic Bench (X WR)")
    wire["positions"]["WR"][0]["our_projection"] = 12.5  # 2.1 above the best bench option: outside the tie
    row = checkin.bench_vs_wire(players, wire, 8)[0]
    assert "worth an add" in row["decision"] and "no add needed" not in row["decision"]
    row = checkin.bench_vs_wire([p for p in players if p["slot"] != "BN"], wire, 8)[0]
    assert row["best_bench"] is None and "no eligible bench replacement" in row["decision"]


def test_a_flagged_starter_on_bye_is_replaced_by_a_bench_player_who_plays():
    players = [B("TE", "Tia Loveland", "TE", None, 8, on_bye=True), B("BN", "Kit Kittle", "TE", 9.0, 11)]
    row = checkin.bench_vs_wire(players, {"positions": {}}, 8)[0]
    assert row["reason"] == "on bye this week" and row["bench_replacements"][0]["player"] == "Kit Kittle (X TE)"
    assert row["decision"] == "no add needed, use Kit Kittle (X TE)"


# ------------------------------------------------------------------ 3. stale, conflicting and unverified injury data
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


def test_injury_flags_cover_stale_conflict_and_yahoo_only():
    fresh = "2026-10-27T12:00:00+00:00"
    old = "2026-10-24T12:00:00+00:00"
    assert checkin.injury_flags({"injury": "Q", "nfl_injury_status": "Questionable",
                                 "nfl_injury_report_date": fresh}, now=NOW) == []
    assert checkin.injury_flags({"injury": "Q", "nfl_injury_status": "Questionable",
                                 "nfl_injury_report_date": old}, now=NOW) == ["stale_injury"]
    assert checkin.injury_flags({"injury": "Q", "nfl_injury_status": "Questionable"}, refresh_stale=True,
                                now=NOW) == ["stale_injury"]
    assert checkin.injury_flags({"injury": "Q", "nfl_injury_status": "Out"}, now=NOW) == ["injury_conflict"]
    assert checkin.injury_flags({"injury": "Q"}, now=NOW) == ["yahoo_only"]
    assert checkin.injury_flags({}, refresh_stale=True, now=NOW) == []   # a healthy player has nothing to flag


def test_a_zero_caused_by_an_unverified_out_is_unknown_not_a_drop_candidate():
    ghost = B("BN", "Gus Ghost", "RB", 0.0, 11, injury="Q", nfl_injury_status="Out", injury_flags=["injury_conflict"])
    solid = B("BN", "Sol Solid", "RB", 6.0, 6)
    cheap = B("BN", "Cho Cheap", "RB", 1.0, 6)
    players = [B("RB", "Ann Starter", "RB", 15.0, 11), ghost, solid, cheap]
    assert "conflict" in checkin.projection_unknown(ghost)
    assert checkin.usable_projection(ghost) is None
    review = checkin.bench_review(players, 8)
    assert [d["name"] for d in review["ranked"]] == ["Cho Cheap", "Sol Solid"]
    assert [u["name"] for u in review["unknown"]] == ["Gus Ghost"]
    assert "injury conflict" in review["unknown"][0]["reason"] and "unknown" in review["unknown"][0]["note"]
    # a confirmed zero (no flags) is a real zero
    sure = {**ghost, "injury_flags": []}
    assert checkin.projection_unknown(sure) is None and checkin.usable_projection(sure) == 0.0
    # and an unknown projection is not the weakest starter a wire player is measured against
    unknown_starter = {**ghost, "slot": "RB"}
    assert checkin._baseline("RB", [unknown_starter, B("RB", "Ann Starter", "RB", 15.0, 11)])[0] == 15.0


@pytest.mark.asyncio
async def test_a_timed_out_refresh_flags_every_injured_player_stale_and_the_zero_unknown(pages):
    async def slow(teams):
        return [], {"partial": True}

    async def cmp(league_id, rws, week):
        players = [_row(r, week) for r in rws]
        for o in players:
            if o["name"] == "Zach Charbonnet":
                o.update(injury="Q", nfl_injury_status="Out", our_projection=0.0)
        return await fake_compare(players=players)(league_id, rws, week)
    r = await build(refresh_injuries=slow, compare=cmp)
    by = {s["name"]: s for s in r["roster"]["slots"]}
    assert "stale_injury" in by["Zach Charbonnet"]["injury_flags"]
    assert by["Zach Charbonnet"]["projection_unknown"]
    assert [u["name"] for u in r["drop_unknown"]] == ["Zach Charbonnet"]
    assert "Zach Charbonnet" not in [d["name"] for d in r["drop_candidates"]]
    assert any("Zach Charbonnet (SEA RB)" in w and "stale" in w for w in r["warnings"])


def test_agree_needs_designation_practice_and_body_part_to_all_match():
    base = P("Pat", injury="Q", nfl="Questionable", practice_status="Limited", nfl_injury_body_part="Knee")
    only_tag = checkin.verify_roster([base], 8, {})["injuries"][0]
    assert only_tag["verdict"] == "partial"
    assert only_tag["fields_missing"] == ["practice (Yahoo)", "body part (Yahoo)"] and only_tag["fields_differ"] == []
    full = {**base, "yahoo_practice": "Limited", "yahoo_body_part": "Knee"}
    assert checkin.verify_roster([full], 8, {})["injuries"][0]["verdict"] == "agree"
    wrong = checkin.verify_roster([{**full, "yahoo_practice": "Full"}], 8, {})["injuries"][0]
    assert wrong["verdict"] == "partial" and wrong["fields_differ"] == ["practice"]
    assert "differs: practice" in wrong["detail"]
    wrong = checkin.verify_roster([{**full, "yahoo_body_part": "Ankle"}], 8, {})["injuries"][0]
    assert wrong["fields_differ"] == ["body part"]
    gone = checkin.verify_roster([{**full, "practice_status": None, "nfl_injury_body_part": None}], 8, {})["injuries"][0]
    assert gone["verdict"] == "partial" and gone["fields_missing"] == ["practice (nfl_mcp)", "body part (nfl_mcp)"]
    text = " ".join(checkin.verification_warnings(checkin.verify_roster([base], 8, {})))
    assert "check is partial" in text and "missing: practice (Yahoo), body part (Yahoo)" in text


def test_news_posts_relayed_by_one_account_are_one_independent_origin():
    items = [{"author": "agg.example", "text": "[Reporter A] x", "mentions_watched_player": ["Pat Robinson"]},
             {"author": "agg.example", "text": "[Reporter B] y", "mentions_watched_player": ["Pat Robinson"]},
             {"author": "other.example", "text": "z", "mentions_watched_player": ["Sam Warren"]}]
    labels = {"Pat Robinson": ["Pat Robinson (ATL WR)"], "Sam Warren": ["Sam Warren (CHI TE)"]}
    out = checkin.news_origins(items, labels)
    assert out["independent_origins"] == 2 and out["origins"] == ["agg.example", "other.example"]
    assert out["by_player"]["Pat Robinson (ATL WR)"] == {"posts": 2, "origins": ["agg.example"],
                                                         "independent_origins": 1}
    assert items[0]["single_source"] and items[0]["relayed_reporter"] == "Reporter A"
    items.append({"author": "third.example", "text": "w", "mentions_watched_player": ["Pat Robinson"]})
    out = checkin.news_origins(items, labels)
    assert out["by_player"]["Pat Robinson (ATL WR)"]["independent_origins"] == 2 and not items[0]["single_source"]


@pytest.mark.asyncio
async def test_the_news_warning_counts_origins_not_posts(pages):
    async def posts(handles, hours, limit, watch):
        return {"items": [{"author": "agg.example", "text": "a", "mentions_watched_player": ["Kirk Cousins"]},
                          {"author": "agg.example", "text": "b", "mentions_watched_player": ["Kirk Cousins"]}],
                "errors": 0}
    r = await build(posts=posts)
    assert r["news"]["count"] == 2 and r["news"]["independent_origins"] == 1
    assert any("2 post(s) from 1 independent origin(s)" in w and "single-source" in w for w in r["warnings"])


# ------------------------------------------------------------------ 4. waiver targets joined to Yahoo's free agents
def _yrow(name, team, pos, pid, tag):
    return {"name": name, "label": f"{name} ({team} {pos})", "team": team, "position": pos, "player_id": pid, "tag": tag}


def test_targets_are_kept_only_when_yahoo_lists_them_and_carry_yahoos_tag():
    index = {"WR": [_yrow("Ann Receiver", "ATL", "WR", "11", "FA"), _yrow("Bo Receiver", "TEN", "WR", "12", "W (Oct 31)"),
                    _yrow("Cy Receiver", "KC", "WR", None, "FA")],
             "DEF": [_yrow("Steelers", "PIT", "DEF", "PIT", "W (Oct 28)")]}
    raw = [{"name": "Ann Receiver", "team": "ATL", "position": "WR", "player_id": "11",
            "waiver_recommendation": "add_now"},
           {"name": "Bo R.", "team": "TEN", "position": "WR", "player_id": "12", "waiver_recommendation": "add_now"},
           {"name": "Cy Receiver", "team": "KC", "position": "WR", "player_id": "13"},   # no id on Yahoo's row: name+team+pos
           {"name": "Dee Receiver", "team": "DAL", "position": "WR", "player_id": "14"},  # Sleeper-only
           {"name": "Wire Guy", "team": "PIT", "position": "DEF", "player_id": None},     # a defense joins on its team
           {"name": "Ann Receiver", "team": "BUF", "position": "WR", "player_id": "99"}]  # same name, other team and id
    warnings: list[str] = []
    kept, dropped = checkin._join_targets(raw, index, warnings)
    assert [(t["name"], t["yahoo_tag"]) for t in kept] == [("Ann Receiver", "FA"), ("Bo R.", "W (Oct 31)"),
                                                           ("Cy Receiver", "FA"), ("Wire Guy", "W (Oct 28)")]
    assert dropped == ["Dee Receiver (DAL WR)", "Ann Receiver (BUF WR)"]
    assert any("Dee Receiver (DAL WR)" in w and "could not be matched to a Yahoo free agent" in w for w in warnings)
    assert any("Bo R. (TEN WR)" in w and "waiver claim" in w for w in warnings)   # Yahoo's W beats "add_now"
    assert not any("Ann Receiver (ATL WR)" in w for w in warnings)


@pytest.mark.asyncio
async def test_checkin_drops_targets_yahoo_does_not_list_and_says_so(pages):
    first = await build(targets=None)
    have = first["wire"]["positions"]["DEF"][0]

    async def partial(**kw):
        assert kw["positions"] == ["DEF", "RB", "WR"] and kw["limit"] == checkin.TARGET_POOL
        return {"success": True, "targets": [
            {"name": have["name"], "position": "DEF", "team": have["team"], "player_id": have["player_id"],
             "upgrade_points": 5.0, "waiver_strategy": {"recommendation": "claim_now"}},
            {"name": "Ghost Pool", "position": "DEF", "team": "ZZZ", "player_id": "ZZZ", "upgrade_points": 9.0}]}
    r = await build(targets=partial)
    assert [t["player_id"] for t in r["wire"]["targets"]] == [have["player_id"]]
    assert r["wire"]["targets"][0]["yahoo_tag"] == have["tag"]
    assert r["wire"]["targets_dropped"] == ["Ghost Pool (ZZZ DEF)"]
    assert any("Ghost Pool (ZZZ DEF)" in w and "dropped from targets" in w for w in r["warnings"])


@pytest.mark.asyncio
async def test_an_empty_target_list_is_explained_not_silent(pages, monkeypatch):
    monkeypatch.setattr(checkin, "starter_holes", lambda slots, players: [])   # no empty, bye or out slot
    async def nothing(**kw):
        return {"success": True, "targets": [],
                "message": "Nothing on waivers beats your current starters in week 8 (212 free agents checked)."}
    r = await build(targets=nothing)
    assert r["wire"]["targets"] == [] and "212 free agents checked" in r["wire"]["targets_note"]

    async def unmappable(**kw):
        return {"success": True, "targets": [{"name": "Ghost", "position": "DEF", "team": "ZZZ", "player_id": "ZZZ"}],
                "message": "1 worth a claim"}
    r = await build(targets=unmappable)
    assert r["wire"]["targets"] == [] and "could not be matched to Yahoo free agents" in r["wire"]["targets_note"]

    async def failed(**kw):
        return {"success": False, "error": "no cached schedule"}
    r = await build(targets=failed)
    assert "no cached schedule" in r["wire"]["targets_note"]
    assert any("get_waiver_targets failed" in w for w in r["warnings"])


# ------------------------------------------------------------------ 5. bye planning
def test_slots_to_fill_lists_each_positions_own_slots_and_the_flex_once():
    slots = [S("QB"), S("RB"), S("RB"), S("WR"), S("WR"), S("TE"), S("W/R/T"), S("K"), S("DEF"), S("BN"), S("IR")]
    need = checkin.slots_to_fill(slots)
    assert need == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1, "FLEX": 1}
    assert sum(need.values()) == 9                    # the nine starters, the flex counted once
    assert checkin.own_slots(slots) == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}


def test_thin_means_fewer_active_players_than_the_positions_own_slots_and_never_counts_the_flex():
    slots = [S("QB", "Q1", "QB"), S("RB", "R1", "RB", bye=9), S("RB", "R2", "RB"), S("W/R/T", "R3", "RB"),
             S("WR", "W1", "WR"), S("WR", "W2", "WR"), S("TE", "T1", "TE"), S("BN", "R4", "RB", bye=9),
             S("BN", "W3", "WR"), S("BN", "T2", "TE")]
    out = {w["week"]: w for w in checkin.bye_outlook(slots, 8, 4)}
    assert out[8]["eligible_active"]["RB"] == 4 and out[8]["thin_positions"] == []
    wk9 = out[9]
    assert wk9["eligible_active"]["RB"] == 2 and wk9["slots_to_fill"]["RB"] == 2    # two backs fill the two RB slots
    assert wk9["thin_positions"] == [] and wk9["short_positions"] == [] and wk9["flags"] == []
    assert wk9["flex"]["line"] == "flex: 2 eligible players left across RB/WR/TE"  # RB 0 + WR 1 + TE 1 beyond own slots
    assert wk9["starters_out"] == ["R1 (RB)"] and wk9["eligible_active"]["WR"] == 3


def test_every_week_names_the_starters_out_and_the_eligible_players_left():
    slots = [S("RB", "Ann Star", "RB", bye=9), S("RB", "Bo Star", "RB", bye=9), S("W/R/T", "Cy Flex", "WR", bye=10),
             S("BN", "Di Bench", "RB")]
    out = {w["week"]: w for w in checkin.bye_outlook(slots, 8, 4)}
    assert out[9]["starters_out"] == ["Ann Star (RB)", "Bo Star (RB)"]
    assert out[9]["eligible_active"]["RB"] == 1 and "RB" in out[9]["short_positions"]
    assert out[10]["starters_out"] == ["Cy Flex (WR)"] and out[8]["starters_out"] == []
    assert checkin.wire_positions([], [out[9]]) == ["RB"]


# ------------------------------------------------------------------ matchup wording and the noise band
@pytest.mark.parametrize("margin,says", [(13.5, "good matchup"), (10.5, "inside the noise band"),
                                         (-10.5, "inside the noise band"), (-13.5, "tough matchup"),
                                         (12.53, "at the edge of the noise band"),
                                         (11.99, "at the edge of the noise band"),
                                         (11.0, "at the edge of the noise band"),
                                         (13.0, "at the edge of the noise band"),
                                         (-12.0, "at the edge of the noise band"),
                                         (12.0, "at the edge of the noise band")])
def test_the_margin_is_only_good_beyond_the_edge_of_the_noise_band(margin, says):
    text = checkin.matchup_read(margin, 12.0)
    assert text.startswith(says)
    assert ("good matchup" in text) is (says == "good matchup")
    assert ("toss-up" in text) is (says == "inside the noise band")      # the edge is not a toss-up either


def test_the_opponent_block_carries_the_margin_and_its_reading():
    me = {"team": {"mid": "9", "name": "Me"}, "total_proj": 131.08, "starters": []}
    them = {"team": {"mid": "5", "name": "Opp"}, "total_proj": 124.0, "starters": []}
    opp, _ = checkin._opponent({"home": me, "away": them}, "9")
    assert opp["margin"] == 7.08 and opp["read"].startswith("inside the noise band")
    assert "good matchup" not in opp["read"]
    them["total_proj"] = 118.55                               # a margin of 12.53: within a point of the band
    opp, _ = checkin._opponent({"home": me, "away": them}, "9")
    assert opp["read"].startswith("at the edge of the noise band") and "good matchup" not in opp["read"]
    them["total_proj"] = 115.0                                # 16.08: clearly outside
    opp, _ = checkin._opponent({"home": me, "away": them}, "9")
    assert opp["read"].startswith("good matchup")


@pytest.mark.parametrize("err,band", [(None, 12.0), (30.0, 15.0), (8.0, 10.0), (24.0, 12.0)])
def test_noise_band_is_half_the_running_team_error_with_a_floor_of_ten(err, band):
    assert checkin.noise_band_points(err) == band


@pytest.mark.asyncio
async def test_the_running_team_error_sets_the_band_for_the_gap_and_the_matchup(pages):
    r = await build(compare=fake_compare(14.0), running_team_error=40.0)       # band 20: a 14 point gap is noise
    assert r["roster"]["gap_is_noise"] and r["roster"]["noise_band"] == 20.0
    assert r["opponent"]["noise_band"] == 20.0


# ------------------------------------------------------------------ follow-up 1: holes, K and DEF
@pytest.mark.asyncio
async def test_a_def_on_bye_with_no_other_def_gets_the_best_available_def_as_a_target(pages):
    async def silent(**kw):
        return {"success": True, "targets": [],
                "message": "Nothing on waivers beats your current starters in week 8 (212 free agents checked)."}
    r = await build(targets=silent)
    assert [h["kind"] for h in r["wire"]["holes"]] == ["on bye"]
    assert not [s for s in r["roster"]["slots"] if s["position"] == "DEF" and s["slot"] == "BN"]   # no other DEF
    best = r["wire"]["positions"]["DEF"][0]
    t = r["wire"]["targets"][0]
    assert t["position"] == "DEF" and t["player_id"] == best["player_id"] and t["name"] == best["name"]
    assert t["tag"] == t["yahoo_tag"] == best["tag"] and t["tag"].startswith("W (")
    assert t["projected_points"] == best["our_projection"] and t["upgrade_points"] == best["our_projection"]
    assert t["verdict"] == "fills_hole" and "Texans (HOU DEF) is on bye" in t["fills"]
    note = r["wire"]["targets_note"]
    assert "beats your current starters" not in note and "Texans (HOU DEF) is on bye" in note


@pytest.mark.asyncio
async def test_an_empty_k_slot_reads_the_k_wire_and_targets_the_best_kicker(pages, monkeypatch):
    prov = await runtime.get_provider(LG)
    real_roster, real_players = prov.data.roster, prov.data.players

    async def roster(mid):
        return [{**sl, "name": None, "team": None, "position": None, "injury": None, "yahoo_id": None,
                 "projected": None, "bye": None, "game": None, "empty": True} if sl["slot"] == "K" else sl
                for sl in await real_roster(mid)]
    kickers = [{"yahoo_id": None, "name": n, "team": tm, "position": "K", "injury": None, "game": None,
                "availability": {"state": "FA", "clears": None, "owner": None}}
               for n, tm in (("Kai Kicker", "BUF"), ("Lee Leg", "DAL"), ("Moe Boot", "MIA"))]

    async def players(pos, status):
        return kickers if pos == "K" else await real_players(pos, status)
    monkeypatch.setattr(prov.data, "roster", roster)
    monkeypatch.setattr(prov.data, "players", players)
    r = await build()
    assert "K" in r["wire"]["positions"] and "K" in r["wire"]["reasons"]
    assert any(p["type"] == "empty_starting_slot" and p["slot"] == "K" for p in r["problems"])
    assert {h["kind"] for h in r["wire"]["holes"]} == {"empty", "on bye"}
    k = next(t for t in r["wire"]["targets"] if t["position"] == "K")
    assert k["name"] == "Kai Kicker" and k["tag"] == "FA" and k["projected_points"] == 9.0   # first = best projected
    assert "No player in the K starting slot" in k["fills"]
    assert k["upgrade_points"] == 9.0                                 # an empty slot is worth the whole projection
    assert next(w for w in r["wire"]["positions"]["K"])["upgrade_over"] == "No player in the K starting slot"
    assert "beats your current starters" not in r["wire"]["targets_note"]


def test_a_hole_is_a_zero_baseline_whether_the_starter_is_on_bye_out_or_missing():
    starters = [_starter("K", "K", 8.0), _starter("RB", "RB", 15.0)]
    empty = [{"slot": "K", "kind": "empty", "positions": ["K"], "reason": "No player in the K starting slot",
              "player": None}]
    assert checkin._baseline("K", starters, empty) == (0.0, "No player in the K starting slot")
    assert checkin._baseline("RB", starters, empty)[0] == 15.0                 # a K hole says nothing about RB
    flex = [{"slot": "FLEX", "kind": "empty", "positions": ["RB", "WR", "TE"], "reason": "x", "player": None}]
    assert all(checkin._baseline(p, starters, flex)[0] == 0.0 for p in ("RB", "WR", "TE"))
    holes = checkin.starter_holes(
        [S("K", empty=True), S("W/R/T", empty=True)],
        [{**_starter("RB", "RB", 15.0), "on_bye": True, "slot": "RB", "name": "Ann"},
         {**_starter("WR", "WR", 9.0), "injury": "O", "slot": "WR", "name": "Bo"},
         {**_starter("BN", "RB", 1.0), "on_bye": True, "slot": "BN", "name": "Bench"},
         {**_starter("QB", "QB", 0.0), "slot": "QB", "name": "Cy", "nfl_injury_status": "Out",
          "injury_flags": ["injury_conflict"]}])
    assert [(h["slot"], h["kind"]) for h in holes] == [("K", "empty"), ("FLEX", "empty"), ("RB", "on bye"), ("WR", "out")]
    assert holes[1]["positions"] == ["RB", "WR", "TE"] and holes[2]["reason"] == "Ann (RB) is on bye"


def test_holes_are_filled_by_the_best_wire_player_who_can_play_the_slot_once_each():
    wire = {"positions": {"WR": [
        {"name": "Rex Runner", "label": "Rex Runner (TEN RB)", "team": "TEN", "position": "RB", "player_id": "1",
         "tag": "FA", "our_projection": 12.0},          # read on the WR page, but he is an RB
        {"name": "Wes Wide", "label": "Wes Wide (KC WR)", "team": "KC", "position": "WR", "player_id": "2",
         "tag": "W (Oct 31)", "our_projection": 9.0}]}}
    holes = [{"slot": "WR", "kind": "out", "positions": ["WR"], "reason": "Ann (WR) is out", "player": "Ann (WR)"},
             {"slot": "FLEX", "kind": "empty", "positions": ["RB", "WR", "TE"], "reason": "flex empty", "player": None},
             {"slot": "K", "kind": "empty", "positions": ["K"], "reason": "k empty", "player": None}]
    got, notes = checkin.hole_targets(holes, wire)
    assert [t["name"] for t in got] == ["Wes Wide", "Rex Runner"]                # the WR slot cannot take the RB
    assert [t["tag"] for t in got] == ["W (Oct 31)", "FA"]
    assert notes == ["no Yahoo free agent was read for the K hole (k empty)"]


# ------------------------------------------------------------------ follow-up 2: thin flags
def test_te_and_wr_are_not_thin_because_of_the_flex_and_the_flex_is_its_own_line():
    slots = [S("QB", "Q1", "QB"), S("RB", "R1", "RB"), S("RB", "R2", "RB"), S("WR", "W1", "WR"), S("WR", "W2", "WR"),
             S("TE", "T1", "TE"), S("W/R/T", "R3", "RB"), S("K", "K1", "K"), S("DEF", "D1", "DEF")]
    out = {w["week"]: w for w in checkin.bye_outlook(slots, 8, 4)}
    assert out[8]["thin_positions"] == [] and out[8]["flex"]["eligible_left"] == 1      # R3 is the only extra body
    assert out[8]["flex"]["line"] == "flex: 1 eligible players left across RB/WR/TE"
    assert sum(out[8]["slots_to_fill"].values()) == 9
    slots2 = slots + [S("BN", "T2", "TE", bye=9)]
    wk9 = {w["week"]: w for w in checkin.bye_outlook(slots2, 8, 4)}[9]
    assert wk9["eligible_active"]["TE"] == 1 and "TE" not in wk9["thin_positions"]      # TE 1 slot, 1 body: not thin
    assert wk9["eligible_active"]["WR"] == 2 and "WR" not in wk9["thin_positions"]
    assert wk9["flags"] == []


def test_a_position_below_its_own_slot_count_is_thin_and_a_short_flex_is_a_separate_flag():
    slots = [S("QB", "Q1", "QB"), S("RB", "R1", "RB"), S("RB", "R2", "RB", bye=9), S("WR", "W1", "WR"),
             S("WR", "W2", "WR"), S("TE", "T1", "TE", bye=9), S("W/R/T", "R3", "RB"), S("K", "K1", "K"),
             S("DEF", "D1", "DEF")]
    wk9 = {w["week"]: w for w in checkin.bye_outlook(slots, 8, 4)}[9]
    assert wk9["thin_positions"] == ["TE"] and wk9["short_positions"] == ["TE"]      # RB: R1 + R3 still fill 2 slots
    assert "thin at TE: 0 active for 1 slots to fill" in wk9["flags"]
    assert wk9["flex"]["eligible_left"] == 0
    assert "flex: 0 eligible players left across RB/WR/TE, for 1 flex slot(s)" in wk9["flags"]
    assert not any(f.startswith("thin at RB") or f.startswith("thin at WR") for f in wk9["flags"])
    assert checkin.wire_positions([], [wk9]) == ["RB", "TE"]   # two starters on bye (RB, TE); TE is also short


# ------------------------------------------------------------------ follow-up 3: vs_bench eligibility and wording
def _wr(name, pos, mine, tag="FA"):
    return {"name": name, "label": f"{name} (X {pos})", "team": "X", "position": pos, "tag": tag, "our_projection": mine}


def test_a_wr_slot_is_replaced_by_wr_only_even_when_the_best_wire_player_is_an_rb():
    players = [B("WR", "Una Starter", "WR", 12.0, 11, injury="Q"), B("WR", "Zed Steady", "WR", 14.0, 5),
               B("BN", "Vic Bench", "WR", 10.4, 6), B("BN", "Rob Back", "RB", 13.0, 6)]
    wire = {"positions": {"WR": [_wr("Rex Runner", "RB", 14.0), _wr("Wes Wide", "WR", 10.6)]}}
    row = checkin.bench_vs_wire(players, wire, 8)[0]
    assert row["slot"] == "WR" and row["eligible_positions"] == ["WR"]
    assert row["best_bench"]["player"] == "Vic Bench (X WR)"                    # the 13.0 RB is not eligible
    assert row["best_wire"]["player"] == "Wes Wide (X WR)"                      # nor the 14.0 RB on the wire
    assert row["decision"].startswith("no add needed, use Vic Bench (X WR)") and "Rex Runner" not in str(row)
    only_rb = checkin.bench_vs_wire(players, {"positions": {"WR": [_wr("Rex Runner", "RB", 14.0)]}}, 8)[0]
    assert only_rb["best_wire"] is None and only_rb["decision"] == "no add needed, use Vic Bench (X WR)"


def test_the_flex_slot_takes_an_rb_wr_or_te_from_the_bench_and_the_wire():
    players = [B("W/R/T", "Fay Flex", "WR", 9.0, 11, injury="Q"), B("WR", "Zed Steady", "WR", 14.0, 5),
               B("BN", "Vic Bench", "WR", 6.0, 6), B("BN", "Rob Back", "RB", 10.5, 6), B("BN", "Tim Tight", "TE", 7.0, 6)]
    wire = {"positions": {"RB": [_wr("Rex Runner", "RB", 11.0)], "WR": [_wr("Wes Wide", "WR", 8.0)]}}
    row = checkin.bench_vs_wire(players, wire, 8)[0]
    assert row["slot"] == "FLEX" and row["eligible_positions"] == ["RB", "WR", "TE"]
    assert row["best_bench"]["player"] == "Rob Back (X RB)" and row["best_wire"]["player"] == "Rex Runner (X RB)"
    assert row["gap"] == 0.5 and row["decision"].startswith("no add needed, use Rob Back (X RB)")
    assert "a tie inside the 1.5-point tie band" in row["decision"]
    assert [b["player"] for b in row["bench_replacements"]] == ["Rob Back (X RB)"]   # 10.5 >= the 9.0 starter


def test_a_worse_wire_player_is_said_to_be_worse_by_more_than_the_tie_band():
    players = [B("WR", "Una Starter", "WR", 12.0, 11, injury="Q"), B("BN", "Vic Bench", "WR", 10.4, 6)]
    row = checkin.bench_vs_wire(players, {"positions": {"WR": [_wr("Wes Wide", "WR", 7.8)]}}, 8)[0]
    assert row["gap"] == -2.6
    assert ("worse than Vic Bench (X WR) by 2.6 (more than the 1.5-point tie band)" in row["decision"])
    assert "under the" not in row["decision"] and row["decision"].startswith("no add needed, use Vic Bench (X WR)")
    row = checkin.bench_vs_wire(players, {"positions": {"WR": [_wr("Wes Wide", "WR", 9.6)]}}, 8)[0]
    assert "worse than" not in row["decision"] and "a tie inside the 1.5-point tie band" in row["decision"]


# ------------------------------------------------------------------ 1. the injury refresh: why it timed out, and one retry
@pytest.fixture
def clean_inflight():
    from nfl_mcp import upstream
    upstream.clear_inflight()
    yield
    upstream.clear_inflight()


@pytest.mark.asyncio
async def test_a_slow_crawl_gets_one_retry_with_a_longer_wait_on_the_same_read(monkeypatch, clean_inflight):
    calls = []

    async def slow(teams=None, db=None, use_cache=True):
        calls.append(teams)
        await asyncio.sleep(0.15)
        return [{"player_name": "A"}]
    monkeypatch.setattr(injury_service, "get_injury_reports", slow)
    rows, meta = await injury_service.get_injury_reports_with_retry(["SF"], None, wait=0.02, retry_wait=1.0)
    assert rows == [{"player_name": "A"}] and meta["partial"] is False and meta["error"] is None
    assert [a["outcome"] for a in meta["attempts"]] == ["timed out", "ok"]
    assert [a["wait_seconds"] for a in meta["attempts"]] == [0.02, 1.0]
    assert len(calls) == 1                       # one crawl, waited on twice: the retry did not start another


@pytest.mark.asyncio
async def test_a_crawl_that_outlasts_both_waits_reports_each_wait_and_keeps_running(monkeypatch, clean_inflight):
    async def very_slow(teams=None, db=None, use_cache=True):
        await asyncio.sleep(0.4)
        return []
    monkeypatch.setattr(injury_service, "get_injury_reports", very_slow)
    rows, meta = await injury_service.get_injury_reports_with_retry(["SF", "GB"], None, wait=0.01, retry_wait=0.05)
    assert meta["partial"] and rows == [] and meta["timed_out_sources"] == [injury_service.SOURCE_INJURY_CRAWL]
    assert [a["outcome"] for a in meta["attempts"]] == ["timed out", "timed out"]
    assert meta["error"] == "TimeoutError after 0.05 s" and meta["teams"] == 2
    assert meta["background_running"] is True and meta["waited_seconds"] >= 0.055
    await asyncio.sleep(0.5)                     # let the background read finish before the loop closes


@pytest.mark.asyncio
async def test_a_failing_crawl_is_retried_once_and_the_real_error_is_kept(monkeypatch, clean_inflight):
    seen = []

    async def boom(teams=None, db=None, use_cache=True):
        seen.append(1)
        raise RuntimeError("espn 503")
    monkeypatch.setattr(injury_service, "get_injury_reports", boom)
    rows, meta = await injury_service.get_injury_reports_with_retry(["SF"], None, wait=0.05, retry_wait=0.05)
    assert len(seen) == 2 and [a["outcome"] for a in meta["attempts"]] == ["error", "error"]
    assert meta["error"] == "RuntimeError: espn 503" and meta["partial"]
    assert injury_service.INJURY_RETRY_WAIT_SECONDS > injury_service.INJURY_CRAWL_WAIT_SECONDS


@pytest.mark.asyncio
async def test_the_refresh_warning_names_each_try_the_waits_and_the_real_error(pages):
    async def refresh(teams):
        return [], {"partial": True, "waited_seconds": 36.1, "teams": 13, "background_running": True,
                    "error": "ConnectError: all connection attempts failed",
                    "attempts": [{"wait_seconds": 6.0, "outcome": "timed out", "error": "TimeoutError after 6 s"},
                                 {"wait_seconds": 30.0, "outcome": "error",
                                  "error": "ConnectError: all connection attempts failed"}]}
    r = await build(refresh_injuries=refresh)
    w = next(x for x in r["warnings"] if "injury refresh timed out" in x)
    assert "Try 1: waited 6 s, timed out (TimeoutError after 6 s)" in w
    assert "Try 2: waited 30 s, error (ConnectError: all connection attempts failed)" in w
    assert "36.1 s in all, 13 teams" in w and "still running in the background" in w


# ------------------------------------------------------------------ 2. report dates and staleness
WEEK_START = date(2026, 10, 27)   # a Tuesday


def rec(**k):
    return {"injury": "Q", "nfl_injury_status": "Questionable", "practice_status": "DNP",
            "nfl_injury_body_part": "Shoulder", **k}


def test_a_record_is_stale_past_three_days_or_from_before_this_nfl_week_even_when_the_refresh_worked():
    assert checkin.stale_reasons(rec(nfl_injury_report_date="2026-10-28T09:00:00+00:00"), now=NOW,
                                 week_start=WEEK_START) == []
    old = checkin.stale_reasons(rec(nfl_injury_report_date="2026-10-24T12:00:00+00:00"), now=NOW,
                                week_start=WEEK_START)
    assert len(old) == 2 and "4 days old (limit 3)" in old[0] and "before this NFL week started (2026-10-27)" in old[1]
    monday = rec(nfl_injury_report_date="2026-10-26T18:00:00+00:00")        # under 3 days old, but last week's
    assert checkin.stale_reasons(monday, now=NOW, week_start=WEEK_START) == [
        "dated 2026-10-26, before this NFL week started (2026-10-27)"]
    assert "stale_injury" in checkin.injury_flags(monday, refresh_stale=False, now=NOW, week_start=WEEK_START)
    stored = rec(nfl_injury_stored_at="2026-10-20T00:00:00+00:00")           # no report date: the stored-at time
    assert checkin.injury_record_date(stored)[1].startswith("stored-at time")
    assert checkin.stale_reasons(stored, now=NOW)
    assert checkin.stale_reasons({"nfl_injury_status": "Questionable"}, now=NOW) == []   # undated: no date rule
    assert checkin.stale_reasons(rec(nfl_injury_report_date="2026-10-28T09:00:00+00:00"), refresh_stale=True,
                                 now=NOW) == ["the injury refresh timed out"]


def test_every_injury_record_shows_its_report_date_and_why_it_is_stale():
    row = checkin.verify_roster([P("Pat", injury="Q", nfl="Questionable",
                                   nfl_injury_report_date="2026-10-24T12:00:00+00:00",
                                   nfl_injury_stored_at="2026-10-28T00:00:00+00:00")], 8, {},
                                now=NOW, week_start=WEEK_START)["injuries"][0]
    assert row["report_date"] == "2026-10-24T12:00:00+00:00" and row["stored_at"] == "2026-10-28T00:00:00+00:00"
    assert row["date_used"] == row["report_date"] and row["date_basis"] == "report date"
    assert len(row["stale_reasons"]) == 2 and "stale_injury" in row["flags"]
    assert row["statement"].startswith("stored report (dated 2026-10-24, stale: dated 2026-10-24, 4 days old")


def test_a_stale_record_is_quoted_as_a_stored_report_with_its_date_never_as_fact():
    old = rec(nfl_injury_report_date="2026-10-24T12:00:00+00:00", nfl_practice_date="2026-10-23")
    st = checkin.injury_statement(old, checkin.stale_reasons(old, now=NOW, week_start=WEEK_START))
    assert st.startswith("stored report (dated 2026-10-24, stale: ")
    assert st.endswith("says Questionable, practice DNP (dated 2026-10-23), Shoulder")
    fresh = rec(nfl_injury_report_date="2026-10-28T09:00:00+00:00")
    assert checkin.injury_statement(fresh, []) == "nfl_mcp report (dated 2026-10-28) says Questionable, practice DNP, Shoulder"
    assert checkin.injury_statement({"injury": "IR-R"}, []) == "Yahoo alone shows IR-R; nfl_mcp has no report"
    assert checkin.injury_statement(rec(), ["x"]).startswith("stored report (undated, stale: x)")


def test_a_pup_or_pre_activation_record_never_makes_a_player_out_or_zero():
    pup = B("BN", "Pat Back", "RB", 0.0, 6, nfl_injury_status="PUP", nfl_injury_report_date="2026-08-20T00:00:00+00:00")
    pup["injury_flags"] = checkin.injury_flags(pup, now=NOW, week_start=WEEK_START)
    assert {"season_long_status", "predates_activation", "stale_injury"} <= set(pup["injury_flags"])
    assert "never makes a player Out" in checkin.projection_unknown(pup) and checkin.usable_projection(pup) is None
    review = checkin.bench_review([B("RB", "Ann Starter", "RB", 15.0, 11), pup], 8)
    assert review["ranked"] == [] and [u["name"] for u in review["unknown"]] == ["Pat Back"]
    # an Out record written before he came back (Yahoo shows no tag) is the same, and makes no hole
    out = B("RB", "Ned Back", "RB", 0.0, 6, nfl_injury_status="Out", nfl_injury_report_date="2026-10-10T00:00:00+00:00")
    out["injury_flags"] = checkin.injury_flags(out, now=NOW, week_start=WEEK_START)
    assert "predates_activation" in out["injury_flags"] and checkin.projection_unknown(out)
    assert checkin.starter_holes([], [out]) == []
    # a fresh Out that Yahoo agrees on is real
    real = B("BN", "Rae Real", "RB", 0.0, 6, injury="O", nfl_injury_status="Out",
             nfl_injury_report_date="2026-10-28T09:00:00+00:00")
    real["injury_flags"] = checkin.injury_flags(real, now=NOW, week_start=WEEK_START)
    assert real["injury_flags"] == [] and checkin.usable_projection(real) == 0.0
    # a PUP both sources show is consistent
    both = B("IR", "Pia Both", "RB", None, 6, injury="PUP-R", nfl_injury_status="PUP",
             nfl_injury_report_date="2026-08-20T00:00:00+00:00")
    assert "season_long_status" not in checkin.injury_flags(both, now=NOW, week_start=WEEK_START)


def test_the_warning_for_an_ignored_record_says_it_creates_no_out_and_no_zero():
    row = checkin.verify_roster([P("Pat", injury=None, nfl="PUP", nfl_injury_report_date="2026-08-20T00:00:00+00:00",
                                   slot="BN")], 8, {}, now=NOW, week_start=WEEK_START)
    text = " ".join(checkin.verification_warnings(row))
    assert "season-long status or a record older than his activation" in text and "no Out and no 0 projection" in text


# ------------------------------------------------------------------ 3. a fresher single-source lead beats a stale record
def _inj(**k):
    return {"label": "Pat Back (X WR)", "stale_reasons": ["dated 2026-10-24, 4 days old (limit 3)"],
            "date_used": "2026-10-24T12:00:00+00:00",
            "statement": "stored report (dated 2026-10-24, stale: x) says Questionable", **k}


def _post(**k):
    return {"author": "agg.example", "relayed_reporter": "Reporter A", "url": "https://example.test/p/1",
            "text": "[Reporter A] WR Pat Back (knee) went from DNP to limited. twitter.com/Reporter/status/1",
            "posted_at": "2026-10-27T19:00:00Z", "mentions": ["Pat Back (X WR)"], "independent_origins": 1, **k}


def test_a_fresher_lead_replaces_a_stale_stored_record_and_is_labelled_single_source():
    stale, fresh = _inj(), _inj(stale_reasons=[])
    other = _inj(label="Someone Else (X WR)")
    checkin.attach_leads([stale, fresh, other], [_post(), _post(posted_at="2026-10-20T19:00:00Z", text="older")])
    assert stale["statement"].startswith("single-source lead from agg.example relaying Reporter A, posted 2026-10-27: ")
    assert "went from DNP to limited" in stale["statement"] and "twitter.com" not in stale["statement"]
    assert "Unconfirmed; it is newer than the stored report (dated 2026-10-24, stale" in stale["statement"]
    assert stale["stored_statement"].startswith("stored report (dated 2026-10-24") and stale["fresher_lead"]["single_source"]
    assert fresh["statement"].startswith("stored report") and "fresher_lead" not in fresh   # a current record is kept
    assert "fresher_lead" not in other
    older_only = _inj()
    checkin.attach_leads([older_only], [_post(posted_at="2026-10-23T19:00:00Z")])           # not newer than the record
    assert "fresher_lead" not in older_only
    two = _inj()
    checkin.attach_leads([two], [_post(independent_origins=2)])
    assert two["statement"].startswith("lead from 2 independent accounts") and not two["fresher_lead"]["single_source"]


@pytest.mark.asyncio
async def test_the_roster_carries_the_statement_to_quote_for_every_injured_player(pages):
    async def cmp(league_id, rws, week):
        players = [_row(r, week) for r in rws]
        for o in players:
            if o["name"] == "Matthew Golden":
                o.update(injury="Q", nfl_injury_status="Questionable", practice_status="DNP",
                         nfl_injury_body_part="Shoulder", nfl_injury_report_date="2026-10-24T12:00:00+00:00")
        return await fake_compare(players=players)(league_id, rws, week)

    async def posts(handles, hours, limit, watch):
        return {"items": [{"author": "agg.example", "text": "[Reporter A] WR Matthew Golden went from DNP to limited",
                           "posted_at": "2026-10-27T19:00:00Z", "mentions_watched_player": ["Matthew Golden"]}],
                "errors": 0}
    r = await build(compare=cmp, posts=posts, now=NOW, week_start=WEEK_START)
    golden = next(s for s in r["roster"]["slots"] if s["name"] == "Matthew Golden")
    assert golden["injury_statement"].startswith("single-source lead from agg.example relaying Reporter A")
    row = next(i for i in r["verification"]["injuries"] if i["name"] == "Matthew Golden")
    assert row["stored_statement"].startswith("stored report (dated 2026-10-24, stale:")
    assert row["report_date"] == "2026-10-24T12:00:00+00:00"


# ------------------------------------------------------------------ 5. a backup at a single-starter position is insurance
class _Prov:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def get_free_agents(self, pos):
        self.calls.append(pos)
        return self.rows.get(pos, [])


def _fa(name, pos):
    return {"name": name, "team": "X", "position": pos, "player_id": name.lower(), "injury": None,
            "availability": {"state": "FA"}}


def _points(table, seen):
    async def project(players, **kw):
        seen.append(kw)
        return {"success": True, "projections": [
            {"player": p["name"], "player_id": p.get("player_id"), "projected_points": table[p["name"]]}
            for p in players]}
    return project


@pytest.mark.asyncio
async def test_replacement_ease_counts_the_free_agents_who_project_above_a_backup():
    players = [B("QB", "Cal Cousins", "QB", 20.0, 13), B("BN", "Sam Shough", "QB", None, 8, on_bye=True, player_id="s1"),
               B("RB", "Ann Starter", "RB", 15.0, 11)]
    prov = _Prov({"QB": [_fa(n, "QB") for n in ("Q1", "Q2", "Q3", "Q4", "Q5")]})
    seen: list = []
    table = {"Sam Shough": 14.0, "Q1": 16.0, "Q2": 15.0, "Q3": 14.5, "Q4": 13.0, "Q5": 12.0}
    warnings: list[str] = []
    out = await checkin.replaceability(prov, _points(table, seen), LG, 8, players, {}, warnings)
    info = out["id:s1"]
    assert info["free_agents_above"] == 3 and info["free_agents_checked"] == 5 and info["easy"]
    assert info["week"] == 9 and seen[0]["week"] == 9 and info["bench_projection"] == 14.0   # on bye: judged after it
    assert "3 of 5 free-agent QBs project above him (14.0) in week 9" in info["text"]
    assert "the best is Q1 (X QB) (16.0, FA)" in info["text"]
    assert prov.calls == ["QB"] and warnings == []          # no bench RB, so the RB list is not read


def test_a_backup_qb_is_injury_insurance_and_easy_insurance_ranks_as_a_drop_first():
    players = [B("QB", "Cal Cousins", "QB", 20.0, 13), B("BN", "Sam Shough", "QB", None, 8, on_bye=True, player_id="s1"),
               B("WR", "Una Wide", "WR", 12.0, 10), B("BN", "Wes Cover", "WR", 9.0, 6)]
    easy = {"id:s1": {"easy": True, "free_agents_above": 3, "free_agents_checked": 5, "week": 9,
                      "text": "3 of 5 free-agent QBs project above him (14.0) in week 9"}}
    got = checkin.bench_review(players, 8, easy)["ranked"]
    assert [d["name"] for d in got] == ["Sam Shough", "Wes Cover"]
    shough = got[0]
    assert shough["does_a_job"] and shough["insurance_kinds"] == ["single"]
    assert "injury insurance: the only QB starter is Cal Cousins (X QB)" in shough["note"]
    assert "easy to replace: 3 of 5 free-agent QBs project above him (14.0) in week 9" in shough["note"]
    hard = {"id:s1": {**easy["id:s1"], "easy": False, "free_agents_above": 0,
                      "text": "0 of 5 free-agent QBs project above him (14.0) in week 9"}}
    got = checkin.bench_review(players, 8, hard)["ranked"]
    assert [d["name"] for d in got] == ["Wes Cover", "Sam Shough"] and "hard to replace: 0 of 5" in got[1]["note"]
    plain = checkin.bench_review(players, 8)["ranked"]
    assert all(d["does_a_job"] for d in plain) and "does neither job" not in " ".join(d["note"] for d in plain)


@pytest.mark.asyncio
async def test_the_checkin_reads_the_backup_positions_free_agents_and_shows_how_replaceable_he_is(pages, monkeypatch):
    prov = await runtime.get_provider(LG)
    real = prov.get_free_agents

    async def agents(pos, status="A"):
        if pos in ("QB", "TE"):
            return [_fa(f"{pos}{i}", pos) for i in range(1, 7)]
        return await real(pos, status)
    monkeypatch.setattr(prov, "get_free_agents", agents)
    r = await build()
    shough = next(d for d in r["drop_candidates"] if d["name"] == "Tyler Shough")
    assert shough["does_a_job"] and shough["replacement"]["week"] == 9            # on bye now: judged in week 9
    assert shough["replacement"]["free_agents_checked"] == 6
    assert "free-agent QBs project above him" in shough["note"] and "replace" in shough["note"]
    kittle = next(d for d in r["drop_candidates"] if d["name"] == "George Kittle")
    assert kittle["replacement"] is not None and "injury insurance: the only TE starter" in kittle["note"]


# ------------------------------------------------------------------ 6. quotes and predictions are not leads
@pytest.mark.parametrize("text,why", [
    ('DB Sam Corner says the Mavericks will win Sunday. Sam said, "we will win this game." Sounds like a guarantee?',
     "prediction"),
    ("Mavericks coach guarantees a win this week", "prediction"),
    ('RB Ann Back said, "I feel great about the offense."', "player quote"),
    ("[Rep] WR Pat Back (knee) is questionable for Sunday; he said \"I feel good\"", None),
    ('Coach says "he will be out Sunday" (WR Pat Back)', None),
    ("[Rep] WR Pat Back went from DNP to limited", None),
    ("Mavericks sign a free agent kicker", None)])
def test_quote_or_prediction(text, why):
    assert checkin.quote_or_prediction(text) == why


@pytest.mark.asyncio
async def test_quotes_and_predictions_are_dropped_before_leads_are_counted(pages):
    async def posts(handles, hours, limit, watch):
        return {"items": [
            {"author": "agg.example", "text": '[Rep] DB Sam Corner says Kirk Cousins\' team will win. Sam said, "it has to be."',
             "posted_at": "2026-10-28T20:00:00Z", "mentions_watched_player": ["Kirk Cousins"]},
            {"author": "agg.example", "text": "[Rep] QB Kirk Cousins (ankle) limited in practice",
             "posted_at": "2026-10-28T19:00:00Z", "mentions_watched_player": ["Kirk Cousins"]},
            {"author": "agg.example", "text": "unrelated", "mentions_watched_player": []}], "errors": 0}
    r = await build(posts=posts)
    assert r["news"]["count"] == 1 and "limited in practice" in r["news"]["items"][0]["text"]
    assert [d["reason"] for d in r["news"]["dropped"]] == ["prediction"]
    assert r["news"]["by_player"]["Kirk Cousins (LV QB)"]["posts"] == 1
    assert any(w.startswith("Dropped 1 news post(s) that are player quotes, predictions or only a score or touchdown") for w in r["warnings"])
    assert any("News: 1 post(s) from 1 independent origin(s)" in w for w in r["warnings"])


# ------------------------------------------------------------------ close calls
def test_close_calls_pair_a_starter_with_a_same_eligibility_bench_player_inside_the_tie_band():
    players = [B("WR", "Una Wide", "WR", 12.0, 11), B("RB", "Ann Back", "RB", 15.0, 11), B("FLEX", "Fay Flex", "WR", 9.0, 11),
               B("BN", "Vic Bench", "WR", 11.0, 6),           # 1.0 under Una: a close call
               B("BN", "Rob Rush", "RB", 15.9, 6),            # 0.9 over Ann: a close call
               B("BN", "Tia Tight", "TE", 9.4, 6),            # the flex takes a TE (0.4 over Fay), the WR slot does not
               B("BN", "Wes Edge", "WR", 13.5, 6),            # exactly 1.5 over Una: outside the band
               B("BN", "Bye Bob", "WR", 12.1, 8, on_bye=True),    # on bye: never
               B("BN", "Gus Ghost", "WR", 0.0, 6, injury="Q", nfl_injury_status="Out", injury_flags=["injury_conflict"])]
    got = checkin.close_calls(players)
    assert [(c["starter"], c["bench"]) for c in got] == [("Fay Flex (X WR)", "Tia Tight (X TE)"), ("Ann Back (X RB)", "Rob Rush (X RB)"),
                                                          ("Una Wide (X WR)", "Vic Bench (X WR)")]
    assert [c["gap"] for c in got] == [0.4, 0.9, -1.0] and got[0]["slot"] == "FLEX"
    assert "Tia Tight (X TE) projects 0.4 higher, but inside the tie band that is not a reason to switch" in got[0]["reading"]
    assert "Una Wide (X WR) projects 1.0 higher, inside the tie band" in got[2]["reading"]
    assert all(c["reading"].startswith("toss-up") and "do not treat either as a pick" in c["reading"] for c in got)
    assert got[1]["compare"] == ["Ann Back (X RB)", "Rob Rush (X RB)"]
    assert checkin.close_calls([B("RB", "Ann Back", "RB", 15.0, 11), B("BN", "Rob Rush", "RB", 15.0, 6)])[0]["reading"].startswith(
        "toss-up: identical projections")
    assert checkin.close_calls([B("RB", "Ann Back", "RB", None, 8, on_bye=True), B("BN", "Rob Rush", "RB", 15.0, 6)]) == []


def test_a_pair_the_sources_disagree_on_is_a_close_call_even_when_the_median_is_outside_the_band():
    # nfl_mcp has them 4.0 apart, Yahoo 1.4 apart: two sources, so the median gap is 2.7, outside the band
    players = [B("RB", "Ann Back", "RB", 18.0, 11, yahoo_projection=15.0), B("BN", "Rob Rush", "RB", 14.0, 6, yahoo_projection=13.6)]
    got = checkin.close_calls(players)
    assert len(got) == 1
    c = got[0]
    assert c["gap"] == -2.7 and c["source_gaps"] == {"nfl_mcp": -4.0, "yahoo": -1.4}
    assert c["sources_disagree"] is True and c["starter_projection"] == 16.5 and c["bench_projection"] == 13.8
    assert "nfl_mcp (-4.0) is outside the 1.5-point tie band while Yahoo (-1.4) is inside it" in c["disagreement"]
    assert c["reading"].startswith("not a clear call") and "Sources disagree" in c["reading"]
    assert "do not treat either as a pick" in c["reading"] and c["compare"] == ["Ann Back (X RB)", "Rob Rush (X RB)"]
    # the same pair with Yahoo agreeing that they are far apart is not a close call
    assert checkin.close_calls([B("RB", "Ann Back", "RB", 18.0, 11, yahoo_projection=15.0),
                                B("BN", "Rob Rush", "RB", 14.0, 6, yahoo_projection=12.0)]) == []


def test_close_calls_use_the_median_of_nfl_mcp_model_sleeper_and_yahoo_and_still_flag_a_dissenting_source():
    st = B("RB", "Ann Back", "RB", 18.0, 11, model_projection=15.0, sleeper_projection=15.0, yahoo_projection=15.0)
    bn = B("BN", "Rob Rush", "RB", 14.0, 6, model_projection=13.9, sleeper_projection=13.9, yahoo_projection=13.6)
    got = checkin.close_calls([st, bn])
    assert len(got) == 1 and got[0]["gap"] == -1.1                    # medians 15.0 vs 13.9: inside the band
    assert got[0]["source_gaps"] == {"nfl_mcp": -4.0, "model": -1.1, "sleeper": -1.1, "yahoo": -1.4}
    assert got[0]["sources_disagree"] and got[0]["reading"].startswith("toss-up")
    assert "nfl_mcp (-4.0) is outside" in got[0]["disagreement"] and "model (-1.1), Sleeper (-1.1), Yahoo (-1.4) are inside it" in got[0]["disagreement"]
    assert got[0]["basis"] == "median of the available sources (nfl_mcp, model, Sleeper, Yahoo)"
    # every source inside the band: a plain toss-up with nothing to flag
    quiet = checkin.close_calls([B("RB", "Ann Back", "RB", 15.0, 11, model_projection=15.2, sleeper_projection=14.8),
                                 B("BN", "Rob Rush", "RB", 14.5, 6, model_projection=14.4, sleeper_projection=14.2)])
    assert len(quiet) == 1 and quiet[0]["sources_disagree"] is False and quiet[0]["disagreement"] is None
    # an unreliable nfl_mcp number does not count, and neither does anything on bye
    assert "nfl_mcp" not in checkin.projection_sources(B("RB", "Ann Back", "RB", 0.0, 11, yahoo_projection=9.0,
                                                          nfl_injury_status="Out", injury="Q", injury_flags=["injury_conflict"]))
    assert checkin.projection_sources(B("RB", "Ann Back", "RB", 15.0, 8, on_bye=True)) == {}


@pytest.mark.asyncio
async def test_the_checkin_lists_its_close_calls_and_each_one_is_a_toss_up_not_a_pick(pages):
    r = await build()
    assert "close_calls" in r
    for c in r["close_calls"]:
        assert abs(c["gap"]) < checkin.PLAYER_TIE_POINTS and c["reading"].startswith("toss-up")
    assert [abs(c["gap"]) for c in r["close_calls"]] == sorted(abs(c["gap"]) for c in r["close_calls"])


# ------------------------------------------------------------------ injury freshness
@pytest.mark.asyncio
async def test_the_checkin_reports_when_injuries_were_last_refreshed_and_whether_a_crawl_runs(pages):
    asked = []

    def freshness(teams):
        asked.append(teams)
        return {"line": f"injuries last refreshed at Thu 2026-10-29 18:12 ET ({len(teams)} teams). No injury crawl is running now.",
                "teams": len(teams), "crawl_running": False}
    r = await build(injury_freshness=freshness)
    assert r["injury_refresh"]["line"].startswith("injuries last refreshed at Thu 2026-10-29 18:12 ET (")
    assert r["injury_refresh"]["crawl_running"] is False and asked and asked[0] == sorted(set(asked[0]))
    assert (await build())["injury_refresh"] is None                       # not wired: the key is there, empty

    def broken(teams):
        raise RuntimeError("db locked")
    r = await build(injury_freshness=broken)
    assert r["injury_refresh"] is None and any("when the injury reports were last refreshed" in w for w in r["warnings"])


# ------------------------------------------------------------------ no ESPN report (capped list)
@pytest.mark.asyncio
async def test_a_player_neither_the_capped_list_nor_the_roster_page_settled_is_not_called_healthy(pages):
    base = await build()
    target = next(p for p in base["roster"]["slots"] if p["slot"] == "BN" and not p["nfl_injury_status"] and not p["on_bye"])
    seen = []

    def missing(team, name):
        seen.append((team, name))
        return name == target["name"]
    r = await build(report_missing=missing)
    flagged = next(p for p in r["roster"]["slots"] if p["name"] == target["name"])
    assert "no_espn_report" in flagged["injury_flags"] and flagged["injury_statement"] == "no ESPN report found (list capped)"
    assert [x["label"] for x in r["verification"]["no_espn_report"]] == [target["label"]]
    assert r["verification"]["no_espn_report"][0]["statement"] == "no ESPN report found (list capped)"
    assert r["verification"]["summary"]["no_espn_report"] == 1
    w = [x for x in r["warnings"] if "no ESPN report found (list capped)" in x]
    assert len(w) == 1 and target["label"] in w[0] and "not a clean bill of health" in w[0]
    assert seen and all(name for _, name in seen)
    # nothing is flagged when the hook says all is settled, or is not wired
    assert (await build(report_missing=lambda t, n: False))["verification"]["no_espn_report"] == []
    assert (await build())["verification"]["no_espn_report"] == []
    hooked = []

    async def lookup(rows):
        hooked.append(len(rows))
        return {}
    await build(gap_lookup=lookup)
    assert hooked and hooked[0] > 5                                  # the roster rows go to the roster-page lookup first

    async def broken(rows):
        raise RuntimeError("espn down")
    assert any("capped injury list may have left out" in x for x in (await build(gap_lookup=broken))["warnings"])


# ------------------------------------------------------------------ the wire: QB, TE, K and DEF are always read and compared
def _starter_row(name, pos, proj, *, slot=None, on_bye=False, started=False, injury=None):
    return {"slot": slot or pos, "name": name, "team": "KC", "position": pos, "player_id": name, "injury": injury,
            "on_bye": on_bye, "started": started, "our_projection": proj, "yahoo_projection": proj, "label": f"{name} (KC {pos})"}


def _free_agent(name, pos, proj, tag="FA"):
    return {"name": name, "label": f"{name} (AA {pos})", "team": "AA", "position": pos, "player_id": name, "tag": tag,
            "injury": None, "our_projection": proj, "upgrade": None, "projection_source": "sleeper_blend"}


def test_a_free_agent_above_your_starter_is_a_target_even_inside_the_noise_and_says_so():
    players = [_starter_row("Quin Back", "QB", 20.7), _starter_row("Tia End", "TE", 10.3),
               _starter_row("Kay Kick", "K", 9.1), _starter_row("Dee Fense", "DEF", 8.3, on_bye=True)]
    cands = {"QB": [_free_agent("Sam Arm", "QB", 21.3), _free_agent("Lou Arm", "QB", 18.0)],
             "TE": [_free_agent("Tom Hands", "TE", 12.5, "W (Oct 31)"), _free_agent("Ted Hands", "TE", 10.3)],
             "K": [_free_agent("Ken Foot", "K", 8.0)],
             "DEF": [_free_agent("Bay Wall", "DEF", 7.0)]}
    why = {p: "always read" for p in cands}
    checks, targets = checkin.starter_wire_checks(players, cands, why)
    by = {c["position"]: c for c in checks}
    qb = by["QB"]
    assert qb["starter"] == "Quin Back (KC QB)" and qb["starter_projection"] == 20.7 and qb["why_read"] == "always read"
    assert qb["best_free_agent"] == {"label": "Sam Arm (AA QB)", "tag": "FA", "projection": 21.3}
    assert qb["gap"] == 0.6 and qb["inside_noise"] is True
    assert "projects above him, but inside the noise (under the 1.5-point noise band)" in qb["read"]
    te = by["TE"]
    assert te["gap"] == 2.2 and te["inside_noise"] is False and "outside the noise" in te["read"]
    assert [a["label"] for a in te["above_starter"]] == ["Tom Hands (AA TE)"]            # a tie is not above
    assert by["K"]["above_starter"] == [] and by["K"]["read"].endswith("no free agent projects above him")
    assert "hole" in by["DEF"]["read"] and by["DEF"]["best_free_agent"] is None            # a bye is the hole logic's
    got = {t["label"]: t for t in targets}
    assert set(got) == {"Sam Arm (AA QB)", "Tom Hands (AA TE)"}
    sam, tom = got["Sam Arm (AA QB)"], got["Tom Hands (AA TE)"]
    assert (sam["upgrade_points"], sam["inside_noise"], sam["tag"], sam["yahoo_tag"]) == (0.6, True, "FA", "FA")
    assert sam["noise_note"].startswith("inside the noise: +0.6 against Quin Back (KC QB)")
    assert (tom["upgrade_points"], tom["inside_noise"], tom["tag"]) == (2.2, False, "W (Oct 31)")
    assert tom["noise_note"].startswith("outside the noise") and tom["verdict"] == "above_starter"


def test_a_locked_starter_a_missing_projection_and_a_position_with_no_starter_are_handled():
    players = [_starter_row("Quin Back", "QB", 20.7, started=True), _starter_row("Kay Kick", "K", None)]
    cands = {"QB": [_free_agent("Sam Arm", "QB", 30.0)], "K": [_free_agent("Ken Foot", "K", 8.0)], "TE": [_free_agent("Tom Hands", "TE", 12.0)],
             "DEF": []}
    checks, targets = checkin.starter_wire_checks(players, cands)
    by = {c["position"]: c for c in checks}
    assert by["QB"]["locked"] is True and "game has started" in by["QB"]["read"] and targets == []
    assert "no projection for your starter" in by["K"]["read"]
    assert "TE" not in by and "DEF" not in by                                              # no starter there: nothing to compare
    none = checkin.starter_wire_checks([_starter_row("Kay Kick", "K", 9.0)], {"K": [_free_agent("Ken Foot", "K", None)]})[0][0]
    assert "none of the 1 free agents read has a projection" in none["read"]


def test_the_targets_note_says_which_positions_were_read_and_never_says_nothing_beats_a_starter_that_one_does():
    none = checkin.starter_check_note(["RB", "QB", "TE", "K", "DEF"], [],
                                      "Nothing on waivers beats your current starters in week 8 at RB, WR (285 free agents checked).")
    assert none.startswith("Read the top free agents at RB, QB, TE, K, DEF and compared each with your starter there: "
                           "none projects above a starter.")
    assert "Nothing on waivers beats your current starters in week 8 at RB, WR (285 free agents checked)" in none
    above = [{"inside_noise": True}, {"inside_noise": False}, {"inside_noise": True}]
    note = checkin.starter_check_note(["QB", "TE"], above,
                                      "Nothing on waivers beats your current starters in week 8 at RB, WR (285 free agents checked).")
    assert "3 project above a starter (1 outside the noise, 2 inside the 1.5-point noise band)" in note
    assert "Nothing on waivers beats" not in note
    assert "found no claim worth making in week 8 at RB, WR (285 free agents checked)" in note


@pytest.mark.asyncio
async def test_the_check_in_always_reads_qb_te_k_and_def_and_lists_each_free_agent_above_a_starter(pages, tmp_path):
    for src, pos in (("players_qb", "QB"), ("players_k", "K"), ("players_te", "TE"), ("players_def_live", "DEF")):
        shutil.copy(FIX / f"{src}.html", tmp_path / f"players_{pos}_A.html")        # the pages as Yahoo served them
    base = await build()
    kicker = next(p for p in base["roster"]["slots"] if p["slot"] == "K")["our_projection"]

    async def project(players, **kw):
        out = []
        for i, p in enumerate(players):
            if p["position"] == "K":     # the second K candidate is just above the starter, the first well above
                v = kicker + 4.0 if i == 0 else kicker + 0.5 if i == 1 else 1.0
            else:
                v = round(9.0 - 0.5 * i, 1)
            out.append({"player": p["name"], "player_id": p.get("player_id"), "projected_points": v,
                        "projection_source": "sleeper_blend"})
        return {"success": True, "projections": out}
    r = await build(project=project)
    read = {x["position"]: x["why"] for x in r["wire"]["positions_read"]}
    assert {"QB", "TE", "K", "DEF"} <= set(read) and "read every check-in" in read["K"]
    assert set(r["wire"]["reasons"]) == set(r["wire"]["positions"])
    k = next(c for c in r["wire"]["starter_checks"] if c["position"] == "K")
    assert k["starter_projection"] == kicker and k["why_read"] == "always read" and k["gap"] == 4.0
    targets = [t for t in r["wire"]["targets"] if t.get("source") == "starter_check"]
    assert [t["inside_noise"] for t in targets] == [False, True] and [t["upgrade_points"] for t in targets] == [4.0, 0.5]
    assert targets[1]["noise_note"].startswith("inside the noise: +0.5") and all(t["tag"] == t["yahoo_tag"] for t in targets)
    assert "Read the top free agents at" in r["wire"]["targets_note"] and "K" in r["wire"]["targets_note"]
    assert "2 project above a starter (1 outside the noise, 1 inside the 1.5-point noise band)" in r["wire"]["targets_note"]


@pytest.mark.asyncio
async def test_a_position_whose_free_agents_could_not_be_read_is_not_claimed_as_read(pages):
    r = await build()                      # the fixture has no QB, TE or K free-agent pages
    assert any(w.startswith("Could not read the QB wire") for w in r["warnings"])
    assert "QB" not in {x["position"] for x in r["wire"]["positions_read"]}
    assert set(r["wire"]["reasons"]) == set(r["wire"]["positions"])


# ------------------------------------------------------------------ news that only reports a score or touchdown
@pytest.mark.parametrize("text,dropped", [
    ("[Packers] Matthew Golden touchdown! twitter.com/packers/stat...", True),
    ("[Panthers] Chuba Hubbard gets the touchdown and does the Lambeau Leap! twitter.com/Panthers/sta...", True),
    ("[Chiefs] Harrison Butker field goal from 52 yards. https://x.com/chiefs/status/1", True),
    ("Kenny Pickett scores on a 3-yard run", True),
    ("[Rams] Kyren Williams touchdown, then left the game with an injury", False),
    ("[Rams] Kyren Williams scored but is questionable for Sunday", False),
    ("[Jets] Garrett Wilson touchdown; he will miss practice Friday", False),
    ("Zach Charbonnet (knee) was a full participant at Thursday's practice", False),
    ("[Bears] Colston Loveland ruled out after the touchdown", False),
    ("", False), (None, False)])
def test_a_post_that_only_reports_a_score_or_touchdown_is_not_availability_news(text, dropped):
    assert (checkin.score_only(text) == "score or touchdown only") is dropped


@pytest.mark.asyncio
async def test_score_only_posts_are_dropped_before_leads_are_counted_and_listed_under_news_dropped(pages):
    async def posts(*a):
        return {"items": [
            {"author": "agg.bsky.social", "text": "[Panthers] Chuba Hubbard gets the touchdown! twitter.com/x/sta...",
             "mentions_watched_player": ["Chuba Hubbard"], "posted_at": "2026-10-30T00:57:32Z"},
            {"author": "agg.bsky.social", "text": "[Packers] Matthew Golden touchdown! twitter.com/p/sta...",
             "mentions_watched_player": ["Matthew Golden"], "posted_at": "2026-10-30T01:34:31Z"},
            {"author": "rep.bsky.social", "text": "[Seahawks] Zach Charbonnet (knee) is questionable for Sunday.",
             "mentions_watched_player": ["Zach Charbonnet"], "posted_at": "2026-10-30T02:00:00Z"}]}
    r = await build(posts=posts)
    assert r["news"]["count"] == 1 and "questionable" in r["news"]["items"][0]["text"]
    assert [d["reason"] for d in r["news"]["dropped"]] == ["score or touchdown only", "score or touchdown only"]
    assert r["news"]["dropped"][0]["author"] == "agg.bsky.social" and "touchdown" in r["news"]["dropped"][0]["text"]
    assert any(w.startswith("Dropped 2 news post(s) that are player quotes, predictions or only a score or touchdown")
               for w in r["warnings"])
