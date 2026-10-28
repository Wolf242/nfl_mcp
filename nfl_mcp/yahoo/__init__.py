"""Yahoo Fantasy provider for nfl_mcp (READ-ONLY).

Adds a ``yahoo:<league_id>`` league id that the Sleeper-shaped tools accept. The
provider emits the same shapes Sleeper's endpoints do (league settings, rosters,
matchups, transactions), so the lineup / waiver / opponent tools run unchanged.

Safety contract (enforced in :mod:`nfl_mcp.yahoo.guard`): this package only ever
issues GET requests to an allow-list of Yahoo *read* pages / API collections. It
has no add, drop, claim, trade or lineup-edit code path at all.
"""
from __future__ import annotations

YAHOO_PREFIX = "yahoo:"


def is_yahoo_league(league_id: object) -> bool:
    """True for ids like ``yahoo:1234567`` (the only way a Yahoo league is addressed)."""
    return isinstance(league_id, str) and league_id.lower().startswith(YAHOO_PREFIX)


def yahoo_league_number(league_id: str) -> str:
    """``yahoo:1234567`` -> ``1234567``."""
    return league_id.split(":", 1)[1].strip()
