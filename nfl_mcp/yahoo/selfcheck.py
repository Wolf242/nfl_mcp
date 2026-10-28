"""Live smoke test: ``python -m nfl_mcp.yahoo.selfcheck <league_number> <my_mid> [--save DIR] [--week N]``
(``--week`` defaults to the current NFL week).

Reads your league through the configured source (logged-in browser, or YAHOO_SNAPSHOT_DIR),
prints what parsed, and reports anything that did not map. ``--save DIR`` also writes the raw
pages so they can become test fixtures or feed the snapshot route. Read-only.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from . import parsers
from .provider import YahooProvider
from .sources import default_source, snapshot_name


async def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    src = default_source()
    try:
        return await _run(src, argv)
    finally:
        # Disconnects an attached Chrome (never closes it) and stops Playwright cleanly.
        if hasattr(src, "close"):
            await src.close()


async def _run(src, argv: list[str]) -> int:
    league, mid = argv[0], argv[1]
    save = Path(argv[argv.index("--save") + 1]) if "--save" in argv else None
    from .. import sleeper_tools
    await sleeper_tools.fetch_all_players()
    prov = YahooProvider(league, src, sleeper_tools._PLAYERS_CACHE["data"])

    async def grab(page, **params):
        html = await prov.data._page(page, **params)
        if save:
            save.mkdir(parents=True, exist_ok=True)
            (save / snapshot_name(page, **params)).write_text(html, encoding="utf-8")
        return html

    lg = await prov.get_league()
    print("scoring source:", lg["metadata"]["scoring_source"],
          "| differs from BFS constants:", lg["metadata"]["scoring_diff_from_bfs"] or "no")
    order = await prov.get_waiver_order()
    print("waiver order:", [(t["waiver"], t["name"]) for t in order])
    me = await prov.get_my_roster(mid)
    print(f"my roster: {len(me['players'])} mapped players, {len(me['starters'])} starter slots")
    print("UNMAPPED:", json.dumps(me["unmapped"]) if me["unmapped"] else "none")
    slots = parsers.parse_roster(await grab("roster", mid=mid))["slots"]  # also saves the raw page
    print("slots:", [(s["slot"], s.get("name"), s.get("injury")) for s in slots])
    fa = await prov.get_free_agents("RB")
    print("first RBs on the wire:", [(p["name"], p["availability"]["state"], p["availability"]["clears"])
                                     for p in fa[:5]])
    week = argv[argv.index("--week") + 1] if "--week" in argv else (await sleeper_tools.get_nfl_state()).get("nfl_state", {}).get("week")
    if week:
        mu = parsers.parse_matchup(await grab("matchup", week=int(week), mid1=mid))
        for side in (mu["home"], mu["away"]):
            rows = side["starters"] + side["bench"]
            bad = [p.get("name") for p in rows if not p.get("empty") and prov._sid(p)[0] is None]
            print(f"matchup week {week}: {(side['team'] or {}).get('name')!r} total={side.get('total_pts')} "
                  f"proj={side.get('total_proj')} starters={len(side['starters'])} bench={len(side['bench'])} "
                  f"UNMAPPED={bad or 'none'}")
    await grab("settings"), await grab("standings")
    await grab("transactions", transactionsfilter="all")
    await grab("transactions", transactionsfilter="waiver")
    await grab("players", pos="RB", status="A")
    if save:
        print("raw pages saved to", save)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
