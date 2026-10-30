"""Yahoo's per-player projection beside nfl_mcp's own. Comparison only.

Yahoo's numbers are never passed into nfl_mcp's projections or the lineup optimizer; a test
pins that. Shared by ``get_yahoo_projection_comparison`` and ``fantasy_checkin``.
"""
from __future__ import annotations

import re

from .. import injury_status
from .idmap import player_key, player_label

# A stored report is read whatever its age (the check-in judges staleness itself).
INJURY_LOOKUP_HOURS = 24 * 30

NOT_PROJECTED_SLOTS = ("IR",)
_NO_SLEEPER = "no Sleeper projection for "
_MORE = re.compile(r"\s*\(\+\d+ more\)\s*$")


def _without_bye_players(warnings: list[str], bye_names: set[str]) -> list[str]:
    """Drop bye players from "no Sleeper projection for ..." warnings: nobody projects a bye week."""
    out = []
    for w in warnings:
        if not w.startswith(_NO_SLEEPER):
            out.append(w)
            continue
        head, sep, tail = w.partition(" — ")
        names = [n for n in _MORE.sub("", head[len(_NO_SLEEPER):]).split(", ") if n]
        keep = [n for n in names if n not in bye_names]
        if keep:
            out.append(_NO_SLEEPER + ", ".join(keep) + sep + tail)
    return out


def _stored_report(db, name: str | None, team: str | None) -> dict:
    """What nfl_mcp's stored injury report says beyond the status: the body part, the date the source
    dated the report (``report_date``), when nfl_mcp last stored it (``stored_at``, which is not the
    report's date) and the date of the latest practice line. Empty when there is none."""
    try:
        rep = db.find_player_injury(name, team, max_age_hours=INJURY_LOOKUP_HOURS) if db is not None else None
    except Exception:
        rep = None
    if not isinstance(rep, dict):
        return {}
    body_part, body_part_unknown = injury_status.body_part(rep.get("injury_type"))
    sources = rep.get("sources") or []
    if isinstance(sources, str):
        sources = [sources]
    # ESPN's own comment, not the sentence nfl_mcp writes for a record it built from the roster page
    comment = None if "espn_roster" in sources else (rep.get("injury_description") or None)
    out = {"body_part": body_part, "body_part_unknown": body_part_unknown, "comment": comment,
           "report_date": rep.get("date_reported") or None,
           "stored_at": rep.get("updated_at") or None, "practice_date": None}
    try:
        from ..practice_reports import lookup_practice
        practice = lookup_practice(db, name, team)
        out["practice_date"] = (practice or {}).get("latest_date")
    except Exception:
        pass
    return out


def _match_projection(r: dict, by_id: dict, by_name: dict) -> dict | None:
    """The engine's row for a roster row: by player id, else by name when only one player has it."""
    pid = r.get("player_id")
    if pid not in (None, "") and str(pid) in by_id:
        return by_id[str(pid)]
    hits = by_name.get(r.get("name")) or []
    return hits[0] if len(hits) == 1 else None


async def compare_projections(league_id: str, rows: list[dict], week: int | None = None, db=None) -> dict:
    """``rows`` are :meth:`YahooProvider.get_projection_inputs` rows.

    ``our_projection`` is the Sleeper-first blend every other lineup tool uses; ``gap`` is ours
    minus Yahoo's. A player on bye (by nfl_mcp's schedule or Yahoo's bye week) has no projection
    from anyone: every projection field is None and ``on_bye`` is True.

    Each row also carries nfl_mcp's own view of the player's injury (``nfl_injury_status`` and where
    it came from), the week's practice line and the game-day decision, which ``fantasy_checkin``
    uses to cross-check Yahoo's tag.
    """
    from ..database import get_shared_db
    from ..projections import project_players

    # Without a database the engine skips its injury lookup and its cached-schedule bye detection,
    # so Q/D/O tags were never priced and nothing was available to cross-check Yahoo's against.
    db = db if db is not None else get_shared_db()
    live = [r for r in rows if r["slot"] not in NOT_PROJECTED_SLOTS and r.get("team") and r.get("position")]
    res = await project_players(
        [{"name": r["name"], "team": r["team"], "position": r["position"], "player_id": r["player_id"]}
         for r in live], league_id=league_id, week=week, db=db)
    by_id: dict[str, dict] = {}
    by_name: dict[str | None, list[dict]] = {}
    for p in res.get("projections") or []:
        if p.get("player_id") not in (None, ""):
            by_id[str(p["player_id"])] = p
        by_name.setdefault(p.get("player"), []).append(p)
    wk = res.get("week")
    out = []
    for r in rows:
        p = _match_projection(r, by_id, by_name) if r in live else None
        # Yahoo's own bye week settles it even when nfl_mcp has no schedule cached for the week.
        on_bye = bool((p or {}).get("on_bye")) or (r.get("bye_week") is not None and wk is not None
                                                    and int(r["bye_week"]) == int(wk))
        mine = None if on_bye else (p.get("projected_points") if p else None)
        theirs = None if on_bye else r["yahoo_projection"]
        # Once his game has started Yahoo shows the points so far in place of a projection.
        points = None if on_bye else r.get("fantasy_points")
        p = p or {}
        report = _stored_report(db, r["name"], r["team"]) if (r["injury"] or p.get("injury_status")) else {}
        out.append({
            "slot": r["slot"], "name": r["name"], "team": r["team"], "position": r["position"],
            "player_id": r.get("player_id"), "key": player_key(r),
            "label": player_label(r["name"], r["team"], r["position"]),
            "injury": r["injury"], "bye_week": r.get("bye_week"), "on_bye": on_bye,
            "started": points is not None, "points_so_far": points,
            "game_status": (r.get("game") or {}).get("kickoff"),
            "yahoo_projection": theirs, "our_projection": mine,
            "model_projection": None if on_bye else p.get("model_projection"),
            "sleeper_projection": None if on_bye else p.get("sleeper_projection"),
            "projection_source": "bye" if on_bye else p.get("projection_source"),
            "nfl_injury_status": p.get("injury_status"), "nfl_injury_source": p.get("injury_source"),
            "practice_status": p.get("practice_status"), "gameday_status": p.get("gameday_status"),
            "nfl_injury_body_part": report.get("body_part"),
            "nfl_injury_body_part_unknown": bool(report.get("body_part_unknown")),
            "nfl_injury_comment": report.get("comment"), "nfl_injury_report_date": report.get("report_date"),
            "nfl_injury_stored_at": report.get("stored_at"), "nfl_practice_date": report.get("practice_date"),
            "role_trend": p.get("role_trend"),
            "engine_bye_status": p.get("bye_status"),
            "gap": round(mine - theirs, 1) if mine is not None and theirs is not None else None,
        })
    warnings = _without_bye_players(list(res.get("warnings") or []), {x["name"] for x in out if x["on_bye"]})
    names = [x["name"] for x in out]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        warnings.append(f"Two players on the roster are both named {dup}: they are told apart by id and team, never by name.")
    starters = [x for x in out if x["slot"] not in ("BN", "IR")]
    if res.get("success") is False:
        warnings.append(res.get("error") or "nfl_mcp projections unavailable")
    totals = starter_totals(starters)
    both = [x for x in starters if not x["started"] and x["gap"] is not None]
    return {
        "week": wk, "players": out,
        "starter_totals": totals,
        "biggest_gaps": sorted(both, key=lambda x: -abs(x["gap"]))[:3],
        "warnings": warnings,
    }


def starter_totals(starters: list[dict]) -> dict:
    """Yahoo's and nfl_mcp's totals over the starters as "points so far plus projections for players yet to play".

    A starter whose game has started counts for his points so far on both sides (the same actual number);
    every other starter counts for each source's projection. A starter missing a number from either source
    (on bye, no projection) is left out of both totals, and ``scope`` says so: a total that covers only part
    of the starters is never given without saying which.
    """
    played = [x for x in starters if x["started"]]
    ahead = [x for x in starters if not x["started"] and x["gap"] is not None]
    left_out = []
    for x in starters:
        if x["started"] or x["gap"] is not None:
            continue
        why = ("on bye" if x["on_bye"] else "no Yahoo projection" if x["yahoo_projection"] is None
               else "no nfl_mcp projection")
        left_out.append({"label": x["label"], "reason": why})
    pts = sum(x["points_so_far"] for x in played)
    yahoo_total = round(pts + sum(x["yahoo_projection"] for x in ahead), 1)
    our_total = round(pts + sum(x["our_projection"] for x in ahead), 1)
    n, compared = len(starters), len(played) + len(ahead)
    scope = (f"all {n} starters" if compared == n else
             f"{compared} of {n} starters; left out of both totals: "
             + ", ".join(f"{o['label']} ({o['reason']})" for o in left_out))
    if played:
        scope += (f"; {len(played)} started, counted at the points scored so far ("
                  + ", ".join(f"{x['label']} {x['points_so_far']:g}" for x in played)
                  + f"), and {len(ahead)} yet to play, counted at each source's projection")
    return {"yahoo": yahoo_total, "ours": our_total, "gap": round(our_total - yahoo_total, 1),
            "players_compared": compared, "starters": n,
            "basis": "points so far for starters whose game has started, plus projections for starters yet to play",
            "counted_as_played": [{"label": x["label"], "points": x["points_so_far"], "status": x.get("game_status")}
                                  for x in played],
            "counted_as_projected": [x["label"] for x in ahead], "not_compared": left_out, "scope": scope}
