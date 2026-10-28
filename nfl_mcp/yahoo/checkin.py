"""One-call weekly check-in for a Yahoo league. READ-ONLY: nothing here changes Yahoo.

``build_checkin`` gathers the pieces the other tools already have (league settings, waiver
order, roster with projections, the matchup, the wire, byes, reporter posts) into one structure
and flags what needs a decision. Pure helpers (``find_problems``, ``bye_outlook``,
``wire_positions``) take parsed roster slots, so they are tested without a browser.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from .idmap import norm_team
from .parsers import IR_ELIGIBLE
from .provider import slot_label

logger = logging.getLogger(__name__)

STARTER_EXCLUDED = ("BN", "IR")
FLEX_POSITIONS = {"FLEX": ["RB", "WR", "TE"], "W/R/T": ["RB", "WR", "TE"], "W/R": ["RB", "WR"]}
WIRE_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
# A back with one of these tags is not going to play, so he does not count as an active RB.
UNAVAILABLE_TAGS = {"O", "IR", "IR-R", "PUP-R", "NFI-R", "SUSP", "COV", "NA"}
MIN_ACTIVE_RBS = 2
BYE_WEEKS_AHEAD = 4
# Below this many team points, a gap between nfl_mcp's and Yahoo's projection is noise.
NOISE_BAND_POINTS = 12.0
READ_ONLY_NOTE = "Read-only: nothing was added, dropped, claimed, traded or changed on Yahoo."


def _is_starter(slot: dict) -> bool:
    return slot_label(slot["slot"]) not in STARTER_EXCLUDED


def _bye_week(slot: dict) -> int | None:
    b = slot.get("bye")
    try:
        return int(b) if b is not None else None
    except (TypeError, ValueError):
        return None


def find_problems(slots: list[dict], week: int) -> list[dict]:
    """Things that need a decision this week, most urgent first. ``slots`` are parsed roster slots."""
    out: list[dict] = []

    def add(severity: str, kind: str, slot: dict, detail: str):
        out.append({"severity": severity, "type": kind, "slot": slot_label(slot["slot"]),
                    "player": slot.get("name"), "position": slot.get("position"), "detail": detail})

    for s in slots:
        label = slot_label(s["slot"])
        if s.get("empty"):
            if _is_starter(s):
                add("high", "empty_starting_slot", s, f"No player in the {label} starting slot.")
            else:
                add("info", "empty_slot", s, f"The {label} slot is empty.")
            continue
        tag = s.get("injury")
        if _is_starter(s):
            if _bye_week(s) == week:
                add("high", "starter_on_bye", s, f"{s['name']} starts at {label} but is on bye in week {week}.")
            if tag == "O":
                add("high", "starter_out", s, f"{s['name']} is tagged O (out) in a starting slot.")
            elif tag == "D":
                add("medium", "starter_doubtful", s, f"{s['name']} is tagged D (doubtful) in a starting slot.")
        if label == "IR" and tag not in IR_ELIGIBLE:
            shown = tag or "no injury tag"
            add("medium", "ir_slot_not_eligible", s,
                f"{s['name']} is in an IR slot with {shown}; IR-eligible tags are "
                f"{', '.join(sorted(IR_ELIGIBLE))}.")
    bench = [s for s in slots if slot_label(s["slot"]) == "BN"]
    if bench and all(not s.get("empty") for s in bench):
        out.append({"severity": "info", "type": "bench_full", "slot": "BN", "player": None, "position": None,
                    "detail": f"All {len(bench)} bench spots are filled, so any add needs a drop."})
    order = {"high": 0, "medium": 1, "info": 2}
    return sorted(out, key=lambda p: order[p["severity"]])


def bye_outlook(slots: list[dict], week: int, weeks: int = BYE_WEEKS_AHEAD) -> list[dict]:
    """For each of the next ``weeks`` weeks: who is on bye and how many active RBs remain."""
    rostered = [s for s in slots if not s.get("empty") and slot_label(s["slot"]) != "IR"]
    out = []
    for w in range(week, week + weeks):
        on_bye = [s for s in rostered if _bye_week(s) == w]
        rbs = [s for s in rostered if (s.get("position") or "").upper() == "RB"
               and _bye_week(s) != w and s.get("injury") not in UNAVAILABLE_TAGS]
        out.append({
            "week": w, "on_bye": [{"name": s["name"], "position": s.get("position"),
                                   "slot": slot_label(s["slot"])} for s in on_bye],
            "starters_on_bye": sum(1 for s in on_bye if _is_starter(s)),
            "active_rbs": len(rbs),
            "flag": f"fewer than {MIN_ACTIVE_RBS} active RBs" if len(rbs) < MIN_ACTIVE_RBS else None,
        })
    return out


def wire_positions(problems: list[dict], outlook: list[dict]) -> list[str]:
    """Positions worth looking at on the wire: where a problem or an RB bye hole is."""
    wanted: list[str] = []

    def want(pos: str | None):
        if pos and pos.upper() in WIRE_POSITIONS and pos.upper() not in wanted:
            wanted.append(pos.upper())

    for p in problems:
        if p["type"] in ("starter_on_bye", "starter_out", "starter_doubtful"):
            want(p["position"])
        elif p["type"] == "empty_starting_slot":
            for pos in FLEX_POSITIONS.get(p["slot"], [p["slot"]]):
                want(pos)
    if any(w["flag"] for w in outlook):
        want("RB")
    return wanted


def _opponent(matchup: dict, mid: str) -> tuple[dict | None, list[str]]:
    sides = [matchup.get("home") or {}, matchup.get("away") or {}]
    mine = next((s for s in sides if (s.get("team") or {}).get("mid") == str(mid)), None)
    theirs = next((s for s in sides if s is not mine and s.get("team")), None)
    if mine is None or theirs is None:
        return None, ["No opponent found on the matchup page for this week (a bye week, or the page changed)."]
    injured = [{"name": p["name"], "slot": p.get("slot"), "tag": p["injury"]}
               for p in theirs["starters"] if p.get("injury") and not p.get("empty")]
    byes = [p["name"] for p in theirs["starters"] if (p.get("game") or {}).get("bye")]
    return {
        "team": theirs["team"]["name"], "mid": theirs["team"]["mid"],
        "projected_points": theirs.get("total_proj"), "my_projected_points": mine.get("total_proj"),
        "injured_starters": injured, "starters_on_bye": byes,
    }, []


def _wire_row(p: dict) -> dict:
    a = p.get("availability") or {}
    tag = "FA" if a.get("state") == "FA" else (f"W ({a['clears']})" if a.get("clears") else a.get("state"))
    return {"name": p["name"], "team": p.get("team"), "position": p.get("position"),
            "tag": tag, "injury": p.get("injury")}


def _tag_targets(wire: dict, warnings: list[str], index: dict | None = None) -> None:
    """Put Yahoo's own FA / W tag on each target, and say so when it contradicts the advice.

    The targets are ranked from Sleeper's player pool, which does not know that Yahoo holds a
    player on waivers: "add now" there can be a claim-only ``W (date)`` here. Yahoo's tag wins.
    """
    by_name, by_team = {}, {}
    # ``index`` holds every row read from Yahoo; the displayed wire is only the top few.
    for rows in (index or wire["positions"]).values():
        for r in rows:
            by_name[(r["name"] or "").lower()] = r["tag"]
            by_team[(r["position"], norm_team(r["team"]))] = r["tag"]  # Sleeper WSH == Yahoo WAS
    for t in wire["targets"]:
        tag = by_name.get((t["name"] or "").lower()) or by_team.get((t["position"], norm_team(t["team"])))
        t["yahoo_tag"] = tag
        if tag and tag.startswith("W") and t.get("waiver_recommendation") in ("add_now", "add"):
            warnings.append(f"{t['name']}: nfl_mcp says add now, but Yahoo lists {tag}, a waiver claim and not an "
                            "instant add. Follow Yahoo's tag.")


async def build_checkin(
    prov, league_id: str, mid: str, week: int, *,
    compare: Callable[..., Awaitable[dict]],
    targets: Callable[..., Awaitable[dict]] | None = None,
    posts: Callable[..., Awaitable[dict]] | None = None,
    wire_depth: int = 6,
) -> dict:
    warnings: list[str] = []
    mid = str(mid)

    league = await prov.get_league()
    meta = league["metadata"]
    diff = meta.get("scoring_diff_from_bfs") or {}
    order = await prov.get_waiver_order()
    mine_w = next((t for t in order if t["mid"] == mid), None)
    league_block = {
        "scoring_source": meta.get("scoring_source"), "scoring_diff_from_bfs": diff,
        "waiver_order": [{"priority": t["waiver"], "team": t["name"], "mid": t["mid"]} for t in order],
        "my_waiver_position": mine_w["waiver"] if mine_w else None,
        "my_team": mine_w["name"] if mine_w else None,
    }
    if diff:
        warnings.append(f"Scoring differs from the verified BFS constants: {diff}. Confirm with the commissioner "
                        "before trusting projections.")

    slots = await prov.data.roster(mid)
    rows = await prov.get_projection_inputs(mid)
    cmp = await compare(league_id, rows, week)
    warnings += cmp.get("warnings") or []
    totals = cmp["starter_totals"]
    noise = abs(totals["gap"]) < NOISE_BAND_POINTS
    gap_note = (f"Team projection gap {totals['gap']:+.1f} (ours {totals['ours']} vs Yahoo {totals['yahoo']}) "
                + ("is under 12 team points: noise, not a reason to change a lineup."
                   if noise else "is 12+ team points: worth finding out why they disagree."))
    warnings.append(gap_note)
    model_only = [p["name"] for p in cmp["players"] if p["projection_source"] == "model_only" and not p["on_bye"]]
    if model_only:
        warnings.append("Projected by nfl_mcp's model alone (no Sleeper projection): " + ", ".join(model_only) + ".")

    problems = find_problems(slots, week)
    outlook = bye_outlook(slots, week)
    positions = wire_positions(problems, outlook)

    wire: dict = {"positions": {}, "targets": []}
    wire_index: dict[str, list[dict]] = {}
    for pos in positions:
        try:
            rows = [_wire_row(p) for p in await prov.get_free_agents(pos)]
            wire_index[pos] = rows
            wire["positions"][pos] = rows[:wire_depth]
        except Exception as e:  # one page failing should not sink the check-in
            warnings.append(f"Could not read the {pos} wire: {e}")
    if targets is not None:
        try:
            t = await targets(league_id=league_id, roster_id=int(mid), week=week,
                              positions=positions or None, limit=5)
            wire["targets"] = [{
                "name": x.get("name"), "position": x.get("position"), "team": x.get("team"),
                "projected_points": x.get("projected_points"), "upgrade_points": x.get("upgrade_points"),
                "verdict": x.get("verdict"),
                "waiver_recommendation": (x.get("waiver_strategy") or {}).get("recommendation"),
            } for x in (t.get("targets") or [])]
            _tag_targets(wire, warnings, wire_index)
            if t.get("success") is False:
                warnings.append("get_waiver_targets failed: " + str(t.get("error")))
            else:
                warnings.append("Waiver targets are ranked from Sleeper's player pool and nfl_mcp's projections; "
                                "check each against the Yahoo FA / W tags above.")
        except Exception as e:
            warnings.append(f"get_waiver_targets unavailable: {e}")

    opponent, opp_warn = None, []
    try:
        opponent, opp_warn = _opponent(await prov.data.matchup(week, mid), mid)
    except Exception as e:
        opp_warn = [f"Could not read the matchup page: {e}"]
    warnings += opp_warn

    news = {"items": [], "count": 0}
    if posts is not None:
        names = [s["name"] for s in slots if not s.get("empty") and s.get("name")]
        try:
            res = await posts(None, 48, 40, names)
            items = [dict(i, status="lead", single_source=True)
                     for i in res.get("items", []) if i.get("mentions_watched_player")]
            news = {"items": items, "count": len(items)}
            if res.get("errors"):
                warnings.append(f"{res['errors']} Bluesky handle(s) failed to load.")
            if items:
                warnings.append("Every news item is a single-source lead from Bluesky; confirm player, body part, "
                                "status and date in a second independent source before acting.")
        except Exception as e:
            warnings.append(f"News unavailable: {e}")

    warnings.append("Injury tags, bye weeks and Yahoo projections come from Yahoo's pages only and are not "
                    "cross-checked against a second source.")
    return {
        "success": True, "read_only": True, "note": READ_ONLY_NOTE,
        "week": week, "league_id": league_id, "roster_id": mid,
        "league": league_block,
        "roster": {"slots": cmp["players"], "starter_totals": totals, "gap_is_noise": noise,
                   "biggest_gaps": cmp["biggest_gaps"]},
        "problems": problems,
        "wire": wire,
        "opponent": opponent,
        "byes": outlook,
        "news": news,
        "warnings": warnings,
    }
