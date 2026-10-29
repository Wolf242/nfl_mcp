"""Yahoo player id -> Sleeper player id.

nfl_mcp keys everything (enrichment, injuries, projections, news) by Sleeper player id,
and Sleeper's player dump carries each player's ``yahoo_id``. Map through that first,
fall back to a unique name+team+position match, and give team defenses their team
abbreviation (Sleeper's DEF ids are the abbreviation, e.g. ``"HOU"``).
"""
from __future__ import annotations

import re
from collections.abc import Mapping

# Yahoo's short team codes that differ from Sleeper / nflverse.
_TEAM_FIX = {"WAS": "WAS", "WSH": "WAS", "JAC": "JAX", "LA": "LAR", "ARZ": "ARI",
             "BLT": "BAL", "CLV": "CLE", "HST": "HOU", "SD": "LAC", "OAK": "LV", "STL": "LAR"}
_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?", re.I)


def norm_team(t: str | None) -> str | None:
    if not t:
        return None
    t = t.upper()
    return _TEAM_FIX.get(t, t)


def norm_name(n: str | None) -> str:
    n = _SUFFIX.sub("", (n or "").lower())
    return re.sub(r"[^a-z]", "", n)


class IdMap:
    """Built once from Sleeper's ``players/nfl`` dump (a ``{sleeper_id: {...}}`` mapping)."""

    def __init__(self, sleeper_players: Mapping[str, Mapping]):
        self.by_yahoo: dict[str, str] = {}
        self.by_name: dict[tuple[str, str | None, str | None], list[str]] = {}
        self.names: dict[str, str] = {}
        for sid, p in (sleeper_players or {}).items():
            if not isinstance(p, Mapping):
                continue
            y = p.get("yahoo_id")
            if y not in (None, ""):
                self.by_yahoo[str(y)] = str(sid)
            full = p.get("full_name") or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)
            if full:
                self.names[str(sid)] = full
                key = (norm_name(full), norm_team(p.get("team")), (p.get("position") or None))
                self.by_name.setdefault(key, []).append(str(sid))

    def resolve(self, yahoo_id: str | None, name: str | None, team: str | None,
                position: str | None) -> tuple[str | None, str]:
        """Return ``(sleeper_id, how)``; ``how`` is ``yahoo_id|name|dst|unmapped``."""
        pos = (position or "").upper()
        if pos in ("DEF", "DST"):
            t = norm_team(team)
            return (t, "dst") if t else (None, "unmapped")
        if yahoo_id and str(yahoo_id) in self.by_yahoo:
            return self.by_yahoo[str(yahoo_id)], "yahoo_id"
        if name:
            first = pos.split("/")[0] if pos else None
            hits = self.by_name.get((norm_name(name), norm_team(team), first)) or []
            if len(hits) == 1:
                return hits[0], "name"
        return None, "unmapped"


def player_label(name: str | None, team: str | None, position: str | None) -> str:
    """``"Full Name (TEAM POS)"``: the one way a player is named in a report.

    A surname alone is ambiguous (two Warrens, two Robinsons on one page), so no report line
    prints one without the team and position that tell them apart.
    """
    who = (name or "").strip() or "Unknown player"
    where = " ".join(x for x in ((norm_team(team) or "").strip(), (position or "").upper().strip()) if x)
    return f"{who} ({where})" if where else who


def player_key(p: Mapping) -> str:
    """Internal identity of a player row: the Sleeper id when mapped, else name+team+position.

    Never the surname or the display name alone: two players can share either.
    """
    pid = p.get("player_id")
    if pid not in (None, "", "0"):
        return f"id:{pid}"
    return "np:" + "|".join((norm_name(p.get("name")), norm_team(p.get("team")) or "", (p.get("position") or "").upper()))
