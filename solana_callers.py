#!/usr/bin/env python3
"""
Identify influential X (Twitter) callers who posted a Solana token CA
before or during its initial price pump.
"""

import argparse
import asyncio
import csv
import sys
from datetime import datetime, timezone, timedelta

import aiohttp
from twscrape import API as TwscrapeAPI


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


# ── Step 2: Scrape X for early tweets mentioning the CA ─────────────────

async def scrape_tweets(ca: str, window_start: datetime, window_end: datetime) -> list[dict]:
    """
    Search X for tweets containing the CA within the time window.

    twscrape requires pre-added & logged-in accounts stored in its SQLite DB.
    Run `twscrape add_accounts` beforehand — see twscrape docs.
    """
    api = TwscrapeAPI()

    since_str = window_start.strftime("%Y-%m-%d_%H:%M:%S_UTC")
    until_str = window_end.strftime("%Y-%m-%d_%H:%M:%S_UTC")
    query = f"{ca} since:{since_str} until:{until_str}"

    print(f"\nSearching X: {query}")

    tweets: list[dict] = []
    try:
        async for tweet in api.search(query, limit=200):
            tweet_time = tweet.date
            if tweet_time.tzinfo is None:
                tweet_time = tweet_time.replace(tzinfo=timezone.utc)

            tweets.append({
                "username": tweet.user.username,
                "followers": tweet.user.followersCount,
                "tweet_time": tweet_time.isoformat(),
                "tweet_time_dt": tweet_time,
                "likes": tweet.likeCount,
                "retweets": tweet.retweetCount,
                "tweet_url": f"https://x.com/{tweet.user.username}/status/{tweet.id}",
            })
    except Exception as exc:
        print(f"[twscrape] Error during search: {exc}")
        print("Make sure you have added & logged in accounts via `twscrape add_accounts`.")

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
    parser = argparse.ArgumentParser(
        description="Find influential X callers for a Solana token before its pump."
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
    window_start = created_at - timedelta(hours=1)
    window_end = pump_peak + timedelta(minutes=30)
    tweets = await scrape_tweets(ca, window_start, window_end)

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
