"""Yahoo Fantasy *official API* route (EXPERIMENTAL - not verified against live Yahoo).

Yahoo gates the Fantasy Sports API behind an app registration, and in earlier tests calls
from a freshly authorised app failed with ``additional_authorization_required``. This module
is the ready-to-try path if Yahoo approves your app. Until then the browser / snapshot route
is the working one.

Safety: scope requested is ``fspt-r`` (READ ONLY), every data request is a GET that passes
:func:`nfl_mcp.yahoo.guard.check_read_only`, and there is no method that POSTs to a fantasy
resource. The only POST is the OAuth token exchange at login.yahoo.com. Client id / secret
come from YOUR environment (``YAHOO_CLIENT_ID``, ``YAHOO_CLIENT_SECRET``); nothing here asks
for, stores or logs your Yahoo password.
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .guard import check_read_only

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
API = "https://fantasysports.yahooapis.com/fantasy/v2"
SCOPE = "fspt-r"  # read-only fantasy sports


# ------------------------------------------------------------------ JSON normaliser
def norm(obj):
    """Undo Yahoo's JSON quirks: numeric-keyed objects become lists, lists of single-key
    dicts merge into one dict."""
    if isinstance(obj, dict):
        keys = [k for k in obj if k != "count"]
        if keys and all(str(k).isdigit() for k in keys):
            return [norm(obj[k]) for k in sorted(keys, key=int)]
        return {k: norm(v) for k, v in obj.items()}
    if isinstance(obj, list):
        items = [norm(x) for x in obj]
        if items and all(isinstance(x, dict | list) for x in items):
            merged: dict = {}
            ok = True
            for x in items:
                if isinstance(x, list):
                    x = _merge_list(x)
                if isinstance(x, dict):
                    merged.update(x)
                else:
                    ok = False
            if ok:
                return merged
        return items
    return obj


def _merge_list(items: list):
    merged: dict = {}
    for x in items:
        if isinstance(x, dict):
            merged.update(x)
        elif isinstance(x, list):
            merged.update(_merge_list(x) if isinstance(_merge_list(x), dict) else {})
    return merged


def _find(obj, key):
    """First value stored under ``key`` anywhere in a normalised tree."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, key)
            if r is not None:
                return r
    return None


# ------------------------------------------------------------------ token handling
class TokenStore:
    def __init__(self, path: str | None = None):
        self.path = Path(path or os.getenv("YAHOO_TOKEN_FILE")
                         or Path.home() / ".nfl_mcp" / "yahoo-token.json")

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def save(self, tok: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(tok))
        os.chmod(self.path, 0o600)


def authorize_url(client_id: str, redirect_uri: str = "oob") -> str:
    """The URL YOU open in a browser to approve read-only access; paste the code it gives back."""
    return AUTH_URL + "?" + urlencode({"client_id": client_id, "redirect_uri": redirect_uri,
                                       "response_type": "code", "scope": SCOPE})


class ApiClient:
    def __init__(self, league: str, client_id: str | None = None, client_secret: str | None = None,
                 store: TokenStore | None = None, http: httpx.AsyncClient | None = None):
        self.league = str(league)
        self.client_id = client_id or os.getenv("YAHOO_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("YAHOO_CLIENT_SECRET", "")
        self.store = store or TokenStore()
        self.http = http or httpx.AsyncClient(timeout=30)

    @property
    def league_key(self) -> str:
        return f"nfl.l.{self.league}"

    async def _token_request(self, data: dict) -> dict:
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = await self.http.post(TOKEN_URL, data=data, headers={"Authorization": f"Basic {basic}"})
        r.raise_for_status()
        tok = r.json()
        tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
        self.store.save(tok)
        return tok

    async def exchange_code(self, code: str, redirect_uri: str = "oob") -> dict:
        return await self._token_request({"grant_type": "authorization_code", "code": code,
                                          "redirect_uri": redirect_uri})

    async def _access_token(self) -> str:
        tok = self.store.load()
        if not tok:
            raise RuntimeError("Not authorised: open authorize_url(...), approve read-only access, "
                               "then call exchange_code(<code>)")
        if tok.get("expires_at", 0) < time.time():
            tok = await self._token_request({"grant_type": "refresh_token",
                                             "refresh_token": tok["refresh_token"]})
        return tok["access_token"]

    async def get(self, path: str) -> dict:
        """GET ``API + path`` as normalised JSON. Read-only by construction."""
        url = check_read_only(f"{API}/{path.lstrip('/')}?format=json", league_id=self.league)
        r = await self.http.get(url, headers={"Authorization": f"Bearer {await self._access_token()}"})
        r.raise_for_status()
        return norm(r.json())


# ------------------------------------------------------------------ adapter
def _slot(sel) -> str:
    pos = (sel or {}).get("position") if isinstance(sel, dict) else None
    return pos or "BN"


def roster_slots(tree: dict) -> list[dict]:
    """Normalised ``/team/<key>/roster/players`` -> the same dicts ``parsers.parse_roster`` emits."""
    players = _find(tree, "players") or []
    out = []
    for p in players if isinstance(players, list) else []:
        if not isinstance(p, dict):
            continue
        p = p.get("player", p)
        name = p.get("name", {}).get("full") if isinstance(p.get("name"), dict) else None
        out.append({
            "slot": _slot(p.get("selected_position")), "yahoo_id": str(p.get("player_id", "")) or None,
            "name": name, "team": (p.get("editorial_team_abbr") or "").upper() or None,
            "position": p.get("display_position"), "injury": p.get("status") or None,
            "empty": False, "projected": None, "game": None,
        })
    return out


def standings_teams(tree: dict) -> list[dict]:
    teams = _find(tree, "teams") or []
    out = []
    for t in teams if isinstance(teams, list) else []:
        if not isinstance(t, dict):
            continue
        t = t.get("team", t)
        st = t.get("team_standings", {}) or {}
        tot = st.get("outcome_totals", {}) or {}
        out.append({
            "rank": int(st["rank"]) if str(st.get("rank", "")).isdigit() else None,
            "mid": str(t.get("team_id", "")), "name": t.get("name"),
            "wins": int(tot.get("wins", 0)), "losses": int(tot.get("losses", 0)),
            "ties": int(tot.get("ties", 0)),
            "points_for": float(st["points_for"]) if st.get("points_for") not in (None, "") else None,
            "points_against": float(st["points_against"]) if st.get("points_against") not in (None, "") else None,
            "streak": None,
            "waiver": int(t["waiver_priority"]) if str(t.get("waiver_priority", "")).isdigit() else None,
            "moves": int(t["number_of_moves"]) if str(t.get("number_of_moves", "")).isdigit() else None,
        })
    return out


class ApiAdapter:
    """Same methods as :class:`~nfl_mcp.yahoo.adapters.HtmlAdapter`. Only what is implemented
    is claimed: matchups, transactions and the settings page still need the HTML route."""

    def __init__(self, client: ApiClient):
        self.client = client

    async def standings(self) -> list[dict]:
        return standings_teams(await self.client.get(f"league/{self.client.league_key}/standings"))

    async def roster(self, mid: str) -> list[dict]:
        return roster_slots(await self.client.get(
            f"team/{self.client.league_key}.t.{mid}/roster/players"))

    async def settings_rows(self) -> list[str]:
        return []  # stat ids need a category lookup; the provider falls back to the BFS constants

    async def players(self, pos: str, status: str) -> list[dict]:
        raise NotImplementedError("free-agent list over the API is not implemented; use the browser route")

    async def matchup(self, week: int, mid: str) -> dict:
        raise NotImplementedError("matchup over the API is not implemented; use the browser route")

    async def transactions(self, kind: str) -> dict:
        raise NotImplementedError("transactions over the API are not implemented; use the browser route")
