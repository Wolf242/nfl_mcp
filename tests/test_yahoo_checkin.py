"""fantasy_checkin, the env defaults and the Yahoo block handling. Offline: fixtures and fakes only."""
import shutil
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from nfl_mcp import defaults, projections, ros, sleeper_tools, waiver_target_tools, week_context
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
    assert out[10]["active_rbs"] == 1 and "fewer than 2" in out[10]["flag"]
    assert out[9]["on_bye"] == [{"name": "W1", "position": "WR", "slot": "WR"}]
    assert out[11]["starters_on_bye"] == 1


def test_wire_positions_follow_problems_and_rb_holes():
    probs = checkin.find_problems([S("QB", "A", "QB", bye=WEEK), S("W/R/T", empty=True)], WEEK)
    assert checkin.wire_positions(probs, []) == ["QB", "RB", "WR", "TE"]
    hole = {"flags": ["fewer than 2 active RBs"], "starters_on_bye": 0, "starters_on_bye_positions": []}
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
    assert "bench_full" in [p["type"] for p in r["problems"]]
    # DEF for the Texans' bye now; RB and WR for week 11, when three starters are out
    assert list(r["wire"]["positions"]) == ["DEF", "RB", "WR"]
    tags = {p["tag"] for p in r["wire"]["positions"]["DEF"]}
    assert tags and all(t.startswith("W (") for t in tags)
    assert any(w.startswith("Week 11: 3 starters on bye") for w in r["warnings"])
    t = r["wire"]["targets"][0]
    assert t["name"] == "Wire Guy" and t["yahoo_tag"].startswith("W (")
    # nfl_mcp says add now; Yahoo's tag is a waiver claim, so the check-in says Yahoo's tag wins
    assert any("Wire Guy" in w and "waiver claim" in w for w in r["warnings"])
    # Sleeper writes Washington as WSH, Yahoo as WAS; the tag must still be found, and from outside the top few
    assert r["wire"]["targets"][1]["yahoo_tag"].startswith("W (")


@pytest.mark.asyncio
async def test_checkin_opponent_news_and_single_source_warnings(pages):
    r = await build()
    assert r["opponent"]["team"].startswith("Team ") and r["opponent"]["projected_points"]
    assert "injured_starters" in r["opponent"] and "starters_on_bye" in r["opponent"]
    assert r["news"]["count"] == 1
    assert r["news"]["items"][0]["single_source"] and r["news"]["items"][0]["status"] == "lead"
    text = " ".join(r["warnings"])
    assert "single-source" in text and "rests on Yahoo alone" in text


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
    assert r["success"] and r["wire"]["targets"] == [] and r["news"]["count"] == 0
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
    assert de[0]["upgrade"] == 9.0 and "Texans is on bye" in de[0]["upgrade_over"]  # replaces a 0 this week


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
    assert out[9]["active_rbs"] == 2 and out[9]["flags"] == ["2 starters on bye (R1 at RB, W1 at WR)"]
    assert out[10]["flags"] == [] and out[10]["starters_on_bye"] == 1


# ------------------------------------------------------------------ verification
def P(name, slot="WR", team="KC", injury=None, nfl=None, bye=12, **kw):
    return {"slot": slot, "name": name, "team": team, "position": "WR", "injury": injury, "bye_week": bye,
            "nfl_injury_status": nfl, "nfl_injury_source": "espn" if nfl else None, "practice_status": None,
            "gameday_status": None, **kw}


def test_injury_tags_are_checked_against_nfl_mcp_s_data():
    v = checkin.verify_roster([
        P("Agree", injury="Q", nfl="Questionable", practice_status="Limited"),
        P("Conflict", injury="Q", nfl="Out"),
        P("YahooOnly", injury="Q"),
        P("OtherOnly", nfl="Doubtful"),
        P("Healthy"),
        P("Reserve", slot="IR", injury="IR-R", nfl="Injured Reserve"),
        P("IrNoTag", slot="IR"),
    ], 8, {})
    got = {r["name"]: r["verdict"] for r in v["injuries"]}
    assert got == {"Agree": "agree", "Conflict": "conflict", "YahooOnly": "yahoo_only",
                   "OtherOnly": "other_only", "Reserve": "agree"}
    assert "practice: Limited" in next(r for r in v["injuries"] if r["name"] == "Agree")["detail"]
    assert v["summary"]["injuries"] == {"agree": 2, "conflict": 1, "yahoo_only": 1, "other_only": 1}
    text = " ".join(checkin.verification_warnings(v))
    assert "Injury tag conflict for Conflict" in text and "OtherOnly: nfl_mcp has an injury report" in text
    assert "only Yahoo reports" in text and "YahooOnly" in text and "2 injury tag(s) agree" in text


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
