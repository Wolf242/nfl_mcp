"""Per-user defaults read from the environment, so no league or team id lives in the repo.

``NFL_MCP_DEFAULT_LEAGUE``  the league tools use when ``league_id`` is omitted (e.g. ``yahoo:1234567``)
``NFL_MCP_MY_ROSTER``       your roster / team id in that league (e.g. ``9``)
"""
from __future__ import annotations

import os

ENV_LEAGUE = "NFL_MCP_DEFAULT_LEAGUE"
ENV_ROSTER = "NFL_MCP_MY_ROSTER"


def _env(name: str) -> str | None:
    v = (os.getenv(name) or "").strip()
    return v or None


def resolve_league(league_id: str | None = None) -> str | None:
    """The explicit league id, else ``NFL_MCP_DEFAULT_LEAGUE``, else None."""
    explicit = (str(league_id).strip() if league_id not in (None, "") else None)
    return explicit or _env(ENV_LEAGUE)


def resolve_roster(roster_id: int | str | None = None) -> str | None:
    """The explicit roster/team id, else ``NFL_MCP_MY_ROSTER``, else None."""
    explicit = (str(roster_id).strip() if roster_id not in (None, "") else None)
    return explicit or _env(ENV_ROSTER)


def missing_league_error() -> dict:
    return {"success": False,
            "error": f"No league: pass league_id or set {ENV_LEAGUE} (for example yahoo:1234567)."}


def missing_roster_error() -> dict:
    return {"success": False,
            "error": f"No roster: pass roster_id (your team number) or set {ENV_ROSTER}."}


def league_and_roster(league_id: str | None, roster_id: int | str | None,
                      user_id: str | None = None) -> tuple[str | None, int | None]:
    """Fill an omitted league and roster id from the environment.

    The roster default is not used when ``user_id`` identifies the team another way.
    """
    lid = resolve_league(league_id)
    if roster_id not in (None, ""):
        return lid, int(roster_id)
    if user_id not in (None, ""):
        return lid, None
    r = resolve_roster(None)
    return lid, (int(r) if r and r.isdigit() else None)
