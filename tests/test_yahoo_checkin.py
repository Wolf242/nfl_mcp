"""fantasy_checkin, the env defaults and the Yahoo block handling. Offline: fixtures and fakes only."""
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from nfl_mcp import defaults, sleeper_tools, waiver_target_tools, week_context
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
    assert checkin.wire_positions([], [{"flag": "fewer than 2 active RBs"}]) == ["RB"]
    assert checkin.wire_positions([], [{"flag": None}]) == []


# ------------------------------------------------------------------ orchestration on fixtures
@pytest.fixture
def pages(tmp_path, monkeypatch):
    monkeypatch.setitem(sleeper_tools._PLAYERS_CACHE, "data", {})
    for src, dst in (("standings", "standings"), ("settings", "settings"), ("roster", "roster_9"),
                     ("matchup", "matchup_9_w8"), ("players_def", "players_DEF_A"),
                     ("players_rb", "players_RB_A")):
        shutil.copy(FIX / f"{src}.html", tmp_path / f"{dst}.html")
    for mid in range(1, 11):  # every team's roster, for tools that read the whole league
        shutil.copy(FIX / "roster.html", tmp_path / f"roster_{mid}.html")
    runtime.set_source(DirSource(tmp_path))
    yield
    runtime.set_source(None)


def fake_compare(gap=5.0, players=None):
    async def _cmp(league_id, rows, week):
        return {"week": week, "players": players if players is not None else [
            {"slot": r["slot"], "name": r["name"], "projection_source": "sleeper_blend", "on_bye": False}
            for r in rows],
            "starter_totals": {"yahoo": 120.0, "ours": 120.0 + gap, "gap": gap, "players_compared": 8, "starters": 9},
            "biggest_gaps": [], "warnings": []}
    return _cmp


async def fake_targets(**kw):
    return {"success": True, "targets": [{"name": "Wire Guy", "position": "DEF", "team": "PIT",
                                          "projected_points": 7.0, "upgrade_points": 6.7, "verdict": "add",
                                          "waiver_strategy": {"recommendation": "claim_now"}}]}


async def fake_posts(handles, hours, limit, watch):
    return {"items": [{"author": "rep", "text": "x", "mentions_watched_player": ["Kirk Cousins"]},
                      {"author": "rep", "text": "y", "mentions_watched_player": []}], "errors": 0}


async def build(week=WEEK, **kw):
    prov = await runtime.get_provider(LG)
    return await checkin.build_checkin(prov, LG, "9", week, compare=kw.pop("compare", fake_compare()),
                                       targets=kw.pop("targets", fake_targets),
                                       posts=kw.pop("posts", fake_posts), **kw)


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
    assert list(r["wire"]["positions"]) == ["DEF"]
    tags = {p["tag"] for p in r["wire"]["positions"]["DEF"]}
    assert tags and all(t.startswith("W (") for t in tags)
    assert r["wire"]["targets"][0]["name"] == "Wire Guy"


@pytest.mark.asyncio
async def test_checkin_opponent_news_and_single_source_warnings(pages):
    r = await build()
    assert r["opponent"]["team"].startswith("Team ") and r["opponent"]["projected_points"]
    assert "injured_starters" in r["opponent"] and "starters_on_bye" in r["opponent"]
    assert r["news"]["count"] == 1
    assert r["news"]["items"][0]["single_source"] and r["news"]["items"][0]["status"] == "lead"
    text = " ".join(r["warnings"])
    assert "single-source" in text and "not cross-checked" in text


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
