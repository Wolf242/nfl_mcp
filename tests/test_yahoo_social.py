from datetime import UTC, datetime, timedelta

from nfl_mcp.yahoo import news_social

PAYLOAD = {"feed": [
    {"post": {"uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz", "author": {"handle": "rapsheet1.bsky.social",
              "displayName": "Ian Rapoport"}, "indexedAt": "2026-10-27T15:00:00Z",
              "record": {"text": "Steelers RB Jaylen Warren is expected to play Sunday.",
                         "createdAt": "2026-10-27T15:00:00Z"},
              "embed": {"external": {"uri": "https://example.com/story"}}}},
    {"post": {"uri": "at://did:plc:abc/app.bsky.feed.post/3old", "author": {"handle": "rapsheet1.bsky.social"},
              "record": {"text": "Old news", "createdAt": "2026-10-20T15:00:00Z"}}},
    {"reason": {"$type": "app.bsky.feed.defs#reasonRepost"},
     "post": {"uri": "at://x/app.bsky.feed.post/3rp", "author": {"handle": "someone.bsky.social"},
              "record": {"text": "reposted thing", "createdAt": "2026-10-27T16:00:00Z"}}},
]}


def test_feed_parsing_drops_reposts_and_stale_posts_and_marks_leads():
    since = datetime(2026, 10, 27, tzinfo=UTC) - timedelta(hours=24)
    items = news_social.parse_feed(PAYLOAD, "rapsheet1.bsky.social", since=since, watch=["Jaylen Warren"])
    assert len(items) == 1
    it = items[0]
    assert it["url"] == "https://bsky.app/profile/rapsheet1.bsky.social/post/3kxyz"
    assert it["link"] == "https://example.com/story"
    assert it["mentions_watched_player"] == ["Jaylen Warren"]
    assert it["status"] == "lead" and it["single_source"] is True


def test_handles_come_from_env(monkeypatch):
    monkeypatch.setenv("NFL_MCP_BLUESKY_HANDLES", "@a.bsky.social, b.bsky.social")
    assert news_social.configured_handles() == ["a.bsky.social", "b.bsky.social"]
