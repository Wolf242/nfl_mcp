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
