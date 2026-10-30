"""Keeps the injury cache warm in the background, so a check-in reads fresh reports instead of waiting
on a crawl.

* At server start the crawl for the teams on your Yahoo roster and your opponent's starts at once.
* It repeats every 30 minutes Thursday to Monday (US Eastern) and every 2 hours on Tuesday and
  Wednesday (:func:`interval_seconds`).
* Never two at once: a refresh is skipped while any injury crawl is running, the warmer's own or a
  tool call's (:func:`crawl_running`).
* The crawl uses ESPN's league-wide team list when it can (see ``injury_service.TEAM_LIST_URL``), so
  it takes seconds.

Read-only toward Yahoo (it reads the two rosters). It writes only nfl_mcp's own injury table, the
same way the prefetch does.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .teams import normalize_team
from .upstream import inflight_keys, single_flight

logger = logging.getLogger(__name__)

WARM_KEY = "injury_reports:warm"          # the warmer's own crawl; every crawl key starts with CRAWL_PREFIX
CRAWL_PREFIX = "injury_reports:"
GAME_DAYS = (3, 4, 5, 6, 0)               # Thursday to Monday, US Eastern
GAME_INTERVAL_SECONDS = int(os.getenv("NFL_MCP_INJURY_WARM_GAME_INTERVAL", "1800"))
OFF_INTERVAL_SECONDS = int(os.getenv("NFL_MCP_INJURY_WARM_OFF_INTERVAL", "7200"))
CHECK_STEP_SECONDS = 60.0                 # how often the loop looks at the clock while it waits
SAME_REFRESH_WINDOW = timedelta(minutes=10)  # reports stamped this close together are one refresh


def warm_enabled() -> bool:
    return os.getenv("NFL_MCP_INJURY_WARM", "1") != "0"


def interval_seconds(now: datetime) -> int:
    """Seconds between refreshes at ``now``: 30 minutes Thursday to Monday, 2 hours Tuesday and Wednesday (US Eastern)."""
    from .practice_reports import to_eastern
    return GAME_INTERVAL_SECONDS if to_eastern(now).weekday() in GAME_DAYS else OFF_INTERVAL_SECONDS


def is_due(last_started: datetime | None, now: datetime) -> bool:
    """Whether a refresh is due: none yet, or the interval for ``now`` has passed since the last one started."""
    return last_started is None or (now - last_started).total_seconds() >= interval_seconds(now)


def next_due_at(last_started: datetime | None, now: datetime) -> datetime:
    return now if last_started is None else last_started + timedelta(seconds=interval_seconds(now))


def crawl_running() -> bool:
    """Whether any injury crawl is running now: the warmer's, or a tool call's."""
    return bool(inflight_keys(CRAWL_PREFIX))


@dataclass
class WarmState:
    runs: int = 0
    skipped: int = 0
    last_started: datetime | None = None
    last_finished: datetime | None = None
    last_ok: datetime | None = None
    last_error: str | None = None
    teams: list[str] = field(default_factory=list)
    reports: int = 0
    seconds: float | None = None
    truncated_teams: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"runs": self.runs, "skipped": self.skipped,
                "last_started": self.last_started.isoformat() if self.last_started else None,
                "last_ok": self.last_ok.isoformat() if self.last_ok else None,
                "last_error": self.last_error, "teams": list(self.teams), "reports": self.reports,
                "seconds": self.seconds, "teams_at_report_cap": list(self.truncated_teams)}


state = WarmState()


async def _crawl_and_store(db, teams: list[str], crawl: Callable[..., Awaitable[tuple]], now: datetime) -> dict:
    state.last_started, state.runs = now, state.runs + 1
    began = time.monotonic()
    try:
        injuries, prunable = await crawl(teams, db)
        written = await asyncio.to_thread(db.upsert_injuries, injuries, prune_missing=True, complete_teams=prunable)
    except Exception as e:  # the next refresh tries again; the stored reports stay as they were
        state.last_error = f"{type(e).__name__}: {e}"
        state.seconds = round(time.monotonic() - began, 1)
        state.last_finished = datetime.now(UTC)
        logger.warning(f"[InjuryWarmer] refresh failed: {state.last_error}")
        return {"status": "error", "error": state.last_error}
    state.last_error = None
    state.seconds = round(time.monotonic() - began, 1)
    state.last_finished = state.last_ok = datetime.now(UTC)
    state.teams, state.reports = sorted(teams), len(injuries)
    state.truncated_teams = sorted(t for t in teams if t not in prunable)
    logger.info(f"[InjuryWarmer] refreshed {len(teams)} teams: {len(injuries)} reports, {written} written, "
                f"{state.seconds}s")
    return {"status": "ok", "teams": len(teams), "fetched": len(injuries), "written": written, "seconds": state.seconds}


async def warm_once(db, teams: list[str], *, crawl: Callable[..., Awaitable[tuple]] | None = None,
                    now: datetime | None = None) -> dict:
    """One refresh of ``teams``, unless an injury crawl is already running (then ``{"status": "skipped"}``).

    The check and the start happen with no await between them, so two callers cannot both start one.
    """
    if crawl is None:
        from .injury_service import crawl_injury_reports as crawl
    running = inflight_keys(CRAWL_PREFIX)
    if running:
        state.skipped += 1
        return {"status": "skipped", "reason": "an injury crawl is already running", "running": running}
    task = single_flight(WARM_KEY, lambda: _crawl_and_store(db, sorted(teams), crawl, now or datetime.now(UTC)))
    return await task


async def yahoo_roster_teams() -> list[str]:
    """The NFL teams of your Yahoo roster and this week's opponent's. ``[]`` when no Yahoo team is configured."""
    from .defaults import resolve_league, resolve_roster
    from .week_context import resolve_season_week
    from .yahoo import is_yahoo_league
    from .yahoo.checkin import _opponent
    from .yahoo.runtime import get_provider

    league, mid = resolve_league(None), resolve_roster(None)
    if not league or not mid or not is_yahoo_league(league) or not str(mid).isdigit():
        return []

    def teams_of(rows: list[dict]) -> set[str]:
        return {t for t in (normalize_team(r.get("team")) for r in rows if r.get("position") != "DEF") if t}

    prov = await get_provider(league)
    teams = teams_of(await prov.get_projection_inputs(str(mid)))
    try:
        _, week, _ = await resolve_season_week(None, None)
        opponent, _ = _opponent(await prov.data.matchup(int(week), str(mid)), str(mid))
        if opponent:
            teams |= teams_of(await prov.get_projection_inputs(str(opponent["mid"])))
    except Exception as e:  # the roster alone is still worth warming
        logger.info(f"[InjuryWarmer] opponent's teams unavailable ({e}); warming your roster's teams only")
    return sorted(teams)


async def _wait(shutdown_event: asyncio.Event, seconds: float) -> bool:
    """Sleep up to ``seconds``; True when shutdown was signalled meanwhile."""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
        return True
    except TimeoutError:
        return False


async def run_loop(db, teams_provider: Callable[[], Awaitable[list[str]]], shutdown_event: asyncio.Event, *,
                   crawl: Callable[..., Awaitable[tuple]] | None = None,
                   clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                   wait: Callable[[asyncio.Event, float], Awaitable[bool]] = _wait,
                   step: float = CHECK_STEP_SECONDS) -> None:
    """Refresh at start, then whenever :func:`is_due`, until ``shutdown_event`` is set. Never raises."""
    if not warm_enabled():
        logger.info("[InjuryWarmer] off (NFL_MCP_INJURY_WARM=0)")
        return
    from .injury_service import InjuryAggregator

    teams: list[str] = []
    while not shutdown_event.is_set():
        now = clock()
        if is_due(state.last_started, now):
            try:
                found = await teams_provider()
                if found:
                    teams = found
                elif not teams:
                    logger.info("[InjuryWarmer] no Yahoo roster configured; nothing to warm")
                    return
            except Exception as e:
                logger.warning(f"[InjuryWarmer] could not read your roster's teams ({type(e).__name__}: {e})")
                teams = teams or list(InjuryAggregator.NFL_TEAMS)
            try:
                result = await warm_once(db, teams, crawl=crawl, now=now)
                if result["status"] == "skipped":
                    # keep the start time where it was: due again at the next look, once the other crawl is done
                    logger.info(f"[InjuryWarmer] skipped: {result['reason']}")
            except Exception as e:
                logger.warning(f"[InjuryWarmer] refresh error: {e}")
                state.last_started = now  # do not retry in a tight loop
        if await wait(shutdown_event, step):
            return


def freshness(db, teams: list[str], now: datetime | None = None) -> dict:
    """What a check-in says about its injury data: when it was last refreshed and for how many teams, and
    whether a crawl is running now. From the stored reports (their own stamps), so it is true however they
    got there."""
    from .practice_reports import to_eastern
    now = now or datetime.now(UTC)
    wanted = sorted({t for t in (normalize_team(x) for x in teams) if t})
    newest: dict[str, datetime] = {}
    try:
        for row in (db.get_all_current_injuries() if db is not None else []) or []:
            team, stamp = normalize_team(row.get("team_id")), row.get("updated_at")
            if team in wanted and stamp:
                try:
                    at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                except ValueError:
                    continue
                at = at if at.tzinfo else at.replace(tzinfo=UTC)
                if team not in newest or at > newest[team]:
                    newest[team] = at
    except Exception as e:
        logger.debug(f"[InjuryWarmer] stored reports unavailable: {e}")
    last = max(newest.values()) if newest else None
    same = sorted(t for t, at in newest.items() if last and last - at <= SAME_REFRESH_WINDOW)
    older = sorted(t for t in wanted if t not in same)
    running = crawl_running()
    out: dict[str, Any] = {
        "last_refreshed_at": last.isoformat() if last else None,
        "last_refreshed_et": to_eastern(last).strftime("%a %Y-%m-%d %H:%M ET") if last else None,
        "teams": len(same), "teams_wanted": len(wanted), "teams_older": older,
        "crawl_running": running,
        "warmer": {"enabled": warm_enabled(), **state.as_dict(),
                   "next_refresh_at": (next_due_at(state.last_started, now).isoformat()
                                       if warm_enabled() and state.last_started else None)}}
    if last:
        line = f"injuries last refreshed at {out['last_refreshed_et']} ({len(same)} teams)"
        if older:
            line += f"; {len(older)} of {len(wanted)} roster team(s) have older or no stored reports: {', '.join(older)}"
    else:
        line = "injuries last refreshed at never (0 teams): no stored injury reports for these teams"
    line += ". " + ("An injury crawl is running now." if running else "No injury crawl is running now.")
    w = out["warmer"]
    if not w["enabled"]:
        line += " The background warmer is off."
    elif w["last_error"]:
        line += f" The last background refresh failed: {w['last_error']}."
    out["line"] = line
    return out
