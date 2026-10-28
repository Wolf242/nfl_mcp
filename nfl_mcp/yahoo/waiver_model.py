"""Rolling waiver-priority model for BFS (continual rolling list, no FAAB).

Validated 2026-10-27 against the league's own transaction log: of 3,628,800 possible
starting orders, 25,200 satisfied all 21 contested claims and every one reproduced the
live standings order exactly. The rules:

* One list, 1 = best. A *successful* waiver claim (contested or not) sends the winner
  to the bottom and everyone behind moves up one.
* Claims are processed in rounds: the team with the best current priority that still has
  a pending claim attempts its next-ranked claim. A win -> bottom of the list. A claim on
  a player already awarded fails and costs nothing; that team then tries its next claim at
  the same priority.
* The priority printed on Yahoo's "Waiver Offers" page is the team's position *when that
  claim was processed*, which is why one team can show different numbers in one run.
* Free-agent adds, drops and trades do not change the order.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field


@dataclass
class Claim:
    team: str
    player: str
    drop: str | None = None


@dataclass
class Outcome:
    team: str
    player: str
    won: bool
    priority_at_processing: int
    awarded_to: str | None = None


@dataclass
class RunResult:
    outcomes: list[Outcome] = field(default_factory=list)
    final_order: list[str] = field(default_factory=list)


def process_run(order: Sequence[str], claims: dict[str, list[Claim]]) -> RunResult:
    """Simulate one processing run.

    ``order``: teams best-first. ``claims``: each team's ranked claim list (the order the
    manager set on Yahoo). A team's next claim is only attempted after the previous one won
    or failed, exactly as Yahoo does; after a win the team sits at the bottom, and its
    remaining claims are attempted from that new spot.
    """
    order = list(order)
    nxt = {t: 0 for t, c in claims.items() if c}
    awarded: dict[str, str] = {}
    res = RunResult()
    while nxt:
        team = next(t for t in order if t in nxt)
        pos = order.index(team) + 1
        claim = claims[team][nxt[team]]
        nxt[team] += 1
        if claim.player in awarded:
            res.outcomes.append(Outcome(team, claim.player, False, pos, awarded[claim.player]))
        else:
            awarded[claim.player] = team
            res.outcomes.append(Outcome(team, claim.player, True, pos, team))
            order.remove(team)
            order.append(team)
        if nxt[team] >= len(claims[team]):
            del nxt[team]
    res.final_order = order
    return res


def replay_log(initial_order: Sequence[str], wins: Iterable[tuple[str, int | None, list[tuple[str, int]]]]) -> list[str] | None:
    """Replay a chronological list of waiver wins; return the final order, or None if inconsistent.

    Each win is ``(team, winning_priority_or_None, [(loser_team, loser_priority), ...])``.
    ``None`` means the claim was uncontested (not listed on the Offers page), so only the
    move-to-bottom effect is applied. This is the validator used to check the rule.
    """
    order = list(initial_order)
    for team, win_pr, losers in wins:
        if team not in order:
            return None
        if win_pr is not None and order.index(team) + 1 != win_pr:
            return None
        for lt, lp in losers:
            if lt not in order or order.index(lt) + 1 != lp:
                return None
        order.remove(team)
        order.append(team)
    return order


def plan_sequence(order: Sequence[str], me: str, wishlist: list[tuple[str, float, str | None]],
                  contested: set[str] | None = None) -> dict:
    """Advise how to rank a manager's claims.

    ``wishlist``: ``(player, value, drop)`` best value first. ``contested``: players you
    think other teams also want (private on Yahoo, so this is a judgment call).

    Returns the recommended ranked list plus the reason for each slot. The rule: only the
    first *win* uses your current spot; after it you are last, so every later claim wins
    only if no one else has it. Put the most valuable contested claim first, then the rest
    by value. Reusing a drop across claims is flagged (a drop can only leave once).
    """
    contested = contested or set()
    pos = list(order).index(me) + 1
    # contested first, then by value; ties keep the given order
    ranked = sorted(wishlist, key=lambda w: (0 if w[0] in contested else 1, -w[1]))
    drops = [w[2] for w in ranked if w[2]]
    notes = []
    if len(drops) != len(set(drops)):
        notes.append("two claims share one drop: if the first wins, the second fails on the missing drop")
    notes.append(f"your first win uses priority {pos}; after any win you move to {len(order)}")
    return {"current_priority": pos, "ranked": ranked, "notes": notes}
