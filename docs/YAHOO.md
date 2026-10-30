# Yahoo league support (read-only) and Bluesky news leads

Pass a Yahoo league as `yahoo:<number>` (for BFS: `yahoo:1234567`) to any league tool. Rosters, matchups, league settings and
users are read from Yahoo and returned in Sleeper's shape, so the lineup, waiver, opponent, bye and injury tools run on it
unchanged. Nine extra tools are Yahoo/news specific (`get_yahoo_waiver_order`, `plan_yahoo_waiver_sequence`,
`get_yahoo_free_agents`, `get_yahoo_transactions`, `check_yahoo_scoring`, `get_yahoo_projection_comparison`, `fantasy_checkin`, `compare_players`, `get_insider_posts`, `verify_news_handles`).

## Weekly check-in and per-user defaults

`fantasy_checkin(week=None, league_id=None, roster_id=None)` is one read-only call that returns the league (scoring source and any
difference from the BFS constants, waiver order and your position), every roster slot with bye week, injury tag and Yahoo vs
nfl_mcp projection, a `problems` list (starters on bye, empty slots, starters tagged O/D, IR-slot players whose tag is not IR-eligible,
a full bench), the wire for each problem or bye-hole position (`FA` vs `W (date)`), with every candidate projected in nfl_mcp's own
model and ranked by its upgrade over what it would replace (zero when a starter is on bye or out), when the claims clear, and the
top `get_waiver_targets` carrying Yahoo's own tag; `drop_candidates` (the bench ranked by a job test, below); the opponent
(projected points, margin and its reading, injured starters, byes); the next four weeks of byes (who is out, how many eligible
players are left at each position against the slots to fill, and weeks with two or more starters on bye); reporter posts that
name your players; and `warnings`. A team-level projection gap under 12 points is reported as noise, and a matchup is called
"good" only when the margin is outside that band by more than a point (within a point of the band it is "at the edge of the noise band"). Players on bye have no projection from anyone and show as on bye.
Every player is printed as `Full Name (TEAM POS)` and matched on the player id, never the surname.

**Drops.** A bench spot has to do one of two jobs: insurance for a starter's bye or injury in the next four weeks (a TE2 covers
the TE1's bye), or a believable path to a bigger role (projecting within 1.5 points of the weakest starter at his position, or a
rising role). The drop is whoever does neither; a bye-week player is never "free to cut" if he covers a later bye. At a position with a single
starter (QB, TE, K, DEF) any backup is injury insurance for him, and `replacement` says how easy he is to replace: how many of the top
free agents at that position project above him (a backup on bye is compared in the first week after it, so a number is never quoted for
his bye week). Insurance that is easy to replace (3 or more above him) ranks as a drop before insurance that is not; this reads one
more Yahoo page for each such position the wire has not already read.
`wire.vs_bench` compares each flagged starter's bench replacements with the best wire pickup first: inside the 1.5-point tie the
answer is "no add needed, use <bench player>". A bench player whose projection of 0 comes from an Out designation that is stale,
in conflict with Yahoo's tag, or Yahoo-only is not ranked: he is listed in `drop_unknown` with the reason.

**Waiver targets.** `get_waiver_targets` ranks Sleeper's player pool, which does not know what Yahoo holds on waivers. The check-in
joins its targets to the Yahoo free-agent lists it read (by player id, then name+team+position, never the surname), keeps only
the ones Yahoo lists and gives each Yahoo's own `FA` / `W (date)` tag. A target it cannot map is dropped and named in `warnings`.
An empty list is explained in `wire.targets_note` (the tool's own "nothing beats your starters" message, or how many targets
were dropped). A starting slot that is empty, or held by a player on bye or out, is a hole and projects 0: the wire for every
position that could fill it (K and DEF included) is read, a wire player is measured against 0 there, and the best available Yahoo
free agent for each hole is always a target (`source: wire_hole`, with Yahoo's `FA` / `W (date)` tag). While a hole exists the note
never says nothing beats your starters.

**Byes.** A position is thin when its active players (not on bye, not tagged out, not on IR) number fewer than its own
starting slots (QB 1, RB 2, WR 2, TE 1, K 1, DEF 1); a thin position's wire is read. The flex is never folded into a position's
count: each week carries a separate `flex` line ("flex: N eligible players left across RB/WR/TE", the players beyond the own slots),
flagged only when it is below the number of flex slots. `slots_to_fill` lists the own slots plus `FLEX` and sums to the nine starters.

**Bench vs wire.** `wire.vs_bench` compares replacements only for the slot the flagged starter occupies: a WR slot takes a WR,
the flex takes an RB, WR or TE, judged by each player's own position (not the wire page he was read from). A wire player worse
than the best bench option by more than the 1.5-point tie band is reported as "worse than <bench player> by X".
Nothing is added, dropped, claimed, traded or changed on Yahoo.

**Verification.** Yahoo's injury tags and bye weeks are cross-checked where nfl_mcp has data: injury tags against nfl_mcp's own
injury reports (with the week's practice line and the game-day decision), bye weeks against the cached NFL schedule for the next
four weeks (fetched and cached if missing). Each injury is `agree` (designation, practice status and body part all match), `partial` (the designations match but a
field differs or is missing; `fields_differ` and `fields_missing` name them, and since Yahoo's roster page gives only the designation a
Yahoo tag alone is `partial`, never `agree`), `conflict`, `yahoo_only` (no second source) or `other_only` (nfl_mcp has an injury
report Yahoo does not show); each bye is `agree`, `conflict` or `unverified`. A `yahoo_only` tag is Yahoo's word alone. Players
whose injury data is not to be trusted carry a flag: `stale_injury` (the refresh timed out or the stored report is more than two days
old), `injury_conflict` or `yahoo_only`. News posts relayed by one aggregator account count as one independent origin. How much gets verified
depends on what your server has cached: byes are checked against a schedule that is fetched if missing, and the check-in asks for fresh injury reports for your roster's teams first (bounded; on a timeout it uses what is stored and says so). Sleeper's injury status is one of nfl_mcp's injury sources, so a tag Sleeper contradicts shows as a `conflict`. Because nfl_mcp's projections now price those injury tags, a Q or D player's projection can sit well below Yahoo's.

**Injury refresh, dates and stale records.** The check-in asks for fresh injury reports for your roster's teams first. That is
a crawl of ESPN's athlete pages behind a rate limiter, not a single request: measured from a home PC, one team took about 1 second
and the 13 teams of a full roster took about 146 seconds (the network was fine; the first wait of 6 seconds was simply shorter than
the work). It now waits 6 seconds, then retries once waiting `NFL_MCP_INJURY_RETRY_WAIT` (default 30) seconds more on the same
background crawl, so no second crawl is started. If it still has not finished, the warning names each try's wait, the real error
and the total, and says the crawl keeps running and stores what it finds: the next check-in reads it (reports stay fresh for 2 hours
Thursday to Monday, 12 hours otherwise, so the first check-in after a long gap is the one that times out).

Every injury record shows its report date (`report_date`, as dated by the source; `stored_at` is only when nfl_mcp saved it and is
used, and labelled, only when the source gave no date). A record is stale when it is older than 3 days or dated before the current
NFL week started (the Tuesday), even if the refresh worked, or when the refresh timed out. Each record has a `statement` to quote:
`nfl_mcp report (dated X) says ...` when current, `stored report (dated X, stale: ...) says ...` when not; a stale stored record is
never stated as fact. When a newer post names the player, the statement becomes that post, labelled a single-source lead
(`stored_statement` keeps the old wording). A season-long status (PUP, NFI, IR) that Yahoo does not show, or any stale record for a
player Yahoo shows active, is flagged `season_long_status` / `predates_activation` and never makes a player Out or projects 0 in the
check-in's rankings (the projection engine's own number is shown, and marked unknown, not rewritten).

**News.** Posts that are player quotes or predictions ("guarantees a win") are dropped before leads are counted, unless they carry
injury or availability wording; they are listed in `news.dropped`.

**Close calls.** `close_calls` lists every starter and same-eligibility bench player (a WR slot takes a WR, the flex an RB, WR
or TE; nobody on bye or out) that the projections cannot separate. The gap is the **median** across each player's available sources
(nfl_mcp, model, Sleeper, Yahoo), the same rule `compare_players` uses, and every source's own gap is listed (`source_gaps`). A pair is
listed when the median gap is inside the 1.5-point tie band, and also when the sources disagree (one source inside the band and another
outside it, or on who leads) even if the median is outside it: those read "not a clear call" with `sources_disagree` and the sources
named. Each is a toss-up or an unclear call, not a reason to switch: the pair goes to `compare_players`.

## While games are being played

Yahoo serves a different matchup page once games start (seen 2026-10-29): two lineup tables drawn by script after the page
loads, with abbreviated names ("C. Hubbard"), the Yahoo player id in `data-ys-playerid`, the points so far in `Fan Pts` and, for a
player whose game has started, two numbers in `Proj` (the pre-game projection, then Yahoo's live one). The roster page does not
change but shows the points instead of a projection for a started player.

* **Reading it.** The browser source waits (up to 15 s) for the lineup tables on a matchup page; `parse_matchup` reads both layouts
  and returns `layout` (`classic`, `live` or None). Each player has `projected`, `points`, `started`, the game line (`scheduled`,
  `live`, `final`, or a bye as "No Game") and, in the live layout, `live_projection` and the stat line. The check-in swaps in full
  names by Yahoo id, or by first initial, surname, team and position when only one player fits.
* **Totals.** A started player counts for his points so far and every other starter for the source's projection, so Yahoo and
  nfl_mcp are compared on the same basis: "points so far plus projections for players yet to play". `starter_totals.counted_as_played`
  names who was counted at points (with the game's status), `counted_as_projected` the rest, and `scope` says which starters a total
  covers. A starter missing a number from either source is left out of both totals and listed with the reason in `not_compared`; a
  total over part of the starters is never given without saying which. The opponent block uses the same rule for both sides (Yahoo's
  numbers, since nfl_mcp has none for their players) and lists `counted_as_played` for each. A finished week is the points scored.
* **No opponent.** If the page gives no lineup or no teams, the check-in says it could not read the Yahoo matchup page (named with its
  week and team number), that the layout may have changed, and that this is not a bye week; the opponent comparison is left out.

## ESPN's comment and the body part

Each stored injury record is shown with ESPN's own comment on it (`comment` in `verification.injuries`, quoted in the `statement`).
An **Out** whose comment says the player was activated or is practicing (not "did not practice", not "a chance to resume
practicing") is flagged `status_may_be_stale`: "status may be stale despite its date". Such a record no longer makes a 0 projection a
fact. A record built from the roster page (source `espn_roster`) has no ESPN comment. A body part that is really a status word
("active", "inactive", "questionable", "out", ...) or "unknown" is ignored and the statement says "body part unknown".

## The injury warmer

A check-in compares Yahoo's injury tags with nfl_mcp's own reports, and a cold crawl used to take minutes (146 s measured for a
13-team roster), so it timed out. The server now keeps those reports fresh in the background (`injury_warmer.py`):

* **At start** it crawls the teams on your Yahoo roster and this week's opponent's (read through the Yahoo provider; needs
  `NFL_MCP_DEFAULT_LEAGUE` and `NFL_MCP_MY_ROSTER`). If the roster cannot be read it warms all 32 teams; with no Yahoo team set it does nothing.
* **Then** every 30 minutes Thursday to Monday and every 2 hours Tuesday and Wednesday (US Eastern; `NFL_MCP_INJURY_WARM_GAME_INTERVAL` /
  `_OFF_INTERVAL`). It is independent of `NFL_MCP_PREFETCH`; `NFL_MCP_INJURY_WARM=0` turns it off.
* **Never two crawls at once.** A refresh is skipped while any injury crawl is running (its own or a tool call's) and tried again a
  minute later. A tool call that arrives while the warmer is refreshing waits for it and reads what it stored.
* **Seconds, not minutes.** ESPN has a league-wide injury list with every report inline (`site.api.espn.com/.../nfl/injuries`), so a
  crawl is one request (about 0.3 s measured for three teams) instead of a list page plus a detail request per report. It shows each
  team's **newest 25 reports only**, so a team at that cap is treated as partial: its reports are stored, but nothing is pruned from
  it, and older reports (mostly long-term IR) come only from an earlier full crawl. `NFL_MCP_INJURY_TEAM_LIST=0` restores the
  per-report crawl, which is complete.
* **Gap-fill from the roster page.** For each team at the cap, the players it leaves out are looked up on ESPN's per-team roster
  page (`.../teams/{id}/roster`, one request per team, which lists every athlete's current injuries): your roster, this week's
  opponent's, and the wire targets of your latest check-in. Only watched players are looked up, never the whole team. A record is
  added only when **no report is stored** for the player, so a stored body part, practice status or report date is never overwritten.
  It is tagged `source=espn_roster`, dated with the fetch time (the page has no report date of its own), body part `unknown`, and
  never pruned as complete. A player the page shows with no injury is settled as healthy; one it does not list, or a page that fails,
  stays unresolved.
  Checked against the full per-report crawl on 2026-10-29 for all 32 teams: every one of the 259 entries the capped list omitted (230 IR, 1
  suspension, 23 Out, 5 Questionable) was on its roster page with the same status and the same date, none showing as healthy.
  (The omitted entries are the oldest ones; anything new enough to matter this week is in the list itself.)
* **Not healthy by default.** A rostered player with no stored report on a capped team whom neither the list nor the roster page
  settled gets the flag `no_espn_report` and the statement "no ESPN report found (list capped)" in the check-in, a warning, and an
  entry in `verification.no_espn_report`. The check-in also runs the roster-page lookup itself for any rostered player no crawl in
  this process has settled yet (a request only when something is unresolved).

The check-in's `injury_refresh` says when the roster's injury reports were last refreshed and for how many teams ("injuries last
refreshed at Thu 2026-10-29 18:12 ET (13 teams)"), lists roster teams with older or no reports, and says whether a crawl is running
now and what the warmer last did.

## Comparing two players: `compare_players`

`compare_players(player_a, player_b, week=None, league_id=None)` is a read-only start/sit tool for two QB/RB/WR/TE. Names are
`Full Name` or `Full Name (TEAM POS)`; a name that fits two players (two Warrens) is an error that lists them. Everything is scored on
**BFS** with the same scorer the rest of the Yahoo tools use, so the 100/200 rushing and receiving and 300/400 passing yardage bonuses
(+2, then +5 in total) are in every game-log number, as are special-teams and fumble-return touchdowns. It works without a Yahoo league;
with `NFL_MCP_DEFAULT_LEAGUE` (a Yahoo league) and `NFL_MCP_MY_ROSTER` set it also reads your roster for Yahoo projections and roster fit.

Per player:

* **Game log**: BFS points for each game before the week (per game, mean, median, min, max, the last three). A week with no stat line is
  not counted (bye, missed game, or nothing recorded).
* **Usage**: by week, offensive snap share (nflverse snap counts, matched to the stats by name and team), share of the team's carries and
  targets, touches, carries inside the 20 and the 10 as a share of the team's, and red-zone targets (play by play). Season and last-3
  summaries and a rising / falling / steady trend. **Teammates** are named when their snap share rose at least 10 points over the last
  four played weeks and they took at least a quarter of the team's carries inside the 10 (goal line) or of its third/fourth-down
  targets (passing downs).
* **Game**: the team's spread (negative = favoured), total and implied team total from the schedule file (not live odds), home or away,
  kickoff in Eastern time, roof and weather where given. `lock` says which player's game starts first, how many hours apart, and what that
  does: the earlier player's choice has to be made before the later game's news exists, and a benched player cannot be swapped in once
  his game has started.
* **Opponent**: BFS points per game the defense allowed to the position this season and last, with rank (1 = allows the fewest, the
  toughest), the same for yards-only and touchdown-only points, the stat splits (rushing yards and touchdowns, receptions, receiving
  yards and touchdowns; passing for a QB), and EPA per rush allowed. `driver` says whether the rank follows yards (stable) or touchdowns
  (noisy); `small_sample` is set under four games.
* **Projections**: nfl_mcp's projection priced on BFS, the model and Sleeper components, and Yahoo's (its own number is in the league's
  scoring and exists only for players on your roster), with the gap against Yahoo; a gap under 1.5 points is noise.
* **Injuries**: both players' records and the opposing defense's injured key starters (at least 60% of the defense's snaps over its last
  three games), each with its report date and the check-in's stale (older than 3 days or before this NFL week), conflicting, Yahoo-only
  and single-source flags. These are stored reports, not refreshed; run the check-in (which refreshes) first for fresh ones.
* **Roster fit**: the slot he fills, the slots he is eligible for and who holds them, bench alternatives for his slot, and roster players
  in the same game: stacks (his quarterback or receiver, his defense), players who share his touches, and hedges (the opponent's players).

The **verdict** uses the **median** of each player's available projection sources (nfl_mcp, model, Sleeper, Yahoo when present), so one
outlier cannot move it, and shows every source's own gap beside it. If the sources disagree on who leads, or one source's gap is
outside the 1.5-point tie band while another's is inside, it says "sources disagree" and names them (in `verdict.projection.disagreement`,
the caveats and the summary). It is `toss-up` whenever the median gap is within the tie band, with `pick: null`, and then lists the
tiebreakers in order:

1. projection (the median);
2. red zone, as two measures: red-zone opportunities per game (carries inside the 20 plus red-zone targets) and inside-10 carry share. If
   they favour different players the result is `split` and no winner is named;
3. floor and ceiling, as the 20th and 80th percentile of the BFS game log (not the lowest and highest game), and "not meaningful (n games)"
   for any player with fewer than 8 games.

Each names the player it favours, or `even` when the difference is negligible. A toss-up is never presented as a pick; outside the band the
verdict is an `edge` with the same tiebreakers beside it. Unconfirmed availability (stale, conflicting or single-source injury data) is
listed as a caveat.

The nflverse files are the weekly player stats, `snap_counts`, `pbp` (play by play) and the `nfldata` schedule; each is fetched once per
season and cached in memory (the current season refreshes), read-only.

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
