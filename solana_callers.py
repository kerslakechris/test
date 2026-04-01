#!/usr/bin/env python3
"""
Identify influential X (Twitter) callers who posted a Solana token CA
before or during its initial price pump.
"""

import argparse
import asyncio
import csv
import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv()

COOKIES_FILE = "cookies.json"

BROWSER_COOKIE_HELP = """\
To export cookies from your browser:
  1. Log in to x.com in your browser.
  2. Open DevTools (F12) → Application → Cookies → https://x.com
  3. Copy the values of these cookies: auth_token, ct0
  4. (Optional but recommended) Also copy: kdt, twid, guest_id

Then run:
  python solana_callers.py setup-cookies
"""

# ── Twitter GraphQL search (direct, no twikit) ──────────────────────────

TWITTER_SEARCH_URL = "https://x.com/i/api/graphql/flaR-PUMshxFWZWPNpq4zA/SearchTimeline"

SEARCH_FEATURES = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "communities_web_enable_tweet_community_results_featuring": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


def load_cookies() -> dict:
    """Load cookies from cookies.json."""
    if not Path(COOKIES_FILE).exists():
        sys.exit(
            f"No {COOKIES_FILE} found.\n"
            "Run: python solana_callers.py setup-cookies\n\n"
            + BROWSER_COOKIE_HELP
        )
    with open(COOKIES_FILE) as f:
        cookies = json.load(f)
    if not cookies.get("auth_token") or not cookies.get("ct0"):
        sys.exit("cookies.json must contain 'auth_token' and 'ct0'.")
    return cookies


def build_twitter_headers(cookies: dict) -> dict:
    """Build headers for Twitter's GraphQL API."""
    return {
        "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
        "x-csrf-token": cookies["ct0"],
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "content-type": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://x.com/search",
    }


def build_cookie_header(cookies: dict) -> str:
    """Format cookies dict as a Cookie header string."""
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


async def twitter_search(
    session: aiohttp.ClientSession,
    query: str,
    cookies: dict,
    max_tweets: int = 200,
) -> list[dict]:
    """Search Twitter via GraphQL API and return parsed tweet dicts."""
    headers = build_twitter_headers(cookies)
    headers["Cookie"] = build_cookie_header(cookies)

    tweets: list[dict] = []
    cursor = None

    for page in range(10):  # max 10 pages
        variables = {
            "rawQuery": query,
            "count": 20,
            "querySource": "typed_query",
            "product": "Latest",
        }
        if cursor:
            variables["cursor"] = cursor

        params = {
            "variables": json.dumps(variables),
            "features": json.dumps(SEARCH_FEATURES),
        }

        url = f"{TWITTER_SEARCH_URL}?{urllib.parse.urlencode(params)}"

        for attempt in range(4):
            try:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 429:
                        wait = 2 ** (attempt + 1)
                        print(f"[X API] Rate-limited, retrying in {wait}s...")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status == 401:
                        sys.exit(
                            "[X API] 401 Unauthorized — cookies are expired.\n"
                            "Re-run: python solana_callers.py setup-cookies"
                        )
                    resp.raise_for_status()
                    data = await resp.json()
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                wait = 2 ** (attempt + 1)
                print(f"[X API] Request error ({exc}), retrying in {wait}s...")
                await asyncio.sleep(wait)
        else:
            print("[X API] Failed after retries, stopping pagination.")
            break

        # Parse response
        new_tweets, cursor = _parse_search_response(data)
        tweets.extend(new_tweets)

        if not cursor or len(tweets) >= max_tweets:
            break

        await asyncio.sleep(1)  # rate-limit pause between pages

    return tweets[:max_tweets]


def _parse_search_response(data: dict) -> tuple[list[dict], str | None]:
    """Extract tweets and next cursor from GraphQL search response."""
    tweets = []
    next_cursor = None

    try:
        instructions = (
            data.get("data", {})
            .get("search_by_raw_query", {})
            .get("search_timeline", {})
            .get("timeline", {})
            .get("instructions", [])
        )
    except (AttributeError, TypeError):
        return tweets, None

    for instruction in instructions:
        entries = instruction.get("entries", [])
        for entry in entries:
            # Cursor entries
            if entry.get("entryId", "").startswith("cursor-bottom"):
                next_cursor = (
                    entry.get("content", {})
                    .get("value")
                    or entry.get("content", {})
                    .get("itemContent", {})
                    .get("value")
                )
                continue

            # Tweet entries
            result = (
                entry.get("content", {})
                .get("itemContent", {})
                .get("tweet_results", {})
                .get("result", {})
            )
            if not result:
                continue

            # Handle tweets wrapped in "tweet" key (tombstoned/limited tweets)
            if "tweet" in result:
                result = result["tweet"]

            core = result.get("core", {}).get("user_results", {}).get("result", {})
            legacy_user = core.get("legacy", {})
            legacy_tweet = result.get("legacy", {})

            if not legacy_tweet or not legacy_user:
                continue

            screen_name = legacy_user.get("screen_name", "")
            tweet_id = legacy_tweet.get("id_str", result.get("rest_id", ""))

            # Parse timestamp
            created_str = legacy_tweet.get("created_at", "")
            try:
                tweet_time = datetime.strptime(created_str, "%a %b %d %H:%M:%S %z %Y")
            except (ValueError, TypeError):
                continue

            tweets.append({
                "username": screen_name,
                "followers": legacy_user.get("followers_count", 0),
                "tweet_time": tweet_time.isoformat(),
                "tweet_time_dt": tweet_time,
                "likes": legacy_tweet.get("favorite_count", 0),
                "retweets": legacy_tweet.get("retweet_count", 0),
                "tweet_url": f"https://x.com/{screen_name}/status/{tweet_id}",
            })

    return tweets, next_cursor


# ── Step 1: Get pump timestamp from DexScreener ─────────────────────────

async def fetch_dexscreener_data(session: aiohttp.ClientSession, ca: str) -> dict:
    """Fetch token pair data from DexScreener and identify the pump timestamp."""
    url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
    for attempt in range(4):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    wait = 2 ** (attempt + 1)
                    print(f"[DexScreener] Rate-limited, retrying in {wait}s...")
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            wait = 2 ** (attempt + 1)
            print(f"[DexScreener] Request error ({exc}), retrying in {wait}s...")
            await asyncio.sleep(wait)
    sys.exit("Failed to fetch DexScreener data after retries.")


def parse_pump_timestamp(data: dict) -> tuple[datetime, datetime]:
    """
    Return (pair_created_at, estimated_pump_peak) from DexScreener response.

    Heuristic: the first major volume spike typically happens within the first
    few minutes after pair creation on Solana meme-token launches.  We use
    pair creation time as the pump start and add 15 minutes as an estimate for
    peak volume (conservative default).
    """
    pairs = data.get("pairs") or []
    if not pairs:
        sys.exit("No pairs found on DexScreener for this CA.")

    # Pick the pair with the highest volume (most relevant)
    pair = max(pairs, key=lambda p: float(p.get("volume", {}).get("h24", 0) or 0))

    created_ms = pair.get("pairCreatedAt")
    if created_ms is None:
        sys.exit("Pair creation time not available in DexScreener response.")

    created_at = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)

    # Estimate pump peak: 15 min after creation (adjustable)
    pump_peak = created_at + timedelta(minutes=15)

    print(f"Pair:          {pair.get('baseToken', {}).get('symbol', '?')}/{pair.get('quoteToken', {}).get('symbol', '?')}")
    print(f"DEX:           {pair.get('dexId', '?')}")
    print(f"Created at:    {created_at.isoformat()}")
    print(f"Est. peak:     {pump_peak.isoformat()}")
    return created_at, pump_peak


# ── Step 2: Scrape X ────────────────────────────────────────────────────

def setup_cookies_interactive() -> None:
    """
    Import cookies from the browser and write cookies.json.

    Prompts for auth_token and ct0 (required), plus optional cookies.
    """
    print("=" * 60)
    print(" Import X (Twitter) cookies from your browser")
    print("=" * 60)
    print()
    print(BROWSER_COOKIE_HELP)

    auth_token = input("auth_token (required): ").strip()
    ct0 = input("ct0 (required):        ").strip()

    if not auth_token or not ct0:
        sys.exit("auth_token and ct0 are both required.")

    cookies = {
        "auth_token": auth_token,
        "ct0": ct0,
    }

    # Optional cookies
    for name in ("kdt", "twid", "guest_id"):
        val = input(f"{name} (optional, enter to skip): ").strip()
        if val:
            cookies[name] = val

    with open(COOKIES_FILE, "w") as f:
        json.dump(cookies, f, indent=2)

    print(f"\nCookies saved to {COOKIES_FILE}")
    print("You can now run: python solana_callers.py <CA>")


async def scrape_tweets(
    session: aiohttp.ClientSession,
    cookies: dict,
    ca: str,
    window_start: datetime,
    window_end: datetime,
) -> list[dict]:
    """Search X for tweets containing the CA within the time window."""
    since_str = window_start.strftime("%Y-%m-%d")
    until_str = (window_end + timedelta(days=1)).strftime("%Y-%m-%d")
    query = f"{ca} since:{since_str} until:{until_str}"

    print(f"\nSearching X: {query}")

    tweets = await twitter_search(session, query, cookies)

    # Filter to exact time window
    filtered = [
        t for t in tweets
        if window_start <= t["tweet_time_dt"] <= window_end
    ]

    print(f"Collected {len(filtered)} tweets in window.")
    return filtered


# ── Step 3: Score callers ────────────────────────────────────────────────

def score_and_rank(tweets: list[dict], peak: datetime) -> list[dict]:
    """
    Score = (followers * 0.4) + (likes * 0.4) + (retweets * 0.2)
    Only tweets posted *before* the peak volume time are included.
    """
    pre_peak = [t for t in tweets if t["tweet_time_dt"] <= peak]

    for t in pre_peak:
        t["score"] = round(
            (t["followers"] * 0.4) + (t["likes"] * 0.4) + (t["retweets"] * 0.2), 2
        )

    pre_peak.sort(key=lambda t: t["score"], reverse=True)
    return pre_peak


# ── Step 4: Output ───────────────────────────────────────────────────────

CSV_COLUMNS = ["username", "followers", "tweet_time", "likes", "retweets", "score", "tweet_url"]


def save_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nResults saved to {path}")


def print_top(rows: list[dict], n: int = 5) -> None:
    print(f"\n{'=' * 80}")
    print(f" Top {n} Influential Callers")
    print(f"{'=' * 80}")
    for i, r in enumerate(rows[:n], 1):
        print(
            f" {i}. @{r['username']:<20} "
            f"followers={r['followers']:<10} "
            f"likes={r['likes']:<6} "
            f"RTs={r['retweets']:<6} "
            f"score={r['score']:<12} "
            f"{r['tweet_url']}"
        )
    if not rows:
        print(" No qualifying tweets found.")
    print()


# ── Main ─────────────────────────────────────────────────────────────────

async def main() -> None:
    # Handle setup-cookies before argparse so it doesn't conflict with positional CA
    if len(sys.argv) >= 2 and sys.argv[1] == "setup-cookies":
        setup_cookies_interactive()
        return

    parser = argparse.ArgumentParser(
        description="Find influential X callers for a Solana token before its pump.",
        epilog="Run 'python solana_callers.py setup-cookies' to import browser cookies.",
    )
    parser.add_argument("ca", help="Solana token contract address")
    parser.add_argument("-o", "--output", default="callers.csv", help="Output CSV path (default: callers.csv)")
    parser.add_argument("--top", type=int, default=5, help="Number of top callers to print (default: 5)")
    args = parser.parse_args()

    ca: str = args.ca
    cookies = load_cookies()
    print("[X] Loaded cookies.")

    async with aiohttp.ClientSession() as session:
        # Step 1 — DexScreener
        dex_data = await fetch_dexscreener_data(session, ca)
        created_at, pump_peak = parse_pump_timestamp(dex_data)

        # Step 2 — Scrape X
        window_start = created_at - timedelta(hours=1)
        window_end = pump_peak + timedelta(minutes=30)
        tweets = await scrape_tweets(session, cookies, ca, window_start, window_end)

    if not tweets:
        print("No tweets found. Exiting.")
        return

    # Step 3 — Score
    ranked = score_and_rank(tweets, pump_peak)

    # Step 4 — Output
    save_csv(ranked, args.output)
    print_top(ranked, n=args.top)


if __name__ == "__main__":
    asyncio.run(main())
