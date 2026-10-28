# Yahoo league support (read-only) and Bluesky news leads

Pass a Yahoo league as `yahoo:<number>` (for BFS: `yahoo:1234567`) to any league tool. Rosters, matchups, league settings and
users are read from Yahoo and returned in Sleeper's shape, so the lineup, waiver, opponent, bye and injury tools run on it
unchanged. Nine extra tools are Yahoo/news specific (`get_yahoo_waiver_order`, `plan_yahoo_waiver_sequence`,
`get_yahoo_free_agents`, `get_yahoo_transactions`, `check_yahoo_scoring`, `get_yahoo_projection_comparison`, `fantasy_checkin`, `get_insider_posts`, `verify_news_handles`).

## Weekly check-in and per-user defaults

`fantasy_checkin(week=None, league_id=None, roster_id=None)` is one read-only call that returns the league (scoring source and any
difference from the BFS constants, waiver order and your position), every roster slot with bye week, injury tag and Yahoo vs
nfl_mcp projection, a `problems` list (starters on bye, empty slots, starters tagged O/D, IR-slot players whose tag is not IR-eligible,
a full bench), the wire for each problem position (`FA` vs `W (date)`) plus the top `get_waiver_targets`, the opponent (projected
points, injured starters, byes), the next four weeks of byes (flagging weeks with fewer than two active RBs), reporter posts that name
your players, and `warnings` for anything single-source or unverified. A team-level projection gap under 12 points is reported as
noise. Nothing is added, dropped, claimed, traded or changed on Yahoo.

No league or team id lives in the repo. Set them in your environment:

```
$env:NFL_MCP_DEFAULT_LEAGUE = "yahoo:1234567"   # used when league_id is omitted
$env:NFL_MCP_MY_ROSTER      = "9"               # your team number, used when roster_id / my_mid is omitted
```

An explicit argument always wins. With neither an argument nor the variable, the tools return an error naming the variable.

**Yahoo rate limiting.** Yahoo answers a burst of page loads with HTTP 999 "Request denied" (a few bytes that would otherwise parse
as an empty league). The reader now raises a clear error and refuses further requests for ten minutes. A check-in loads roughly a
dozen pages; do not run several at once, and do not script repeated runs.

## Safety contract

* **Read-only.** There is no add, drop, claim, trade or lineup-edit code in this package. `nfl_mcp/yahoo/guard.py`
  allow-lists the pages that may be loaded (roster, matchup, players, standings, transactions, settings) and rejects any
  URL with a write verb or a `crumb` token. API requests are held to the same rule: only the league's own read collections and a
  team's roster/matchups/stats, for your league id, are allowed. Yahoo's add/drop links are plain GETs, so the allow-list - not
  "GET only" - is what protects the roster. The guard only constrains nfl_mcp: a Chrome you leave signed in with the debug port
  open can still be driven by any program on your computer, so close it when you are done. `tests/test_yahoo_core.py` pins this.
* **No credentials in code.** Browser route: you sign in yourself; only the browser profile (cookies) is kept. API route: client
  id/secret come from your environment; the token file is mode 600.
* **News is leads.** Bluesky posts are tagged `single_source` / `lead`. Confirm player, body part, status and date in a second
  independent source before acting.
* Yahoo's terms restrict automated access. The reader is rate-limited (1.5 s between pages, 60 s cache) and only reads your own
  league; whether that is acceptable is your call.

## Three ways to give it Yahoo data

| Route | Setup | Status |
|---|---|---|
| **Logged-in browser** (default) | `pip install -e ".[yahoo]" && playwright install chromium`, then `python -m nfl_mcp.yahoo.login` and sign in yourself in the window | parsers verified against a live league (roster, matchup, standings, players, transactions, settings) and covered by scrubbed real-page fixtures in `tests/fixtures/yahoo`; Yahoo can change its markup, so re-run the self-check below after any surprise |
| **Saved pages** | set `YAHOO_SNAPSHOT_DIR=/path` with pages saved by `selfcheck --save` (or by hand) | simplest; nothing logs in |
| **Official API** | register an app at developer.yahoo.com (Fantasy Sports, **read** permission), set `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`, `YAHOO_ACCESS=api` | **experimental, unverified** against live Yahoo; implements standings + rosters only; earlier tests of a new app returned `additional_authorization_required` |

**If Google blocks the sign-in** ("This browser or app may not be secure"): the `login` window is an automated browser and Google
refuses it. Either use Yahoo's own email/password or emailed-code sign-in in that window, or attach to your normal Chrome:

```
# Windows PowerShell - start a separate Chrome profile with a local debug port, and sign in to Yahoo there by hand
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$HOME\.nfl_mcp\chrome-profile"
$env:YAHOO_CDP_URL = "http://localhost:9222"
py -m nfl_mcp.yahoo.selfcheck 1234567 9 --save .\yahoo-pages
```

If Chrome is already running, close every window first (check the system tray): a new window then joins the existing
process and silently ignores `--remote-debugging-port`. In PowerShell start Chrome with `Start-Process` or `&`, not a bare quoted path.

Leave that Chrome window open while the server runs; nfl_mcp opens its own tabs in it, reads only the allow-listed pages, and
never closes your browser. The debug port is reachable only from your computer; close that Chrome when you are done.

Docker: `Dockerfile.yahoo` bundles Chromium. Do the one-time sign-in on a machine with a display, then mount the profile:
`docker run -p 9000:9000 -v $HOME/.nfl_mcp:/root/.nfl_mcp nfl_mcp_yahoo`. A profile created on another OS may need a fresh
sign-in inside the container's Chromium version.

## First run: verify against the live site

```
python -m nfl_mcp.yahoo.selfcheck 1234567 9 --save ./yahoo-pages
```

It prints the scoring source, the waiver order, how many of your players mapped to Sleeper ids, every **UNMAPPED** player, the
first running backs on the wire with their `FA` / `W (date)` tag, and both sides of your matchup (`--week N`, default the
current NFL week) with totals and unmapped players. Anything unmapped or empty means a selector needs a fix. Saved pages
(`--save`) hold your account details and session tokens: keep them out of git, and scrub them (league id, team and manager
names, add/drop/watch links with a `crumb` token) before turning them into test fixtures.

## Environment

`YAHOO_PROFILE_DIR`, `YAHOO_SNAPSHOT_DIR`, `YAHOO_ACCESS` (`api` for the experimental route), `YAHOO_CLIENT_ID`,
`YAHOO_CLIENT_SECRET`, `YAHOO_TOKEN_FILE`, `NFL_MCP_BLUESKY_HANDLES` (comma separated).

## BFS rules encoded here

* Scoring (`nfl_mcp/yahoo/scoring.py`): read from Yahoo's settings page on 2026-10-27 and re-read live on every `get_league`;
  a commissioner change shows up in `metadata.scoring_diff_from_bfs`. nfl_mcp pays the yardage bonus as a non-stacking pair, so
  BFS's "+2 at 100/300, +5 in total at 200/400" is stored as 2 and 5.
* Waivers (`nfl_mcp/yahoo/waiver_model.py`): one rolling list; every successful claim (contested or not) sends the winner to the
  bottom; failed claims are free; free-agent adds/drops do not move anyone; the priority on Yahoo's Waiver Offers page is the
  position when that claim was processed. Validated against the league log (21 contested claims, final order reproduced exactly).

## Known gaps

* Projections: the lineup tools use nfl_mcp's own projections, never Yahoo's. `get_yahoo_projection_comparison` shows Yahoo's
  per-player projection beside ours (comparison only; a test pins that the optimizer does not read it). Yahoo's figure is the
  current week's, and is blank for players on bye.
* Bluesky handles in `DEFAULT_HANDLES` (`rapsheet.bsky.social`, Ian Rapoport; `nflnewsposter.bsky.social`, a manually
  maintained account that reposts reporters from Twitter, so second-hand) were resolved live on 2026-10-27. Run
  `verify_news_handles` if one stops returning posts. X/Twitter has no free read API, so it is not included.
* The API route does not implement matchups, free agents or transactions.
