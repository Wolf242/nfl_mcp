"""Breaking-news leads from Bluesky (public AT Protocol AppView, no login).

Why Bluesky and not X: X has no free read API, and several NFL reporters moved to Bluesky
or Threads. Reporters on this list are **leads, never sources of record**: every item is
tagged ``single_source`` and ``status: "lead"``. Per the kernel's verify-before-you-state
rule, an injury or role claim needs a second independent source before it moves a call.

Handles are configuration, not code: set ``NFL_MCP_BLUESKY_HANDLES`` (comma separated).
``DEFAULT_HANDLES`` were confirmed to resolve on 2026-10-27 (``verify_handles``): Ian Rapoport,
and "NFL News Poster", a manually maintained account that reposts reporters from Twitter - so
treat its posts as second-hand. Re-run ``verify_handles`` if a handle stops returning posts.
"""
from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta

from ..config import create_http_client

API = "https://public.api.bsky.app/xrpc"

DEFAULT_HANDLES = ["rapsheet.bsky.social", "nflnewsposter.bsky.social"]


def configured_handles() -> list[str]:
    raw = os.getenv("NFL_MCP_BLUESKY_HANDLES")
    if raw:
        return [h.strip().lstrip("@") for h in raw.split(",") if h.strip()]
    return list(DEFAULT_HANDLES)


def post_url(handle: str, uri: str) -> str:
    rkey = uri.rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def parse_feed(payload: dict, handle: str, since: datetime | None = None,
               watch: list[str] | None = None) -> list[dict]:
    """``getAuthorFeed`` JSON -> news leads (original posts only, newest first)."""
    watch_n = [(w, re.sub(r"[^a-z]", "", w.lower())) for w in (watch or [])]
    out = []
    for item in payload.get("feed", []):
        if item.get("reason"):  # a repost, not the reporter's own words
            continue
        post = item.get("post", {})
        rec = post.get("record", {})
        text = rec.get("text", "")
        created = rec.get("createdAt") or post.get("indexedAt")
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None
        except ValueError:
            ts = None
        if since and ts and ts < since:
            continue
        flat = re.sub(r"[^a-z]", "", text.lower())
        mentions = [w for w, n in watch_n if n and n in flat]
        ext = (post.get("embed") or {}).get("external") or {}
        out.append({
            "author": post.get("author", {}).get("handle", handle),
            "display_name": post.get("author", {}).get("displayName"),
            "text": text, "posted_at": created,
            "url": post_url(post.get("author", {}).get("handle", handle), post.get("uri", "")),
            "link": ext.get("uri"), "mentions_watched_player": mentions,
            "status": "lead", "single_source": True,
        })
    return out


async def verify_handles(handles: list[str] | None = None) -> dict:
    """Resolve handles to profiles so a human can confirm each one is who we think it is."""
    handles = handles or configured_handles()
    out = []
    async with create_http_client() as client:
        for h in handles:
            r = await client.get(f"{API}/app.bsky.actor.getProfile", params={"actor": h})
            if r.status_code != 200:
                out.append({"handle": h, "resolves": False, "http_status": r.status_code})
                continue
            p = r.json()
            out.append({"handle": h, "resolves": True, "display_name": p.get("displayName"),
                        "description": (p.get("description") or "")[:200],
                        "followers": p.get("followersCount"), "posts": p.get("postsCount"),
                        "did": p.get("did")})
    return {"profiles": out,
            "note": "Confirm each display name / bio is the reporter you expect before trusting posts."}


async def get_insider_posts(handles: list[str] | None = None, hours: int = 24, limit: int = 15,
                            watch_names: list[str] | None = None) -> dict:
    """Latest original posts from the configured reporters, newest first."""
    handles = handles or configured_handles()
    since = datetime.now(UTC) - timedelta(hours=hours)
    items, errors = [], []
    async with create_http_client() as client:
        for h in handles:
            r = await client.get(f"{API}/app.bsky.feed.getAuthorFeed",
                                 params={"actor": h, "limit": min(max(limit, 1), 50),
                                         "filter": "posts_no_replies"})
            if r.status_code != 200:
                errors.append({"handle": h, "http_status": r.status_code})
                continue
            items.extend(parse_feed(r.json(), h, since=since, watch=watch_names))
    items.sort(key=lambda i: i.get("posted_at") or "", reverse=True)
    return {"items": items[:limit], "count": min(len(items), limit), "errors": errors,
            "caveat": "Leads only: single-source, unverified. Confirm body part, status and date "
                      "in a second independent source before acting."}
