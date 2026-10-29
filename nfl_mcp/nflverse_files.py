"""nflverse files the weekly player stats do not cover: snap counts, play by play and the schedule.

Read-only and key-free. Each file is fetched once per season and kept in memory (the current season
expires like the other nflverse caches, see ``matchup_tools.season_cache_fresh``; past seasons are
final). Concurrent callers share one download. A season that is not published yet (404) is ``None``.

The parsers are pure and take the file's text (the play by play also its gzip bytes), so tests feed
them a few lines instead of a download.

* ``snap_counts``: offensive and defensive snap share per player and week. Joined to the weekly
  stats by name and team (the file carries PFR ids, the weekly stats GSIS ids).
* ``pbp``: kept as slim rows (rushes and targets with field position, down and EPA) so a season's
  carries inside the 20 and the 10, red-zone targets and passing-down targets can be summed.
* ``games``: the schedule with spread, total, roof and kickoff, one file for every season.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import logging
from datetime import UTC, datetime
from io import StringIO
from typing import Any

from .config import LONG_TIMEOUT, create_http_client
from .matchup_tools import season_cache_fresh
from .opportunity import OPPORTUNITY_POSITIONS
from .teams import normalize_team

logger = logging.getLogger(__name__)

_RELEASES = "https://github.com/nflverse/nflverse-data/releases/download/"
SNAP_COUNTS_URL = _RELEASES + "snap_counts/snap_counts_{season}.csv"
PBP_URL = _RELEASES + "pbp/play_by_play_{season}.csv.gz"
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"

_cache: dict[tuple[str, int], tuple[datetime, Any]] = {}


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, "", "NA") else None
    except (TypeError, ValueError):
        return None


def _num(v) -> float:
    x = _f(v)
    return 0.0 if x is None else x


def _team(t: str | None) -> str | None:
    t = (t or "").strip().upper()
    return (normalize_team(t) or t) or None


def norm_person(name: str | None) -> str:
    """A name for matching across files: lowercase letters only, no Jr./Sr./II suffix."""
    import re
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", (name or "").lower())
    return re.sub(r"[^a-z]", "", n)


# ------------------------------------------------------------------ weekly player stats
_WEEKLY_NUMERIC = (
    "completions", "attempts", "passing_yards", "passing_tds", "passing_2pt_conversions",
    "sacks_suffered", "sack_fumbles", "sack_fumbles_lost", "carries", "rushing_yards", "rushing_tds",
    "rushing_fumbles", "rushing_fumbles_lost", "rushing_2pt_conversions", "rushing_epa",
    "receptions", "targets", "receiving_yards", "receiving_tds", "receiving_fumbles",
    "receiving_fumbles_lost", "receiving_2pt_conversions", "special_teams_tds", "fumble_recovery_tds",
)


def parse_weekly_rows(text: str) -> list[dict]:
    """Regular-season QB/RB/WR/TE rows of the weekly player stats, every stat the BFS scorer prices.

    ``interceptions`` is the file's ``passing_interceptions`` (the name the scorer reads).
    """
    out = []
    for row in csv.DictReader(StringIO(text)):
        if (row.get("season_type") or "").upper() != "REG":
            continue
        pos = (row.get("position") or row.get("position_group") or "").upper()
        if pos not in OPPORTUNITY_POSITIONS:
            continue
        pid, wk, team = row.get("player_id"), row.get("week"), _team(row.get("team") or row.get("recent_team"))
        if not pid or not wk or not team:
            continue
        r: dict[str, Any] = {
            "player_id": pid, "name": row.get("player_display_name") or row.get("player_name"),
            "position": pos, "team": team, "opponent": _team(row.get("opponent_team")), "week": int(wk),
            "target_share": _f(row.get("target_share")),
            "interceptions": _num(row.get("passing_interceptions") or row.get("interceptions")),
        }
        for col in _WEEKLY_NUMERIC:
            r[col] = _num(row.get(col))
        out.append(r)
    return out


# ------------------------------------------------------------------ snap counts
def parse_snap_counts(text: str) -> list[dict]:
    """Regular-season snap rows: ``{week, team, name, norm, position, off_pct, def_pct}``.

    The shares are 0-1 fractions of the team's snaps that game."""
    out = []
    for row in csv.DictReader(StringIO(text)):
        if (row.get("game_type") or "REG").upper() != "REG":
            continue
        try:
            week = int(row["week"])
        except (KeyError, TypeError, ValueError):
            continue
        team = _team(row.get("team"))
        if not team:
            continue
        out.append({"week": week, "team": team, "name": row.get("player"), "norm": norm_person(row.get("player")),
                    "position": (row.get("position") or "").upper(),
                    "off_pct": _num(row.get("offense_pct")), "def_pct": _num(row.get("defense_pct"))})
    return out


# ------------------------------------------------------------------ play by play
def parse_pbp(raw: bytes | str) -> list[dict]:
    """Slim rows for the rushes and passes of a season's play by play (gzip bytes or text).

    Kneels, spikes and two-point tries are left out. ``kind`` is ``rush`` or ``pass``; a sack is not
    a target. ``yardline_100`` is the distance to the opponent's end zone."""
    text = gzip.decompress(raw).decode("utf-8") if isinstance(raw, bytes) else raw
    out = []
    for row in csv.DictReader(StringIO(text)):
        if (row.get("season_type") or "REG").upper() != "REG" or not row.get("posteam"):
            continue
        if row.get("two_point_attempt") == "1":
            continue
        play_type = (row.get("play_type") or "").lower()
        if play_type == "run" and row.get("rush_attempt") == "1":
            kind = "rush"
        elif play_type == "pass" and row.get("pass_attempt") == "1":
            kind = "pass"
        else:
            continue
        try:
            week = int(row["week"])
        except (KeyError, TypeError, ValueError):
            continue
        down = _f(row.get("down"))
        out.append({
            "week": week, "posteam": _team(row["posteam"]), "defteam": _team(row.get("defteam")), "kind": kind,
            "yardline_100": _f(row.get("yardline_100")), "down": int(down) if down is not None else None,
            "ydstogo": _f(row.get("ydstogo")), "epa": _f(row.get("epa")),
            "rusher_id": row.get("rusher_player_id") or None, "receiver_id": row.get("receiver_player_id") or None,
            "sack": row.get("sack") == "1", "scramble": row.get("qb_scramble") == "1"})
    return out


# ------------------------------------------------------------------ schedule
def parse_games(text: str, season: int) -> dict[tuple[int, str], dict]:
    """``{(week, team): game}`` for a season's regular-season games, once for each side.

    ``team_spread`` is the team's own line (negative = favoured); ``implied_total`` is the team's
    share of the game total at that spread. ``kickoff`` is a naive datetime in Eastern time."""
    out: dict[tuple[int, str], dict] = {}
    for row in csv.DictReader(StringIO(text)):
        if row.get("season") != str(season) or (row.get("game_type") or "").upper() != "REG":
            continue
        home, away = _team(row.get("home_team")), _team(row.get("away_team"))
        try:
            week = int(row["week"])
        except (KeyError, TypeError, ValueError):
            continue
        if not home or not away:
            continue
        spread, total = _f(row.get("spread_line")), _f(row.get("total_line"))
        kickoff = None
        try:
            kickoff = datetime.fromisoformat(f"{row['gameday']}T{row['gametime']}")
        except (KeyError, TypeError, ValueError):
            pass
        base = {"game_id": row.get("game_id"), "week": week, "gameday": row.get("gameday"),
                "gametime": row.get("gametime") or None, "kickoff": kickoff, "home": home, "away": away,
                "spread_line": spread, "total": total, "roof": row.get("roof") or None,
                "surface": row.get("surface") or None, "temp": _f(row.get("temp")), "wind": _f(row.get("wind")),
                "played": bool((row.get("home_score") or "").strip())}
        for team, opp, is_home in ((home, away, True), (away, home, False)):
            sgn = 1.0 if is_home else -1.0
            out[(week, team)] = {
                **base, "team": team, "opponent": opp, "is_home": is_home,
                "team_spread": (-sgn * spread) if spread is not None else None,
                "implied_total": ((total + sgn * spread) / 2.0) if spread is not None and total is not None else None}
    return out


# ------------------------------------------------------------------ fetching
async def _fetch(kind: str, season: int, url: str, parse, *, binary: bool = False) -> Any | None:
    """The parsed file for a season, from the cache or one shared download. None on a 404."""
    hit = _cache.get((kind, season))
    if hit and season_cache_fresh(season, hit[0]):
        return hit[1]
    from .upstream import single_flight

    async def go():
        async with create_http_client(timeout=LONG_TIMEOUT) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            # parsing a season of play by play is CPU work: keep it off the event loop
            parsed = await asyncio.to_thread(parse, resp.content if binary else resp.text)
        _cache[(kind, season)] = (datetime.now(UTC), parsed)
        return parsed
    return await asyncio.shield(single_flight(f"nflverse:{kind}:{season}", go))


async def fetch_snap_counts(season: int) -> list[dict] | None:
    return await _fetch("snaps", season, SNAP_COUNTS_URL.format(season=season), parse_snap_counts)


async def fetch_pbp(season: int) -> list[dict] | None:
    return await _fetch("pbp", season, PBP_URL.format(season=season), parse_pbp, binary=True)


async def fetch_games(season: int) -> dict[tuple[int, str], dict] | None:
    return await _fetch("games", season, GAMES_URL, lambda text: parse_games(text, season))


async def fetch_weekly_rows(season: int) -> list[dict] | None:
    """The weekly player stats as parsed rows. The CSV itself is the one the other tools download."""
    hit = _cache.get(("weekly", season))
    if hit and season_cache_fresh(season, hit[0]):
        return hit[1]
    from .matchup_tools import nflverse_week_csv
    async with create_http_client(timeout=LONG_TIMEOUT) as client:
        text = await nflverse_week_csv(season, client)
    if text is None:
        return None
    rows = await asyncio.to_thread(parse_weekly_rows, text)
    _cache[("weekly", season)] = (datetime.now(UTC), rows)
    return rows


def clear_cache() -> None:
    _cache.clear()
