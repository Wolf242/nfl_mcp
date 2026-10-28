"""Yahoo's per-player projection beside nfl_mcp's own. Comparison only.

Yahoo's numbers are never passed into nfl_mcp's projections or the lineup optimizer; a test
pins that. Shared by ``get_yahoo_projection_comparison`` and ``fantasy_checkin``.
"""
from __future__ import annotations

NOT_PROJECTED_SLOTS = ("IR",)


async def compare_projections(league_id: str, rows: list[dict], week: int | None = None) -> dict:
    """``rows`` are :meth:`YahooProvider.get_projection_inputs` rows.

    ``our_projection`` is the Sleeper-first blend every other lineup tool uses; ``gap`` is ours
    minus Yahoo's. A player on bye (by nfl_mcp's schedule or Yahoo's bye week) has neither number.
    """
    from ..projections import project_players

    live = [r for r in rows if r["slot"] not in NOT_PROJECTED_SLOTS and r.get("team") and r.get("position")]
    res = await project_players(
        [{"name": r["name"], "team": r["team"], "position": r["position"], "player_id": r["player_id"]}
         for r in live], league_id=league_id, week=week)
    ours = {p.get("player"): p for p in res.get("projections") or []}
    wk = res.get("week")
    out, warnings = [], list(res.get("warnings") or [])
    for r in rows:
        p = ours.get(r["name"]) if r in live else None
        # Yahoo's own bye week settles it even when nfl_mcp has no schedule cached for the week.
        on_bye = bool((p or {}).get("on_bye")) or (r.get("bye_week") is not None and wk is not None
                                                    and int(r["bye_week"]) == int(wk))
        mine = None if on_bye else (p.get("projected_points") if p else None)
        theirs = None if on_bye else r["yahoo_projection"]
        out.append({
            "slot": r["slot"], "name": r["name"], "team": r["team"], "position": r["position"],
            "injury": r["injury"], "bye_week": r.get("bye_week"), "on_bye": on_bye,
            "yahoo_projection": theirs, "our_projection": mine,
            "model_projection": p.get("model_projection") if p else None,
            "sleeper_projection": p.get("sleeper_projection") if p else None,
            "projection_source": p.get("projection_source") if p else None,
            "gap": round(mine - theirs, 1) if mine is not None and theirs is not None else None,
        })
    started = [x for x in out if x["slot"] not in ("BN", "IR")]
    both = [x for x in started if x["gap"] is not None]
    if res.get("success") is False:
        warnings.append(res.get("error") or "nfl_mcp projections unavailable")
    yahoo_total = round(sum(x["yahoo_projection"] for x in both), 1)
    our_total = round(sum(x["our_projection"] for x in both), 1)
    return {
        "week": wk, "players": out,
        "starter_totals": {"yahoo": yahoo_total, "ours": our_total, "gap": round(our_total - yahoo_total, 1),
                           "players_compared": len(both), "starters": len(started)},
        "biggest_gaps": sorted(both, key=lambda x: -abs(x["gap"]))[:3],
        "warnings": warnings,
    }
