"""One-time login helper: ``python -m nfl_mcp.yahoo.login``.

Opens a visible Chromium window on the saved profile. YOU sign in to Yahoo there; this
program never types, reads or stores credentials - it only keeps the browser profile
(cookies) in ``YAHOO_PROFILE_DIR`` so the headless reader can reuse the session.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path


async def main() -> None:
    from playwright.async_api import async_playwright
    prof = os.getenv("YAHOO_PROFILE_DIR") or str(Path.home() / ".nfl_mcp" / "yahoo-profile")
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(prof, headless=False)
        page = await ctx.new_page()
        await page.goto("https://login.yahoo.com/")
        print(f"Sign in to Yahoo in the window, then close it. Profile: {prof}")
        print("If Google says 'This browser or app may not be secure', use Yahoo's own email/password "
              "or emailed-code sign-in here, or use the Chrome attach route in docs/YAHOO.md.")
        await ctx.wait_for_event("close", timeout=0)


if __name__ == "__main__":
    asyncio.run(main())
