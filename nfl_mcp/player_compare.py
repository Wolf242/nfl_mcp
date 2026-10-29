"""Two players, one start/sit call, scored on BFS.

``compare_players`` lays out what decides a close start/sit: each player's BFS game log, the usage
behind it, the game environment and which kickoff comes first, how the opponent defends the position
(and whether that is yards or touchdowns), nfl_mcp's and Yahoo's projections on BFS, injury records
with their dates, where each fits on the roster and what he hedges or stacks with, and a verdict.

The verdict never turns a toss-up into a pick: when the projections are inside the single-player tie
band it says "toss-up" and lists the tiebreakers in order with the player each favours.

Everything here is pure over parsed nflverse rows and injected callables (see :class:`Sources`), so it
is tested without a network. The tool wrapper in ``nfl_mcp.tools.yahoo`` supplies the real ones.
"""
from __future__ import annotations

import json
import re
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from .nflverse_files import norm_person
from .scoring import LeagueScoring, ScoringModel
from .yahoo import checkin as ck
from .yahoo.idmap import norm_team
from .yahoo.scoring import BFS_SCORING

POSITIONS = ("QB", "RB", "WR", "TE")
TIE_BAND = ck.PLAYER_TIE_POINTS
BFS_MODEL = ScoringModel(dict(BFS_SCORING), source="league", name="BFS")
BFS_SCORER = LeagueScoring(BFS_MODEL)  # what the projection engine takes as ``scoring``

TREND_WINDOW = 4            # played weeks a usage trend and the teammate scan look at
SNAP_RISING_POINTS = 10.0   # a teammate's snap share rose at least this many points across the window
GOAL_LINE_SHARE = 0.25      # a teammate's share of the team's carries inside the 10 that counts as taking the work
PASSING_DOWN_SHARE = 0.25   # ... and of the team's targets on 3rd and 4th down
SMALL_SAMPLE_GAMES = 4      # a defense with fewer games than this is a small sample
KEY_DEFENDER_SNAP_SHARE = 0.60
# Differences too small to favour either player in a tiebreaker.
PROJECTION_EPS, RED_ZONE_EPS, FLOOR_CEILING_EPS = 0.05, 0.5, 1.0
INSIDE_10_EPS = 5.0          # points of inside-10 carry share below which two players are level
MIN_GAMES_PERCENTILE = 8     # fewer games than this and a 20th/80th percentile says nothing
SOURCE_NAMES = {"nfl_mcp": "nfl_mcp", "model": "model", "sleeper": "Sleeper", "yahoo": "Yahoo"}
_SPLITS = {
    "QB": ("passing_yards", "passing_tds", "rushing_yards", "rushing_tds"),
    "RB": ("rushing_yards", "rushing_tds", "receptions", "receiving_yards", "receiving_tds"),
    "WR": ("rushing_yards", "rushing_tds", "receptions", "receiving_yards", "receiving_tds"),
    "TE": ("rushing_yards", "rushing_tds", "receptions", "receiving_yards", "receiving_tds"),
}
_TD_KEYS = (("passing_tds", "pass_td"), ("rushing_tds", "rush_td"), ("receiving_tds", "rec_td"))


# ------------------------------------------------------------------ scoring
def bfs_points(row: dict, position: str | None = None) -> float:
    """One weekly stat line on BFS: the shared scorer, so the 100/200 rushing and receiving and the
    300/400 passing yardage bonuses (+2 at the first, +5 in total at the second) are included, plus
    the special-teams and fumble-return touchdowns the scorer does not read."""
    pts = BFS_MODEL.game_points(row, position or row.get("position"))
    pts += BFS_SCORING["st_td"] * (row.get("special_teams_tds") or 0.0)
    pts += BFS_SCORING["fum_rec_td"] * (row.get("fumble_recovery_tds") or 0.0)
    return round(pts, 2)


def _td_points(row: dict) -> float:
    return sum(BFS_SCORING[w] * (row.get(k) or 0.0) for k, w in _TD_KEYS)


def _r1(x: float | None) -> float | None:
    return None if x is None else round(x, 1)


def _pct(x: float | None) -> float | None:
    return None if x is None else round(100.0 * x, 1)


def _slope(values: list[float]) -> float:
    """Least-squares slope per step."""
    n = len(values)
    if n < 2:
        return 0.0
    mx, my = (n - 1) / 2.0, sum(values) / n
    den = sum((i - mx) ** 2 for i in range(n))
    return sum((i - mx) * (v - my) for i, v in enumerate(values)) / den if den else 0.0


def _direction(values: list[float], threshold: float) -> str:
    """rising / falling / steady: the slope times the window against a per-metric threshold."""
    if len(values) < 3:
        return "insufficient_data"
    moved = _slope(values) * (len(values) - 1)
    return "rising" if moved >= threshold else "falling" if moved <= -threshold else "steady"


def label(p: dict) -> str:
    return ck.label_of(p)


# ------------------------------------------------------------------ finding the players
def index_players(rows: list[dict]) -> dict[str, dict]:
    idx: dict[str, dict] = {}
    for r in rows:
        e = idx.setdefault(r["player_id"], {"player_id": r["player_id"], "name": r["name"],
                                            "position": r["position"], "team": r["team"], "last_week": 0, "games": []})
        e["games"].append(r)
        if r["week"] >= e["last_week"]:
            e.update(team=r["team"], position=r["position"], last_week=r["week"])
    return idx


_QUERY = re.compile(r"^(?P<name>.+?)\s*\(\s*(?P<team>[A-Za-z]{2,3})(?:\s+(?P<pos>QB|RB|WR|TE))?\s*\)\s*$", re.I)


def resolve_player(idx: dict[str, dict], query: str) -> tuple[dict | None, str | None]:
    """The player a query names: ``"Full Name"``, ``"Full Name (TEAM POS)"`` or a GSIS id. A name that
    fits more than one player is an error that lists them, never a guess."""
    q = (query or "").strip()
    if q in idx:
        return idx[q], None
    m = _QUERY.match(q)
    name, team, pos = (m.group("name"), norm_team(m.group("team")), (m.group("pos") or "").upper()) if m else (q, None, "")
    want = norm_person(name)
    if not want:
        return None, "Name a player, for example \"Full Name\" or \"Full Name (TEAM POS)\"."
    found = [e for e in idx.values() if norm_person(e["name"]) == want]
    if not found and len(want) >= 5:
        found = [e for e in idx.values() if want in norm_person(e["name"])]
    if team:
        found = [e for e in found if e["team"] == team] or found
    if pos:
        found = [e for e in found if e["position"] == pos] or found
    if not found:
        return None, f"No QB, RB, WR or TE named {query!r} has a stat line this season."
    if len(found) > 1:
        names = ", ".join(sorted(label(e) for e in found))
        return None, f"{query!r} fits more than one player: {names}. Say which one, e.g. \"Full Name (TEAM POS)\"."
    return found[0], None


# ------------------------------------------------------------------ game log
def game_log(entry: dict, week: int) -> dict:
    """BFS points for each game before ``week``: per game, mean, median, min, max and the last three."""
    games = sorted((g for g in entry["games"] if g["week"] < week), key=lambda g: g["week"])
    log = [{"week": g["week"], "opponent": g["opponent"], "bfs_points": bfs_points(g, entry["position"])} for g in games]
    pts = [x["bfs_points"] for x in log]
    out: dict[str, Any] = {"games": len(log), "log": log, "last_3": log[-3:]}
    if pts:
        out.update(mean=_r1(statistics.mean(pts)), median=_r1(statistics.median(pts)), min=min(pts), max=max(pts))
    else:
        out.update(mean=None, median=None, min=None, max=None)
    if len(pts) >= MIN_GAMES_PERCENTILE:
        q = statistics.quantiles(pts, n=5, method="inclusive")  # cut points at the 20th, 40th, 60th and 80th percentile
        out.update(floor_p20=_r1(q[0]), ceiling_p80=_r1(q[3]))
    else:
        out.update(floor_p20=None, ceiling_p80=None)
    out["note"] = ("Points are for the weeks he has a stat line; a week with none is a bye, a game he missed or one in "
                   "which he recorded nothing.")
    return out


# ------------------------------------------------------------------ usage
class UsageData:
    """Team totals, snaps and play-by-play counts, indexed once for every player's usage."""

    def __init__(self, rows: list[dict], snaps: list[dict] | None, plays: list[dict] | None):
        self.has_snaps, self.has_pbp = snaps is not None, plays is not None
        self.team_totals: dict[tuple[str, int], dict] = {}
        for r in rows:
            t = self.team_totals.setdefault((r["team"], r["week"]), {"carries": 0.0, "targets": 0.0})
            t["carries"] += r["carries"]
            t["targets"] += r["targets"]
        self.snaps: dict[tuple[str, int], list[dict]] = {}
        for s in snaps or []:
            self.snaps.setdefault((s["team"], s["week"]), []).append(s)
        self.rush: dict[tuple[int, str, str], list[int]] = {}
        self.team_rush: dict[tuple[int, str], list[int]] = {}
        self.tgt: dict[tuple[int, str, str], list[int]] = {}
        self.team_tgt: dict[tuple[int, str], list[int]] = {}
        self.weeks_with_plays: set[int] = set()
        for p in plays or []:
            self.weeks_with_plays.add(p["week"])
            yl = p["yardline_100"]
            if p["kind"] == "rush":
                flags = [1, int(yl is not None and yl <= 20), int(yl is not None and yl <= 10)]
                self._add(self.team_rush, (p["week"], p["posteam"]), flags)
                if p["rusher_id"]:
                    self._add(self.rush, (p["week"], p["posteam"], p["rusher_id"]), flags)
            elif not p["sack"]:
                flags = [1, int(yl is not None and yl <= 20), int(p["down"] in (3, 4))]
                self._add(self.team_tgt, (p["week"], p["posteam"]), flags)
                if p["receiver_id"]:
                    self._add(self.tgt, (p["week"], p["posteam"], p["receiver_id"]), flags)

    @staticmethod
    def _add(table: dict, key: tuple, flags: list[int]) -> None:
        cur = table.setdefault(key, [0] * len(flags))
        for i, v in enumerate(flags):
            cur[i] += v

    def snap_pct(self, team: str, week: int, name: str) -> float | None:
        want = norm_person(name)
        for s in self.snaps.get((team, week), []):
            if s["norm"] == want:
                return _pct(s["off_pct"])
        return None

    def goal_line(self, pid: str, team: str, weeks: list[int]) -> tuple[int, int]:
        """(player's carries inside the 10, the team's) over ``weeks``."""
        mine = sum(self.rush.get((w, team, pid), [0, 0, 0])[2] for w in weeks)
        tot = sum(self.team_rush.get((w, team), [0, 0, 0])[2] for w in weeks)
        return mine, tot

    def passing_down(self, pid: str, team: str, weeks: list[int]) -> tuple[int, int]:
        """(player's targets on 3rd and 4th down, the team's) over ``weeks``."""
        mine = sum(self.tgt.get((w, team, pid), [0, 0, 0])[2] for w in weeks)
        tot = sum(self.team_tgt.get((w, team), [0, 0, 0])[2] for w in weeks)
        return mine, tot


def _share(num: float, den: float) -> float | None:
    return _pct(num / den) if den else None


def usage(entry: dict, week: int, u: UsageData) -> dict:
    """Per-week and summary usage before ``week``: snap share, carry and target share, touches, the
    carries inside the 20 and the 10 as a share of the team's, and red-zone targets."""
    pid, team = entry["player_id"], entry["team"]
    rows = []
    for g in sorted((g for g in entry["games"] if g["week"] < week), key=lambda g: g["week"]):
        w, t = g["week"], g["team"]
        tot = u.team_totals.get((t, w), {"carries": 0.0, "targets": 0.0})
        rush = u.rush.get((w, t, pid), [0, 0, 0]) if w in u.weeks_with_plays else None
        team_rush = u.team_rush.get((w, t), [0, 0, 0]) if w in u.weeks_with_plays else None
        tg = u.tgt.get((w, t, pid), [0, 0, 0]) if w in u.weeks_with_plays else None
        rows.append({
            "week": w, "snap_pct": u.snap_pct(t, w, entry["name"]) if u.has_snaps else None,
            "carries": g["carries"], "carry_share": _share(g["carries"], tot["carries"]),
            "targets": g["targets"],
            "target_share": _pct(g["target_share"]) if g.get("target_share") is not None
            else _share(g["targets"], tot["targets"]),
            "touches": g["carries"] + g["receptions"],
            "carries_inside_20": rush[1] if rush else None, "carries_inside_10": rush[2] if rush else None,
            "team_carries_inside_10": team_rush[2] if team_rush else None,
            "red_zone_targets": tg[1] if tg else None})

    def summary(sel: list[dict]) -> dict:
        n = len(sel)
        weeks = [r["week"] for r in sel]
        pbp = [r for r in sel if r["carries_inside_20"] is not None]
        t20 = sum(u.team_rush.get((r["week"], team), [0, 0, 0])[1] for r in pbp)
        t10 = sum(u.team_rush.get((r["week"], team), [0, 0, 0])[2] for r in pbp)
        m20, m10 = sum(r["carries_inside_20"] for r in pbp), sum(r["carries_inside_10"] for r in pbp)
        tot_c = sum(u.team_totals.get((team, w), {"carries": 0})["carries"] for w in weeks)
        snaps = [r["snap_pct"] for r in sel if r["snap_pct"] is not None]
        shares = [r["target_share"] for r in sel if r["target_share"] is not None]
        return {
            "games": n, "snap_pct": _r1(statistics.mean(snaps)) if snaps else None,
            "carry_share": _share(sum(r["carries"] for r in sel), tot_c),
            "target_share": _r1(statistics.mean(shares)) if shares else None,
            "touches_per_game": _r1(sum(r["touches"] for r in sel) / n) if n else None,
            "inside_20_carry_share": _share(m20, t20), "inside_10_carry_share": _share(m10, t10),
            "carries_inside_20": m20 if pbp else None, "carries_inside_10": m10 if pbp else None,
            "red_zone_targets": sum(r["red_zone_targets"] for r in sel if r["red_zone_targets"] is not None) if pbp else None,
            "red_zone_opportunities_per_game": (_r1((m20 + sum(r["red_zone_targets"] for r in pbp)) / len(pbp))
                                                if pbp else None)}
    window = rows[-TREND_WINDOW:]
    return {
        "weeks": rows, "season": summary(rows), "last_3": summary(rows[-3:]),
        "trend": {"snap_pct": _direction([r["snap_pct"] for r in window if r["snap_pct"] is not None], SNAP_RISING_POINTS),
                  "carry_share": _direction([r["carry_share"] for r in window if r["carry_share"] is not None], 8.0),
                  "target_share": _direction([r["target_share"] for r in window if r["target_share"] is not None], 5.0)},
        "note": ("Carry and target shares are of the team's QB/RB/WR/TE totals; inside-20 and inside-10 shares and "
                 "red-zone targets come from play by play." if u.has_pbp else
                 "Play by play was unavailable, so inside-20/inside-10 shares and red-zone targets are missing.")}


def teammates_taking_work(entry: dict, week: int, u: UsageData, names: dict[str, dict]) -> dict:
    """Teammates whose snap share is rising and who take the goal-line (carries inside the 10) or the
    passing-down (3rd/4th-down target) work, over the last :data:`TREND_WINDOW` played weeks."""
    team = entry["team"]
    weeks = sorted({w for (t, w) in u.snaps if t == team and w < week})[-TREND_WINDOW:]
    if not u.has_snaps or len(weeks) < 3:
        return {"teammates": [], "note": "Not enough snap data to scan the teammates (needs 3 played weeks)."}
    series: dict[str, dict] = {}
    for w in weeks:
        for s in u.snaps[(team, w)]:
            if s["position"] in ("RB", "FB", "WR", "TE") and s["norm"] != norm_person(entry["name"]):
                e = series.setdefault(s["norm"], {"name": s["name"], "position": s["position"], "snaps": {}})
                e["snaps"][w] = _pct(s["off_pct"])
    out, room = [], []
    for norm, e in series.items():
        values = [e["snaps"].get(w, 0.0) for w in weeks]
        direction = _direction(values, SNAP_RISING_POINTS)
        player = names.get(norm)
        goal = pd = (0, 0)
        if player and u.has_pbp:
            goal = u.goal_line(player["player_id"], team, weeks)
            pd = u.passing_down(player["player_id"], team, weeks)
        takes = []
        if goal[1] >= 3 and goal[0] / goal[1] >= GOAL_LINE_SHARE:
            takes.append(f"goal-line: {goal[0]} of the team's {goal[1]} carries inside the 10 over the last {len(weeks)} weeks")
        if pd[1] >= 4 and pd[0] / pd[1] >= PASSING_DOWN_SHARE:
            takes.append(f"passing downs: {pd[0]} of the team's {pd[1]} third/fourth-down targets")
        row = {"player": f"{e['name']} ({team} {e['position']})", "snap_pct_by_week": [e["snaps"].get(w) for w in weeks],
               "snap_trend": direction}
        room.append({**row, "recent_snap_pct": _r1(statistics.mean(values[-3:]))})
        if direction == "rising" and takes:
            out.append({**row, "takes": takes})
    room.sort(key=lambda r: -(r["recent_snap_pct"] or 0))
    return {"weeks": weeks, "teammates": out, "room": room[:4],
            "note": ("Named only when the snap share rose at least "
                     f"{SNAP_RISING_POINTS:g} points across the window and he takes the goal-line or passing-down work.")}


# ------------------------------------------------------------------ the game
def game_context(games: dict | None, team: str, week: int) -> dict:
    if games is None:
        return {"found": False, "note": "The schedule file is unavailable."}
    g = games.get((week, team))
    if g is None:
        return {"found": False, "bye": True, "note": f"{team} has no game in week {week} (bye)."}
    kick = g["kickoff"]
    return {
        "found": True, "opponent": g["opponent"], "home_or_away": "home" if g["is_home"] else "away",
        "team_spread": g["team_spread"], "total": g["total"], "implied_team_total": _r1(g["implied_total"]),
        "kickoff": f"{kick:%a %Y-%m-%d %H:%M} ET" if kick else None, "kickoff_dt": kick,
        "roof": g["roof"], "surface": g["surface"], "temp": g["temp"], "wind": g["wind"],
        "note": "Spread is the team's own line (negative = favoured); lines are the schedule file's, not live odds."}


def lock_analysis(a: dict, b: dict, ca: dict, cb: dict) -> dict:
    """Which kickoff comes first, and what that does to the choice."""
    ka, kb = ca.get("kickoff_dt"), cb.get("kickoff_dt")
    if not ka or not kb:
        return {"locks_first": None, "effect": "A kickoff time is missing, so the lock order cannot be given."}
    if ka == kb:
        return {"locks_first": None, "first_kickoff": ca["kickoff"], "hours_between": 0,
                "effect": "Same kickoff: both lock together, so the same news is known when you decide."}
    (first, kf, cf), (second, ks, cs) = sorted([(a, ka, ca), (b, kb, cb)], key=lambda x: x[1])
    hours = (ks - kf).total_seconds() / 3600.0
    return {
        "locks_first": label(first), "first_kickoff": cf["kickoff"], "second_kickoff": cs["kickoff"],
        "hours_between": round(hours, 1), "first_is_thursday": kf.weekday() == 3 and ks.weekday() != 3,
        "effect": (f"{label(first)} locks first, at {cf['kickoff']}, {hours:.0f} hours before {label(second)} "
                   f"({cs['kickoff']}). Whatever is only known later (inactives, weather, {label(second)}'s injury "
                   f"designation, Sunday news) cannot inform that choice. Starting {first['name']} commits you before "
                   f"it; starting {second['name']} keeps the decision open until his kickoff, but a benched "
                   f"{first['name']} is locked out once his game starts and cannot be swapped back in.")}


# ------------------------------------------------------------------ the opponent
def _defense_table(rows: list[dict], position: str) -> dict[str, dict]:
    agg: dict[str, dict[int, dict]] = {}
    keys = ("bfs", "td_pts", "passing_yards", "passing_tds", "rushing_yards", "rushing_tds", "receptions",
            "receiving_yards", "receiving_tds")
    for r in rows:
        if r["position"] != position or not r["opponent"]:
            continue
        w = agg.setdefault(r["opponent"], {}).setdefault(r["week"], dict.fromkeys(keys, 0.0))
        w["bfs"] += bfs_points(r, position)
        w["td_pts"] += _td_points(r)
        for k in keys[2:]:
            w[k] += r[k]
    table = {}
    for d, weeks in agg.items():
        n = len(weeks)
        per = {k: sum(w[k] for w in weeks.values()) / n for k in keys}
        per["yard_pts"] = per["bfs"] - per["td_pts"]
        table[d] = {"games": n, **per}
    return table


def _epa_table(rows: list[dict]) -> dict[str, dict]:
    acc: dict[str, list[float]] = {}
    for r in rows:
        if r["opponent"] and r["carries"]:
            a = acc.setdefault(r["opponent"], [0.0, 0.0])
            a[0] += r["rushing_epa"]
            a[1] += r["carries"]
    return {d: {"epa_per_rush": e / c, "carries": c} for d, (e, c) in acc.items() if c}


def _rank(table: dict[str, dict], key: str, team: str) -> int | None:
    """1 = the defense allowing the least (the toughest)."""
    if team not in table:
        return None
    return 1 + sum(1 for t in table.values() if t[key] < table[team][key])


def opponent_profile(rows: list[dict] | None, position: str, opp: str | None, season: int) -> dict:
    """What the opposing defense allowed to the position on BFS in one season: points per game and
    rank (1 = allows the least), the same for yards-only and touchdown-only points, the stat splits and
    EPA per rush allowed. ``driver`` says whether the rank follows yards (stable) or touchdowns (noisy)."""
    if not rows or not opp:
        return {"season": season, "available": False}
    table, epa = _defense_table(rows, position), _epa_table(rows)
    if opp not in table:
        return {"season": season, "available": False, "note": f"{opp} has no games against {position}s in {season}."}
    t = table[opp]
    ranks = {k: _rank(table, k, opp) for k in ("bfs", "yard_pts", "td_pts")}
    drift_y = abs(ranks["bfs"] - ranks["yard_pts"])
    drift_t = abs(ranks["bfs"] - ranks["td_pts"])
    td_share = t["td_pts"] / t["bfs"] if t["bfs"] > 0 else None
    if drift_y <= 4 and drift_y <= drift_t:
        driver = "yards (stable)"
    elif drift_t <= 4 and drift_t < drift_y:
        driver = "touchdowns (noisy)"
    else:
        driver = "mixed"
    splits = {k: {"per_game": round(t[k], 2), "rank": _rank(table, k, opp)} for k in _SPLITS[position]}
    out = {
        "season": season, "available": True, "games": t["games"], "of": len(table),
        "bfs_per_game_allowed": round(t["bfs"], 1), "rank": ranks["bfs"],
        "yard_points_per_game": {"per_game": round(t["yard_pts"], 1), "rank": ranks["yard_pts"]},
        "td_points_per_game": {"per_game": round(t["td_pts"], 1), "rank": ranks["td_pts"]},
        "td_share_of_points": round(td_share, 2) if td_share is not None else None,
        "driver": driver, "splits": splits,
        "rank_note": "Rank 1 allows the fewest BFS points (the toughest matchup).",
        "small_sample": t["games"] < SMALL_SAMPLE_GAMES}
    if opp in epa:
        e = epa[opp]
        out["epa_per_rush_allowed"] = {"value": round(e["epa_per_rush"], 3), "carries": int(e["carries"]),
                                       "rank": 1 + sum(1 for x in epa.values() if x["epa_per_rush"] < e["epa_per_rush"])}
    return out


# ------------------------------------------------------------------ injuries
def injury_record(p: dict, who: str, now: datetime, week_start: date | None) -> dict:
    """An injury record with its dates and the check-in's staleness, conflict and single-source flags."""
    status = p.get("nfl_injury_status")
    flags = ck.injury_flags(p, False, now, week_start)
    reasons = ck.stale_reasons(p, False, now, week_start) if status else []
    if status and p.get("nfl_injury_source") not in (None, "both") and "single_source" not in flags:
        flags.append("single_source")
    stamp, basis = ck.injury_record_date(p)
    return {"player": who, "yahoo_tag": p.get("injury"), "status": status, "source": p.get("nfl_injury_source"),
            "practice": p.get("practice_status"), "practice_date": p.get("nfl_practice_date"),
            "body_part": p.get("nfl_injury_body_part"), "report_date": p.get("nfl_injury_report_date"),
            "stored_at": p.get("nfl_injury_stored_at"), "date_used": stamp, "date_basis": basis,
            "flags": flags, "stale_reasons": reasons, "statement": ck.injury_statement(p, reasons)}


def defense_injuries(records: list[dict], snaps: list[dict] | None, team: str, week: int,
                     now: datetime, week_start: date | None) -> dict:
    """Injured key starters on the opposing defense: players on the report who took at least
    :data:`KEY_DEFENDER_SNAP_SHARE` of the defense's snaps over its last three games."""
    recent: dict[str, list[float]] = {}
    weeks = sorted({s["week"] for s in snaps or [] if s["team"] == team and s["week"] < week})[-3:]
    for s in snaps or []:
        if s["team"] == team and s["week"] in weeks and s["def_pct"] > 0:
            recent.setdefault(s["norm"], []).append(s["def_pct"])
    out = []
    for r in records or []:
        status = r.get("injury_status")
        if not ck.nfl_tag(status):
            continue
        norm = norm_person(r.get("player_name"))
        share = statistics.mean(recent[norm]) if recent.get(norm) else None
        if snaps is not None and (share is None or share < KEY_DEFENDER_SNAP_SHARE):
            continue
        sources = r.get("sources") or []
        if isinstance(sources, str):
            try:
                sources = json.loads(sources)
            except ValueError:
                sources = [sources]
        p = {"nfl_injury_status": status, "nfl_injury_source": "both" if len(sources) > 1 else "report",
             "nfl_injury_body_part": r.get("injury_type"), "nfl_injury_report_date": r.get("date_reported"),
             "nfl_injury_stored_at": r.get("updated_at")}
        rec = injury_record(p, f"{r.get('player_name')} ({team} {r.get('position') or 'DEF'})", now, week_start)
        rec["defense_snap_pct"] = _pct(share)
        out.append(rec)
    note = ("Key starters = defenders on the injury report with at least "
            f"{int(KEY_DEFENDER_SNAP_SHARE * 100)}% of the defense's snaps over its last three games."
            if snaps is not None else "Snap counts were unavailable, so every injured defender is listed, starters or not.")
    return {"team": team, "players": out, "note": note}


# ------------------------------------------------------------------ roster fit
def _slot_positions(slot: str) -> list[str]:
    """Positions that can play a starting slot: a flex takes RB, WR or TE; any other slot its own position."""
    return ck.FLEX_POSITIONS.get(slot, [slot])


def _find_roster(roster: list[dict] | None, entry: dict) -> dict | None:
    if not roster:
        return None
    want = norm_person(entry["name"])
    hits = [r for r in roster if norm_person(r["name"]) == want and r.get("position") == entry["position"]]
    hits = [r for r in hits if norm_team(r.get("team")) == entry["team"]] or hits
    return hits[0] if len(hits) == 1 else None


def roster_fit(entry: dict, other: dict, roster: list[dict] | None, opponent: str | None) -> dict:
    """The slot he fills, the starting slots he is eligible for (and who holds them), who else is
    eligible for his slot, and the players on the roster in his game: same-team ones (stacks, or rivals
    for the same touches) and opponents (hedges)."""
    if roster is None:
        return {"on_roster": None, "note": "No roster was read (no Yahoo league), so roster fit is unavailable."}
    mine, other_mine = _find_roster(roster, entry), _find_roster(roster, other)
    pos = entry["position"]
    starters = [r for r in roster if r["slot"] not in ck.STARTER_EXCLUDED]
    eligible = [{"slot": s, "occupants": [label(r) for r in starters if r["slot"] == s]}
                for s in dict.fromkeys(r["slot"] for r in starters) if pos in _slot_positions(s)]
    slot = mine["slot"] if mine else None
    alts = []
    if slot and slot not in ck.STARTER_EXCLUDED:
        alts = [{"player": label(r), "yahoo_projection": r.get("yahoo_projection")} for r in roster
                if r["slot"] == "BN" and r is not mine and r is not other_mine
                and r["position"] in _slot_positions(slot)]
    game = {entry["team"], opponent} - {None}
    links = []
    for r in roster:
        if r is mine or r is other_mine or r["slot"] == "IR" or norm_team(r.get("team")) not in game:
            continue
        if norm_team(r.get("team")) != entry["team"]:
            kind, why = "hedge", "plays for the opponent: his scoring and yours pull in opposite directions"
        elif "QB" in (pos, r["position"]) and {pos, r["position"]} & {"WR", "TE", "RB"}:
            kind, why = "stack", "same offense, quarterback and pass-catcher: good games tend to come together"
        elif r["position"] == pos:
            kind, why = "same_touches", "same position on the same offense: shares the same touches"
        elif r["position"] in ("DEF", "K"):
            kind, why = "stack", "his team's defense or kicker: a lead helps both"
        else:
            kind, why = "same_team", "same offense"
        links.append({"player": label(r), "slot": r["slot"], "kind": kind, "why": why})
    return {"on_roster": mine is not None, "slot": slot, "eligible_slots": eligible,
            "bench_alternatives_for_slot": alts, "same_game": links,
            "note": ("On the bench: the eligible slots show whom he would replace." if slot == "BN" else
                     None if mine else "Not on your roster: the eligible slots show whom he would replace.")}


# ------------------------------------------------------------------ the verdict
def _favor(a: float | None, b: float | None, name_a: str, name_b: str, eps: float = 1e-9) -> str:
    """The player a number favours, or ``even`` when it is missing or the difference is within ``eps``."""
    if a is None or b is None or abs(a - b) <= eps:
        return "even"
    return name_a if a > b else name_b


def _source_values(p: dict) -> dict[str, float]:
    """Every projection source that has a number for this player (none when he is on bye)."""
    proj = p["projection"]
    if proj.get("on_bye"):
        return {}
    return {k: proj[k] for k in SOURCE_NAMES if proj.get(k) is not None}


def projection_view(a: dict, b: dict) -> dict:
    """The median of each player's available projection sources, every source's own gap, and whether the
    sources disagree (on who leads, or on whether the gap is outside the tie band)."""
    la, lb = a["label"], b["label"]
    va, vb = _source_values(a), _source_values(b)
    med_a = round(statistics.median(va.values()), 1) if va else None
    med_b = round(statistics.median(vb.values()), 1) if vb else None
    gaps = {k: round(va[k] - vb[k], 1) for k in SOURCE_NAMES if k in va and k in vb}
    view: dict[str, Any] = {
        "basis": "median of the available sources (" + ", ".join(SOURCE_NAMES[k] for k in SOURCE_NAMES if k in va or k in vb) + ")",
        "a_sources": va, "b_sources": vb, "a_median": med_a, "b_median": med_b,
        "gap": round(med_a - med_b, 1) if med_a is not None and med_b is not None else None,
        "source_gaps": gaps, "sources_disagree": False, "disagreement": None}
    reasons = []
    leaders = {k: (la if g > 0 else lb) for k, g in gaps.items() if abs(g) > PROJECTION_EPS}
    if len(set(leaders.values())) > 1:
        reasons.append("they disagree on who leads: " + "; ".join(
            f"{', '.join(SOURCE_NAMES[k] for k in leaders if leaders[k] == who)} favor{'s' if sum(1 for k in leaders if leaders[k] == who) == 1 else ''} {who}"
            for who in dict.fromkeys(leaders.values())))
    outside = [k for k, g in gaps.items() if abs(g) >= TIE_BAND]
    inside = [k for k, g in gaps.items() if abs(g) < TIE_BAND]
    if outside and inside:
        reasons.append(f"{', '.join(f'{SOURCE_NAMES[k]} ({gaps[k]:+.1f})' for k in outside)} "
                       f"{'is' if len(outside) == 1 else 'are'} outside the {TIE_BAND:g}-point tie band while "
                       f"{', '.join(f'{SOURCE_NAMES[k]} ({gaps[k]:+.1f})' for k in inside)} "
                       f"{'is' if len(inside) == 1 else 'are'} inside it")
    if reasons:
        view["sources_disagree"] = True
        view["disagreement"] = "sources disagree: " + "; and ".join(reasons) + "."
    return view


def _red_zone(a: dict, b: dict) -> dict:
    """Red-zone opportunities per game and inside-10 carry share, each with the player it favours; when the
    two measures favour different players the answer is ``split`` and no winner is named."""
    la, lb = a["label"], b["label"]
    sa, sb = a["usage"]["season"], b["usage"]["season"]
    rz = (sa["red_zone_opportunities_per_game"], sb["red_zone_opportunities_per_game"])
    i10 = (sa["inside_10_carry_share"], sb["inside_10_carry_share"])
    f_rz, f_i10 = _favor(*rz, la, lb, RED_ZONE_EPS), _favor(*i10, la, lb, INSIDE_10_EPS)
    split = "even" not in (f_rz, f_i10) and f_rz != f_i10
    favors = "split" if split else (f_rz if f_rz != "even" else f_i10)
    detail = (f"red-zone opportunities (carries inside the 20 + red-zone targets) per game: {la} {rz[0]} vs {lb} {rz[1]}; "
              f"inside-10 carry share: {la} {i10[0]}% vs {lb} {i10[1]}%")
    if split:
        detail += f". The measures split ({f_rz} on opportunities per game, {f_i10} on inside-10 share): no winner is named."
    return {"favors": favors, "measures_split": split,
            "red_zone_opportunities_per_game": {"a": rz[0], "b": rz[1], "favors": f_rz},
            "inside_10_carry_share": {"a": i10[0], "b": i10[1], "favors": f_i10}, "detail": detail}


def _floor_ceiling(a: dict, b: dict) -> dict:
    """20th and 80th percentile of each player's game log; not meaningful under :data:`MIN_GAMES_PERCENTILE` games."""
    la, lb = a["label"], b["label"]
    ga, gb = a["game_log"], b["game_log"]
    short = [f"{p['label']} {p['game_log']['games']} games" for p in (a, b) if p["game_log"]["floor_p20"] is None]
    if short:
        return {"floor_favors": "not meaningful", "ceiling_favors": "not meaningful", "favors": "not meaningful",
                "detail": f"not meaningful ({'; '.join(short)}; a percentile needs at least {MIN_GAMES_PERCENTILE})"}
    fa, fb, ca_, cb_ = ga["floor_p20"], gb["floor_p20"], ga["ceiling_p80"], gb["ceiling_p80"]
    return {"floor_favors": _favor(fa, fb, la, lb, FLOOR_CEILING_EPS), "ceiling_favors": _favor(ca_, cb_, la, lb, FLOOR_CEILING_EPS),
            "favors": _favor(fa, fb, la, lb, FLOOR_CEILING_EPS),
            "detail": f"floor (20th percentile) {la} {fa} vs {lb} {fb}; ceiling (80th percentile) {la} {ca_} vs {lb} {cb_}"}


def verdict(a: dict, b: dict) -> dict:
    """Pick only outside the tie band; otherwise a toss-up with the tiebreakers in order."""
    la, lb = a["label"], b["label"]
    view = projection_view(a, b)
    caveats = []
    for p in (a, b):
        flagged = [i for i in p["injuries"]["player"]["flags"]
                   if i in ("stale_injury", "injury_conflict", "yahoo_only", "single_source", "predates_activation")]
        status = p["injuries"]["player"]["status"]
        if flagged and status:
            caveats.append(f"{p['label']}'s availability is not confirmed ({', '.join(flagged)}): "
                           f"{p['injuries']['player']['statement']}.")
        elif p["projection"].get("on_bye"):
            caveats.append(f"{p['label']} is on bye.")
    if view["disagreement"]:
        caveats.append(view["disagreement"][0].upper() + view["disagreement"][1:])
    out: dict[str, Any] = {"projection_basis": view["basis"], "projection": view, "tie_band": TIE_BAND, "caveats": caveats}
    if view["gap"] is None:
        return {**out, "call": "no call", "pick": None, "gap": None,
                "summary": "At least one player has no projection (bye, or none available), so no call is made."}
    gap, ma, mb = view["gap"], view["a_median"], view["b_median"]
    out["gap"] = gap
    tiebreakers = [
        {"order": 1, "name": "projection", "favors": _favor(ma, mb, la, lb, PROJECTION_EPS),
         "detail": f"{la} {ma:.1f} vs {lb} {mb:.1f} ({view['basis']}, BFS); source gaps: "
                   + (", ".join(f"{SOURCE_NAMES[k]} {g:+.1f}" for k, g in view["source_gaps"].items()) or "none in common")},
        {"order": 2, "name": "red_zone_share", **_red_zone(a, b)},
        {"order": 3, "name": "floor_ceiling", **_floor_ceiling(a, b)}]
    disagree = (" " + view["disagreement"][0].upper() + view["disagreement"][1:]) if view["disagreement"] else ""
    if abs(gap) < TIE_BAND:
        return {**out, "call": "toss-up", "pick": None, "tiebreakers": tiebreakers,
                "summary": (f"Toss-up: the median projections are {abs(gap):.1f} apart, inside the {TIE_BAND:g}-point tie band, so "
                            "they cannot separate the two. The tiebreakers below are for your risk preference; none of them "
                            "is a pick." + disagree)}
    pick = la if gap > 0 else lb
    return {**out, "call": "edge", "pick": pick, "tiebreakers": tiebreakers,
            "summary": (f"{pick} has the edge on the median projection by {abs(gap):.1f}, outside the {TIE_BAND:g}-point tie band; "
                        "the tiebreakers show whether usage and variance agree." + disagree)}


# ------------------------------------------------------------------ assembling it
@dataclass
class Sources:
    """Everything the comparison reads. Each callable may be omitted; a missing one is a warning."""
    weekly: Callable[[int], Awaitable[list[dict] | None]]
    snaps: Callable[[int], Awaitable[list[dict] | None]] | None = None
    pbp: Callable[[int], Awaitable[list[dict] | None]] | None = None
    games: Callable[[int], Awaitable[dict | None]] | None = None
    project: Callable[..., Awaitable[dict]] | None = None
    roster: Callable[[], Awaitable[list[dict] | None]] | None = None
    stored_report: Callable[[str, str], dict] | None = None
    team_injuries: Callable[[str], list[dict]] | None = None
    week_start: date | None = None
    notes: list[str] = field(default_factory=list)


async def _opt(call, season: int, warnings: list[str], what: str):
    if call is None:
        warnings.append(f"{what} is not configured.")
        return None
    try:
        got = await call(season)
    except Exception as e:
        warnings.append(f"{what} unavailable for {season}: {e}")
        return None
    if got is None:
        warnings.append(f"{what} is not published for {season}.")
    return got


async def compare_players(player_a: str, player_b: str, *, season: int, week: int, src: Sources,
                          now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    warnings: list[str] = []
    rows = await src.weekly(season)
    if not rows:
        return {"success": False, "error": f"The nflverse weekly player stats for {season} are not available."}
    idx = index_players(rows)
    ea, err_a = resolve_player(idx, player_a)
    eb, err_b = resolve_player(idx, player_b)
    if err_a or err_b:
        return {"success": False, "error": " ".join(e for e in (err_a, err_b) if e)}
    if ea["player_id"] == eb["player_id"]:
        return {"success": False, "error": "Both names are the same player."}
    for e in (ea, eb):
        if e["position"] not in POSITIONS:
            return {"success": False, "error": f"{label(e)}: compare_players covers QB, RB, WR and TE."}

    prior_rows = await _opt(src.weekly, season - 1, warnings, "Last season's weekly stats")
    snaps = await _opt(src.snaps, season, warnings, "Snap counts")
    plays = await _opt(src.pbp, season, warnings, "Play by play")
    games = await _opt(src.games, season, warnings, "The schedule")
    u = UsageData(rows, snaps, plays)
    names = {norm_person(e["name"]): e for e in idx.values()}
    roster = None
    if src.roster is not None:
        try:
            roster = await src.roster()
        except Exception as e:
            warnings.append(f"Roster unavailable ({e}): roster fit and Yahoo projections are missing.")

    entries = [ea, eb]
    ctx = [game_context(games, e["team"], week) for e in entries]
    proj_rows = {}
    if src.project is not None:
        ask = []
        for e in entries:
            mine = _find_roster(roster, e)
            ask.append({"name": e["name"], "team": e["team"], "position": e["position"],
                        "player_id": (mine or {}).get("player_id")})
        try:
            index = ck._index_projections(await src.project(ask, scoring=BFS_SCORER, week=week))
            proj_rows = {i: ck._find_projection(index, r) for i, r in enumerate(ask)}
        except Exception as e:
            warnings.append(f"nfl_mcp's projection failed ({e}).")
    out_players = []
    for i, e in enumerate(entries):
        other = entries[1 - i]
        mine = _find_roster(roster, e)
        pr = proj_rows.get(i) or {}
        proj = {
            "nfl_mcp": None if pr.get("on_bye") else pr.get("projected_points"),
            "model": pr.get("model_projection"), "sleeper": pr.get("sleeper_projection"),
            "source": pr.get("projection_source"), "on_bye": bool(pr.get("on_bye")) or bool(ctx[i].get("bye")),
            "yahoo": (mine or {}).get("yahoo_projection") if mine and not ctx[i].get("bye") else None,
            "yahoo_note": ("Yahoo's number is in the league's scoring (BFS) and exists only for players on your roster."
                           if mine else "Not on your roster, so Yahoo's projection is not available."),
            "scoring": "BFS"}
        proj["gap_vs_yahoo"] = (round(proj["nfl_mcp"] - proj["yahoo"], 1)
                                if proj["nfl_mcp"] is not None and proj["yahoo"] is not None else None)
        proj["gap_is_noise"] = (abs(proj["gap_vs_yahoo"]) < TIE_BAND) if proj["gap_vs_yahoo"] is not None else None
        stored = src.stored_report(e["name"], e["team"]) if src.stored_report else {}
        rec_in = {"injury": (mine or {}).get("injury"), "nfl_injury_status": pr.get("injury_status"),
                  "nfl_injury_source": pr.get("injury_source"), "practice_status": pr.get("practice_status"),
                  "nfl_practice_date": stored.get("practice_date"), "nfl_injury_body_part": stored.get("body_part"),
                  "nfl_injury_report_date": stored.get("report_date"), "nfl_injury_stored_at": stored.get("stored_at"),
                  "slot": (mine or {}).get("slot"), "yahoo_known": mine is not None}
        opp = ctx[i].get("opponent")
        records = src.team_injuries(opp) if (src.team_injuries and opp) else []
        out_players.append({
            "label": label(e), "player_id": e["player_id"], "name": e["name"], "team": e["team"], "position": e["position"],
            "game_log": game_log(e, week),
            "usage": usage(e, week, u),
            "teammates": teammates_taking_work(e, week, u, names),
            "context": {k: v for k, v in ctx[i].items() if k != "kickoff_dt"},
            "opponent": {"name": opp,
                         "this_season": opponent_profile(rows, e["position"], opp, season),
                         "last_season": opponent_profile(prior_rows, e["position"], opp, season - 1)},
            "projection": proj,
            "injuries": {"player": injury_record(rec_in, label(e), now, src.week_start),
                         "opposing_defense": defense_injuries(records, snaps, opp, week, now, src.week_start) if opp else None},
            "fit": roster_fit(e, other, roster, opp)})
    a, b = out_players
    result = {
        "success": True, "read_only": True, "scoring": "BFS", "season": season, "week": week,
        "note": "Read-only. Scored on BFS; every number is for the weeks before this one.",
        "a": a, "b": b,
        "lock": lock_analysis(ea, eb, ctx[0], ctx[1]),
        "verdict": verdict(a, b), "warnings": warnings}
    return result
