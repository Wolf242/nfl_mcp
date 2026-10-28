"""BFS scoring, built from Yahoo's own league-settings page.

``BFS_SCORING`` was read off ``/f1/1234567/settings`` on 2026-10-27 and cross-checked
against box scores (410 passing yards = +5 total, 334 = +2; see the project notes).
:func:`parse_settings_rows` rebuilds the same dict from a live settings page so a
commissioner change is picked up instead of silently ignored.

nfl_mcp's :class:`~nfl_mcp.scoring.ScoringModel` treats the two yardage bonuses as a
*non-stacking pair* (300-399 gets the first, 400+ gets only the second). BFS pays
+2 at 100/300 and **+5 in total** at 200/400, so the second key carries the total (5),
not the increment (3).
"""
from __future__ import annotations

import re

BFS_SCORING: dict[str, float] = {
    # passing: 25 yards per point
    "pass_yd": 0.04, "pass_td": 6.0, "pass_int": -2.0, "pass_2pt": 2.0,
    "bonus_pass_yd_300": 2.0, "bonus_pass_yd_400": 5.0,
    # rushing: 10 yards per point
    "rush_yd": 0.1, "rush_td": 6.0, "rush_2pt": 2.0,
    "bonus_rush_yd_100": 2.0, "bonus_rush_yd_200": 5.0,
    # receiving: full PPR
    "rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0, "rec_2pt": 2.0,
    "bonus_rec_yd_100": 2.0, "bonus_rec_yd_200": 5.0,
    # misc offense
    "fum_lost": -2.0, "fum_rec_td": 6.0, "st_td": 6.0,
    # kicking (misses cost only inside 40; 50-59 = 5, 60+ = 6)
    "fgm_0_19": 3.0, "fgm_20_29": 3.0, "fgm_30_39": 3.0, "fgm_40_49": 4.0,
    "fgm_50_59": 5.0, "fgm_60p": 6.0,
    "fgmiss_0_19": -1.0, "fgmiss_20_29": -1.0, "fgmiss_30_39": -1.0,
    "xpm": 1.0, "xpmiss": -1.0,
    # defense / special teams
    "sack": 1.0, "int": 2.0, "fum_rec": 2.0, "def_td": 6.0, "safe": 2.0,
    "blk_kick": 2.0, "def_st_td": 6.0, "xp_ret": 2.0,
    "pts_allow_0": 10.0, "pts_allow_1_6": 7.0, "pts_allow_7_13": 4.0,
    "pts_allow_14_20": 1.0, "pts_allow_21_27": 0.0, "pts_allow_28_34": -1.0,
    "pts_allow_35p": -4.0,
}

# Yahoo settings label -> (sleeper key, kind). kind: "pts" = the cell is a number,
# "per_yard" = "<n> yards per point; ...", plus optional bonus thresholds.
_LABELS: dict[str, str] = {
    "passing touchdowns": "pass_td", "interceptions": "pass_int",
    "rushing touchdowns": "rush_td", "receptions": "rec",
    "receiving touchdowns": "rec_td", "return touchdowns": "st_td",
    "2-point conversions": "__2pt", "fumbles lost": "fum_lost",
    "offensive fumble return td": "fum_rec_td",
    "field goals 0-19 yards": "fgm_0_19", "field goals 20-29 yards": "fgm_20_29",
    "field goals 30-39 yards": "fgm_30_39", "field goals 40-49 yards": "fgm_40_49",
    "field goals 50-59 yards": "fgm_50_59", "field goals 60+ yards": "fgm_60p",
    "field goals missed 0-19 yards": "fgmiss_0_19",
    "field goals missed 20-29 yards": "fgmiss_20_29",
    "field goals missed 30-39 yards": "fgmiss_30_39",
    "point after attempt made": "xpm", "point after attempt missed": "xpmiss",
    "sack": "sack", "interception": "int", "fumble recovery": "fum_rec",
    "touchdown": "def_td", "safety": "safe", "block kick": "blk_kick",
    "kickoff and punt return touchdowns": "def_st_td",
    "extra point returned": "xp_ret",
    "points allowed 0 points": "pts_allow_0", "points allowed 1-6 points": "pts_allow_1_6",
    "points allowed 7-13 points": "pts_allow_7_13",
    "points allowed 14-20 points": "pts_allow_14_20",
    "points allowed 21-27 points": "pts_allow_21_27",
    "points allowed 28-34 points": "pts_allow_28_34",
    "points allowed 35+ points": "pts_allow_35p",
}
_YARD_LABELS = {"passing yards": "pass", "rushing yards": "rush", "receiving yards": "rec"}
_PER_YARD = re.compile(r"(?P<n>[\d.]+)\s+yards?\s+per\s+point", re.I)
_BONUS = re.compile(r"(?P<pts>[\d.]+)\s+points?\s+at\s+(?P<yds>\d+)\s+yards", re.I)


def _num(s: str) -> float | None:
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", s or "")
    return float(m.group(1)) if m else None


def parse_settings_rows(rows: list[str]) -> dict[str, float]:
    """Build a Sleeper-style scoring dict from rows like ``"Passing Touchdowns = 6 = 4"``.

    ``rows`` are the settings table rows joined by `` = `` (label, league value, Yahoo
    default). Unknown labels are ignored; the caller compares against
    :data:`BFS_SCORING` and surfaces differences rather than guessing.
    """
    out: dict[str, float] = {}
    for raw in rows:
        parts = [p.strip() for p in re.split(r"\s=\s", raw)]
        if len(parts) < 2:
            continue
        label = re.sub(r"\(Yahoo Default:[^)]*\)", "", parts[0], flags=re.I).strip().lower()
        value = parts[1]
        if label in _YARD_LABELS:
            kind = _YARD_LABELS[label]
            m = _PER_YARD.search(value)
            if m:
                out[f"{kind}_yd"] = round(1.0 / float(m.group("n")), 4)
            bonuses = sorted((int(b.group("yds")), float(b.group("pts")))
                             for b in _BONUS.finditer(value))
            running = 0.0
            for yds, pts in bonuses:  # "2 at 100; 3 at 200" is cumulative -> totals 2, 5
                running += pts
                out[f"bonus_{kind}_yd_{yds}"] = running
            continue
        key = _LABELS.get(label)
        if not key:
            continue
        n = _num(value)
        if n is None:
            continue
        if key == "__2pt":
            out["pass_2pt"] = out["rush_2pt"] = out["rec_2pt"] = n
        else:
            out[key] = n
    return out


def diff_from_bfs(parsed: dict[str, float]) -> dict[str, tuple[float | None, float]]:
    """Keys where a freshly parsed settings page disagrees with :data:`BFS_SCORING`."""
    return {k: (BFS_SCORING.get(k), v) for k, v in parsed.items()
            if BFS_SCORING.get(k) != v}
