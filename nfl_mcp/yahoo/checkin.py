"""One-call weekly check-in for a Yahoo league. READ-ONLY: nothing here changes Yahoo.

``build_checkin`` gathers the pieces the other tools already have (league settings, waiver
order, roster with projections, the matchup, the wire, byes, reporter posts) into one structure
and flags what needs a decision. Pure helpers (``find_problems``, ``bye_outlook``,
``wire_positions``, ``verify_roster``, ``drop_candidates``) take parsed data, so they are tested
without a browser.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import date

from ..teams import normalize_team
from .idmap import norm_team
from .parsers import IR_ELIGIBLE
from .provider import slot_label

logger = logging.getLogger(__name__)

STARTER_EXCLUDED = ("BN", "IR")
FLEX_POSITIONS = {"FLEX": ["RB", "WR", "TE"], "W/R/T": ["RB", "WR", "TE"], "W/R": ["RB", "WR"]}
WIRE_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
# A back with one of these tags is not going to play, so he does not count as an active RB.
UNAVAILABLE_TAGS = {"O", "IR", "IR-R", "PUP-R", "NFI-R", "SUSP", "COV", "NA"}
RESERVE_TAGS = {"IR", "IR-R", "PUP-R", "NFI-R"}
MIN_ACTIVE_RBS = 2
STARTERS_ON_BYE_FLAG = 2  # a week with this many starters on bye is flagged
BYE_WEEKS_AHEAD = 4
WIRE_CANDIDATES = 10  # rows read per position and projected; the best few are shown
# Below this many team points, a gap between nfl_mcp's and Yahoo's projection is noise.
NOISE_BAND_POINTS = 12.0
READ_ONLY_NOTE = "Read-only: nothing was added, dropped, claimed, traded or changed on Yahoo."

# nfl_mcp's injury wording -> Yahoo's tag letters.
_NFL_TAGS = (("questionable", "Q"), ("doubtful", "D"), ("out", "O"), ("injured reserve", "IR"), ("ir", "IR"),
             ("pup", "PUP-R"), ("nfi", "NFI-R"), ("susp", "SUSP"), ("covid", "COV"))
_WAIVER_TAG = re.compile(r"^W \((\w{3}) (\d{1,2})\)$")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


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
    """For each of the next ``weeks`` weeks: who is on bye, how many active RBs remain, and any flags.

    A week is flagged when fewer than two active RBs remain, or when two or more starters are on
    bye (a week can break the lineup while still leaving two backs).
    """
    rostered = [s for s in slots if not s.get("empty") and slot_label(s["slot"]) != "IR"]
    out = []
    for w in range(week, week + weeks):
        on_bye = [s for s in rostered if _bye_week(s) == w]
        starters_out = [s for s in on_bye if _is_starter(s)]
        rbs = [s for s in rostered if (s.get("position") or "").upper() == "RB"
               and _bye_week(s) != w and s.get("injury") not in UNAVAILABLE_TAGS]
        flags = []
        if len(rbs) < MIN_ACTIVE_RBS:
            flags.append(f"fewer than {MIN_ACTIVE_RBS} active RBs")
        if len(starters_out) >= STARTERS_ON_BYE_FLAG:
            flags.append(f"{len(starters_out)} starters on bye ("
                         + ", ".join(f"{s['name']} at {slot_label(s['slot'])}" for s in starters_out) + ")")
        out.append({
            "week": w, "on_bye": [{"name": s["name"], "position": s.get("position"),
                                   "slot": slot_label(s["slot"])} for s in on_bye],
            "starters_on_bye": len(starters_out),
            "starters_on_bye_positions": [s.get("position") for s in starters_out],
            "active_rbs": len(rbs),
            "flags": flags, "flag": "; ".join(flags) if flags else None,
        })
    return out


def wire_positions(problems: list[dict], outlook: list[dict]) -> list[str]:
    """Positions worth looking at on the wire: where a problem or a bye hole is."""
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
    for w in outlook:
        if w["starters_on_bye"] >= STARTERS_ON_BYE_FLAG:
            for pos in w["starters_on_bye_positions"]:
                want(pos)
        if any(f.startswith("fewer than") for f in w["flags"]):
            want("RB")
    return wanted


# ------------------------------------------------------------------ verification
def nfl_tag(status: str | None) -> str | None:
    """nfl_mcp's injury status in Yahoo's tag letters; None for healthy / probable / unknown."""
    s = (status or "").strip().lower()
    if not s:
        return None
    for word, tag in _NFL_TAGS:
        if s == word or s.startswith(word):
            return tag
    return None


def _same_tag(a: str | None, b: str | None) -> bool:
    return a == b or (a in RESERVE_TAGS and b in RESERVE_TAGS)


def verify_roster(players: list[dict], week: int, schedules: dict[int, dict | None],
                  weeks: int = BYE_WEEKS_AHEAD) -> dict:
    """Cross-check Yahoo's injury tags and bye weeks against nfl_mcp's own data.

    ``players`` are comparison rows (Yahoo tag and bye week, plus nfl_mcp's injury status and
    practice line); ``schedules`` maps a week to ``{team: opponent}`` or None when that week's NFL
    schedule is not cached. Verdicts: ``agree``, ``conflict``, ``yahoo_only`` (nfl_mcp has nothing
    on him, so Yahoo's tag is unverified), ``other_only`` (nfl_mcp reports an injury Yahoo does not
    show) and, for byes, ``unverified`` (no schedule for that week).
    """
    injuries, byes = [], []
    for p in players:
        if p["slot"] == "IR" and not p.get("injury"):
            continue
        y, n = p.get("injury"), nfl_tag(p.get("nfl_injury_status"))
        if y or n:
            if y and n:
                verdict = "agree" if _same_tag(y, n) else "conflict"
            else:
                verdict = "yahoo_only" if y else "other_only"
            bits = [f"Yahoo {y or 'no tag'}", f"nfl_mcp {p.get('nfl_injury_status') or 'no report'}"
                    + (f" ({p['nfl_injury_source']})" if p.get("nfl_injury_source") and n else "")]
            if p.get("practice_status"):
                bits.append(f"practice: {p['practice_status']}")
            if p.get("gameday_status"):
                bits.append(f"game day: {p['gameday_status']}")
            injuries.append({"name": p["name"], "slot": p["slot"], "yahoo_tag": y,
                             "nfl_mcp_status": p.get("nfl_injury_status"), "practice": p.get("practice_status"),
                             "gameday": p.get("gameday_status"), "verdict": verdict, "detail": "; ".join(bits)})
        team = normalize_team(p.get("team"))
        yb = p.get("bye_week")
        if p["slot"] == "IR" or not team:
            continue
        for w in range(week, week + weeks):
            sched = schedules.get(w)
            yahoo_bye = yb is not None and int(yb) == w
            if sched is None:
                if yahoo_bye:
                    byes.append({"name": p["name"], "team": p["team"], "week": w, "yahoo_bye_week": int(yb),
                                 "verdict": "unverified",
                                 "detail": f"No cached NFL schedule for week {w}; only Yahoo says {p['team']} is on bye."})
                continue
            sched_bye = team not in sched
            if yahoo_bye and sched_bye:
                byes.append({"name": p["name"], "team": p["team"], "week": w, "yahoo_bye_week": w,
                             "verdict": "agree", "detail": f"The NFL schedule has no {team} game in week {w}."})
            elif yahoo_bye != sched_bye:
                said = f"Yahoo has the bye in week {int(yb)}" if yb is not None else "Yahoo shows no bye week"
                sched_says = (f"the NFL schedule has no {team} game in week {w}" if sched_bye
                              else f"the NFL schedule has {team} playing {sched[team]} in week {w}")
                byes.append({"name": p["name"], "team": p["team"], "week": w,
                             "yahoo_bye_week": int(yb) if yb is not None else None,
                             "verdict": "conflict", "detail": f"{said}, but {sched_says}."})

    def count(rows):
        out: dict[str, int] = {}
        for r in rows:
            out[r["verdict"]] = out.get(r["verdict"], 0) + 1
        return out
    return {"injuries": injuries, "byes": byes, "summary": {"injuries": count(injuries), "byes": count(byes)}}


def verification_warnings(verify: dict) -> list[str]:
    """Plain-language warnings from a verification: every conflict, and what is still single-source."""
    out = []
    for r in verify["injuries"]:
        if r["verdict"] == "conflict":
            out.append(f"Injury tag conflict for {r['name']}: {r['detail']}. Check the team report before deciding.")
        elif r["verdict"] == "other_only":
            out.append(f"{r['name']}: nfl_mcp has an injury report ({r['nfl_mcp_status']}) but Yahoo shows no tag.")
    for r in verify["byes"]:
        if r["verdict"] == "conflict":
            out.append(f"Bye conflict for {r['name']}: {r['detail']}")
    inj, bye = verify["summary"]["injuries"], verify["summary"]["byes"]
    only = [r["name"] for r in verify["injuries"] if r["verdict"] == "yahoo_only"]
    if only:
        out.append("Injury tags only Yahoo reports (nfl_mcp has no matching report, so they are unverified): "
                   + ", ".join(only) + ".")
    if inj.get("agree"):
        out.append(f"{inj['agree']} injury tag(s) agree with nfl_mcp's injury data.")
    unver = sorted({r["name"] for r in verify["byes"] if r["verdict"] == "unverified"})
    if unver:
        out.append("Bye weeks not checked against an NFL schedule (none cached): " + ", ".join(unver) + ".")
    if bye.get("agree"):
        out.append(f"{bye['agree']} bye week(s) confirmed by the NFL schedule.")
    return out


# ------------------------------------------------------------------ wire, drops, timing
def drop_candidates(players: list[dict]) -> list[dict]:
    """Bench players, least useful this week first. A ranking by this week's projection only: it
    does not know long-term value, byes ahead or injuries, so it is an input to the decision."""
    depth: dict[str, int] = {}
    for p in players:
        if p["slot"] != "IR":
            depth[p["position"]] = depth.get(p["position"], 0) + 1
    rows = []
    for p in players:
        if p["slot"] != "BN":
            continue
        proj = p["our_projection"] if p["our_projection"] is not None else p["yahoo_projection"]
        rows.append({
            "name": p["name"], "position": p["position"], "team": p["team"], "this_week": proj,
            "on_bye": p["on_bye"], "bye_week": p["bye_week"], "injury": p["injury"],
            "players_at_position": depth.get(p["position"], 0),
            "note": ("on bye this week, so he costs nothing now; check his bye and role before cutting"
                     if p["on_bye"] else None),
        })
    # Players who play this week come first (cheapest first), then bye players, whose value is later.
    return sorted(rows, key=lambda r: (r["on_bye"], r["this_week"] is None, r["this_week"] or 0.0))


def _wire_row(p: dict) -> dict:
    a = p.get("availability") or {}
    tag = "FA" if a.get("state") == "FA" else (f"W ({a['clears']})" if a.get("clears") else a.get("state"))
    return {"name": p["name"], "team": p.get("team"), "position": p.get("position"),
            "player_id": p.get("player_id"), "tag": tag, "injury": p.get("injury"),
            "our_projection": None, "upgrade": None, "projection_source": None}


def _baseline(pos: str, players: list[dict]) -> tuple[float | None, str]:
    """What a wire player at ``pos`` would replace: 0 when a starter there is on bye or out, else the
    weakest starter's projection."""
    at = [p for p in players if p["position"] == pos and p["slot"] not in ("BN", "IR")]
    if not at:
        return None, "no starter at this position"
    if any(p["on_bye"] or p["injury"] == "O" for p in at):
        who = next(p for p in at if p["on_bye"] or p["injury"] == "O")
        return 0.0, f"{who['name']} is {'on bye' if who['on_bye'] else 'out'}"
    proj = [p["our_projection"] for p in at if p["our_projection"] is not None]
    return (min(proj) if proj else None), "weakest starter at the position"


async def _project_wire(project, league_id: str, week: int, pos: str, rows: list[dict],
                        players: list[dict]) -> list[dict]:
    """Project the candidates in nfl_mcp's own model and rank them; the upgrade is over what they replace."""
    res = await project([{"name": r["name"], "team": r["team"], "position": r["position"], "player_id": r["player_id"]}
                         for r in rows], league_id=league_id, week=week)
    by_name = {p.get("player"): p for p in res.get("projections") or []}
    base, base_note = _baseline(pos, players)
    for r in rows:
        p = by_name.get(r["name"])
        if p and not p.get("on_bye"):
            r["our_projection"] = p.get("projected_points")
            r["projection_source"] = p.get("projection_source")
            if r["our_projection"] is not None and base is not None:
                r["upgrade"] = round(r["our_projection"] - base, 1)
        r["upgrade_over"] = base_note
    return sorted(rows, key=lambda r: (r["our_projection"] is None, -(r["our_projection"] or 0.0)))


def claim_timing(wire: dict, today: date | None = None) -> list[str]:
    """When the ``W (date)`` claims on the displayed wire clear, relative to today."""
    today = today or date.today()
    dates: dict[tuple[int, int], str] = {}
    for rows in wire["positions"].values():
        for r in rows:
            m = _WAIVER_TAG.match(r["tag"] or "")
            if m:
                try:
                    d = date(today.year, _MONTHS[m.group(1).lower()], int(m.group(2)))
                except (KeyError, ValueError):
                    continue
                dates[(d.month, d.day)] = f"{m.group(1)} {int(m.group(2))}"
    notes = []
    for (month, day), label in sorted(dates.items()):
        delta = (date(today.year, month, day) - today).days
        when = ("today" if delta == 0 else "tomorrow" if delta == 1 else
                f"in {delta} days" if delta > 1 else f"{-delta} day(s) ago")
        notes.append(f"Claims tagged W ({label}) clear {when}: they are waiver claims, not instant adds"
                     + ("; the claim has to be in before then." if delta >= 0 else "."))
    return notes


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
    project: Callable[..., Awaitable[dict]] | None = None,
    targets: Callable[..., Awaitable[dict]] | None = None,
    posts: Callable[..., Awaitable[dict]] | None = None,
    schedules: Callable[[list[int]], Awaitable[dict]] | None = None,
    today: date | None = None,
    wire_depth: int = 6,
) -> dict:
    warnings: list[str] = []
    mid = str(mid)
    weeks = list(range(week, week + BYE_WEEKS_AHEAD))

    # Fetched (and cached) first: byes are checked against it, and projections need it.
    sched: dict[int, dict | None] = {}
    if schedules is not None:
        try:
            sched = await schedules(weeks)
        except Exception as e:
            warnings.append(f"NFL schedule unavailable ({e}); bye weeks cannot be cross-checked.")

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
    warnings.append(f"Team projection gap {totals['gap']:+.1f} (ours {totals['ours']} vs Yahoo {totals['yahoo']}) "
                    + ("is under 12 team points: noise, not a reason to change a lineup."
                       if noise else "is 12+ team points: worth finding out why they disagree."))
    model_only = [p["name"] for p in cmp["players"] if p["projection_source"] == "model_only" and not p["on_bye"]]
    if model_only:
        warnings.append("Projected by nfl_mcp's model alone (no Sleeper projection): " + ", ".join(model_only) + ".")

    problems = find_problems(slots, week)
    outlook = bye_outlook(slots, week)
    for w in outlook:
        if w["flags"] and w["week"] != week:
            warnings.append(f"Week {w['week']}: " + "; ".join(w["flags"]) + ".")
    positions = wire_positions(problems, outlook)

    wire: dict = {"positions": {}, "targets": [], "timing": []}
    wire_index: dict[str, list[dict]] = {}
    for pos in positions:
        try:
            wire_rows = [_wire_row(p) for p in (await prov.get_free_agents(pos))]
            wire_index[pos] = wire_rows
            candidates = wire_rows[:WIRE_CANDIDATES]
            if project is not None:
                try:
                    candidates = await _project_wire(project, league_id, week, pos, candidates, cmp["players"])
                except Exception as e:
                    warnings.append(f"Could not project the {pos} wire: {e}")
            wire["positions"][pos] = candidates[:wire_depth]
        except Exception as e:  # one page failing should not sink the check-in
            warnings.append(f"Could not read the {pos} wire: {e}")
    wire["timing"] = claim_timing(wire, today)
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

    drops = drop_candidates(cmp["players"])
    verify = verify_roster(cmp["players"], week, sched)
    warnings += verification_warnings(verify)

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

    warnings.append("Yahoo's projections are a comparison source only. Injury tags and bye weeks are cross-checked "
                    "above where nfl_mcp has data; anything marked yahoo_only or unverified rests on Yahoo alone.")
    return {
        "success": True, "read_only": True, "note": READ_ONLY_NOTE,
        "week": week, "league_id": league_id, "roster_id": mid,
        "league": league_block,
        "roster": {"slots": cmp["players"], "starter_totals": totals, "gap_is_noise": noise,
                   "biggest_gaps": cmp["biggest_gaps"]},
        "problems": problems,
        "wire": wire,
        "drop_candidates": drops,
        "opponent": opponent,
        "byes": outlook,
        "verification": verify,
        "news": news,
        "warnings": warnings,
    }
