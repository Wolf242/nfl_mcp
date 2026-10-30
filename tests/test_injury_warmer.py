"""The injury warmer: its schedule, the one-crawl-at-a-time guard, the team-list fast path and what a check-in
says about how fresh the injury data is. Fictional players; no network."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from nfl_mcp import injury_service as svc
from nfl_mcp import injury_warmer as w
from nfl_mcp.upstream import single_flight


def utc(*a):
    return datetime(*a, tzinfo=UTC)


# Eastern is UTC-4 until 2026-11-01 02:00.
THU_NOON = utc(2026, 10, 29, 16, 0)          # Thu 12:00 ET
THU_MIDNIGHT = utc(2026, 10, 29, 4, 0)       # Thu 00:00 ET
MON_LATE = utc(2026, 10, 27, 3, 30)          # Mon 23:30 ET
TUE_EARLY = utc(2026, 10, 27, 4, 1)          # Tue 00:01 ET
WED_LATE = utc(2026, 10, 29, 3, 59)          # Wed 23:59 ET


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    monkeypatch.setattr(w, "state", w.WarmState())
    monkeypatch.setattr(w, "GAME_INTERVAL_SECONDS", 1800)
    monkeypatch.setattr(w, "OFF_INTERVAL_SECONDS", 7200)
    monkeypatch.delenv("NFL_MCP_INJURY_WARM", raising=False)


# ------------------------------------------------------------------ the schedule
def test_every_30_minutes_thursday_to_monday_and_every_2_hours_otherwise():
    assert w.interval_seconds(THU_MIDNIGHT) == 1800       # Thursday starts the game window at 00:00 ET
    assert w.interval_seconds(THU_NOON) == 1800
    assert w.interval_seconds(utc(2026, 10, 31, 18, 0)) == 1800   # Saturday
    assert w.interval_seconds(utc(2026, 11, 1, 18, 0)) == 1800    # Sunday
    assert w.interval_seconds(MON_LATE) == 1800            # Monday 23:30 ET is still game window
    assert w.interval_seconds(TUE_EARLY) == 7200           # Tuesday 00:01 ET is not
    assert w.interval_seconds(utc(2026, 10, 28, 16, 0)) == 7200   # Wednesday
    assert w.interval_seconds(WED_LATE) == 7200            # Wednesday 23:59 ET


def test_a_refresh_is_due_when_none_has_run_or_the_interval_for_now_has_passed():
    assert w.is_due(None, THU_NOON)
    assert not w.is_due(THU_NOON - timedelta(minutes=29), THU_NOON)
    assert w.is_due(THU_NOON - timedelta(minutes=30), THU_NOON)
    tue = utc(2026, 10, 27, 16, 0)
    assert not w.is_due(tue - timedelta(minutes=119), tue)
    assert w.is_due(tue - timedelta(minutes=120), tue)
    # the interval in force is the one at the time of asking: a refresh at Wednesday 23:30 ET is due
    # 40 minutes later, after Thursday 00:00 ET has started the 30-minute window
    assert w.is_due(utc(2026, 10, 29, 3, 30), utc(2026, 10, 29, 4, 10))
    assert not w.is_due(utc(2026, 10, 29, 3, 30), utc(2026, 10, 29, 3, 59))
    assert w.next_due_at(THU_NOON, THU_NOON + timedelta(minutes=5)) == THU_NOON + timedelta(minutes=30)
    assert w.next_due_at(None, THU_NOON) == THU_NOON


class FakeDb:
    def __init__(self, rows=None):
        self.rows, self.writes = rows or [], []

    def upsert_injuries(self, injuries, prune_missing=False, complete_teams=None):
        self.writes.append((list(injuries), prune_missing, set(complete_teams or ())))
        return len(injuries)

    def get_all_current_injuries(self):
        return self.rows


def crawler(calls, gate=None, fail=None):
    async def crawl(teams, db):
        calls.append(list(teams))
        if gate is not None:
            await gate.wait()
        if fail:
            raise RuntimeError(fail)
        return [{"player_id": "1", "team_id": teams[0], "injury_status": "Questionable"}], {teams[0]}
    return crawl


async def fake_wait(clock, seconds, shutdown, stop_after, ticks):
    ticks.append(1)
    clock[0] += timedelta(seconds=seconds)
    if len(ticks) >= stop_after:
        shutdown.set()
    return shutdown.is_set()


async def run(start, stop_after, step=600, provider=None, calls=None, crawl=None):
    clock, shutdown, ticks = [start], asyncio.Event(), []
    calls = [] if calls is None else calls
    db = FakeDb()

    async def wait(ev, seconds):
        return await fake_wait(clock, seconds, shutdown, stop_after, ticks)

    async def default_provider():
        return ["ATL", "LAR"]

    await w.run_loop(db, provider or default_provider, shutdown, crawl=crawl or crawler(calls),
                     clock=lambda: clock[0], wait=wait, step=step)
    return calls, db


@pytest.mark.asyncio
async def test_it_refreshes_at_start_and_then_on_the_game_window_interval():
    calls, db = await run(THU_NOON, stop_after=8, step=600)          # looks at 0, 10, ... 70 minutes
    assert calls == [["ATL", "LAR"]] * 3                              # at 0, 30 and 60 minutes
    assert len(db.writes) == 3 and db.writes[0][1] is True            # stored with the prune rule, as the prefetch does


@pytest.mark.asyncio
async def test_it_refreshes_every_2_hours_on_a_tuesday():
    calls, _ = await run(utc(2026, 10, 27, 16, 0), stop_after=14, step=600)   # 0 to 130 minutes
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_no_roster_means_nothing_to_warm_and_a_failing_roster_read_falls_back_to_every_team():
    async def none():
        return []

    async def boom():
        raise RuntimeError("yahoo down")

    calls, _ = await run(THU_NOON, 3, provider=none)
    assert calls == []
    calls, _ = await run(THU_NOON, 2, provider=boom)
    assert len(calls) == 1 and len(calls[0]) == 32


@pytest.mark.asyncio
async def test_the_warmer_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("NFL_MCP_INJURY_WARM", "0")
    calls, _ = await run(THU_NOON, 3)
    assert calls == [] and not w.warm_enabled()


# ------------------------------------------------------------------ one crawl at a time
@pytest.mark.asyncio
async def test_a_second_refresh_never_starts_while_one_is_running():
    gate, calls, db = asyncio.Event(), [], FakeDb()
    first = asyncio.ensure_future(w.warm_once(db, ["ATL"], crawl=crawler(calls, gate)))
    await asyncio.sleep(0)
    assert w.crawl_running()
    second = await w.warm_once(db, ["ATL", "LAR"], crawl=crawler(calls, gate))
    third = await w.warm_once(db, ["ATL"], crawl=crawler(calls, gate))
    assert second["status"] == third["status"] == "skipped" and "already running" in second["reason"]
    assert w.WARM_KEY in second["running"] and w.state.skipped == 2
    gate.set()
    assert (await first)["status"] == "ok"
    assert calls == [["ATL"]] and not w.crawl_running()
    again = await w.warm_once(db, ["ATL"], crawl=crawler(calls))     # free again once it finished
    assert again["status"] == "ok" and len(calls) == 2


@pytest.mark.asyncio
async def test_two_refreshes_started_together_run_one_crawl():
    gate, calls, db = asyncio.Event(), [], FakeDb()
    tasks = [asyncio.ensure_future(w.warm_once(db, ["ATL"], crawl=crawler(calls, gate))) for _ in range(3)]
    await asyncio.sleep(0.01)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert sorted(r["status"] for r in results) == ["ok", "skipped", "skipped"] and len(calls) == 1


@pytest.mark.asyncio
async def test_a_crawl_a_tool_call_started_also_blocks_the_warmer():
    gate, calls = asyncio.Event(), []

    async def on_demand():
        await gate.wait()
    task = single_flight("injury_reports:ATL,LAR", on_demand)
    assert w.crawl_running()
    got = await w.warm_once(FakeDb(), ["ATL"], crawl=crawler(calls))
    assert got["status"] == "skipped" and calls == [] and got["running"] == ["injury_reports:ATL,LAR"]
    gate.set()
    await task
    assert not w.crawl_running()
    assert (await w.warm_once(FakeDb(), ["ATL"], crawl=crawler(calls)))["status"] == "ok"


@pytest.mark.asyncio
async def test_a_skipped_refresh_is_tried_again_at_the_next_look_and_a_failed_one_is_recorded():
    gate, calls = asyncio.Event(), []

    async def on_demand():
        await gate.wait()
    single_flight("injury_reports:*", on_demand)
    c, _ = await run(THU_NOON, 2, calls=calls)                       # blocked at start
    assert c == [] and w.state.last_started is None and w.state.skipped >= 1
    gate.set()
    await asyncio.sleep(0.01)
    c, _ = await run(THU_NOON, 2, calls=calls)                       # free: runs at once
    assert len(c) == 1
    got = await w.warm_once(FakeDb(), ["ATL"], crawl=crawler([], fail="ESPN down"))
    assert got["status"] == "error" and "ESPN down" in w.state.last_error
    assert w.state.last_ok is not None                               # the earlier success stands


@pytest.mark.asyncio
async def test_a_tool_call_waits_for_the_running_refresh_instead_of_starting_its_own_crawl(monkeypatch):
    started = []
    gate = asyncio.Event()

    async def warm():
        await gate.wait()
        return "warm"

    async def get_reports(teams=None, db=None, use_cache=True):
        started.append(teams)
        return [{"player_id": "1"}]
    monkeypatch.setattr(svc, "get_injury_reports", get_reports)
    single_flight(w.WARM_KEY, warm)
    call = asyncio.ensure_future(svc.get_injury_reports_bounded(["ATL"], FakeDb(), wait=5, inflight_cap=None))
    await asyncio.sleep(0.05)
    assert started == []                                             # still waiting on the warmer
    gate.set()
    rows, meta = await call
    assert started == [["ATL"]] and rows == [{"player_id": "1"}] and meta["partial"] is False


# ------------------------------------------------------------------ the team-list fast path
def item(pid, status="Questionable", name="Ann Back", pos="RB", date="2026-10-29T14:00Z", details=True):
    out = {"id": "9001", "status": status, "date": date, "shortComment": f"{name} update",
           "athlete": {"displayName": name, "position": {"abbreviation": pos},
                       "links": [{"href": f"https://www.espn.com/nfl/player/_/id/{pid}/ann-back"}]}}
    if details:
        out["details"] = {"type": "Hamstring", "returnDate": "2026-11-05",
                          "fantasyStatus": {"description": "QUESTIONABLE"}}
    return out


def payload(sizes=None, only=None):
    sizes = sizes or {}
    teams = only or svc.InjuryAggregator.NFL_TEAMS
    return {"injuries": [{"id": str(i), "displayName": t, "injuries": [
        item(f"{i}{n:03d}", name=f"Player {t} {n}") for n in range(sizes.get(t, 3))]} for i, t in enumerate(teams)]}


class Resp:
    def __init__(self, status, body):
        self.status_code, self._b, self.headers = status, body, {}

    def json(self):
        return self._b


class Client:
    def __init__(self, resp):
        self.resp, self.urls = resp, []

    async def get(self, url, **kw):
        self.urls.append(url)
        return self.resp


def test_the_team_list_parses_into_the_crawls_report_shape():
    got = svc.parse_team_injury_list(payload(only=["ATL"]))
    assert list(got) == ["ATL"] and len(got["ATL"]) == 3
    r = got["ATL"][0].to_dict()
    assert r["player_id"] == "0000"
    assert r["player_name"] == "Player ATL 0" and r["position"] == "RB" and r["team_id"] == "ATL"
    assert r["injury_status"] == "Questionable" and r["injury_type"] == "Hamstring" and r["return_date"] == "2026-11-05"
    assert r["game_status"] == "Questionable" and r["date_reported"] == "2026-10-29T14:00Z" and r["confidence"] == 65
    bare = svc.parse_team_injury_list({"injuries": [{"displayName": "Atlanta Falcons", "injuries": [
        item("77", status="Injured Reserve", details=False), {"athlete": {"displayName": "No Id"}}]}]})
    assert len(bare["ATL"]) == 1 and bare["ATL"][0].injury_status == "IR"
    assert svc.parse_team_injury_list(None) == {} and svc.parse_team_injury_list({"injuries": "x"}) == {}


@pytest.mark.asyncio
async def test_the_team_list_replaces_the_athlete_page_crawl_and_marks_a_team_at_the_cap_partial(monkeypatch):
    client = Client(Resp(200, payload(sizes={"ATL": svc.TEAM_LIST_CAP, "LAR": 0})))

    async def never(self, team, headers):
        raise AssertionError(f"the athlete pages were fetched for {team}")
    monkeypatch.setattr(svc.InjuryAggregator, "_fetch_team_espn_injuries", never)
    agg = svc.InjuryAggregator(http_client=client, persist=False)
    got = await agg.fetch_espn_injuries(["ATL", "LAR", "PIT"])
    assert len(client.urls) == 1 and client.urls[0] == svc.TEAM_LIST_URL            # one request for the whole league
    assert len(got) == svc.TEAM_LIST_CAP + 0 + 3
    assert agg.complete_teams == {"LAR", "PIT"} and agg.empty_teams == {"LAR"}      # ATL is at the cap: not complete
    assert agg.team_list_truncated == ["ATL"] and agg.team_list_seconds is not None


@pytest.mark.asyncio
async def test_it_falls_back_to_the_athlete_pages_when_the_list_is_off_missing_or_not_the_leagues(monkeypatch):
    crawled = []

    async def per_team(self, team, headers):
        crawled.append(team)
        return []
    monkeypatch.setattr(svc.InjuryAggregator, "_fetch_team_espn_injuries", per_team)
    for resp in (Resp(404, {}), Resp(200, {"injuries": []}), Resp(200, payload(only=["ATL", "LAR"]))):
        crawled.clear()
        await svc.InjuryAggregator(http_client=Client(resp), persist=False).fetch_espn_injuries(["ATL", "LAR"])
        assert sorted(crawled) == ["ATL", "LAR"]

    class Boom:
        async def get(self, url, **kw):
            raise TimeoutError("slow")
    crawled.clear()
    await svc.InjuryAggregator(http_client=Boom(), persist=False).fetch_espn_injuries(["ATL"])
    assert crawled == ["ATL"]
    monkeypatch.setenv("NFL_MCP_INJURY_TEAM_LIST", "0")
    crawled.clear()
    client = Client(Resp(200, payload()))
    await svc.InjuryAggregator(http_client=client, persist=False).fetch_espn_injuries(["ATL"])
    assert crawled == ["ATL"] and client.urls == []


# ------------------------------------------------------------------ what the check-in says
def stored(team, at, status="Questionable"):
    return {"team_id": team, "updated_at": at.isoformat(), "injury_status": status, "player_id": team}


def test_the_check_in_says_when_the_injuries_were_last_refreshed_for_how_many_teams():
    at = utc(2026, 10, 29, 22, 12)                                    # 18:12 ET
    db = FakeDb([stored("ATL", at), stored("LAR", at - timedelta(minutes=2)), stored("PIT", at - timedelta(hours=9))])
    f = w.freshness(db, ["ATL", "LAR", "PIT", "SF"], now=at)
    assert f["line"].startswith("injuries last refreshed at Thu 2026-10-29 18:12 ET (2 teams); 2 of 4 roster team(s) "
                                "have older or no stored reports: PIT, SF.")
    assert f["teams"] == 2 and f["teams_older"] == ["PIT", "SF"] and f["crawl_running"] is False
    assert f["line"].endswith("No injury crawl is running now.")
    clean = w.freshness(FakeDb([stored("ATL", at), stored("LAR", at)]), ["ATL", "LAR"], now=at)["line"]
    assert clean.startswith("injuries last refreshed at Thu 2026-10-29 18:12 ET (2 teams). No injury crawl")
    none = w.freshness(FakeDb(), ["ATL"], now=at)["line"]
    assert none.startswith("injuries last refreshed at never (0 teams)")


@pytest.mark.asyncio
async def test_the_check_in_also_says_whether_a_crawl_is_running_and_what_the_warmer_did(monkeypatch):
    at = utc(2026, 10, 29, 22, 12)
    gate = asyncio.Event()

    async def on_demand():
        await gate.wait()
    single_flight("injury_reports:ATL", on_demand)
    f = w.freshness(FakeDb([stored("ATL", at)]), ["ATL"], now=at)
    assert f["crawl_running"] is True and "An injury crawl is running now." in f["line"]
    gate.set()
    await asyncio.sleep(0)
    await w.warm_once(FakeDb(), ["ATL"], crawl=crawler([], fail="ESPN down"), now=at)
    f = w.freshness(FakeDb([stored("ATL", at)]), ["ATL"], now=at)
    assert "The last background refresh failed: RuntimeError: ESPN down." in f["line"]
    assert f["warmer"]["last_started"] == at.isoformat() and f["warmer"]["next_refresh_at"] == (at + timedelta(minutes=30)).isoformat()
    monkeypatch.setenv("NFL_MCP_INJURY_WARM", "0")
    assert "The background warmer is off." in w.freshness(FakeDb(), ["ATL"], now=at)["line"]


# ------------------------------------------------------------------ started with the server
@pytest.mark.asyncio
async def test_the_server_starts_the_warmer_with_its_lifespan_and_stops_it_at_shutdown(monkeypatch):
    from unittest.mock import MagicMock

    from nfl_mcp import server
    seen = {}

    async def fake_loop(db, provider, shutdown, **kw):
        seen["args"] = (db, provider)
        seen["running"] = True
        await shutdown.wait()
        seen["running"] = False
    monkeypatch.setattr(w, "run_loop", fake_loop)
    db = MagicMock()
    async with server._create_prefetch_lifespan(db)(None):
        await asyncio.sleep(0.05)
        assert seen["running"] is True and seen["args"] == (db, w.yahoo_roster_teams)   # started with the server
    assert seen["running"] is False and server._warmer_task is None                    # and stopped with it

    seen.clear()
    monkeypatch.setenv("NFL_MCP_INJURY_WARM", "0")
    async with server._create_prefetch_lifespan(db)(None):
        await asyncio.sleep(0.02)
    assert seen == {}


@pytest.mark.asyncio
async def test_your_rosters_and_your_opponents_teams_are_what_it_warms(monkeypatch):
    class Prov:
        class data:
            @staticmethod
            async def matchup(week, mid):
                return {"home": {"team": {"mid": "9", "name": "Mine"}, "starters": [], "total_proj": 100},
                        "away": {"team": {"mid": "5", "name": "Theirs"}, "starters": [], "total_proj": 90}}

        async def get_projection_inputs(self, mid):
            rows = {"9": [("ATL", "RB"), ("LAR", "WR"), ("WAS", "DEF"), ("", "WR")],
                    "5": [("PIT", "QB"), ("ATL", "TE")]}[str(mid)]
            return [{"team": t, "position": p} for t, p in rows]

    async def get_provider(league):
        return Prov()

    async def season_week(*a, **k):
        return 2026, 8, None
    monkeypatch.setenv("NFL_MCP_DEFAULT_LEAGUE", "yahoo:1")
    monkeypatch.setenv("NFL_MCP_MY_ROSTER", "9")
    monkeypatch.setattr("nfl_mcp.yahoo.runtime.get_provider", get_provider)
    monkeypatch.setattr("nfl_mcp.week_context.resolve_season_week", season_week)
    assert await w.yahoo_roster_teams() == ["ATL", "LAR", "PIT"]          # a defense is not an injury team; a free agent has none
    monkeypatch.delenv("NFL_MCP_MY_ROSTER")
    assert await w.yahoo_roster_teams() == []
