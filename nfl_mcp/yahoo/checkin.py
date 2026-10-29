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
STALE_INJURY_DAYS = 3  # a stored injury report older than this (or from before this NFL week) is not current
EDGE_BAND_POINTS = 1.0  # a margin this close to the noise band is "at the edge" of it
EASY_REPLACEMENT_COUNT = 3  # this many free agents projecting above a backup make him easy to replace
# Statuses that last for weeks or the season: they say nothing about this week's game.
SEASON_LONG_TAGS = {"IR", "PUP-R", "NFI-R"}
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


def flex_slots(slots: list[dict]) -> tuple[int, list[str]]:
    """How many flex starting slots there are, and the positions that can fill them."""
    n, positions = 0, []
    for s in slots:
        allowed = FLEX_POSITIONS.get(slot_label(s["slot"]))
        if allowed:
            n += 1
            positions += [p for p in allowed if p not in positions]
    return n, positions


def slots_to_fill(slots: list[dict]) -> dict[str, int]:
    """Starting slots by kind: each position's own slots plus ``FLEX``. Sums to the starter count."""
    need = own_slots(slots)
    n, _ = flex_slots(slots)
    if n:
        need["FLEX"] = n
    return need


def bye_outlook(slots: list[dict], week: int, weeks: int = BYE_WEEKS_AHEAD) -> list[dict]:
    """For each of the next ``weeks`` weeks: which starters are out, how many eligible players are
    left at each position, and any flags.

    A position is thin when its active players (not on bye, not tagged out, not on IR) number fewer
    than its own starting slots (QB 1, RB 2, WR 2, TE 1, K 1, DEF 1). The flex is never folded into
    a position's count: it is a separate ``flex`` line, the players left across the flex-eligible
    positions once their own slots are filled. A week is also flagged when two or more starters are
    on bye, which can break the lineup while every position still has bodies.
    """
    rostered = [s for s in slots if not s.get("empty") and slot_label(s["slot"]) != "IR"]
    own, need = own_slots(slots), slots_to_fill(slots)
    n_flex, flex_pos = flex_slots(slots)
    out = []
    for w in range(week, week + weeks):
        on_bye = [s for s in rostered if _bye_week(s) == w]
        starters_out = [s for s in on_bye if _is_starter(s)]
        active = {pos: sum(1 for s in rostered if (s.get("position") or "").upper() == pos
                           and _bye_week(s) != w and s.get("injury") not in UNAVAILABLE_TAGS)
                  for pos in dict.fromkeys([*own, *flex_pos])}
        thin = [pos for pos, n in own.items() if active[pos] < n]
        left = sum(max(0, active[p] - own.get(p, 0)) for p in flex_pos)
        flex = ({"slots": n_flex, "positions": flex_pos, "eligible_left": left,
                 "line": f"flex: {left} eligible players left across {'/'.join(flex_pos)}"} if n_flex else None)
        flags = [f"thin at {pos}: {active[pos]} active for {own[pos]} slots to fill" for pos in thin]
        if flex and left < n_flex:
            flags.append(f"{flex['line']}, for {n_flex} flex slot(s)")
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
            "eligible_active": active, "slots_to_fill": need, "thin_positions": thin, "short_positions": thin,
            "flex": flex,
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


def _when(stamp: str | None) -> datetime | None:
    """An ISO date or timestamp as an aware datetime, or None when it is missing or unreadable."""
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(str(stamp).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def injury_record_date(p: dict) -> tuple[str | None, str | None]:
    """The date of nfl_mcp's stored injury record and where it came from: the source's own report
    date, else the time nfl_mcp stored it (which says nothing about when the report was written)."""
    if p.get("nfl_injury_report_date"):
        return p["nfl_injury_report_date"], "report date"
    if p.get("nfl_injury_stored_at"):
        return p["nfl_injury_stored_at"], "stored-at time (the source gave no report date)"
    return None, None


def stale_reasons(p: dict, refresh_stale: bool = False, now: datetime | None = None,
                  week_start: date | None = None) -> list[str]:
    """Why the stored injury record cannot be trusted as current: the refresh timed out, the record is
    more than :data:`STALE_INJURY_DAYS` days old, or it is dated before this NFL week started. The
    date rules apply even when the refresh succeeded."""
    reasons = ["the injury refresh timed out"] if refresh_stale else []
    stamp, _ = injury_record_date(p)
    when = _when(stamp)
    if when is not None:
        age = ((now or datetime.now(UTC)) - when).total_seconds() / 86400.0
        if age > STALE_INJURY_DAYS:
            reasons.append(f"dated {stamp[:10]}, {age:.0f} days old (limit {STALE_INJURY_DAYS})")
        if week_start is not None and when.date() < week_start:
            reasons.append(f"dated {stamp[:10]}, before this NFL week started ({week_start.isoformat()})")
    return reasons


def injury_flags(p: dict, refresh_stale: bool = False, now: datetime | None = None,
                 week_start: date | None = None) -> list[str]:
    """Why a player's injury data must not drive a decision on its own.

    ``stale_injury``: the refresh timed out, or the record is more than three days old or from before
    this NFL week. ``injury_conflict``: Yahoo's tag and nfl_mcp's designation differ. ``yahoo_only``:
    Yahoo shows a tag and nfl_mcp has no report at all. ``season_long_status``: nfl_mcp has a PUP, NFI
    or IR record that Yahoo does not show. ``predates_activation``: nfl_mcp has a record but Yahoo
    shows the player active, and the record is season-long or old, so it was written before he came
    back. Neither of the last two can make a player Out or project 0.
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
    reasons = stale_reasons(p, refresh_stale, now, week_start)
    if reasons:
        flags.append("stale_injury")
    if n in SEASON_LONG_TAGS and y not in RESERVE_TAGS:
        flags.append("season_long_status")
    dated = [r for r in reasons if not r.startswith("the injury refresh")]
    if status and not y and p.get("slot") != "IR" and (n in SEASON_LONG_TAGS or dated):
        flags.append("predates_activation")
    return flags


def injury_statement(p: dict, reasons: list[str]) -> str:
    """How the record may be quoted: with its date, and as a stored report (not as fact) when stale."""
    status, y = p.get("nfl_injury_status"), p.get("injury")
    if not status:
        return f"Yahoo alone shows {y}; nfl_mcp has no report" if y else "no injury data"
    bits = [status]
    if p.get("practice_status"):
        bits.append(f"practice {p['practice_status']}"
                    + (f" (dated {str(p['nfl_practice_date'])[:10]})" if p.get("nfl_practice_date") else ""))
    if p.get("nfl_injury_body_part"):
        bits.append(p["nfl_injury_body_part"])
    stamp, _ = injury_record_date(p)
    when = f"dated {stamp[:10]}" if stamp else "undated"
    if reasons:
        return f"stored report ({when}, stale: {'; '.join(reasons)}) says {', '.join(bits)}"
    return f"nfl_mcp report ({when}) says {', '.join(bits)}"


def projection_unknown(p: dict) -> str | None:
    """Why a projection of 0 cannot be trusted, else None.

    nfl_mcp prices an Out designation as 0. When that designation is stale, in conflict with
    Yahoo's tag, or only Yahoo's, the 0 is a guess about availability and not a measure of the
    player, so it is not used to rank him. A PUP, NFI or IR record that Yahoo does not show, or one
    that predates the player's activation, never makes him Out either.
    """
    flags = p.get("injury_flags") or []
    if not flags or p.get("our_projection") not in (0, 0.0):
        return None
    n = nfl_tag(p.get("nfl_injury_status"))
    if n == "O":
        return ("projection of 0 comes from an Out designation that is "
                + " / ".join(f.replace("_", " ") for f in flags) + "; check the team report")
    if n in SEASON_LONG_TAGS and {"season_long_status", "predates_activation"} & set(flags):
        return (f"projection of 0 comes from a {p.get('nfl_injury_status')} record that Yahoo does not show "
                "(a season-long status, or a record that predates his activation); it never makes a player Out")
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
                  now: datetime | None = None, week_start: date | None = None) -> dict:
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
            reasons = stale_reasons(p, refresh_stale, now, week_start) if p.get("nfl_injury_status") else []
            stamp, basis = injury_record_date(p)
            injuries.append({"key": p.get("key"), "name": p["name"], "label": label_of(p), "slot": p["slot"],
                             "yahoo_tag": y, "nfl_mcp_status": p.get("nfl_injury_status"),
                             "practice": p.get("practice_status"), "practice_date": p.get("nfl_practice_date"),
                             "body_part": p.get("nfl_injury_body_part"),
                             "report_date": p.get("nfl_injury_report_date"), "stored_at": p.get("nfl_injury_stored_at"),
                             "date_used": stamp, "date_basis": basis, "stale_reasons": reasons,
                             "gameday": p.get("gameday_status"), "verdict": verdict,
                             "fields_differ": differ, "fields_missing": missing,
                             "flags": injury_flags(p, refresh_stale, now, week_start),
                             "statement": injury_statement(p, reasons), "detail": "; ".join(bits)})
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
        if "predates_activation" in r.get("flags", []) or "season_long_status" in r.get("flags", []):
            out.append(f"{who}: the {r['statement']}. That is a season-long status or a record older than his "
                       "activation, so it is ignored: it creates no Out and no 0 projection.")
        elif "stale_injury" in r.get("flags", []):
            out.append(f"{who}: injury data is stale: the {r['statement']}. Do not act on it alone.")
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


def single_starter_positions(players: list[dict]) -> set[str]:
    """Positions with exactly one starting slot of their own (QB, TE, K and DEF in a standard lineup):
    when that one starter is hurt there is nobody else to play."""
    counts: dict[str, int] = {}
    for p in players:
        if p["slot"] not in STARTER_EXCLUDED and p["slot"] not in FLEX_POSITIONS and p.get("name"):
            counts[p["slot"]] = counts.get(p["slot"], 0) + 1
    return {pos for pos, n in counts.items() if n == 1}


def bench_review(players: list[dict], week: int, replacement: dict[str, dict] | None = None) -> dict:
    """Rank the bench by what each player is for, not by this week's projection.

    A bench spot has to do one of two jobs: insurance (he can fill in for a starter at his position
    whose bye falls in the next four weeks, or who is hurt now; at a position with a single starter,
    any backup is injury insurance for him) or a believable path to a bigger role (he projects within
    a tie of the weakest starter at his position, or his role is rising). The drop is whoever does
    neither. ``replacement`` (see :func:`replaceability`) says how many free agents project above a
    backup: insurance that is easy to replace ranks as a drop before insurance that is not. A player whose projection is an unreliable zero (an Out
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

    single = single_starter_positions(players)
    ranked, unknown = [], []
    covered_by: dict[str, list[str]] = {}
    jobs: dict[str, list[str]] = {}
    kinds_of: dict[str, list[str]] = {}

    def cover(starter: dict, backup: dict) -> None:
        who = covered_by.setdefault(starter.get("key") or player_key(starter), [])
        if (backup.get("key") or player_key(backup)) not in who:
            who.append(backup.get("key") or player_key(backup))

    for b_ in bench:
        why = projection_unknown(b_)
        if why:
            unknown.append({"key": b_.get("key") or player_key(b_), "name": b_["name"], "label": label_of(b_),
                            "position": b_["position"], "team": b_["team"], "injury": b_.get("injury"),
                            "injury_flags": b_.get("injury_flags") or [], "reason": why,
                            "note": "not ranked: his projection is unknown, not zero"})
            continue
        mine: list[str] = []
        kinds: list[str] = []
        if b_.get("injury") not in UNAVAILABLE_TAGS:
            for st in starters:
                if st["position"] != b_["position"] or not st.get("name"):
                    continue
                sb = _week_of(st.get("bye_week"))
                if _in_window(sb, week) and _week_of(b_.get("bye_week")) != sb:
                    mine.append(f"covers the week {sb} bye of {label_of(st)}")
                    kinds.append("bye")
                    cover(st, b_)
                elif st.get("injury") in ("Q", "D", "O") and not b_.get("on_bye"):
                    mine.append(f"covers {label_of(st)}, who is tagged {st['injury']}")
                    kinds.append("injury")
                    cover(st, b_)
                if b_["position"] in single and st["slot"] == b_["position"]:
                    mine.append(f"injury insurance: the only {b_['position']} starter is {label_of(st)}")
                    kinds.append("single")
                    cover(st, b_)
        path = None
        proj = usable_projection(b_) if not b_.get("on_bye") else None
        low = weakest(b_["position"])
        if b_.get("role_trend") == "role_up":
            path = "his role is rising (role_up)"
        elif proj is not None and low is not None and proj >= low[1] - PLAYER_TIE_POINTS:
            path = (f"projects {proj:.1f}, within {PLAYER_TIE_POINTS:g} of {label_of(low[0])} "
                    f"({low[1]:.1f}), the weakest starter at {b_['position']}")
        jobs[b_.get("key") or player_key(b_)] = mine
        kinds_of[b_.get("key") or player_key(b_)] = kinds
        ranked.append({"b": b_, "covers": mine, "path": path, "proj": proj})

    rows = []
    for r in ranked:
        b_ = r["b"]
        key = b_.get("key") or player_key(b_)
        mine_keys = [st for st, who in covered_by.items() if key in who]
        redundant = bool(r["covers"]) and all(len(set(covered_by[k])) > 1 for k in mine_keys)
        rep_info = (replacement or {}).get(key)
        cheap = bool(rep_info and rep_info.get("easy") and set(kinds_of[key]) == {"single"} and not r["path"])
        tier = 0 if not r["covers"] and not r["path"] else 1 if r["covers"] and (redundant or cheap) and not r["path"] \
            else 2 if not r["covers"] else 3
        if b_.get("on_bye"):
            lead = "on bye this week (no projection); "
        else:
            lead = ""
        if tier == 0:
            note = (lead + "does neither job: covers no starter's bye or injury in the next "
                    f"{BYE_WEEKS_AHEAD} weeks and has no path to a bigger role")
        else:
            bits = list(r["covers"]) + ([f"path: {r['path']}"] if r["path"] else [])
            note = lead + "; ".join(bits) + ("; another bench player covers the same starter" if redundant and tier == 1
                                             else "")
        if rep_info:
            note += ("; easy to replace: " if rep_info["easy"] else "; hard to replace: ") + rep_info["text"]
        rows.append({
            "key": key, "name": b_["name"], "label": label_of(b_), "position": b_["position"], "team": b_["team"],
            "player_id": b_.get("player_id"), "this_week": r["proj"], "on_bye": b_["on_bye"],
            "bye_week": b_.get("bye_week"), "injury": b_.get("injury"), "injury_flags": b_.get("injury_flags") or [],
            "players_at_position": depth.get(b_["position"], 0),
            "insurance_for": r["covers"], "insurance_kinds": kinds_of[key], "path_to_role": r["path"],
            "replacement": rep_info, "does_a_job": tier > 0,
            "redundant_cover": redundant and tier == 1, "note": note,
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
    week against the best projected wire pickup, both limited to the slot he occupies: a WR slot
    takes a WR only, the flex takes an RB, WR or TE (judged by the player's own position, not the
    page the wire row was read from).

    The bench is checked first: when the wire pickup beats the best bench option by less than a tie
    (:data:`PLAYER_TIE_POINTS`), the answer is "no add needed, use <bench player>".
    """
    starters = [p for p in players if p["slot"] not in ("BN", "IR") and p.get("name")]
    bench = [p for p in players if p["slot"] == "BN"]
    wire_all = [r for rows in wire["positions"].values() for r in rows]
    tie = f"{PLAYER_TIE_POINTS:g}-point tie band"
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
        wire_rows = [r for r in wire_all if (r.get("position") or "").upper() in allowed
                     and r.get("our_projection") is not None]
        best_wire = max(wire_rows, key=lambda r: r["our_projection"]) if wire_rows else None
        reason = "on bye this week" if st.get("on_bye") else f"tagged {st['injury']}"
        row = {
            "starter": label_of(st), "slot": slot_label(st["slot"]), "eligible_positions": allowed,
            "reason": reason, "starter_projection": base,
            "bench_replacements": [{"player": label_of(b), "projection": usable_projection(b)} for b in same_or_better],
            "best_bench": ({"player": label_of(best_bench), "projection": usable_projection(best_bench)}
                           if best_bench else None),
            "best_wire": ({"player": best_wire.get("label") or label_of(best_wire), "tag": best_wire["tag"],
                           "projection": best_wire["our_projection"]} if best_wire else None),
            "gap": None,
        }
        if best_bench and best_wire:
            row["gap"] = round(best_wire["our_projection"] - usable_projection(best_bench), 1)
        gap = row["gap"]
        if best_bench and (best_wire is None or gap < PLAYER_TIE_POINTS):
            row["decision"] = f"no add needed, use {label_of(best_bench)}"
            if best_wire and gap > -PLAYER_TIE_POINTS:
                row["decision"] += (f" (the best wire pickup, {row['best_wire']['player']}, is {gap:+.1f} against him: "
                                    f"a tie inside the {tie})")
            elif best_wire:
                row["decision"] += (f": the best wire pickup, {row['best_wire']['player']}, is worse than "
                                    f"{label_of(best_bench)} by {-gap:.1f} (more than the {tie})")
        elif best_bench:
            row["decision"] = (f"{row['best_wire']['player']} ({best_wire['tag']}) projects {gap:.1f} above "
                               f"the best bench option {label_of(best_bench)}: worth an add")
        elif best_wire:
            row["decision"] = (f"no eligible bench replacement; best wire pickup {row['best_wire']['player']} "
                               f"({best_wire['tag']})")
        else:
            row["decision"] = "no eligible bench replacement and no projected wire player"
        out.append(row)
    return out


def _wire_row(p: dict) -> dict:
    a = p.get("availability") or {}
    tag = "FA" if a.get("state") == "FA" else (f"W ({a['clears']})" if a.get("clears") else a.get("state"))
    return {"name": p["name"], "label": label_of(p), "team": p.get("team"), "position": p.get("position"),
            "player_id": p.get("player_id"), "tag": tag, "injury": p.get("injury"),
            "our_projection": None, "upgrade": None, "projection_source": None}


def starter_holes(slots: list[dict], players: list[dict]) -> list[dict]:
    """Starting slots nobody can fill this week: empty, or held by a player on bye or out.

    Each projects 0 when a wire player is measured against the weakest starter. ``positions`` are the
    positions that could fill it (a FLEX slot takes RB, WR or TE; every other slot takes its own).
    A player out only by an unverified nfl_mcp designation is not a hole: his status is unknown.
    """
    holes = []
    for s in slots:
        label = slot_label(s["slot"])
        if s.get("empty") and label not in STARTER_EXCLUDED:
            holes.append({"slot": label, "kind": "empty", "player": None,
                          "positions": FLEX_POSITIONS.get(label, [label]),
                          "reason": f"No player in the {label} starting slot"})
    for p in players:
        if p["slot"] in STARTER_EXCLUDED or not p.get("name"):
            continue
        gone = p.get("injury") == "O" or (nfl_tag(p.get("nfl_injury_status")) == "O" and not p.get("injury_flags"))
        if p.get("on_bye") or gone:
            kind = "on bye" if p.get("on_bye") else "out"
            holes.append({"slot": p["slot"], "kind": kind, "player": label_of(p),
                          "positions": FLEX_POSITIONS.get(p["slot"], [p["position"]]),
                          "reason": f"{label_of(p)} is {kind}"})
    return holes


def hole_targets(holes: list[dict], wire: dict) -> tuple[list[dict], list[str]]:
    """The best available Yahoo free agent for each hole, with Yahoo's own FA / W (date) tag.

    A hole is filled by the best-projected wire player whose own position can fill that slot (a
    row's position, not the page it was read from), never the same player twice. Returns the targets
    and a note for each hole no wire player could fill.
    """
    targets, used, notes = [], set(), []
    for h in holes:
        pool = [r for rows in wire["positions"].values() for r in rows
                if (r.get("position") or "").upper() in h["positions"] and r.get("tag")
                and player_key(r) not in used]
        projected = [r for r in pool if r.get("our_projection") is not None]
        best = max(projected, key=lambda r: r["our_projection"]) if projected else (pool[0] if pool else None)
        if best is None:
            notes.append(f"no Yahoo free agent was read for the {h['slot']} hole ({h['reason']})")
            continue
        used.add(player_key(best))
        mine = best.get("our_projection")
        targets.append({
            "name": best["name"], "label": best.get("label") or label_of(best), "position": best.get("position"),
            "team": best.get("team"), "player_id": best.get("player_id"), "projected_points": mine,
            "upgrade_points": round(mine, 1) if mine is not None else None,
            "verdict": "fills_hole", "source": "wire_hole", "fills": h["reason"],
            "waiver_recommendation": None, "tag": best["tag"], "yahoo_tag": best["tag"]})
    return targets, notes


def _baseline(pos: str, players: list[dict], holes: list[dict] | None = None) -> tuple[float | None, str]:
    """What a wire player at ``pos`` would replace: 0 when a starting slot he could fill is empty, or
    its player is on bye or out; else the weakest starter's projection (a starter whose projection
    is unknown is left out)."""
    for h in holes or []:
        if pos in h["positions"]:
            return 0.0, h["reason"]
    at = [p for p in players if p["position"] == pos and p["slot"] not in ("BN", "IR")]
    if not at:
        return None, "no starter at this position"
    if any(p["on_bye"] or p["injury"] == "O" for p in at):
        who = next(p for p in at if p["on_bye"] or p["injury"] == "O")
        return 0.0, f"{label_of(who)} is {'on bye' if who['on_bye'] else 'out'}"
    proj = [v for v in (usable_projection(p) for p in at) if v is not None]
    return (min(proj) if proj else None), "weakest starter at the position"


async def _project_wire(project, league_id: str, week: int, pos: str, rows: list[dict],
                        players: list[dict], holes: list[dict] | None = None) -> list[dict]:
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
    base, base_note = _baseline(pos, players, holes)
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
    if abs(abs(margin) - band) <= EDGE_BAND_POINTS + 1e-9:
        return (f"at the edge of the noise band (margin {margin:+.1f} against a +/-{band:g} band): "
                "too close to call either way")
    if abs(margin) < band:
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


def _index_projections(res: dict | None) -> tuple[dict, dict]:
    by_id: dict[str, dict] = {}
    by_name: dict[str | None, list[dict]] = {}
    for p in (res or {}).get("projections") or []:
        if p.get("player_id") not in (None, ""):
            by_id[str(p["player_id"])] = p
        by_name.setdefault(p.get("player"), []).append(p)
    return by_id, by_name


def _find_projection(index: tuple[dict, dict], row: dict) -> dict | None:
    by_id, by_name = index
    if row.get("player_id") not in (None, "") and str(row["player_id"]) in by_id:
        return by_id[str(row["player_id"])]
    hits = by_name.get(row.get("name")) or []
    return hits[0] if len(hits) == 1 else None


async def replaceability(prov, project, league_id: str, week: int, players: list[dict],
                         pages: dict[str, list[dict]], warnings: list[str]) -> dict[str, dict]:
    """How easy each backup at a single-starter position (QB, TE, K, DEF) is to replace: how many of
    the top free agents at his position project above him. A backup on bye is compared in the first
    week after it (he has no number this week). Reads that position's free-agent list if the wire
    has not (one more Yahoo page), and projects the backup with the top free agents in one call."""
    out: dict[str, dict] = {}
    for pos in sorted(single_starter_positions(players)):
        bench = [b for b in players if b["slot"] == "BN" and b["position"] == pos and not projection_unknown(b)]
        if not bench:
            continue
        try:
            if pos not in pages:
                pages[pos] = [_wire_row(p) for p in await prov.get_free_agents(pos)]
        except Exception as e:
            warnings.append(f"Could not read the {pos} free agents to judge how easy {pos} depth is to replace: {e}")
            continue
        fas = pages[pos][:WIRE_CANDIDATES]
        for b in bench:
            ref = week + 1 if b.get("on_bye") else week
            ask = [{"name": x["name"], "team": x["team"], "position": x["position"], "player_id": x.get("player_id")}
                   for x in [b, *fas]]
            try:
                index = _index_projections(await project(ask, league_id=league_id, week=ref))
            except Exception as e:
                warnings.append(f"Could not project {pos} free agents against {label_of(b)}: {e}")
                continue
            mine = _find_projection(index, b)
            if not mine or mine.get("on_bye") or mine.get("projected_points") is None:
                continue
            bp = mine["projected_points"]
            scored = []
            for fa in fas:
                pr = _find_projection(index, fa)
                if pr and not pr.get("on_bye") and pr.get("projected_points") is not None:
                    scored.append((pr["projected_points"], fa))
            above = sorted((x for x in scored if x[0] > bp), key=lambda x: -x[0])
            text = (f"{len(above)} of {len(scored)} free-agent {pos}s project above him ({bp:.1f}) in week {ref}"
                    + (f"; the best is {above[0][1].get('label') or label_of(above[0][1])} "
                       f"({above[0][0]:.1f}, {above[0][1]['tag']})" if above else ""))
            out[b.get("key") or player_key(b)] = {
                "week": ref, "bench_projection": bp, "free_agents_above": len(above),
                "free_agents_checked": len(scored), "easy": len(above) >= EASY_REPLACEMENT_COUNT, "text": text}
    return out


_QUOTE = re.compile(r'["\u201c\u201d]')
_SPEECH = re.compile(r"\b(said|says|told|asked|responded|stated|added|tweeted|insisted)\b", re.I)
_PREDICT = re.compile(r"\b(guarantee[sd]?|predict(?:s|ed|ion|ions)?|will win|going to win|win this game|"
                      r"picks? to win|bold prediction)\b", re.I)
_AVAILABILITY = re.compile(
    r"\b(questionable|doubtful|probable|injur\w*|practice\w*|limited|dnp|inactive|ruled|surgery|pup|"
    r"designat\w*|activated|placed on|hamstring|knee|ankle|shoulder|concussion|illness|suspend\w*|"
    r"(?:is|are|be|listed|declared)\s+out|return\w*\s+(?:to|from|this|next)|injured reserve)\b", re.I)


def quote_or_prediction(text: str | None) -> str | None:
    """Why a post is a player quote or a prediction ("guarantees a win") and not availability news,
    else None. A post that carries injury or availability wording is kept either way."""
    t = text or ""
    if _AVAILABILITY.search(t):
        return None
    if _PREDICT.search(t):
        return "prediction"
    if _QUOTE.search(t) and _SPEECH.search(t):
        return "player quote"
    return None


def attach_leads(injuries: list[dict], items: list[dict]) -> None:
    """Prefer a fresher news lead to a stale stored record, labelled for what it is.

    For each injury record that is stale, the newest post that names the player and is newer than the
    record becomes the ``statement``: a lead from one account (single-source unless two independent
    accounts carry it), not a confirmed status. The stored record's own wording is kept in
    ``stored_statement``.
    """
    for r in injuries:
        if not r.get("stale_reasons"):
            continue
        record = _when(r.get("date_used"))
        best = None
        for it in items:
            posted = _when(it.get("posted_at"))
            if r["label"] not in (it.get("mentions") or []) or posted is None:
                continue
            if record is not None and posted <= record:
                continue
            if best is None or posted > best[0]:
                best = (posted, it)
        if best is None:
            continue
        posted, it = best
        text = re.sub(r"\s*(?:https?://)?(?:twitter|x)\.com/\S+", "", it.get("text") or "")
        text = re.sub(r"^\s*\[[^\]]+\]\s*", "", text).strip()
        text = text if len(text) <= 200 else text[:197] + "..."
        origins = it.get("independent_origins", 1)
        label = "single-source lead" if origins < 2 else f"lead from {origins} independent accounts"
        relayed = f" relaying {it['relayed_reporter']}" if it.get("relayed_reporter") else ""
        r["fresher_lead"] = {"author": it.get("author"), "relayed_reporter": it.get("relayed_reporter"),
                             "posted_at": it.get("posted_at"), "text": text, "url": it.get("url"),
                             "single_source": origins < 2, "independent_origins": origins}
        r["stored_statement"] = r["statement"]
        r["statement"] = (f"{label} from {it.get('author')}{relayed}, posted {it.get('posted_at', '')[:10]}: "
                          f"\"{text}\". Unconfirmed; it is newer than the {r['stored_statement']}")


def _refresh_warning(meta: dict) -> str:
    """The injury refresh timeout, with each try's wait and the real error, not a generic message."""
    text = ("nfl_mcp's injury refresh timed out, so the cross-check uses stored injury reports that may be "
            "out of date")
    attempts = meta.get("attempts") or []
    if attempts:
        text += ". " + "; ".join(
            f"Try {i + 1}: waited {a['wait_seconds']:g} s, {a['outcome']}" + (f" ({a['error']})" if a.get("error") else "")
            for i, a in enumerate(attempts))
        text += f" ({meta.get('waited_seconds', 0):g} s in all"
        text += f", {meta['teams']} teams" if meta.get("teams") else ""
        text += ")"
        if meta.get("background_running"):
            text += (". The ESPN crawl is still running in the background and stores what it finds, so the next "
                     "check-in should read fresh reports (a cold crawl of a full roster's teams can take minutes)")
    return text + ". Every stored injury is flagged stale_injury."


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
    week_start: date | None = None,
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
                warnings.append(_refresh_warning(meta))
        except Exception as e:
            refresh_stale = True
            warnings.append(f"Could not refresh nfl_mcp's injury reports ({e}); injury tags may be unverified.")
    cmp = await compare(league_id, rows, week)
    warnings += cmp.get("warnings") or []
    for p in cmp["players"]:
        p.setdefault("label", label_of(p))
        p.setdefault("key", player_key(p))
        p["injury_flags"] = injury_flags(p, refresh_stale, now, week_start)
        p["injury_stale_reasons"] = (stale_reasons(p, refresh_stale, now, week_start)
                                     if p.get("nfl_injury_status") else [])
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
    holes = starter_holes(slots, cmp["players"])
    positions = wire_positions(problems, outlook)
    for h in holes:  # every position that could fill an empty, bye or out starting slot is read, K and DEF included
        for pos in h["positions"]:
            if pos in WIRE_POSITIONS and pos not in positions:
                positions.append(pos)
    reasons = wire_reasons(problems, outlook)
    for h in holes:
        for pos in h["positions"]:
            if pos in WIRE_POSITIONS and not any((h["player"] or h["reason"]) in r for r in reasons.get(pos, [])):
                reasons.setdefault(pos, []).append(h["reason"])

    wire: dict = {"positions": {}, "reasons": reasons, "holes": holes, "targets": [], "timing": []}
    wire_index: dict[str, list[dict]] = {}
    for pos in positions:
        try:
            wire_rows = [_wire_row(p) for p in (await prov.get_free_agents(pos))]
            wire_index[pos] = wire_rows
            candidates = wire_rows[:WIRE_CANDIDATES]
            if project is not None:
                try:
                    candidates = await _project_wire(project, league_id, week, pos, candidates, cmp["players"], holes)
                except Exception as e:
                    warnings.append(f"Could not project the {pos} wire: {e}")
            wire["positions"][pos] = candidates[:wire_depth]
        except Exception as e:  # one page failing should not sink the check-in
            warnings.append(f"Could not read the {pos} wire: {e}")
    wire["timing"] = claim_timing(wire, today)
    wire["vs_bench"] = bench_vs_wire(cmp["players"], wire, week)
    wire["targets_note"] = None
    joined: list[dict] = []
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
                joined, dropped = _join_targets(raw, wire_index, warnings)
                wire["targets_dropped"] = dropped
                if not joined:
                    wire["targets_note"] = (
                        (t.get("message") or "The target ranking returned nothing.")
                        + (f" {len(raw)} ranked target(s) could not be matched to Yahoo free agents and were dropped."
                           if raw else ""))
                warnings.append("Waiver targets are ranked from Sleeper's player pool and nfl_mcp's projections, then "
                                "kept only if Yahoo lists them as available; each carries Yahoo's own FA / W tag.")
        except Exception as e:
            warnings.append(f"get_waiver_targets unavailable: {e}")
    # A slot that is empty, or held by a player on bye or out, projects 0: the best available Yahoo player
    # for it is always a target, whatever the Sleeper-pool ranking said.
    hole_t, hole_notes = hole_targets(holes, wire)
    seen = {player_key(t) for t in hole_t}
    wire["targets"] = (hole_t + [t for t in joined if player_key(t) not in seen])[:TARGETS_SHOWN]
    if holes:
        wire["targets_note"] = (
            f"{len(holes)} starting slot(s) are empty, on bye or out ({'; '.join(h['reason'] for h in holes)}): "
            "the best available Yahoo free agent for each is listed as a target"
            + (f"; {'; '.join(hole_notes)}" if hole_notes else "") + ".")

    replacement: dict[str, dict] = {}
    if project is not None:
        replacement = await replaceability(prov, project, league_id, week, cmp["players"], dict(wire_index), warnings)
    review = bench_review(cmp["players"], week, replacement)
    drops, drop_unknown = review["ranked"], review["unknown"]
    verify = verify_roster(cmp["players"], week, sched, refresh_stale=refresh_stale, now=now, week_start=week_start)
    warnings += verification_warnings(verify)

    opponent, opp_warn = None, []
    try:
        opponent, opp_warn = _opponent(await prov.data.matchup(week, mid), mid, band)
    except Exception as e:
        opp_warn = [f"Could not read the matchup page: {e}"]
    warnings += opp_warn

    news = {"items": [], "count": 0, "dropped": [], "independent_origins": 0, "origins": [], "by_player": {}}
    if posts is not None:
        names = [s["name"] for s in slots if not s.get("empty") and s.get("name")]
        labels_by_name: dict[str, list[str]] = {}
        for s in slots:
            if not s.get("empty") and s.get("name"):
                labels_by_name.setdefault(s["name"], []).append(label_of(s))
        try:
            res = await posts(None, 48, 40, names)
            items, dropped = [], []
            for i in res.get("items", []):
                if not i.get("mentions_watched_player"):
                    continue
                why = quote_or_prediction(i.get("text"))
                if why:
                    dropped.append({"author": i.get("author"), "reason": why, "text": (i.get("text") or "")[:120]})
                else:
                    items.append(dict(i, status="lead"))
            news = {"items": items, "count": len(items), "dropped": dropped, **news_origins(items, labels_by_name)}
            if dropped:
                warnings.append(f"Dropped {len(dropped)} news post(s) that are player quotes or predictions, not "
                                "availability news, before counting leads.")
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

    attach_leads(verify["injuries"], news["items"])
    by_key = {r["key"]: r for r in verify["injuries"] if r.get("key")}
    for p in cmp["players"]:
        r = by_key.get(p.get("key"))
        p["injury_statement"] = r["statement"] if r else None
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
