"""Yahoo league + social-news tools. READ-ONLY: no add / drop / claim / trade / lineup code path.

MCP tool wrappers; registered by ``nfl_mcp.tool_registry``. ``league_id`` (and your team number
where one is needed) may be omitted when ``NFL_MCP_DEFAULT_LEAGUE`` / ``NFL_MCP_MY_ROSTER`` are set.
"""
from __future__ import annotations

import functools

from ..config import validate_string_input
from ..defaults import (
    missing_league_error,
    missing_roster_error,
    resolve_league,
    resolve_roster,
)
from ..metrics import timing_decorator
from ..yahoo import is_yahoo_league, news_social
from ..yahoo.runtime import get_provider
from ..yahoo.scoring import BFS_SCORING
from ..yahoo.sources import YahooBlocked, YahooLoginRequired
from ..yahoo.waiver_model import plan_sequence


def _bad(league_id: str):
    return {"success": False, "error": "Use a Yahoo league id like 'yahoo:1234567'"}


def _clean_errors(fn):
    """Return a Yahoo block or an expired sign-in as an error message, not a stack trace."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (YahooBlocked, YahooLoginRequired) as e:
            return {"success": False, "error": str(e)}
    return wrapper


def _league(league_id: str | None) -> tuple[str | None, dict | None]:
    """(validated Yahoo league id, None) or (None, error response). Falls back to the env default."""
    lid = resolve_league(league_id)
    if lid is None:
        return None, missing_league_error()
    if not is_yahoo_league(lid):
        return None, _bad(lid)
    return validate_string_input(lid, "yahoo_league_id", max_length=30, required=True), None


def _mid(my_mid: str | int | None) -> tuple[str | None, dict | None]:
    m = resolve_roster(my_mid)
    if m is None:
        return None, missing_roster_error()
    if not m.isdigit():
        return None, {"success": False, "error": "Your Yahoo team number must be digits, e.g. '9'"}
    return m, None


@timing_decorator("get_yahoo_waiver_order", tool_type="yahoo")
@_clean_errors
async def get_yahoo_waiver_order(league_id: str | None = None) -> dict:
    """Current waiver order for a Yahoo league (1 = first claim) and how the rolling list works.

    BFS uses one continual rolling list: every successful claim (contested or not) sends the
    winner to the bottom; a failed claim costs nothing; free-agent adds and drops do not move
    anyone. The priority shown on Yahoo's Waiver Offers page is the position when that claim
    was processed. ``league_id`` defaults to NFL_MCP_DEFAULT_LEAGUE.
    """
    league_id, err = _league(league_id)
    if err:
        return err
    order = await (await get_provider(league_id)).get_waiver_order()
    return {"success": True, "order": [{"priority": t["waiver"], "team": t["name"], "mid": t["mid"]} for t in order],
            "rule": "rolling list; win -> bottom; failed claim free; FA adds/drops do not change order"}


@timing_decorator("plan_yahoo_waiver_sequence", tool_type="yahoo")
@_clean_errors
async def plan_yahoo_waiver_sequence(league_id: str | None = None, my_mid: str | None = None,
                                     wishlist: list[dict] | None = None,
                                     contested: list[str] | None = None) -> dict:
    """Rank claims for the rolling list. ``wishlist`` items: ``{player, value, drop}``.

    Only the first win uses your current spot; after it you are last, so later claims win only
    if nobody else wants them. Contested claims go first, then the rest by value. Advisory
    only - this tool never submits anything to Yahoo. ``league_id`` / ``my_mid`` default to
    NFL_MCP_DEFAULT_LEAGUE / NFL_MCP_MY_ROSTER.
    """
    league_id, err = _league(league_id)
    if err:
        return err
    my_mid, err = _mid(my_mid)
    if err:
        return err
    if not wishlist:
        return {"success": False, "error": "wishlist is required: [{player, value, drop}, ...]"}
    prov = await get_provider(league_id)
    order = [t["mid"] for t in await prov.get_waiver_order()]
    names = {t["mid"]: t["name"] for t in await prov.get_waiver_order()}
    plan = plan_sequence(order, str(my_mid),
                         [(w["player"], float(w.get("value", 0)), w.get("drop")) for w in wishlist],
                         set(contested or []))
    plan["order"] = [names[m] for m in order]
    return {"success": True, **plan}


@timing_decorator("get_yahoo_free_agents", tool_type="yahoo")
@_clean_errors
async def get_yahoo_free_agents(league_id: str | None = None, position: str = "RB", status: str = "A") -> dict:
    """Available players with the W/FA tag: ``W`` + date = waiver claim until then, ``FA`` = instant add."""
    league_id, err = _league(league_id)
    if err:
        return err
    position = validate_string_input(position, "position", max_length=5, required=True).upper()
    players = await (await get_provider(league_id)).get_free_agents(position, status)
    return {"success": True, "players": players, "count": len(players)}


@timing_decorator("get_yahoo_transactions", tool_type="yahoo")
@_clean_errors
async def get_yahoo_transactions(league_id: str | None = None, kind: str = "all") -> dict:
    """Yahoo transaction log. ``kind``: ``all`` or ``waiver`` (contested claims with every team's priority)."""
    league_id, err = _league(league_id)
    if err:
        return err
    if kind not in ("all", "waiver", "add", "drop", "trade"):
        return {"success": False, "error": "kind must be all|waiver|add|drop|trade"}
    return {"success": True, **await (await get_provider(league_id)).get_transactions(kind)}


@timing_decorator("check_yahoo_scoring", tool_type="yahoo")
@_clean_errors
async def check_yahoo_scoring(league_id: str | None = None) -> dict:
    """Compare the league's live scoring settings with the verified BFS constants and report any change."""
    league_id, err = _league(league_id)
    if err:
        return err
    league = await (await get_provider(league_id)).get_league()
    meta = league["metadata"]
    return {"success": True, "source": meta["scoring_source"],
            "differences_from_bfs": meta["scoring_diff_from_bfs"], "bfs_constants": len(BFS_SCORING)}


@timing_decorator("get_yahoo_projection_comparison", tool_type="yahoo")
@_clean_errors
async def get_yahoo_projection_comparison(league_id: str | None = None, my_mid: str | None = None,
                                          week: int | None = None) -> dict:
    """Your Yahoo roster with Yahoo's projection next to nfl_mcp's own, and the gap between them.

    Comparison only: Yahoo's numbers are not fed into nfl_mcp's projections or the lineup
    optimizer, which keep using their own. ``our_projection`` is the Sleeper-first blend every other
    lineup tool uses (``model_projection`` and ``sleeper_projection`` are its parts); ``gap`` is
    ours minus Yahoo's. Yahoo's figure is the one its roster page shows, i.e. for the current
    week, so ``week`` should be left unset. Large gaps are worth a second look, not an override.
    ``league_id`` / ``my_mid`` default to NFL_MCP_DEFAULT_LEAGUE / NFL_MCP_MY_ROSTER.
    """
    league_id, err = _league(league_id)
    if err:
        return err
    my_mid, err = _mid(my_mid)
    if err:
        return err
    from ..yahoo.compare import compare_projections
    rows = await (await get_provider(league_id)).get_projection_inputs(my_mid)
    cmp = await compare_projections(league_id, rows, week)
    return {
        "success": True, "week": cmp["week"], "players": cmp["players"],
        "starter_totals": cmp["starter_totals"], "biggest_gaps": cmp["biggest_gaps"],
        "note": "Comparison only - the lineup optimizer does not use Yahoo's projections.",
        "warnings": cmp["warnings"],
    }


@timing_decorator("fantasy_checkin", tool_type="yahoo")
@_clean_errors
async def fantasy_checkin(week: int | None = None, league_id: str | None = None,
                          roster_id: str | None = None) -> dict:
    """ONE-CALL WEEKLY CHECK-IN for a Yahoo league. Read-only: nothing is changed on Yahoo.

    ``league_id`` and ``roster_id`` (your team number) default to NFL_MCP_DEFAULT_LEAGUE and
    NFL_MCP_MY_ROSTER; with neither given nor set it returns an error saying so. ``week`` defaults
    to the current NFL week. Returns one structure:
    - league: scoring source and any difference from the verified BFS constants, the waiver order
      and your position
    - roster: every slot with bye week, injury tag, Yahoo projection vs nfl_mcp's and the gap, and
      the starters' team total (a team gap under 12 points is noise)
    - problems: starters on bye, empty slots, starters tagged O/D, IR-slot players whose tag is not
      IR-eligible, a full bench
    - wire: FA / W (date) tags at each position with a bye hole or a problem, each candidate
      projected and ranked with its upgrade over what it replaces, when claims clear, vs_bench
      (each flagged starter's bench replacements compared with the best wire pickup first, both
      limited to the slot he occupies: inside a 1.5-point tie the answer is "no add needed, use
      <bench player>"), holes (starting slots that are empty or held by a player on bye or out,
      each projecting 0), and targets: the best available Yahoo free agent for every hole, then the
      top get_waiver_targets kept only if Yahoo lists them as available, each with Yahoo's own tag
      (a target that cannot be mapped to a Yahoo player is dropped and named in warnings;
      targets_note says why the list is empty or which holes it fills)
    - drop_candidates: the bench ranked by a job test: insurance for a starter's bye or injury in
      the next four weeks (a backup at a single-starter position such as QB always counts, with how
      many free agents project above him), or a path to a bigger role; the drop is whoever does
      neither.
      drop_unknown lists bench players whose projection of 0 comes from an unverified Out
      designation (stale, conflicting or Yahoo-only): they are not ranked
    - close_calls: each starter and same-eligibility bench player whose projections are inside the
      1.5-point tie band: a toss-up, never a pick; compare_players takes it from there
    - opponent: projected points, margin and its reading (a "good matchup" only outside the
      noise band), injured starters, starters on bye
    - byes: the next four weeks: which starters are out (full names), the active players at each
      position against its own slots (QB 1, RB 2, WR 2, TE 1, K 1, DEF 1; thin when fewer), the
      flex as a separate line ("flex: N eligible players left across RB/WR/TE"), and flags
    - verification: each injury tag and bye week checked against nfl_mcp's injury data and the NFL
      schedule: injuries agree (designation, practice and body part all match), partial (the
      differing or missing fields are named), conflict, yahoo_only or other_only; byes agree,
      conflict or unverified. Every injury record shows its report date; one older than 3 days or
      from before this NFL week is stale even when the refresh worked. Players carry stale_injury /
      injury_conflict / yahoo_only / season_long_status / predates_activation flags (a PUP, NFI or
      IR record Yahoo does not show, or one older than the player's activation, never makes him Out).
      Each has a statement to quote: "stored report (dated X, stale: ...) says ...", or, when a
      newer post names him, a labelled single-source lead
    - news: reporter posts naming your players, each a lead; player quotes and predictions are
      dropped first, and posts relayed by one aggregator account count as one independent origin
    - warnings: anything single-source or unverified
    Every player is printed as "Full Name (TEAM POS)" and keyed internally on the player id.
    Yahoo's projections are shown for comparison only and never feed nfl_mcp's own.
    """
    league_id, err = _league(league_id)
    if err:
        return err
    mid, err = _mid(roster_id)
    if err:
        return err
    from .. import injury_service, projections, ros, waiver_target_tools
    from ..database import get_shared_db
    from ..week_context import resolve_season_week
    from ..yahoo.checkin import build_checkin
    from ..yahoo.compare import compare_projections
    season, week, _ = await resolve_season_week(None, week)
    if not week:
        return {"success": False, "error": "Could not determine the NFL week; pass week."}

    db = get_shared_db()

    async def schedules(weeks: list[int]) -> dict:
        return await ros.schedules_for(db, int(season), weeks)

    async def refresh_injuries(teams: list[str]):
        # one retry with a longer wait; the real error and the waits go into the warning
        return await injury_service.get_injury_reports_with_retry(teams=teams, db=db)

    from datetime import UTC, datetime

    from ..practice_reports import practice_week_start, to_eastern
    week_start = practice_week_start(to_eastern(datetime.now(UTC)).date())

    async def project(players: list[dict], **kw) -> dict:
        return await projections.project_players(players, db=db, **kw)

    return await build_checkin(
        await get_provider(league_id), league_id, mid, int(week), compare=compare_projections,
        project=project, targets=waiver_target_tools.get_waiver_targets,
        posts=news_social.get_insider_posts, schedules=schedules if season else None,
        refresh_injuries=refresh_injuries, week_start=week_start)


class _ReadOnlyDb:
    """The shared database as compare_players uses it: reads pass through, and the one thing the
    projection engine would append (``record_projections``, its pre-kickoff log for the weekly retro)
    is skipped, so the comparison leaves nothing behind."""

    def __init__(self, db):
        self._db = db

    def record_projections(self, *args, **kwargs) -> int:
        return 0

    def __getattr__(self, name):
        return getattr(self._db, name)


@timing_decorator("compare_players", tool_type="yahoo")
@_clean_errors
async def compare_players(player_a: str, player_b: str, week: int | None = None,
                          league_id: str | None = None) -> dict:
    """Two players side by side for a start/sit call, scored on BFS. Read-only.

    Names are "Full Name" or "Full Name (TEAM POS)"; a name that fits two players is an error that
    lists them. For each player: the BFS game log this season (per game, mean, median, min, max, last
    three, with the 100/200 and 300/400 yardage bonuses); usage by week (snap share, carry and target
    share, touches, carries inside the 20 and 10 as a share of the team's, red-zone targets) and any
    teammate whose snap share is rising and who takes the goal-line or passing-down work; the game
    (team spread, total, implied total, home/away, kickoff) and which player locks first; the opponent's
    BFS points allowed to the position this and last season with league rank, split into yards and
    touchdowns (and whether the rank follows the noisy touchdowns or the stable yards) and EPA per rush
    allowed; nfl_mcp's projection rescored to BFS beside Yahoo's with the gap; injury records for both
    players and the opposing defense's key starters, each with its report date and stale / conflicting /
    single-source flags; roster fit (slot, eligible alternatives, stacks and hedges in the same game)
    when ``league_id`` (default NFL_MCP_DEFAULT_LEAGUE) is a Yahoo league and NFL_MCP_MY_ROSTER is set.

    The verdict says "toss-up" whenever the projections are within the 1.5-point single-player tie band,
    and then lists the tiebreakers in order (projection, red-zone share, floor and ceiling) with the
    player each favours; a toss-up is never presented as a pick. Stats, snaps, play by play and the
    schedule come from nflverse (key-free, cached per season); nothing is written anywhere.
    """
    from datetime import UTC, datetime

    from .. import injury_service, nflverse_files, player_compare, projections
    from ..database import get_shared_db
    from ..practice_reports import practice_week_start, to_eastern
    from ..week_context import resolve_season_week
    from ..yahoo.compare import _stored_report

    a = validate_string_input(player_a, "player_a", max_length=80, required=True)
    b = validate_string_input(player_b, "player_b", max_length=80, required=True)
    explicit = league_id not in (None, "")
    lid = resolve_league(league_id)
    if explicit and not is_yahoo_league(lid):
        return _bad(lid)
    season, week, _ = await resolve_season_week(None, week)
    if not week:
        return {"success": False, "error": "Could not determine the NFL week; pass week."}
    db = _ReadOnlyDb(get_shared_db())

    notes: list[str] = []
    roster = None
    mid = resolve_roster(None)
    if lid and is_yahoo_league(lid) and mid and mid.isdigit():
        async def roster():
            return await (await get_provider(lid)).get_projection_inputs(mid)
    else:
        notes.append("No Yahoo league and team number (NFL_MCP_DEFAULT_LEAGUE, NFL_MCP_MY_ROSTER): roster fit and "
                     "Yahoo projections are left out.")

    async def project(players, **kw):
        return await projections.project_players(players, db=db, **kw)

    def team_injuries(team: str):
        return db.get_team_injuries_from_cache(team, max_age_hours=injury_service.STORED_FALLBACK_MAX_AGE_HOURS)

    src = player_compare.Sources(
        weekly=nflverse_files.fetch_weekly_rows, snaps=nflverse_files.fetch_snap_counts, pbp=nflverse_files.fetch_pbp,
        games=nflverse_files.fetch_games, project=project, roster=roster,
        stored_report=lambda name, team: _stored_report(db, name, team), team_injuries=team_injuries,
        week_start=practice_week_start(to_eastern(datetime.now(UTC)).date()))
    result = await player_compare.compare_players(a, b, season=int(season), week=int(week), src=src)
    if result.get("success"):
        result["warnings"] = notes + result["warnings"]
    return result


@timing_decorator("verify_news_handles", tool_type="yahoo")
async def verify_news_handles(handles: list[str] | None = None) -> dict:
    """Resolve Bluesky handles to profiles (name, bio, followers) so you can confirm they are the reporters you expect."""
    return {"success": True, **await news_social.verify_handles(handles)}


@timing_decorator("get_insider_posts", tool_type="yahoo")
async def get_insider_posts(handles: list[str] | None = None, hours: int = 24, limit: int = 15,
                            watch_names: list[str] | None = None) -> dict:
    """Latest original posts from NFL reporters on Bluesky. Leads only: single-source, unverified until a second source agrees."""
    hours = max(1, min(int(hours), 168))
    return {"success": True, **await news_social.get_insider_posts(handles, hours, limit, watch_names)}
