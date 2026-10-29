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
from datetime import UTC, date, datetime, timedelta

from ..teams import normalize_team
from .idmap import norm_name, norm_team, player_key, player_label
from .parsers import IR_ELIGIBLE
from .provider import slot_label

logger = logging.getLogger(__name__)

STARTER_EXCLUDED = ("BN", "IR")
FLEX_POSITIONS = {"FLEX": ["RB", "WR", "TE"], "W/R/T": ["RB", "WR", "TE"], "W/R": ["RB", "WR"]}
WIRE_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
# A back with one of these tags is not going to play, so he does not count as an active RB.
UNAVAILABLE_TAGS = {"O", "IR", "IR-R", "PUP-R", "NFI-R", "SUSP", "COV", "NA"}
RESERVE_TAGS = {"IR", "IR-R", "PUP-R", "NFI-R"}
STARTERS_ON_BYE_FLAG = 2  # a week with this many starters on bye is flagged
BYE_WEEKS_AHEAD = 4
WIRE_CANDIDATES = 10  # rows read per position and projected; the best few are shown
TARGET_POOL = 10  # targets asked for before the Yahoo join drops the ones Yahoo does not list
TARGETS_SHOWN = 5
# Below this many team points, a gap between nfl_mcp's and Yahoo's projection is noise.
NOISE_BAND_POINTS = 12.0
# Half the running per-team projection error sets the band, never below this floor (team totals).
NOISE_FLOOR_POINTS = 10.0
# Two single players closer than this are a tie: the projections cannot tell them apart.
PLAYER_TIE_POINTS = 1.5
STALE_INJURY_DAYS = 2  # a stored injury report older than this is not current
READ_ONLY_NOTE = "Read-only: nothing was added, dropped, claimed, traded or changed on Yahoo."

# nfl_mcp's injury wording -> Yahoo's tag letters.
_NFL_TAGS = (("questionable", "Q"), ("doubtful", "D"), ("out", "O"), ("injured reserve", "IR"), ("ir", "IR"),
             ("pup", "PUP-R"), ("nfi", "NFI-R"), ("susp", "SUSP"), ("covid", "COV"))
_WAIVER_TAG = re.compile(r"^W \((\w{3}) (\d{1,2})\)$")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def noise_band_points(running_team_error: float | None = None) -> float:
    """Team-point band inside which two projections (or a matchup margin) cannot be told apart.

    Half the running per-team projection error with a floor of 10 when that error is known; the
    check-in's fixed 12 otherwise (nfl_mcp grades single players, not team totals, so there is
    no running team error to read yet).
    """
    if running_team_error is None:
        return NOISE_BAND_POINTS
    return max(NOISE_FLOOR_POINTS, float(running_team_error) / 2.0)


def label_of(p: dict) -> str:
    """``"Full Name (TEAM POS)"`` for a roster, wire or matchup row."""
    return player_label(p.get("name"), p.get("team"), p.get("position"))


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
                    "player": slot.get("name"), "label": label_of(slot), "position": slot.get("position"),
                    "detail": detail})

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
                add("high", "starter_on_bye", s, f"{label_of(s)} starts at {label} but is on bye in week {week}.")
            if tag == "O":
                add("high", "starter_out", s, f"{label_of(s)} is tagged O (out) in a starting slot.")
            elif tag == "D":
                add("medium", "starter_doubtful", s, f"{label_of(s)} is tagged D (doubtful) in a starting slot.")
        if label == "IR" and tag not in IR_ELIGIBLE:
            shown = tag or "no injury tag"
            add("medium", "ir_slot_not_eligible", s,
                f"{label_of(s)} is in an IR slot with {shown}; IR-eligible tags are "
                f"{', '.join(sorted(IR_ELIGIBLE))}.")
    bench = [s for s in slots if slot_label(s["slot"]) == "BN"]
    if bench and all(not s.get("empty") for s in bench):
        out.append({"severity": "info", "type": "bench_full", "slot": "BN", "player": None, "label": None, "position": None,
                    "detail": f"All {len(bench)} bench spots are filled, so any add needs a drop."})
    order = {"high": 0, "medium": 1, "info": 2}
    return sorted(out, key=lambda p: order[p["severity"]])


def own_slots(slots: list[dict]) -> dict[str, int]:
    """Starting slots that only one position can fill (RB: 2), flex slots not counted."""
    own: dict[str, int] = {}
    for s in slots:
        label = slot_label(s["slot"])
        if label not in STARTER_EXCLUDED and label not in FLEX_POSITIONS:
            own[label] = own.get(label, 0) + 1
    return own


def slots_to_fill(slots: list[dict]) -> dict[str, int]:
    """Players each position must supply if its own slots and every flex slot it can fill are used.

    RB with two RB slots and one FLEX needs 3; a position that cannot play flex needs only its own.
    """
    need = own_slots(slots)
    for s in slots:
        for pos in FLEX_POSITIONS.get(slot_label(s["slot"]), []):
            need[pos] = need.get(pos, 0) + 1
    return need


def bye_outlook(slots: list[dict], week: int, weeks: int = BYE_WEEKS_AHEAD) -> list[dict]:
    """For each of the next ``weeks`` weeks: which starters are out, how many eligible players are
    left at each position against the slots to fill, and any flags.

    A position is thin when its active players (not on bye, not tagged out, not on IR) number fewer
    than the slots it has to fill (RB: two slots plus the flex = 3); it is short, and worth a look on
    the wire, when they number fewer than its own slots. A week is also flagged when two
    or more starters are on bye, which can break the lineup while every position still has bodies.
    """
    rostered = [s for s in slots if not s.get("empty") and slot_label(s["slot"]) != "IR"]
    need, own = slots_to_fill(slots), own_slots(slots)
    out = []
    for w in range(week, week + weeks):
        on_bye = [s for s in rostered if _bye_week(s) == w]
        starters_out = [s for s in on_bye if _is_starter(s)]
        active = {pos: sum(1 for s in rostered if (s.get("position") or "").upper() == pos
                           and _bye_week(s) != w and s.get("injury") not in UNAVAILABLE_TAGS)
                  for pos in need}
        thin = [pos for pos, n in need.items() if active[pos] < n]
        holes = [pos for pos in thin if active[pos] < own.get(pos, 0)]
        flags = [f"thin at {pos}: {active[pos]} active for {need[pos]} slots to fill"
                 + (" (counting the FLEX)" if need[pos] > own.get(pos, 0) else "") for pos in thin]
        if len(starters_out) >= STARTERS_ON_BYE_FLAG:
            flags.append(f"{len(starters_out)} starters on bye ("
                         + ", ".join(f"{label_of(s)} at {slot_label(s['slot'])}" for s in starters_out) + ")")
        out.append({
            "week": w,
            "on_bye": [{"name": s["name"], "label": label_of(s), "position": s.get("position"),
                        "slot": slot_label(s["slot"])} for s in on_bye],
            "starters_out": [label_of(s) for s in starters_out],
            "starters_on_bye": len(starters_out),
            "starters_on_bye_positions": [s.get("position") for s in starters_out],
            "eligible_active": active, "slots_to_fill": need, "thin_positions": thin, "short_positions": holes,
            "active_rbs": active.get("RB", 0),
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
        for pos in w.get("short_positions") or []:  # fewer players than the position's own slots
            want(pos)
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


def _age_days(stamp: str | None, now: datetime) -> float | None:
    """Days since an ISO timestamp, or None when it is missing or unreadable."""
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (now - when).total_seconds() / 86400.0


def injury_flags(p: dict, refresh_stale: bool = False, now: datetime | None = None) -> list[str]:
    """Why a player's injury data must not drive a decision on its own.

    ``stale_injury``: nfl_mcp's refresh timed out, or its report is older than two days.
    ``injury_conflict``: Yahoo's tag and nfl_mcp's designation differ.
    ``yahoo_only``: Yahoo shows a tag and nfl_mcp has no report at all.
    """
    y, status = p.get("injury"), p.get("nfl_injury_status")
    if not (y or status):
        return []
    n = nfl_tag(status)
    flags = []
    if y and not status:
        flags.append("yahoo_only")
    if y and n and not _same_tag(y, n):
        flags.append("injury_conflict")
    age = _age_days(p.get("nfl_injury_reported_at"), now or datetime.now(UTC))
    if refresh_stale or (age is not None and age > STALE_INJURY_DAYS):
        flags.append("stale_injury")
    return flags


def projection_unknown(p: dict) -> str | None:
    """Why a projection of 0 cannot be trusted, else None.

    nfl_mcp prices an Out designation as 0. When that designation is stale, in conflict with
    Yahoo's tag, or only Yahoo's, the 0 is a guess about availability and not a measure of the
    player, so it is not used to rank him.
    """
    flags = p.get("injury_flags") or []
    if flags and nfl_tag(p.get("nfl_injury_status")) == "O" and p.get("our_projection") in (0, 0.0):
        return ("projection of 0 comes from an Out designation that is "
                + " / ".join(f.replace("_", " ") for f in flags) + "; check the team report")
    return None


def usable_projection(p: dict) -> float | None:
    """The projection to rank with: nfl_mcp's, else Yahoo's; None when unknown or unreliable."""
    if projection_unknown(p):
        return None
    mine = p.get("our_projection")
    return mine if mine is not None else p.get("yahoo_projection")


def _same_text(a: str | None, b: str | None) -> bool:
    return norm_name(a) == norm_name(b)


def verify_roster(players: list[dict], week: int, schedules: dict[int, dict | None],
                  weeks: int = BYE_WEEKS_AHEAD, refresh_stale: bool = False,
                  now: datetime | None = None) -> dict:
    """Cross-check Yahoo's injury tags and bye weeks against nfl_mcp's own data.

    ``players`` are comparison rows (Yahoo tag and bye week, plus nfl_mcp's injury status and
    practice line); ``schedules`` maps a week to ``{team: opponent}`` or None when that week's NFL
    schedule is not cached. Injury verdicts: ``agree`` only when designation, practice status and
    body part all match; ``partial`` when the designations match but another field differs or is
    missing (``fields_differ`` and ``fields_missing`` name them: Yahoo's roster page gives only
    the designation, so a Yahoo tag alone is never ``agree``); ``conflict`` (designations differ);
    ``yahoo_only`` (nfl_mcp has nothing on him, so Yahoo's tag is unverified); ``other_only``
    (nfl_mcp reports an injury Yahoo does not show). Byes: ``agree``, ``conflict``, ``unverified``
    (no schedule for that week).
    """
    injuries, byes = [], []
    for p in players:
        if p["slot"] == "IR" and not p.get("injury"):
            continue
        y, n = p.get("injury"), nfl_tag(p.get("nfl_injury_status"))
        if y or n:
            differ: list[str] = []
            missing: list[str] = []
            if y and n:
                for field, a, b, same in (
                        ("designation", y, n, _same_tag),
                        ("practice", p.get("yahoo_practice"), p.get("practice_status"), _same_text),
                        ("body part", p.get("yahoo_body_part"), p.get("nfl_injury_body_part"), _same_text)):
                    if a and b:
                        if not same(a, b):
                            differ.append(field)
                    else:
                        who = "both" if not a and not b else ("Yahoo" if not a else "nfl_mcp")
                        missing.append(f"{field} ({who})")
                if "designation" in differ:
                    verdict = "conflict"
                elif differ or missing:
                    verdict = "partial"
                else:
                    verdict = "agree"
            else:
                verdict = "yahoo_only" if y else "other_only"
            bits = [f"Yahoo {y or 'no tag'}", f"nfl_mcp {p.get('nfl_injury_status') or 'no report'}"
                    + (f" ({p['nfl_injury_source']})" if p.get("nfl_injury_source") and n else "")]
            if p.get("practice_status"):
                bits.append(f"practice: {p['practice_status']}")
            if p.get("nfl_injury_body_part"):
                bits.append(f"body part: {p['nfl_injury_body_part']}")
            if p.get("gameday_status"):
                bits.append(f"game day: {p['gameday_status']}")
            if differ:
                bits.append("differs: " + ", ".join(differ))
            if missing and verdict == "partial":
                bits.append("not compared, missing: " + ", ".join(missing))
            injuries.append({"name": p["name"], "label": label_of(p), "slot": p["slot"], "yahoo_tag": y,
                             "nfl_mcp_status": p.get("nfl_injury_status"), "practice": p.get("practice_status"),
                             "body_part": p.get("nfl_injury_body_part"),
                             "reported_at": p.get("nfl_injury_reported_at"),
                             "gameday": p.get("gameday_status"), "verdict": verdict,
                             "fields_differ": differ, "fields_missing": missing,
                             "flags": injury_flags(p, refresh_stale, now), "detail": "; ".join(bits)})
        team = normalize_team(p.get("team"))
        yb = p.get("bye_week")
        if p["slot"] == "IR" or not team:
            continue
        for w in range(week, week + weeks):
            sched = schedules.get(w)
            yahoo_bye = yb is not None and int(yb) == w
            if sched is None:
                if yahoo_bye:
                    byes.append({"name": p["name"], "label": label_of(p), "team": p["team"], "week": w, "yahoo_bye_week": int(yb),
                                 "verdict": "unverified",
                                 "detail": f"No cached NFL schedule for week {w}; only Yahoo says {p['team']} is on bye."})
                continue
            sched_bye = team not in sched
            if yahoo_bye and sched_bye:
                byes.append({"name": p["name"], "label": label_of(p), "team": p["team"], "week": w, "yahoo_bye_week": w,
                             "verdict": "agree", "detail": f"The NFL schedule has no {team} game in week {w}."})
            elif yahoo_bye != sched_bye:
                said = f"Yahoo has the bye in week {int(yb)}" if yb is not None else "Yahoo shows no bye week"
                sched_says = (f"the NFL schedule has no {team} game in week {w}" if sched_bye
                              else f"the NFL schedule has {team} playing {sched[team]} in week {w}")
                byes.append({"name": p["name"], "label": label_of(p), "team": p["team"], "week": w,
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
        who = r.get("label") or r["name"]
        if r["verdict"] == "conflict":
            out.append(f"Injury tag conflict for {who}: {r['detail']}. Check the team report before deciding.")
        elif r["verdict"] == "other_only":
            out.append(f"{who}: nfl_mcp has an injury report ({r['nfl_mcp_status']}) but Yahoo shows no tag.")
        if "stale_injury" in r.get("flags", []):
            out.append(f"{who}: injury data is stale (the refresh timed out or the report is more than "
                       f"{STALE_INJURY_DAYS} days old); do not act on it alone.")
    for r in verify["byes"]:
        if r["verdict"] == "conflict":
            out.append(f"Bye conflict for {r.get('label') or r['name']}: {r['detail']}")
    inj, bye = verify["summary"]["injuries"], verify["summary"]["byes"]
    only = [r.get("label") or r["name"] for r in verify["injuries"] if r["verdict"] == "yahoo_only"]
    if only:
        out.append("Injury tags only Yahoo reports (nfl_mcp has no matching report, so they are unverified): "
                   + ", ".join(only) + ".")
    for r in verify["injuries"]:
        if r["verdict"] == "partial":
            out.append(f"{r.get('label') or r['name']}: designations match but the check is partial"
                       + (f"; differs: {', '.join(r['fields_differ'])}" if r["fields_differ"] else "")
                       + (f"; not compared, missing: {', '.join(r['fields_missing'])}" if r["fields_missing"] else "")
                       + ".")
    if inj.get("agree"):
        out.append(f"{inj['agree']} injury tag(s) agree with nfl_mcp on designation, practice and body part.")
    unver = sorted({r.get("label") or r["name"] for r in verify["byes"] if r["verdict"] == "unverified"})
    if unver:
        out.append("Bye weeks not checked against an NFL schedule (none cached): " + ", ".join(unver) + ".")
    if bye.get("agree"):
        out.append(f"{bye['agree']} bye week(s) confirmed by the NFL schedule.")
    return out


# ------------------------------------------------------------------ wire, drops, timing
def _playable(p: dict) -> bool:
    """On the roster for this week's lineup: not on bye and not tagged out or reserved."""
    return not p.get("on_bye") and p.get("injury") not in UNAVAILABLE_TAGS


def _week_of(x) -> int | None:
    try:
        return int(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def _in_window(bye: int | None, week: int) -> bool:
    return bye is not None and week <= bye < week + BYE_WEEKS_AHEAD


def bench_review(players: list[dict], week: int) -> dict:
    """Rank the bench by what each player is for, not by this week's projection.

    A bench spot has to do one of two jobs: insurance (he can fill in for a starter at his position
    whose bye falls in the next four weeks, or who is hurt now) or a believable path to a bigger
    role (he projects within a tie of the weakest starter at his position, or his role is rising).
    The drop is whoever does neither. A player whose projection is an unreliable zero (an Out
    designation that is stale, contested or Yahoo-only) is not ranked: he is listed as unknown with
    the reason. Returns ``{"ranked": [...], "unknown": [...]}``, ranked least useful first.
    """
    starters = [p for p in players if p["slot"] not in ("BN", "IR")]
    bench = [p for p in players if p["slot"] == "BN"]
    depth: dict[str, int] = {}
    for p in players:
        if p["slot"] != "IR":
            depth[p["position"]] = depth.get(p["position"], 0) + 1

    def weakest(pos: str) -> tuple[dict, float] | None:
        rows = [(s, usable_projection(s)) for s in starters if s["position"] == pos and not s.get("on_bye")]
        rows = [(s, v) for s, v in rows if v is not None]
        return min(rows, key=lambda r: r[1]) if rows else None

    ranked, unknown = [], []
    covered_by: dict[str, list[str]] = {}
    jobs: dict[str, list[str]] = {}
    for b_ in bench:
        why = projection_unknown(b_)
        if why:
            unknown.append({"key": b_.get("key") or player_key(b_), "name": b_["name"], "label": label_of(b_),
                            "position": b_["position"], "team": b_["team"], "injury": b_.get("injury"),
                            "injury_flags": b_.get("injury_flags") or [], "reason": why,
                            "note": "not ranked: his projection is unknown, not zero"})
            continue
        mine: list[str] = []
        if b_.get("injury") not in UNAVAILABLE_TAGS:
            for st in starters:
                if st["position"] != b_["position"] or not st.get("name"):
                    continue
                sb = _week_of(st.get("bye_week"))
                if _in_window(sb, week) and _week_of(b_.get("bye_week")) != sb:
                    mine.append(f"covers the week {sb} bye of {label_of(st)}")
                    covered_by.setdefault(st.get("key") or player_key(st), []).append(b_.get("key") or player_key(b_))
                elif st.get("injury") in ("Q", "D", "O") and not b_.get("on_bye"):
                    mine.append(f"covers {label_of(st)}, who is tagged {st['injury']}")
                    covered_by.setdefault(st.get("key") or player_key(st), []).append(b_.get("key") or player_key(b_))
        path = None
        proj = usable_projection(b_) if not b_.get("on_bye") else None
        low = weakest(b_["position"])
        if b_.get("role_trend") == "role_up":
            path = "his role is rising (role_up)"
        elif proj is not None and low is not None and proj >= low[1] - PLAYER_TIE_POINTS:
            path = (f"projects {proj:.1f}, within {PLAYER_TIE_POINTS:g} of {label_of(low[0])} "
                    f"({low[1]:.1f}), the weakest starter at {b_['position']}")
        jobs[b_.get("key") or player_key(b_)] = mine
        ranked.append({"b": b_, "covers": mine, "path": path, "proj": proj})

    rows = []
    for r in ranked:
        b_ = r["b"]
        key = b_.get("key") or player_key(b_)
        mine_keys = [st for st, who in covered_by.items() if key in who]
        redundant = bool(r["covers"]) and all(len(set(covered_by[k])) > 1 for k in mine_keys)
        tier = 0 if not r["covers"] and not r["path"] else 1 if r["covers"] and redundant and not r["path"] else \
            2 if not r["covers"] else 3
        if b_.get("on_bye"):
            lead = "on bye this week (no projection); "
        else:
            lead = ""
        if tier == 0:
            note = (lead + "does neither job: covers no starter's bye or injury in the next "
                    f"{BYE_WEEKS_AHEAD} weeks and has no path to a bigger role")
        else:
            bits = list(r["covers"]) + ([f"path: {r['path']}"] if r["path"] else [])
            note = lead + "; ".join(bits) + ("; another bench player covers the same starter" if tier == 1 else "")
        rows.append({
            "key": key, "name": b_["name"], "label": label_of(b_), "position": b_["position"], "team": b_["team"],
            "player_id": b_.get("player_id"), "this_week": r["proj"], "on_bye": b_["on_bye"],
            "bye_week": b_.get("bye_week"), "injury": b_.get("injury"), "injury_flags": b_.get("injury_flags") or [],
            "players_at_position": depth.get(b_["position"], 0),
            "insurance_for": r["covers"], "path_to_role": r["path"], "does_a_job": tier > 0,
            "redundant_cover": tier == 1, "note": note,
            "_tier": tier,
        })
    rows.sort(key=lambda x: (x["_tier"], x["on_bye"], x["this_week"] is None, x["this_week"] or 0.0))
    for x in rows:
        x.pop("_tier")
    return {"ranked": rows, "unknown": unknown}


def drop_candidates(players: list[dict], week: int = 0) -> list[dict]:
    """The ranked half of :func:`bench_review`: bench players who do neither job come first."""
    return bench_review(players, week)["ranked"]


def bench_vs_wire(players: list[dict], wire: dict, week: int) -> list[dict]:
    """For each flagged starter (on bye or injury-tagged), the bench players who can fill in this
    week against the best projected wire pickup at his position.

    The bench is checked first: when the wire pickup beats the best bench option by less than a tie
    (:data:`PLAYER_TIE_POINTS`), the answer is "no add needed, use <bench player>".
    """
    starters = [p for p in players if p["slot"] not in ("BN", "IR") and p.get("name")]
    bench = [p for p in players if p["slot"] == "BN"]
    out = []
    for st in starters:
        if not (st.get("on_bye") or st.get("injury")):
            continue
        allowed = FLEX_POSITIONS.get(slot_label(st["slot"]), [st["position"]])
        base = None if st.get("on_bye") else usable_projection(st)
        options = [b for b in bench if b["position"] in allowed and _playable(b) and usable_projection(b) is not None]
        options.sort(key=lambda b: -usable_projection(b))
        same_or_better = [b for b in options if base is None or usable_projection(b) >= base]
        best_bench = options[0] if options else None
        wire_rows = [r for r in wire["positions"].get(st["position"], []) if r.get("our_projection") is not None]
        best_wire = max(wire_rows, key=lambda r: r["our_projection"]) if wire_rows else None
        reason = "on bye this week" if st.get("on_bye") else f"tagged {st['injury']}"
        row = {
            "starter": label_of(st), "slot": slot_label(st["slot"]), "reason": reason,
            "starter_projection": base,
            "bench_replacements": [{"player": label_of(b), "projection": usable_projection(b)} for b in same_or_better],
            "best_bench": ({"player": label_of(best_bench), "projection": usable_projection(best_bench)}
                           if best_bench else None),
            "best_wire": ({"player": f"{best_wire['name']} ({best_wire.get('team')} {best_wire.get('position')})",
                           "tag": best_wire["tag"], "projection": best_wire["our_projection"]} if best_wire else None),
        }
        if best_bench and (best_wire is None or best_wire["our_projection"] - usable_projection(best_bench)
                           < PLAYER_TIE_POINTS):
            row["gap"] = (round(best_wire["our_projection"] - usable_projection(best_bench), 1) if best_wire else None)
            row["decision"] = f"no add needed, use {label_of(best_bench)}"
            if best_wire:
                row["decision"] += (f" (the best wire pickup is {row['gap']:+.1f} against him: "
                                    + ("a tie" if row["gap"] > -PLAYER_TIE_POINTS else "no better")
                                    + f", under the {PLAYER_TIE_POINTS:g}-point tie band)")
        elif best_bench:
            row["gap"] = round(best_wire["our_projection"] - usable_projection(best_bench), 1)
            row["decision"] = (f"{row['best_wire']['player']} ({best_wire['tag']}) projects {row['gap']:.1f} above "
                               f"the best bench option {label_of(best_bench)}: worth an add")
        elif best_wire:
            row["gap"] = None
            row["decision"] = (f"no eligible bench replacement; best wire pickup {row['best_wire']['player']} "
                               f"({best_wire['tag']})")
        else:
            row["gap"] = None
            row["decision"] = "no eligible bench replacement and no projected wire player"
        out.append(row)
    return out


def _wire_row(p: dict) -> dict:
    a = p.get("availability") or {}
    tag = "FA" if a.get("state") == "FA" else (f"W ({a['clears']})" if a.get("clears") else a.get("state"))
    return {"name": p["name"], "label": label_of(p), "team": p.get("team"), "position": p.get("position"),
            "player_id": p.get("player_id"), "tag": tag, "injury": p.get("injury"),
            "our_projection": None, "upgrade": None, "projection_source": None}


def _baseline(pos: str, players: list[dict]) -> tuple[float | None, str]:
    """What a wire player at ``pos`` would replace: 0 when a starter there is on bye or out, else the
    weakest starter's projection (a starter whose projection is unknown is left out)."""
    at = [p for p in players if p["position"] == pos and p["slot"] not in ("BN", "IR")]
    if not at:
        return None, "no starter at this position"
    if any(p["on_bye"] or p["injury"] == "O" for p in at):
        who = next(p for p in at if p["on_bye"] or p["injury"] == "O")
        return 0.0, f"{label_of(who)} is {'on bye' if who['on_bye'] else 'out'}"
    proj = [v for v in (usable_projection(p) for p in at) if v is not None]
    return (min(proj) if proj else None), "weakest starter at the position"


async def _project_wire(project, league_id: str, week: int, pos: str, rows: list[dict],
                        players: list[dict]) -> list[dict]:
    """Project the candidates in nfl_mcp's own model and rank them; the upgrade is over what they replace.

    Results are matched to candidates by player id, then by name only when one result carries it.
    """
    res = await project([{"name": r["name"], "team": r["team"], "position": r["position"], "player_id": r["player_id"]}
                         for r in rows], league_id=league_id, week=week)
    by_id: dict[str, dict] = {}
    by_name: dict[str | None, list[dict]] = {}
    for p in res.get("projections") or []:
        if p.get("player_id") not in (None, ""):
            by_id[str(p["player_id"])] = p
        by_name.setdefault(p.get("player"), []).append(p)
    base, base_note = _baseline(pos, players)
    for r in rows:
        p = by_id.get(str(r["player_id"])) if r.get("player_id") not in (None, "") else None
        if p is None and len(by_name.get(r["name"]) or []) == 1:
            p = by_name[r["name"]][0]
        if p and not p.get("on_bye"):
            r["our_projection"] = p.get("projected_points")
            r["projection_source"] = p.get("projection_source")
            if r["our_projection"] is not None and base is not None:
                r["upgrade"] = round(r["our_projection"] - base, 1)
        r["upgrade_over"] = base_note
    return sorted(rows, key=lambda r: (r["our_projection"] is None, -(r["our_projection"] or 0.0)))


def wire_reasons(problems: list[dict], outlook: list[dict]) -> dict[str, list[str]]:
    """Why each wire position is listed, so a future-bye position is not read as this week's lineup."""
    out: dict[str, list[str]] = {}

    def add(pos: str | None, why: str):
        if pos and pos.upper() in WIRE_POSITIONS:
            out.setdefault(pos.upper(), [])
            if why not in out[pos.upper()]:
                out[pos.upper()].append(why)

    for p in problems:
        if p["type"] in ("starter_on_bye", "starter_out", "starter_doubtful"):
            add(p["position"], p["detail"])
        elif p["type"] == "empty_starting_slot":
            for pos in FLEX_POSITIONS.get(p["slot"], [p["slot"]]):
                add(pos, p["detail"])
    for w in outlook:
        if w["starters_on_bye"] >= STARTERS_ON_BYE_FLAG:
            for pos in w["starters_on_bye_positions"]:
                add(pos, f"week {w['week']}: {w['starters_on_bye']} starters on bye (depth for a later week)")
        for pos in w.get("short_positions") or []:
            later = "" if w["week"] == outlook[0]["week"] else " (depth for a later week)"
            add(pos, f"week {w['week']}: {w['eligible_active'][pos]} active {pos}, fewer than its own "
                     f"slots{later}")
    return out


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


def matchup_read(margin: float | None, band: float) -> str | None:
    """How to read a projected margin: only outside the noise band is it a good (or tough) matchup."""
    if margin is None:
        return None
    if abs(margin) <= band:
        return f"inside the noise band (margin {margin:+.1f}, band +/-{band:g}): treat it as a toss-up"
    if margin > 0:
        return f"good matchup: projected ahead by {margin:.1f}, outside the +/-{band:g} noise band"
    return f"tough matchup: projected behind by {-margin:.1f}, outside the +/-{band:g} noise band"


def _opponent(matchup: dict, mid: str, band: float = NOISE_BAND_POINTS) -> tuple[dict | None, list[str]]:
    sides = [matchup.get("home") or {}, matchup.get("away") or {}]
    mine = next((s for s in sides if (s.get("team") or {}).get("mid") == str(mid)), None)
    theirs = next((s for s in sides if s is not mine and s.get("team")), None)
    if mine is None or theirs is None:
        return None, ["No opponent found on the matchup page for this week (a bye week, or the page changed)."]
    injured = [{"name": p["name"], "label": label_of(p), "slot": p.get("slot"), "tag": p["injury"]}
               for p in theirs["starters"] if p.get("injury") and not p.get("empty")]
    byes = [label_of(p) for p in theirs["starters"] if (p.get("game") or {}).get("bye")]
    theirs_pts, mine_pts = theirs.get("total_proj"), mine.get("total_proj")
    margin = (round(mine_pts - theirs_pts, 2)
              if isinstance(theirs_pts, (int, float)) and isinstance(mine_pts, (int, float)) else None)
    return {
        "team": theirs["team"]["name"], "mid": theirs["team"]["mid"],
        "projected_points": theirs_pts, "my_projected_points": mine_pts,
        "margin": margin, "noise_band": band, "read": matchup_read(margin, band),
        "injured_starters": injured, "starters_on_bye": byes,
    }, []


def _join_targets(raw: list[dict], index: dict[str, list[dict]],
                  warnings: list[str]) -> tuple[list[dict], list[str]]:
    """Keep only the targets that are available on Yahoo, each carrying Yahoo's own FA / W (date) tag.

    The targets are ranked from Sleeper's player pool, which does not know what Yahoo holds on
    waivers. They are joined to the Yahoo free-agent rows by player id; failing that, by name, team
    and position when exactly one row matches (never by surname), and a defense by its team. A
    target that cannot be mapped to a Yahoo player is dropped and named in ``warnings``.
    Yahoo's tag wins over the target's own ``waiver_recommendation``.
    """
    rows = [r for pos_rows in index.values() for r in pos_rows]
    by_id = {str(r["player_id"]): r for r in rows if r.get("player_id") not in (None, "")}
    by_ident: dict[tuple, list[dict]] = {}
    for r in rows:
        by_ident.setdefault((norm_name(r["name"]), norm_team(r["team"]), (r["position"] or "").upper()), []).append(r)
    kept, dropped = [], []
    for t in raw:
        pos = (t.get("position") or "").upper()
        hit = by_id.get(str(t.get("player_id"))) if t.get("player_id") not in (None, "") else None
        if hit is None:
            cands = by_ident.get((norm_name(t.get("name")), norm_team(t.get("team")), pos)) or []
            if len(cands) == 1:
                hit = cands[0]
        if hit is None and pos in ("DEF", "DST"):
            cands = [r for r in rows if (r["position"] or "").upper() in ("DEF", "DST")
                     and norm_team(r["team"]) == norm_team(t.get("team"))]
            if len(cands) == 1:
                hit = cands[0]
        who = label_of(t)
        if hit is None:
            warnings.append(f"{who} was ranked from Sleeper's player pool but could not be matched to a Yahoo "
                            "free agent, so it was dropped from targets (its Yahoo availability is unconfirmed).")
            dropped.append(who)
            continue
        tag = hit["tag"]
        kept.append({**t, "label": label_of(hit), "player_id": hit.get("player_id") or t.get("player_id"),
                     "tag": tag, "yahoo_tag": tag})
        if tag and tag.startswith("W") and t.get("waiver_recommendation") in ("add_now", "add"):
            warnings.append(f"{who}: nfl_mcp says add now, but Yahoo lists {tag}, a waiver claim and not an "
                            "instant add. Follow Yahoo's tag.")
    return kept, dropped


def news_origins(items: list[dict], labels_by_name: dict[str, list[str]]) -> dict:
    """Group reporter posts by independent origin.

    Posts relayed by the same aggregator account are one source however many there are, so the
    count that matters is the number of distinct accounts, not of posts. Per player and overall.
    """
    by_player: dict[str, dict] = {}
    for i in items:
        origin = i.get("author") or "unknown"
        i["origin"] = origin
        m = re.match(r"\s*\[([^\]]+)\]", i.get("text") or "")
        if m:
            i["relayed_reporter"] = m.group(1)
        i["mentions"] = [lab for n in i.get("mentions_watched_player") or [] for lab in labels_by_name.get(n, [n])]
        for lab in i["mentions"]:
            e = by_player.setdefault(lab, {"posts": 0, "origins": []})
            e["posts"] += 1
            if origin not in e["origins"]:
                e["origins"].append(origin)
    for e in by_player.values():
        e["independent_origins"] = len(e["origins"])
    for i in items:
        counts = [by_player[lab]["independent_origins"] for lab in i["mentions"]]
        i["independent_origins"] = min(counts) if counts else 1
        i["single_source"] = i["independent_origins"] < 2
    origins = sorted({i["origin"] for i in items})
    return {"by_player": by_player, "origins": origins, "independent_origins": len(origins)}


async def build_checkin(
    prov, league_id: str, mid: str, week: int, *,
    compare: Callable[..., Awaitable[dict]],
    project: Callable[..., Awaitable[dict]] | None = None,
    targets: Callable[..., Awaitable[dict]] | None = None,
    posts: Callable[..., Awaitable[dict]] | None = None,
    schedules: Callable[[list[int]], Awaitable[dict]] | None = None,
    refresh_injuries: Callable[[list[str]], Awaitable[tuple]] | None = None,
    today: date | None = None,
    now: datetime | None = None,
    running_team_error: float | None = None,
    wire_depth: int = 6,
) -> dict:
    warnings: list[str] = []
    mid = str(mid)
    weeks = list(range(week, week + BYE_WEEKS_AHEAD))
    band = noise_band_points(running_team_error)

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
    # nfl_mcp's own injury reports for the roster's teams, so Yahoo's tags have something to be checked
    # against. Bounded: on a timeout the stored reports are used and the result says so.
    refresh_stale = False
    if refresh_injuries is not None:
        teams = sorted({t for t in (normalize_team(r.get("team")) for r in rows if r["position"] != "DEF") if t})
        try:
            _, meta = await refresh_injuries(teams)
            if meta.get("partial"):
                refresh_stale = True
                warnings.append("nfl_mcp's injury refresh timed out, so the cross-check uses stored injury reports "
                                "that may be out of date. Every injury below is flagged stale_injury.")
        except Exception as e:
            refresh_stale = True
            warnings.append(f"Could not refresh nfl_mcp's injury reports ({e}); injury tags may be unverified.")
    cmp = await compare(league_id, rows, week)
    warnings += cmp.get("warnings") or []
    for p in cmp["players"]:
        p.setdefault("label", label_of(p))
        p["injury_flags"] = injury_flags(p, refresh_stale, now)
        p["projection_unknown"] = projection_unknown(p)
        if p["projection_unknown"]:
            warnings.append(f"{p['label']}: {p['projection_unknown']}. He is not ranked on projection.")
    totals = cmp["starter_totals"]
    noise = abs(totals["gap"]) < band
    warnings.append(f"Team projection gap {totals['gap']:+.1f} (ours {totals['ours']} vs Yahoo {totals['yahoo']}) "
                    + (f"is under {band:g} team points: noise, not a reason to change a lineup."
                       if noise else f"is {band:g}+ team points: worth finding out why they disagree."))
    model_only = [p["label"] for p in cmp["players"] if p["projection_source"] == "model_only" and not p["on_bye"]]
    if model_only:
        warnings.append("Projected by nfl_mcp's model alone (no Sleeper projection): " + ", ".join(model_only) + ".")

    problems = find_problems(slots, week)
    outlook = bye_outlook(slots, week)
    for w in outlook:
        if w["flags"] and w["week"] != week:
            warnings.append(f"Week {w['week']}: " + "; ".join(w["flags"]) + ".")
    positions = wire_positions(problems, outlook)

    wire: dict = {"positions": {}, "reasons": wire_reasons(problems, outlook), "targets": [], "timing": []}
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
    wire["vs_bench"] = bench_vs_wire(cmp["players"], wire, week)
    wire["targets_note"] = None
    if targets is not None and not wire_index:
        wire["targets_note"] = ("No position has a bye hole, injury or thin depth this week, and no Yahoo "
                                "free-agent list was read, so no targets were ranked.")
    elif targets is not None:
        try:
            t = await targets(league_id=league_id, roster_id=int(mid), week=week,
                              positions=list(wire_index), limit=TARGET_POOL)
            raw = [{
                "name": x.get("name"), "position": x.get("position"), "team": x.get("team"),
                "player_id": x.get("player_id"),
                "projected_points": x.get("projected_points"), "upgrade_points": x.get("upgrade_points"),
                "verdict": x.get("verdict"),
                "waiver_recommendation": (x.get("waiver_strategy") or {}).get("recommendation"),
            } for x in (t.get("targets") or [])]
            if t.get("success") is False:
                warnings.append("get_waiver_targets failed: " + str(t.get("error")))
                wire["targets_note"] = "get_waiver_targets failed: " + str(t.get("error"))
            else:
                kept, dropped = _join_targets(raw, wire_index, warnings)
                wire["targets"] = kept[:TARGETS_SHOWN]
                wire["targets_dropped"] = dropped
                if not wire["targets"]:
                    wire["targets_note"] = (
                        (t.get("message") or "The target ranking returned nothing.")
                        + (f" {len(raw)} ranked target(s) could not be matched to Yahoo free agents and were dropped."
                           if raw else ""))
                warnings.append("Waiver targets are ranked from Sleeper's player pool and nfl_mcp's projections, then "
                                "kept only if Yahoo lists them as available; each carries Yahoo's own FA / W tag.")
        except Exception as e:
            warnings.append(f"get_waiver_targets unavailable: {e}")

    review = bench_review(cmp["players"], week)
    drops, drop_unknown = review["ranked"], review["unknown"]
    verify = verify_roster(cmp["players"], week, sched, refresh_stale=refresh_stale, now=now)
    warnings += verification_warnings(verify)

    opponent, opp_warn = None, []
    try:
        opponent, opp_warn = _opponent(await prov.data.matchup(week, mid), mid, band)
    except Exception as e:
        opp_warn = [f"Could not read the matchup page: {e}"]
    warnings += opp_warn

    news = {"items": [], "count": 0, "independent_origins": 0, "origins": [], "by_player": {}}
    if posts is not None:
        names = [s["name"] for s in slots if not s.get("empty") and s.get("name")]
        labels_by_name: dict[str, list[str]] = {}
        for s in slots:
            if not s.get("empty") and s.get("name"):
                labels_by_name.setdefault(s["name"], []).append(label_of(s))
        try:
            res = await posts(None, 48, 40, names)
            items = [dict(i, status="lead") for i in res.get("items", []) if i.get("mentions_watched_player")]
            news = {"items": items, "count": len(items), **news_origins(items, labels_by_name)}
            if res.get("errors"):
                warnings.append(f"{res['errors']} Bluesky handle(s) failed to load.")
            if items:
                one = news["independent_origins"] < 2
                warnings.append(
                    f"News: {len(items)} post(s) from {news['independent_origins']} independent origin(s) "
                    f"({', '.join(news['origins'])}); posts relayed by one aggregator account count as one source. "
                    + ("Every news item is a single-source lead from Bluesky; " if one else
                       "Every news item is a lead from Bluesky; ")
                    + "confirm player, body part, status and date in a second independent source before acting.")
        except Exception as e:
            warnings.append(f"News unavailable: {e}")

    warnings.append("Yahoo's projections are a comparison source only. Injury tags and bye weeks are cross-checked "
                    "above where nfl_mcp has data; anything marked yahoo_only or unverified rests on Yahoo alone.")
    return {
        "success": True, "read_only": True, "note": READ_ONLY_NOTE,
        "week": week, "league_id": league_id, "roster_id": mid,
        "league": league_block,
        "roster": {"slots": cmp["players"], "starter_totals": totals, "gap_is_noise": noise,
                   "noise_band": band, "biggest_gaps": cmp["biggest_gaps"]},
        "problems": problems,
        "wire": wire,
        "drop_candidates": drops,
        "drop_unknown": drop_unknown,
        "drop_basis": ("job test: insurance for a starter's bye or injury in the next "
                       f"{BYE_WEEKS_AHEAD} weeks, or a path to a bigger role; the drop is whoever does neither"),
        "opponent": opponent,
        "byes": outlook,
        "verification": verify,
        "news": news,
        "warnings": warnings,
    }
