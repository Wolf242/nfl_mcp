"""Synthetic Yahoo pages that mirror the real DOM observed on 2026-10-27 (class names,
table ids, column order). They contain invented players/ids only - no league data."""
from __future__ import annotations

BASE = "https://sports.yahoo.com/nfl/players"


def _pcell(name, yid, team_pos, inj=None, kick="Sun 1:00 pm @", opp="TB"):
    injs = f'<span class="Pstart-sm">{inj}</span>' if inj else ""
    return (f'<td class="Alt Ta-start player"><a class="Nowrap name F-link playernote" '
            f'href="{BASE}/{yid}">{name}</a>{injs}<span class="ysf-player-icon">Player Note</span>'
            f'<span class="Fz-xxs">{team_pos}</span><a class="F-reset" href="#">{kick}</a>'
            f'<a class="Inline" href="#">{opp}</a></td>')


def roster_html(rows):
    """rows: (slot, name, yid, team_pos, proj, inj)"""
    body = ""
    for slot, name, yid, tp, proj, inj in rows:
        body += (f'<tr class="Alt"><td class="Alt Ta-c pos">{slot}</td>'
                 f'<td class="Ta-start edit Js-hidden">RBW/R/TBN</td>{_pcell(name, yid, tp, inj)}'
                 f'<td>11</td><td>–</td><td>{proj}</td><td>100%</td><td>98%</td></tr>')
    head = ('<thead><tr><th></th><th></th><th></th><th></th><th colspan="3">Fantasy</th><th>Trends</th></tr>'
            '<tr><th>Pos</th><th>Edit</th><th>Offense</th><th>Bye</th><th>Fan Pts</th>'
            '<th>Proj Pts</th><th>% Start</th><th>% Ros</th></tr></thead>')
    return f'<html><body><table id="statTable0">{head}<tbody>{body}</tbody></table></body></html>'


def standings_html(teams):
    """teams: (rank, name, mid, wlt, pf, pa, waiver, moves)"""
    body = ""
    for rank, name, mid, wlt, pf, pa, waiver, moves in teams:
        body += (f'<tr><td>{rank}</td><td><a href="/f1/1234567/{mid}">{name}</a></td><td>{wlt}</td>'
                 f'<td>{pf}</td><td>{pa}</td><td>W-1</td><td>{waiver}</td><td>{moves}</td></tr>')
    return ('<html><body><table id="standingstable"><thead><tr><th>Rank</th><th>Team</th><th>W-L-T</th>'
            '<th>PF</th><th>PA</th><th>Streak</th><th>Waiver</th><th>Moves</th></tr></thead>'
            f'<tbody>{body}</tbody></table></body></html>')


def players_html(rows):
    """rows: (name, yid, team_pos, avail_text)"""
    body = ""
    for name, yid, tp, avail in rows:
        body += (f'<tr><td></td>{_pcell(name, yid, tp)}<td>{avail}</td><td>1</td><td>14</td></tr>')
    return ('<html><body><table><thead><tr><th></th><th>Offense</th><th>Roster Status</th></tr></thead>'
            f'<tbody>{body}</tbody></table></body></html>')


def matchup_html(left_team, right_team, rows, totals):
    """rows: (pos, left(name,yid,tp,proj), right(name,yid,tp,proj))"""
    def side(p):
        return _pcell(p[0], p[1], p[2])
    body = ""
    for pos, l, r in rows:
        body += (f'<tr><td></td>{side(l)}<td>{l[3]}</td><td>–</td><td>{pos}</td><td>{pos}</td>'
                 f'<td>{pos}</td><td>–</td><td>{r[3]}</td>{side(r)}<td></td></tr>')
    lp, rp = totals
    body += (f'<tr><td></td><td></td><td>{lp}</td><td>0.00</td><td>Total</td><td>TOTAL</td><td>Total</td>'
             f'<td>0.00</td><td>{rp}</td><td></td><td></td></tr>')
    nav = ('<a href="/f1/1234567/9">My Team</a>'
           f'<a href="/f1/1234567/{left_team[0]}">{left_team[1]}</a>'
           f'<a href="/f1/1234567/{right_team[0]}">{right_team[1]}</a>')
    return (f'<html><body>{nav}<table id="statTable1"><thead><tr><th>Stats</th><th>Player</th><th>Proj</th>'
            '<th>Fan Pts</th><th>Pos</th><th>Pos</th><th>Pos</th><th>Fan Pts</th><th>Proj</th>'
            f'<th>Player</th><th>Stats</th></tr></thead><tbody>{body}</tbody></table></body></html>')


def transactions_all_html(rows):
    """rows: (team, mid, when, [(player, yid, team_pos, label)])"""
    body = ""
    for team, mid, when, moves in rows:
        ent = "".join(
            f'<div class="Pbot-xs"><a href="{BASE}/{y}">{n}</a><span class="F-position">{tp}</span>'
            f'<a class="yfa-icon" href="#"></a><h6 class="F-shade">{lab}</h6></div>'
            for n, y, tp, lab in moves)
        body += (f'<tr><td class="Grid-u-1-12"></td><td class="Fill-x">{ent}</td><td class="Ta-end">'
                 f'<a href="/f1/1234567/{mid}">{team}</a><span class="Block">{when}</span></td></tr>')
    return ('<html><body><table class="Table Table-mid No-bdr Tst-transaction-table">'
            f'<tbody>{body}</tbody></table></body></html>')


def transactions_waiver_html(rows):
    """rows: (player, yid, team_pos, win_pr, [(loser, mid, pr)], winner, winner_mid, when)"""
    body = ""
    for player, yid, tp, win_pr, losers, winner, wmid, when in rows:
        ls = " ".join(f'<a href="/f1/1234567/{m}">{n}</a> (Lower waiver priority: {p})'
                      for n, m, p in losers)
        body += (f'<tr><td class="Grid-u-1-12"></td><td class="No-pstart"><a href="{BASE}/{yid}">{player}</a>'
                 f'<span class="F-position">{tp}</span><h6 class="F-shade">Winning waiver priority: {win_pr}</h6>'
                 f'{ls}</td><td class="Fz-xxs">Awarded To:</td><td class="Ta-end">'
                 f'<a href="/f1/1234567/{wmid}">{winner}</a><span class="Block">{when}</span></td></tr>')
    return ('<html><body><table class="Table Table-mid No-bdr Tst-transaction-table">'
            f'<tbody>{body}</tbody></table></body></html>')


SETTINGS_ROWS = [
    "Passing Yards = 25 yards per point; 2 points at 300 yards; 3 points at 400 yards",
    "Passing Touchdowns(Yahoo Default: 4) = 6 = 4",
    "Interceptions(Yahoo Default: -1) = -2 = -1",
    "Rushing Yards = 10 yards per point; 2 points at 100 yards; 3 points at 200 yards",
    "Rushing Touchdowns = 6", "Receptions = 1 = 0.5",
    "Receiving Yards = 10 yards per point; 2 points at 100 yards; 3 points at 200 yards",
    "Receiving Touchdowns = 6", "Return Touchdowns = 6", "2-Point Conversions = 2",
    "Fumbles Lost = -2", "Offensive Fumble Return TD = 6",
    "Field Goals 0-19 Yards = 3", "Field Goals 20-29 Yards = 3", "Field Goals 30-39 Yards = 3",
    "Field Goals 40-49 Yards = 4", "Field Goals Missed 0-19 Yards = -1 = 0",
    "Field Goals Missed 20-29 Yards = -1 = 0", "Field Goals Missed 30-39 Yards = -1 = 0",
    "Point After Attempt Made = 1", "Point After Attempt Missed = -1 = 0",
    "Field Goals 50-59 Yards = 5 = 0", "Field Goals 60+ Yards = 6 = 0",
    "Sack = 1", "Interception = 2", "Fumble Recovery = 2", "Touchdown = 6", "Safety = 2",
    "Block Kick = 2", "Kickoff and Punt Return Touchdowns = 6",
    "Points Allowed 0 points = 10", "Points Allowed 1-6 points = 7",
    "Points Allowed 7-13 points = 4", "Points Allowed 14-20 points = 1",
    "Points Allowed 21-27 points = 0", "Points Allowed 28-34 points = -1",
    "Points Allowed 35+ points = -4", "Extra Point Returned = 2",
]


def settings_html():
    body = "".join("<tr>" + "".join(f"<td>{c.strip()}</td>" for c in r.split(" = ")) + "</tr>"
                   for r in SETTINGS_ROWS)
    return f"<html><body><table>{body}</table></body></html>"
