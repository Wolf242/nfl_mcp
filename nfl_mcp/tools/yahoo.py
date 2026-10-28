"""Yahoo league + social-news tools. READ-ONLY: no add / drop / claim / trade / lineup code path.

MCP tool wrappers; registered by ``nfl_mcp.tool_registry``.
"""
from __future__ import annotations

from ..config import validate_string_input
from ..metrics import timing_decorator
from ..yahoo import is_yahoo_league, news_social
from ..yahoo.runtime import get_provider
from ..yahoo.scoring import BFS_SCORING
from ..yahoo.waiver_model import plan_sequence


def _bad(league_id: str):
    return {"success": False, "error": "Use a Yahoo league id like 'yahoo:1234567'"}


@timing_decorator("get_yahoo_waiver_order", tool_type="yahoo")
async def get_yahoo_waiver_order(league_id: str) -> dict:
    """Current waiver order for a Yahoo league (1 = first claim) and how the rolling list works.

    BFS uses one continual rolling list: every successful claim (contested or not) sends the
    winner to the bottom; a failed claim costs nothing; free-agent adds and drops do not move
    anyone. The priority shown on Yahoo's Waiver Offers page is the position when that claim
    was processed.
    """
    if not is_yahoo_league(league_id):
        return _bad(league_id)
    league_id = validate_string_input(league_id, "yahoo_league_id", max_length=30, required=True)
    order = await (await get_provider(league_id)).get_waiver_order()
    return {"success": True, "order": [{"priority": t["waiver"], "team": t["name"], "mid": t["mid"]} for t in order],
            "rule": "rolling list; win -> bottom; failed claim free; FA adds/drops do not change order"}


@timing_decorator("plan_yahoo_waiver_sequence", tool_type="yahoo")
async def plan_yahoo_waiver_sequence(league_id: str, my_mid: str, wishlist: list[dict],
                                     contested: list[str] | None = None) -> dict:
    """Rank claims for the rolling list. ``wishlist`` items: ``{player, value, drop}``.

    Only the first win uses your current spot; after it you are last, so later claims win only
    if nobody else wants them. Contested claims go first, then the rest by value. Advisory
    only - this tool never submits anything to Yahoo.
    """
    if not is_yahoo_league(league_id):
        return _bad(league_id)
    league_id = validate_string_input(league_id, "yahoo_league_id", max_length=30, required=True)
    prov = await get_provider(league_id)
    order = [t["mid"] for t in await prov.get_waiver_order()]
    names = {t["mid"]: t["name"] for t in await prov.get_waiver_order()}
    plan = plan_sequence(order, str(my_mid),
                         [(w["player"], float(w.get("value", 0)), w.get("drop")) for w in wishlist],
                         set(contested or []))
    plan["order"] = [names[m] for m in order]
    return {"success": True, **plan}


@timing_decorator("get_yahoo_free_agents", tool_type="yahoo")
async def get_yahoo_free_agents(league_id: str, position: str = "RB", status: str = "A") -> dict:
    """Available players with the W/FA tag: ``W`` + date = waiver claim until then, ``FA`` = instant add."""
    if not is_yahoo_league(league_id):
        return _bad(league_id)
    league_id = validate_string_input(league_id, "yahoo_league_id", max_length=30, required=True)
    position = validate_string_input(position, "position", max_length=5, required=True).upper()
    players = await (await get_provider(league_id)).get_free_agents(position, status)
    return {"success": True, "players": players, "count": len(players)}


@timing_decorator("get_yahoo_transactions", tool_type="yahoo")
async def get_yahoo_transactions(league_id: str, kind: str = "all") -> dict:
    """Yahoo transaction log. ``kind``: ``all`` or ``waiver`` (contested claims with every team's priority)."""
    if not is_yahoo_league(league_id):
        return _bad(league_id)
    league_id = validate_string_input(league_id, "yahoo_league_id", max_length=30, required=True)
    if kind not in ("all", "waiver", "add", "drop", "trade"):
        return {"success": False, "error": "kind must be all|waiver|add|drop|trade"}
    return {"success": True, **await (await get_provider(league_id)).get_transactions(kind)}


@timing_decorator("check_yahoo_scoring", tool_type="yahoo")
async def check_yahoo_scoring(league_id: str) -> dict:
    """Compare the league's live scoring settings with the verified BFS constants and report any change."""
    if not is_yahoo_league(league_id):
        return _bad(league_id)
    league_id = validate_string_input(league_id, "yahoo_league_id", max_length=30, required=True)
    league = await (await get_provider(league_id)).get_league()
    meta = league["metadata"]
    return {"success": True, "source": meta["scoring_source"],
            "differences_from_bfs": meta["scoring_diff_from_bfs"], "bfs_constants": len(BFS_SCORING)}


@timing_decorator("get_yahoo_projection_comparison", tool_type="yahoo")
async def get_yahoo_projection_comparison(league_id: str, my_mid: str, week: int | None = None) -> dict:
    """Your Yahoo roster with Yahoo's projection next to nfl_mcp's own, and the gap between them.

    Comparison only: Yahoo's numbers are not fed into nfl_mcp's projections or the lineup
    optimizer, which keep using their own. ``our_projection`` is the Sleeper-first blend every other
    lineup tool uses (``model_projection`` and ``sleeper_projection`` are its parts); ``gap`` is
    ours minus Yahoo's. Yahoo's figure is the one its roster page shows, i.e. for the current
    week, so ``week`` should be left unset. Large gaps are worth a second look, not an override.
    """
    if not is_yahoo_league(league_id):
        return _bad(league_id)
    league_id = validate_string_input(league_id, "yahoo_league_id", max_length=30, required=True)
    if not str(my_mid).isdigit():
        return {"success": False, "error": "my_mid is your Yahoo team number, e.g. '9'"}
    rows = await (await get_provider(league_id)).get_projection_inputs(str(my_mid))
    live = [r for r in rows if r["slot"] != "IR" and r.get("team") and r.get("position")]
    from ..projections import project_players
    res = await project_players(
        [{"name": r["name"], "team": r["team"], "position": r["position"], "player_id": r["player_id"]}
         for r in live], league_id=league_id, week=week)
    ours = {p.get("player"): p for p in res.get("projections") or []}
    out, warnings = [], list(res.get("warnings") or [])
    for r in rows:
        p = ours.get(r["name"]) if r in live else None
        wk = res.get("week")
        # Yahoo's own bye week settles it even when nfl_mcp has no schedule cached for the week.
        on_bye = bool((p or {}).get("on_bye")) or (r.get("bye_week") is not None and wk is not None
                                                    and int(r["bye_week"]) == int(wk))
        mine = None if on_bye else (p.get("projected_points") if p else None)
        theirs = None if on_bye else r["yahoo_projection"]
        out.append({
            "slot": r["slot"], "name": r["name"], "team": r["team"], "position": r["position"],
            "injury": r["injury"], "on_bye": on_bye, "yahoo_projection": theirs, "our_projection": mine,
            "model_projection": p.get("model_projection") if p else None,
            "sleeper_projection": p.get("sleeper_projection") if p else None,
            "projection_source": p.get("projection_source") if p else None,
            "gap": round(mine - theirs, 1) if mine is not None and theirs is not None else None,
        })
    started = [x for x in out if x["slot"] not in ("BN", "IR")]
    both = [x for x in started if x["gap"] is not None]
    if res.get("success") is False:
        warnings.append(res.get("error") or "nfl_mcp projections unavailable")
    return {
        "success": True, "week": res.get("week"), "players": out,
        "starter_totals": {"yahoo": round(sum(x["yahoo_projection"] for x in both), 1),
                           "ours": round(sum(x["our_projection"] for x in both), 1),
                           "players_compared": len(both), "starters": len(started)},
        "biggest_gaps": sorted((x for x in both), key=lambda x: -abs(x["gap"]))[:3],
        "note": "Comparison only - the lineup optimizer does not use Yahoo's projections.",
        "warnings": warnings,
    }


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
