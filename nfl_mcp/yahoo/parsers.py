"""Parsers for Yahoo Fantasy *read* pages (HTML), written against the live DOM seen on
2026-10-27. Pure functions: ``html -> dict``. No network, no browser, easy to test.

Observed layout (football.fantasysports.yahoo.com/f1/<league>/...):

* Roster ``/<mid>``: ``table#statTable0`` (offense), ``#statTable1`` (K), ``#statTable2``
  (DEF). Row: ``cells[0]`` slot label (QB, RB, W/R/T, BN, IR), ``cells[1]`` eligibility
  text, the player cell is ``td.player`` (name link ``a.Nowrap``, ``span.Fz-xxs`` = "Atl - RB",
  Yahoo player id in the ``/nfl/players/<id>`` href, injury label as a ``span.F-injury``).
* Players list ``/players``: the player cell is followed by an availability cell whose text
  is ``FA``, ``W (Oct 28)`` or an owner name; the add link contains ``apid=<id>``. **That link
  is a write action - the parser reads the id out of it and never follows it.**
* Matchup ``/matchup``: ``table#statTable1`` starters, ``#statTable2`` bench; one row holds
  both teams: ``[stats][my player][proj][pts][pos][pos][pos][pts][proj][their player][stats]``.
* Standings ``/``: the waiver order is the ``Waiver`` column; see :func:`parse_standings`.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

_PLAYER_ID = re.compile(r"/nfl/players/(\d+)")
_TEAM_HREF = re.compile(r"/f1/(\d+)/(\d+)$")
_APID = re.compile(r"apid=(\d+)")
# Spaces around the hyphen are required: injury badges such as "IR-R" or "PUP-R" share the span class.
_TEAM_POS = re.compile(r"^\s*([A-Za-z]{2,4})\s+-\s+([A-Z/]+)\s*$")
_WAIVER_TAG = re.compile(r"^W\s*\((?P<date>[A-Za-z]{3}\s+\d{1,2})\)$")
IR_ELIGIBLE = {"IR", "IR-R", "PUP-R", "NFI-R", "O"}
INJURY_TAGS = {"Q", "D", "O", "IR", "IR-R", "PUP-R", "NFI-R", "SUSP", "COV", "NA"}


def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el is not None else ""


def _float(s: str) -> float | None:
    s = (s or "").strip().replace(",", "")
    if s in ("", "-", "–", "—"):
        return None
    try:
        v = float(s.rstrip("%"))
    except ValueError:
        return None
    return v


def _player_from_cell(cell) -> dict:
    """Common player-cell extraction shared by the roster / players / matchup tables."""
    name_a = cell.select_one("a.Nowrap") or cell.find("a", href=_PLAYER_ID)
    pid = None
    for a in cell.find_all("a", href=True):
        m = _PLAYER_ID.search(a["href"])
        if m:
            pid = m.group(1)
            break
    if pid is None:
        for a in cell.find_all("a", href=True):
            m = _APID.search(a["href"])
            if m:
                pid = m.group(1)
                break
    team = pos = None
    for sp in cell.select("span.Fz-xxs, span.Fz-xs"):
        m = _TEAM_POS.match(_text(sp))
        if m:
            team, pos = m.group(1).upper(), m.group(2)
            break
    inj = None
    for sp in cell.find_all("span"):
        t = _text(sp)
        if t in INJURY_TAGS and not sp.find("span"):
            inj = t
            break
    game = None
    ga = cell.select_one("a.F-reset")
    if ga is not None:
        gt = _text(ga)
        opp = cell.select_one("a.Inline")
        game = {"kickoff": re.sub(r"\s*[@v]$", "", gt).strip(),
                "home": gt.rstrip().endswith("v"),
                "opponent": _text(opp).upper() if opp is not None else None}
    if game is None:
        gs = cell.select_one("span.ysf-game-status")
        if gs is not None and _text(gs).lower() == "bye":
            game = {"kickoff": None, "home": None, "opponent": None, "bye": True}
    name = _text(name_a) if name_a is not None else None
    return {"yahoo_id": pid, "name": name, "team": team, "position": pos, "injury": inj, "game": game}


def parse_roster(html: str) -> dict:
    """Roster page -> ``{"slots": [...], "owner": str|None}``.

    Each slot: ``{slot, yahoo_id, name, team, position, injury, projected, pct_start,
    pct_rostered, bye}``. Only the columns Yahoo shows for the chosen week are filled.
    """
    soup = BeautifulSoup(html, "lxml")
    slots: list[dict] = []
    for tid in ("statTable0", "statTable1", "statTable2"):
        t = soup.find(id=tid)
        if t is None:
            continue
        heads = [[_text(c) for c in tr.find_all(["th", "td"])] for tr in t.select("thead tr")]
        flat = heads[-1] if heads else []
        for tr in t.select("tbody tr"):
            cells = tr.find_all("td", recursive=False)
            if len(cells) < 3:
                continue
            slot = _text(cells[0])
            pcell = _find_player_cell(tr) or tr.select_one("td.player") or cells[2]
            if not _text(pcell) or _text(pcell).lower().startswith("(empty)"):
                slots.append({"slot": slot, "yahoo_id": None, "name": None, "empty": True})
                continue
            p = _player_from_cell(pcell)
            p["slot"] = slot
            p["empty"] = False
            by = {}
            for i, c in enumerate(cells):
                if i < len(flat) and flat[i]:
                    by.setdefault(flat[i], _text(c))
            p["bye"] = _float(by.get("Bye", ""))
            p["projected"] = _float(by.get("Proj Pts", ""))
            p["fantasy_points"] = _float(by.get("Fan Pts", ""))
            p["pct_start"] = _float(by.get("% Start", ""))
            p["pct_rostered"] = _float(by.get("% Ros", ""))
            slots.append(p)
    return {"slots": slots}


def _find_player_cell(tr):
    """The td holding the player's profile link (``/nfl/players/<id>``, not ``/news``)."""
    for td in tr.find_all("td", recursive=False):
        for a in td.find_all("a", href=True):
            if re.search(r"/nfl/players/\d+$", a["href"]):
                return td
    return None


def parse_players(html: str) -> dict:
    """Free-agent / all-player list -> ``{"players": [...]}`` with availability.

    ``availability`` is ``{"state": "FA"|"W"|"OWNED", "clears": "Oct 28"|None, "owner": str|None}``.
    ``FA`` is an instant add, ``W`` is a waiver claim until the date, ``OWNED`` shows the owner.
    The add links in this table are write actions; ids are read from the profile link only.
    """
    soup = BeautifulSoup(html, "lxml")
    out = []
    for t in soup.find_all("table"):
        rows = [tr for tr in t.select("tbody tr") if _find_player_cell(tr) is not None]
        if not rows:
            continue
        for tr in rows:
            cells = tr.find_all("td", recursive=False)
            pcell = _find_player_cell(tr)
            p = _player_from_cell(pcell)
            idx = cells.index(pcell)
            avail_txt = _text(cells[idx + 1]) if idx + 1 < len(cells) else ""
            m = _WAIVER_TAG.match(avail_txt)
            if m:
                p["availability"] = {"state": "W", "clears": m.group("date"), "owner": None}
            elif avail_txt in ("FA", "Free Agent"):
                p["availability"] = {"state": "FA", "clears": None, "owner": None}
            elif avail_txt:
                p["availability"] = {"state": "OWNED", "clears": None, "owner": avail_txt}
            else:
                p["availability"] = {"state": "UNKNOWN", "clears": None, "owner": None}
            out.append(p)
        break
    return {"players": out}


def parse_matchup(html: str) -> dict:
    """Matchup page -> ``{"home": {...}, "away": {...}}`` (``home`` = the page's left team)."""
    soup = BeautifulSoup(html, "lxml")
    sides = ({"starters": [], "bench": [], "total_proj": None, "total_pts": None},
             {"starters": [], "bench": [], "total_proj": None, "total_pts": None})
    for tid, key in (("statTable1", "starters"), ("statTable2", "bench")):
        t = soup.find(id=tid)
        if t is None:
            continue
        for tr in t.select("tbody tr"):
            cells = tr.find_all("td", recursive=False)
            if len(cells) < 11:
                continue
            pos = _text(cells[4])
            if pos.lower() == "total":
                sides[0]["total_proj"] = _float(_text(cells[2]))
                sides[0]["total_pts"] = _float(_text(cells[3]))
                sides[1]["total_pts"] = _float(_text(cells[7]))
                sides[1]["total_proj"] = _float(_text(cells[8]))
                continue
            left = _player_from_cell(cells[1])
            right = _player_from_cell(cells[9])
            left.update(slot=pos, projected=_float(_text(cells[2])), points=_float(_text(cells[3])))
            right.update(slot=_text(cells[6]) or pos, projected=_float(_text(cells[8])),
                         points=_float(_text(cells[7])))
            sides[0][key].append(left)
            sides[1][key].append(right)
    teams, seen = [], set()
    for a in soup.find_all("a", href=_TEAM_HREF):
        txt = _text(a)
        mid = _TEAM_HREF.search(a["href"]).group(2)
        if txt and txt.lower() != "my team" and mid not in seen:
            seen.add(mid)
            teams.append({"mid": mid, "name": txt})
    sides[0]["team"] = teams[0] if len(teams) > 0 else None
    sides[1]["team"] = teams[1] if len(teams) > 1 else None
    return {"home": sides[0], "away": sides[1]}


_LOWER_N = re.compile(r"Lower waiver priority:\s*(\d+)")
_WINNING = re.compile(r"Winning waiver priority:\s*(\d+)")


def parse_standings(html: str) -> dict:
    """League home -> ``{"teams": [...]}`` from ``table#standingstable``.

    Columns: Rank | Team | W-L-T | PF | PA | Streak | Waiver | Moves. ``waiver`` is the
    team's CURRENT position on the rolling list (1 = first claim).
    """
    soup = BeautifulSoup(html, "lxml")
    t = soup.find(id="standingstable")
    teams = []
    if t is None:
        return {"teams": teams}
    heads = [_text(h) for h in t.select("thead th")]
    col = {h.lower(): i for i, h in enumerate(heads)}
    for tr in t.select("tbody tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < len(heads):
            continue
        # The first team link wraps the logo image and has no text; the name link follows it.
        links = tr.find_all("a", href=_TEAM_HREF)
        a = next((x for x in links if _text(x)), links[0] if links else None)
        mid = _TEAM_HREF.search(a["href"]).group(2) if a else None
        wlt = _text(cells[col.get("w-l-t", 2)]).split("-")
        def num(k, cells=cells):
            return _float(_text(cells[col[k]])) if k in col else None
        teams.append({
            "rank": int(_text(cells[0])) if _text(cells[0]).isdigit() else None,
            "mid": mid, "name": _text(a) if a else _text(cells[1]),
            "wins": int(wlt[0]) if len(wlt) > 0 and wlt[0].isdigit() else 0,
            "losses": int(wlt[1]) if len(wlt) > 1 and wlt[1].isdigit() else 0,
            "ties": int(wlt[2]) if len(wlt) > 2 and wlt[2].isdigit() else 0,
            "points_for": num("pf"), "points_against": num("pa"),
            "streak": _text(cells[col["streak"]]) if "streak" in col else None,
            "waiver": int(num("waiver")) if num("waiver") is not None else None,
            "moves": int(num("moves")) if num("moves") is not None else None,
        })
    return {"teams": teams}


def _entries(cell) -> list[dict]:
    """``div.Pbot-xs`` move entries in a transactions row: player, team/pos, label."""
    out = []
    for d in cell.select("div.Pbot-xs"):
        a = d.find("a")
        if a is None:
            continue
        pid = None
        for link in d.find_all("a", href=True):
            m = _PLAYER_ID.search(link["href"])
            if m:
                pid = m.group(1)
                break
        pos = d.select_one("span.F-position")
        tp = _TEAM_POS.match(_text(pos)) if pos is not None else None
        label = _text(d.select_one("h6.F-shade")) if d.select_one("h6.F-shade") else ""
        out.append({"player": _text(a), "yahoo_id": pid,
                    "team": tp.group(1).upper() if tp else None,
                    "position": tp.group(2) if tp else None, "label": label})
    return out


def parse_transactions(html: str) -> dict:
    """Transactions log (``?transactionsfilter=all``) -> ``{"events": [...]}``.

    Each row is one team's move, newest first: ``{team, mid, when, moves: [{player, yahoo_id,
    label}]}`` where label is ``Free Agent`` (added from FA), ``Waiver`` (won a claim), ``To
    Waivers`` (dropped) or a trade label. The waiver-offers view (``=waiver``) rows also
    carry ``winning_priority`` and ``losers`` [(team, priority)].
    """
    soup = BeautifulSoup(html, "lxml")
    t = next((x for x in soup.find_all("table")
              if any("Tst-transaction" in c for c in (x.get("class") or []))), None)
    events = []
    if t is None:
        return {"events": events}
    for tr in t.select("tbody tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 3:
            continue
        txt = _text(tr)
        end = cells[-1]
        ta = end.find("a", href=_TEAM_HREF)
        when = _text(end.select_one("span.Block")) or None
        if not when:
            m = re.search(r"([A-Z][a-z]{2} \d{1,2}, ?\d{1,2}:\d{2} [ap]m)", _text(end))
            when = m.group(1) if m else None
        ev = {"team": _text(ta) if ta else None,
              "mid": _TEAM_HREF.search(ta["href"]).group(2) if ta else None,
              "when": when, "moves": _entries(cells[1])}
        w = _WINNING.search(txt)
        if w:  # a contested claim from the waiver-offers view
            p = _player_from_cell(cells[1])
            losers = []
            for a in cells[1].find_all("a", href=_TEAM_HREF):
                sib = a.next_sibling
                m = _LOWER_N.search(sib if isinstance(sib, str) else "")
                if m:
                    losers.append((_text(a), int(m.group(1))))
            ev.update(winning_priority=int(w.group(1)),
                      moves=[{"player": p["name"], "yahoo_id": p["yahoo_id"], "label": "Waiver"}],
                      losers=losers)
        events.append(ev)
    return {"events": events}


def parse_settings_rows(html: str) -> list[str]:
    """League settings page -> ``["<label> = <league value> = <default>", ...]`` rows.

    Fed to :func:`nfl_mcp.yahoo.scoring.parse_settings_rows`.
    """
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for tr in soup.select("table tr"):
        parts = [_text(c) for c in tr.find_all(["th", "td"], recursive=False)]
        parts = [p for p in parts if p != ""]
        if len(parts) >= 2:
            rows.append(" = ".join(parts))
    return rows
