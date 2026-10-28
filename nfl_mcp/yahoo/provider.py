"""YahooProvider: Yahoo pages -> the Sleeper-shaped dicts the rest of nfl_mcp consumes.

Shapes follow Sleeper's REST payloads:

* league  : ``{league_id, name, season, sport, status, total_rosters, roster_positions,
              scoring_settings, settings{reserve_slots, ...}, metadata{platform: "yahoo"}}``
* roster  : ``{roster_id, owner_id, league_id, players, starters, reserve, settings{wins,
              losses, ties, fpts, fpts_against, waiver_position, total_moves}, metadata}``
* matchup : ``{roster_id, matchup_id, points, starters, players, players_points}``

Player ids are Sleeper ids (resolved through :class:`~nfl_mcp.yahoo.idmap.IdMap`), so the
enrichment, injury, projection and news layers all work. Anything that cannot be mapped is
kept under ``unmapped`` with the Yahoo name so it is visible, not silently dropped.
"""
from __future__ import annotations

from datetime import UTC, datetime

from .adapters import HtmlAdapter
from .idmap import IdMap
from .scoring import BFS_SCORING, diff_from_bfs
from .scoring import parse_settings_rows as parse_scoring_rows

# Yahoo slot label -> Sleeper roster_positions label.
SLOT_MAP = {"W/R/T": "FLEX", "W/R": "FLEX", "BN": "BN", "IR": "IR", "DEF": "DEF"}
ROSTER_POSITIONS = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "K", "DEF"] + ["BN"] * 6
IR_SLOTS = 2


def slot_label(yahoo_slot: str) -> str:
    return SLOT_MAP.get(yahoo_slot, yahoo_slot)


class YahooProvider:
    def __init__(self, league_number: str, source, sleeper_players: dict | None = None,
                 season: int | None = None, ttl: float = 60.0):
        """``source`` is a page source (has ``fetch``) or an adapter (has ``standings``)."""
        self.league = str(league_number)
        self.data = source if hasattr(source, "standings") else HtmlAdapter(source, self.league, ttl)
        self.ids = IdMap(sleeper_players or {})
        self.season = season or datetime.now(UTC).year

    def _sid(self, p: dict) -> tuple[str | None, str]:
        return self.ids.resolve(p.get("yahoo_id"), p.get("name"), p.get("team"), p.get("position"))

    # ---- league -------------------------------------------------------------
    async def get_league(self) -> dict:
        scoring, scoring_source, scoring_diff = dict(BFS_SCORING), "bfs_constant", {}
        try:
            live = parse_scoring_rows(await self.data.settings_rows())
            if live:
                scoring_diff = diff_from_bfs(live)
                scoring.update(live)
                scoring_source = "yahoo_settings_page"
        except Exception:  # settings page unavailable: the verified constant is the fallback
            pass
        standings = await self.data.standings()
        return {
            "league_id": f"yahoo:{self.league}", "name": "Yahoo league " + self.league,
            "sport": "nfl", "season": str(self.season), "status": "in_season",
            "total_rosters": len(standings) or 10,
            "roster_positions": ROSTER_POSITIONS,
            "scoring_settings": scoring,
            "settings": {"reserve_slots": IR_SLOTS, "waiver_type": "rolling", "waiver_budget": 0},
            "metadata": {"platform": "yahoo", "scoring_source": scoring_source,
                         "scoring_diff_from_bfs": scoring_diff},
        }

    # ---- rosters ------------------------------------------------------------
    def _roster_from_slots(self, mid: str, team: dict | None, slots: list[dict]) -> dict:
        players, starters, reserve, unmapped = [], [], [], []
        for s in slots:
            label = slot_label(s["slot"])
            if s.get("empty"):
                if label not in ("BN", "IR"):
                    starters.append("0")
                continue
            sid, how = self._sid(s)
            if sid is None:
                unmapped.append({"name": s.get("name"), "yahoo_id": s.get("yahoo_id"), "slot": s["slot"]})
                if label not in ("BN", "IR"):
                    starters.append("0")
                continue
            players.append(sid)
            if label == "IR":
                reserve.append(sid)
            elif label != "BN":
                starters.append(sid)
        t = team or {}
        return {
            "roster_id": int(mid), "owner_id": str(mid), "league_id": f"yahoo:{self.league}",
            "players": players, "starters": starters, "reserve": reserve, "taxi": None,
            "settings": {"wins": t.get("wins", 0), "losses": t.get("losses", 0),
                         "ties": t.get("ties", 0), "fpts": t.get("points_for"),
                         "fpts_against": t.get("points_against"),
                         "waiver_position": t.get("waiver"), "total_moves": t.get("moves")},
            "metadata": {"team_name": t.get("name"), "platform": "yahoo"},
            "unmapped": unmapped,
            "yahoo_slots": [{"slot": s["slot"], "name": s.get("name"), "injury": s.get("injury"),
                             "projected": s.get("projected"), "game": s.get("game")}
                            for s in slots if not s.get("empty")],
        }

    async def get_rosters(self) -> list[dict]:
        out = []
        for t in await self.data.standings():
            out.append(self._roster_from_slots(t["mid"], t, await self.data.roster(t["mid"])))
        return out

    async def get_my_roster(self, mid: str) -> dict:
        teams = {t["mid"]: t for t in await self.data.standings()}
        return self._roster_from_slots(mid, teams.get(str(mid)), await self.data.roster(mid))

    # ---- matchups -----------------------------------------------------------
    async def get_matchups(self, week: int) -> list[dict]:
        teams = await self.data.standings()
        seen: set[frozenset] = set()
        out, mu_id = [], 0
        for t in teams:
            m = await self.data.matchup(week, t["mid"])
            a, b = m["home"], m["away"]
            if not a.get("team") or not b.get("team"):
                continue
            pair = frozenset({a["team"]["mid"], b["team"]["mid"]})
            if pair in seen:
                continue
            seen.add(pair)
            mu_id += 1
            for side in (a, b):
                ids, pp, starters = [], {}, []
                for grp in ("starters", "bench"):
                    for p in side[grp]:
                        sid, _ = self._sid(p)
                        if sid is None:
                            continue
                        ids.append(sid)
                        if grp == "starters":
                            starters.append(sid)
                        pp[sid] = p.get("points") if p.get("points") is not None else 0.0
                out.append({"roster_id": int(side["team"]["mid"]), "matchup_id": mu_id,
                            "points": side.get("total_pts") or 0.0,
                            "projected_points": side.get("total_proj"),
                            "starters": starters, "players": ids, "players_points": pp,
                            "custom_points": None})
        return out

    # ---- waiver / transactions ---------------------------------------------
    async def get_waiver_order(self) -> list[dict]:
        teams = await self.data.standings()
        return sorted((t for t in teams if t.get("waiver")), key=lambda t: t["waiver"])

    async def get_free_agents(self, pos: str = "RB", status: str = "A") -> list[dict]:
        out = []
        for p in await self.data.players(pos, status):
            sid, how = self._sid(p)
            out.append({**p, "player_id": sid, "id_source": how})
        return out

    async def get_transactions(self, kind: str = "all") -> dict:
        return await self.data.transactions(kind)
