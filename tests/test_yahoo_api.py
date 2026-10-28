"""API route: JSON normaliser, parsers on a synthetic payload, token handling, read-only guard."""
import json
import sys

import httpx
import pytest

from nfl_mcp.yahoo import api, guard

ROSTER = {"fantasy_content": {"team": [[{"team_key": "nfl.l.1234567.t.9"}, {"name": "Team Nine"}],
    {"roster": {"0": {"players": {"0": {"player": [[{"player_key": "x"}, {"player_id": "40055"},
        {"name": {"full": "Bijan Robinson"}}, {"editorial_team_abbr": "Atl"},
        {"display_position": "RB"}], {"selected_position": [{"coverage_type": "week"}, {"position": "RB"}]}]},
        "1": {"player": [[{"player_id": "41001"}, {"name": {"full": "Matthew Golden"}},
        {"editorial_team_abbr": "GB"}, {"display_position": "WR"}, {"status": "Q"}],
        {"selected_position": [{"coverage_type": "week"}, {"position": "BN"}]}]}, "count": 2}}}}]}}

STANDINGS = {"fantasy_content": {"league": [{"league_key": "nfl.l.1234567"}, {"standings": [{"teams": {
    "0": {"team": [[{"team_id": "9"}, {"name": "Team Nine"}, {"waiver_priority": "6"},
                    {"number_of_moves": "26"}],
                   {"team_standings": {"rank": "1", "points_for": "949.28", "points_against": "706.82",
                                       "outcome_totals": {"wins": "6", "losses": "1", "ties": "0"}}}]},
    "count": 1}}]}]}}


def test_normaliser_flattens_numeric_keys_and_fragment_lists():
    t = api.norm(ROSTER)
    assert isinstance(api._find(t, "players"), list)


def test_roster_and_standings_map_to_the_html_parser_shape():
    slots = api.roster_slots(api.norm(ROSTER))
    assert [(s["slot"], s["name"], s["yahoo_id"], s["team"], s["injury"]) for s in slots] == [
        ("RB", "Bijan Robinson", "40055", "ATL", None), ("BN", "Matthew Golden", "41001", "GB", "Q")]
    t = api.standings_teams(api.norm(STANDINGS))[0]
    assert (t["mid"], t["name"], t["wins"], t["waiver"], t["moves"], t["points_for"]) == (
        "9", "Team Nine", 6, 6, 26, 949.28)


def test_authorize_url_requests_read_only_scope():
    u = api.authorize_url("CID")
    assert "scope=fspt-r" in u and "client_id=CID" in u


@pytest.mark.asyncio
async def test_get_sends_bearer_token_and_only_reads(tmp_path):
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        if req.url.host == "api.login.yahoo.com":
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})
        return httpx.Response(200, json=json.loads(json.dumps(STANDINGS)))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c = api.ApiClient("1234567", "cid", "sec", api.TokenStore(str(tmp_path / "tok.json")), http)
    await c.exchange_code("CODE")
    teams = await api.ApiAdapter(c).standings()
    assert teams[0]["name"] == "Team Nine"
    data_calls = [r for r in seen if r.url.host == "fantasysports.yahooapis.com"]
    assert all(r.method == "GET" for r in data_calls) and data_calls[0].headers["authorization"] == "Bearer AT"
    if sys.platform != "win32":  # NTFS has no POSIX file modes
        assert (tmp_path / "tok.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_api_client_refuses_non_read_paths(tmp_path):
    c = api.ApiClient("1234567", "cid", "sec", api.TokenStore(str(tmp_path / "t.json")),
                      httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))))
    c.store.save({"access_token": "AT", "refresh_token": "RT", "expires_at": 9e12})
    with pytest.raises(guard.WriteBlocked):
        await c.get("admin/league")
