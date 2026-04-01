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
import re
import sys
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


# ── Patch twikit's broken regex before importing ─────────────────────────
# Twitter changed ondemand.s.js structure in March 2026, breaking twikit's
# ClientTransaction.get_indices.  We monkey-patch the regex constants and
# method so twikit can generate X-Client-Transaction-Id headers again.
# See: https://github.com/d60/twikit/issues/408 / PR #410

def _patch_twikit_transaction():
    """Apply regex fix from twikit PR #410 to installed twikit."""
    try:
        import twikit.x_client_transaction.transaction as txn
    except ImportError:
        return  # twikit not installed

    # New regex: captures the numeric index for the ondemand chunk
    txn.ON_DEMAND_FILE_REGEX = re.compile(
        r""",([\d]+):['"]ondemand\.s['"]"""
    )

    # Pattern to find the hash for a given chunk index
    ON_DEMAND_HASH_PATTERN = r',{}:"([0-9a-f]+)"'

    # Simpler indices regex matching the new JS structure
    txn.INDICES_REGEX = re.compile(r"\[(\d+)\],\s*16")

    # Replace get_indices with fixed version
    original_validate = txn.ClientTransaction.validate_response

    async def patched_get_indices(self, home_page_response, session, headers):
        key_byte_indices = []
        response = original_validate(home_page_response) or self.home_page_response
        response_str = str(response)

        on_demand_match = txn.ON_DEMAND_FILE_REGEX.search(response_str)
        if on_demand_match:
            chunk_index = on_demand_match.group(1)
            # Find the hash for this chunk
            hash_pattern = re.compile(ON_DEMAND_HASH_PATTERN.format(chunk_index))
            hash_match = hash_pattern.search(response_str)
            if hash_match:
                file_hash = hash_match.group(1)
                url = f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{file_hash}a.js"
                on_demand_response = await session.request(
                    method="GET", url=url, headers=headers
                )
                indices_matches = txn.INDICES_REGEX.finditer(str(on_demand_response.text))
                for item in indices_matches:
                    key_byte_indices.append(item.group(1))

        if not key_byte_indices:
            raise Exception("Couldn't get KEY_BYTE indices")

        key_byte_indices = list(map(int, key_byte_indices))
        return key_byte_indices[0], key_byte_indices[1:]

    txn.ClientTransaction.get_indices = patched_get_indices


_patch_twikit_transaction()

from twikit import Client  # noqa: E402 — must import after patch


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

    pair = max(pairs, key=lambda p: float(p.get("volume", {}).get("h24", 0) or 0))

    created_ms = pair.get("pairCreatedAt")
    if created_ms is None:
        sys.exit("Pair creation time not available in DexScreener response.")

    created_at = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
    pump_peak = created_at + timedelta(minutes=15)

    print(f"Pair:          {pair.get('baseToken', {}).get('symbol', '?')}/{pair.get('quoteToken', {}).get('symbol', '?')}")
    print(f"DEX:           {pair.get('dexId', '?')}")
    print(f"Created at:    {created_at.isoformat()}")
    print(f"Est. peak:     {pump_peak.isoformat()}")
    return created_at, pump_peak


# ── Step 2: Scrape X for early tweets mentioning the CA ─────────────────

async def get_twikit_client() -> Client:
    """
    Authenticate with X via twikit.

    Tries cookies.json first, then env-var login, then directs to setup-cookies.
    """
    client = Client(language="en-US")

    if Path(COOKIES_FILE).exists():
        client.load_cookies(COOKIES_FILE)
        print("[twikit] Loaded saved cookies.")
        return client

    username = os.environ.get("X_USERNAME")
    email = os.environ.get("X_EMAIL")
    password = os.environ.get("X_PASSWORD")
    if not all([username, email, password]):
        sys.exit(
            "No cookies.json found and X credentials not set.\n"
            "Either:\n"
            "  1. Set env vars X_USERNAME, X_EMAIL, X_PASSWORD (in .env) and re-run\n"
            "  2. Run: python solana_callers.py setup-cookies  (import browser cookies)\n"
        )

    print("[twikit] Logging in...")
    try:
        await client.login(
            auth_info_1=username,
            auth_info_2=email,
            password=password,
        )
    except Exception as exc:
        sys.exit(
            f"[twikit] Login failed: {exc}\n\n"
            "Import browser cookies instead:\n"
            "  python solana_callers.py setup-cookies\n\n"
            + BROWSER_COOKIE_HELP
        )
    client.save_cookies(COOKIES_FILE)
    print("[twikit] Login successful, cookies saved.")
    return client


def setup_cookies_interactive() -> None:
    """Import cookies from the browser and write cookies.json."""
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

    for name in ("kdt", "twid", "guest_id"):
        val = input(f"{name} (optional, enter to skip): ").strip()
        if val:
            cookies[name] = val

    with open(COOKIES_FILE, "w") as f:
        json.dump(cookies, f, indent=2)

    print(f"\nCookies saved to {COOKIES_FILE}")
    print("You can now run: python solana_callers.py <CA>")


def _parse_tweet_time(raw: str) -> datetime:
    """Parse the timestamp string returned by twikit."""
    try:
        dt = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
    except (ValueError, TypeError):
        dt = datetime.now(tz=timezone.utc)
    return dt


async def scrape_tweets(
    client: Client,
    ca: str,
    window_start: datetime,
    window_end: datetime,
    max_tweets: int = 200,
) -> list[dict]:
    """Search X for tweets containing the CA within the time window."""
    since_str = window_start.strftime("%Y-%m-%d")
    until_str = (window_end + timedelta(days=1)).strftime("%Y-%m-%d")
    query = f"{ca} since:{since_str} until:{until_str}"

    print(f"\nSearching X: {query}")

    tweets: list[dict] = []
    try:
        results = await client.search_tweet(query, product="Latest", count=20)

        while results and len(tweets) < max_tweets:
            for tweet in results:
                tweet_time = _parse_tweet_time(tweet.created_at)

                if tweet_time < window_start or tweet_time > window_end:
                    continue

                tweets.append({
                    "username": tweet.user.screen_name,
                    "followers": tweet.user.followers_count,
                    "tweet_time": tweet_time.isoformat(),
                    "tweet_time_dt": tweet_time,
                    "likes": tweet.favorite_count,
                    "retweets": tweet.retweet_count,
                    "tweet_url": f"https://x.com/{tweet.user.screen_name}/status/{tweet.id}",
                })

            if len(tweets) >= max_tweets:
                break
            try:
                await asyncio.sleep(1)
                results = await results.next()
            except Exception:
                break

    except Exception as exc:
        print(f"[twikit] Error during search: {exc}")

    print(f"Collected {len(tweets)} tweets in window.")
    return tweets


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

    # Step 1 — DexScreener
    async with aiohttp.ClientSession() as session:
        dex_data = await fetch_dexscreener_data(session, ca)

    created_at, pump_peak = parse_pump_timestamp(dex_data)

    # Step 2 — Scrape X
    client = await get_twikit_client()
    window_start = created_at - timedelta(hours=1)
    window_end = pump_peak + timedelta(minutes=30)
    tweets = await scrape_tweets(client, ca, window_start, window_end)

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
