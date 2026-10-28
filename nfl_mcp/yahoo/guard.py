"""Read-only guard for every Yahoo request this package makes.

Yahoo's *write* actions are plain GET links (``/addplayer?apid=...``,
``/dropplayer``, ``/proposetrade``, ``/editroster``), so "we only send GETs" is
not enough: a mis-parsed link followed by a browser would still change a roster.
The guard therefore allow-lists the *paths* that may be loaded and rejects
everything else, including any URL that carries a write verb or a ``crumb``
(Yahoo's CSRF token that every state-changing link includes).
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

HOSTS = frozenset({
    "football.fantasysports.yahoo.com",
    "fantasysports.yahooapis.com",
})

# Path suffixes under /f1/<league_id>/ that are read pages. Anything else is refused.
READ_PAGES = (
    "",                # /f1/<id>  (league home)
    "/<mid>",          # a team roster page
    "matchup", "players", "standings", "transactions", "settings",
    "playersearch", "pointsagainst", "scoreboard", "draftresults",
    "playerswatch",
)

WRITE_WORDS = re.compile(
    r"(add|drop|claim|trade|propose|accept|reject|cancel|edit|submit|"
    r"confirm|waiver(?:s)?_?(?:add|drop|claim)|setlineup|swap|move)",
    re.IGNORECASE,
)

_LEAGUE_PATH = re.compile(r"^/f1/(?P<league>\d+)(?:/(?P<rest>[^/?#]*))?/?$")
_API_PATH = re.compile(r"^/fantasy/v2/(?:league|team|game|users|players)/", re.IGNORECASE)


class WriteBlocked(Exception):
    """Raised for any URL that is not a known read-only Yahoo page or API collection."""


def check_read_only(url: str, *, league_id: str | None = None) -> str:
    """Return the url if it is a permitted read; raise :class:`WriteBlocked` otherwise."""
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in HOSTS:
        raise WriteBlocked(f"host not allowed: {u.hostname!r}")
    if re.search(r"(^|[&?])crumb=", u.query, re.IGNORECASE):
        raise WriteBlocked("URL carries a crumb token (state-changing link)")
    if u.hostname == "fantasysports.yahooapis.com":
        if not _API_PATH.match(u.path):
            raise WriteBlocked(f"API path not allowed: {u.path}")
        if re.search(r"/(transactions|players)/[^/]*;?.*(add|drop)", u.path, re.IGNORECASE):
            # Reading /league/<k>/transactions is fine; POST-style sub-resources are not.
            pass
        return url
    m = _LEAGUE_PATH.match(u.path)
    if not m:
        raise WriteBlocked(f"path not allowed: {u.path}")
    if league_id and m.group("league") != str(league_id):
        raise WriteBlocked(f"other league: {m.group('league')}")
    rest = (m.group("rest") or "").lower()
    if rest == "" or rest.isdigit() or rest in READ_PAGES:
        return url
    raise WriteBlocked(f"page not on the read allow-list: /{rest}")


def check_method(method: str) -> None:
    if method.upper() != "GET":
        raise WriteBlocked(f"only GET is permitted, got {method}")
