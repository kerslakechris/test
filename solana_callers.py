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
    async def patched_get_indices(self, home_page_response, session, headers):
        key_byte_indices = []
        response = self.validate_response(home_page_response) or self.home_page_response
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


def _patch_twikit_gql():
    """Patch twikit's GraphQL search endpoint to use a current query ID.

    X rotates these IDs; we try to fetch the latest from twikit's repo,
    falling back to a list of known IDs.
    """
    try:
        import twikit.client.gql as gql
    except ImportError:
        return

    KNOWN_SEARCH_QIDS = [
        "flaR-PUMshxFWZWPNpq4zA",
        "4fpceYZ6-YQCx_JSl_Cn_A",
        "nK1dw4oV3k4w5TdtcAdSww",
    ]

    # Allow env-var override
    env_qid = os.environ.get("TWITTER_SEARCH_QID")
    if env_qid:
        KNOWN_SEARCH_QIDS.insert(0, env_qid)

    # Check what twikit currently has
    current = getattr(gql, "SEARCH_TIMELINE", "")
    current_qid = current.rsplit("/graphql/", 1)[-1].split("/")[0] if "/graphql/" in str(current) else ""

    if current_qid and current_qid not in KNOWN_SEARCH_QIDS:
        KNOWN_SEARCH_QIDS.insert(0, current_qid)

    # Store the list so we can try them at search time
    gql._SEARCH_QIDS = KNOWN_SEARCH_QIDS

    # Wrap the search_timeline method to retry with different QIDs on 404
    original_search = gql.GQLClient.search_timeline

    async def patched_search_timeline(self, query, product, count, cursor):
        last_exc = None
        for qid in gql._SEARCH_QIDS:
            gql.SEARCH_TIMELINE = f"https://x.com/i/api/graphql/{qid}/SearchTimeline"
            try:
                result = await original_search(self, query, product, count, cursor)
                return result
            except Exception as exc:
                exc_str = str(exc)
                # 404 means wrong QID, try next
                if "404" in exc_str:
                    print(f"[twikit] Search QID {qid} returned 404, trying next...")
                    last_exc = exc
                    continue
                raise  # non-404 error, propagate immediately
        raise last_exc or Exception("All search query IDs failed")

    gql.GQLClient.search_timeline = patched_search_timeline


_patch_twikit_transaction()
_patch_twikit_gql()

from twikit import Client  # noqa: E402 — must import after patch


# ── Step 1: Get pump timestamp from DexScreener ─────────────────────────

async def fetch_dexscreener_data(session: aiohttp.ClientSession, ca: str) -> dict | None:
    """Fetch token pair data from DexScreener. Returns None if token not found."""
    urls = [
        f"https://api.dexscreener.com/latest/dex/tokens/{ca}",
        f"https://api.dexscreener.com/token-pairs/v1/solana/{ca}",
    ]
    for url in urls:
        for attempt in range(4):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        wait = 2 ** (attempt + 1)
                        print(f"[DexScreener] Rate-limited, retrying in {wait}s...")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status == 404:
                        break  # try next URL
                    resp.raise_for_status()
                    data = await resp.json()
                    pairs = data.get("pairs") or data if isinstance(data, list) else []
                    if isinstance(data, dict):
                        pairs = data.get("pairs") or []
                    if pairs:
                        return {"pairs": pairs if isinstance(pairs, list) else []}
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                wait = 2 ** (attempt + 1)
                print(f"[DexScreener] Request error ({exc}), retrying in {wait}s...")
                await asyncio.sleep(wait)
    return None


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
        return None, None

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
    # Don't use since:/until: in the query — they're unreliable for older tweets.
    # We filter by timestamp after collecting results instead.
    query = ca

    print(f"\nSearching X: {query}")
    print(f"Time window: {window_start.isoformat()} → {window_end.isoformat()}")

    all_tweets: list[dict] = []
    seen_ids = set()

    # Try both Latest and Top products — Latest gives recency, Top gives popular
    for product in ("Latest", "Top"):
        print(f"\n[twikit] Searching ({product})...")
        try:
            results = await client.search_tweet(query, product=product, count=20)

            page = 0
            while results and len(all_tweets) < max_tweets and page < 10:
                for tweet in results:
                    if tweet.id in seen_ids:
                        continue
                    seen_ids.add(tweet.id)

                    tweet_time = _parse_tweet_time(tweet.created_at)
                    all_tweets.append({
                        "username": tweet.user.screen_name,
                        "followers": tweet.user.followers_count,
                        "tweet_time": tweet_time.isoformat(),
                        "tweet_time_dt": tweet_time,
                        "likes": tweet.favorite_count,
                        "retweets": tweet.retweet_count,
                        "tweet_url": f"https://x.com/{tweet.user.screen_name}/status/{tweet.id}",
                    })

                page += 1
                if len(all_tweets) >= max_tweets:
                    break
                try:
                    await asyncio.sleep(1)
                    results = await results.next()
                except Exception:
                    break

        except Exception as exc:
            print(f"[twikit] Error during {product} search: {exc}")

    print(f"\n[twikit] Total raw tweets collected: {len(all_tweets)}")

    if all_tweets:
        # Show date range of returned tweets so user can see if X is returning anything relevant
        timestamps = sorted(t["tweet_time_dt"] for t in all_tweets)
        print(f"[twikit] Tweet date range: {timestamps[0].isoformat()} → {timestamps[-1].isoformat()}")

    filtered = [
        t for t in all_tweets
        if window_start <= t["tweet_time_dt"] <= window_end
    ]
    print(f"[twikit] Tweets in target window: {len(filtered)}")

    if all_tweets and not filtered:
        print(
            "\n[!] X returned tweets, but none fall inside the pump window.\n"
            "    This usually means X's search index doesn't cover the pump\n"
            "    timeframe (it tends to drop older tweets).\n"
            "    Saving all returned tweets to CSV anyway for inspection."
        )
        return all_tweets

    return filtered


# ── Step 3: Score callers ────────────────────────────────────────────────

def score_and_rank(tweets: list[dict], peak: datetime) -> list[dict]:
    """
    Score = (followers * 0.4) + (likes * 0.4) + (retweets * 0.2)
    Only tweets posted *before* the peak volume time are included.
    Falls back to all tweets if none are pre-peak (e.g. X dropped older results).
    """
    pre_peak = [t for t in tweets if t["tweet_time_dt"] <= peak]
    scored = pre_peak if pre_peak else tweets

    if not pre_peak and tweets:
        print("[!] No pre-peak tweets found; ranking all returned tweets instead.")

    for t in scored:
        t["score"] = round(
            (t["followers"] * 0.4) + (t["likes"] * 0.4) + (t["retweets"] * 0.2), 2
        )

    scored.sort(key=lambda t: t["score"], reverse=True)
    return scored


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
    parser.add_argument(
        "--pump-time",
        help="Manual pump timestamp (ISO format, e.g. 2026-03-23T01:39:00Z). "
             "Use when DexScreener no longer has the token.",
    )
    args = parser.parse_args()

    ca: str = args.ca
    created_at = None
    pump_peak = None

    # Step 1 — Get pump timestamp
    if args.pump_time:
        # Manual override
        try:
            created_at = datetime.fromisoformat(args.pump_time.replace("Z", "+00:00"))
        except ValueError:
            parser.error(f"Invalid --pump-time format: {args.pump_time}")
        pump_peak = created_at + timedelta(minutes=15)
        print(f"Using manual pump time: {created_at.isoformat()}")
        print(f"Est. peak:             {pump_peak.isoformat()}")
    else:
        async with aiohttp.ClientSession() as session:
            dex_data = await fetch_dexscreener_data(session, ca)

        if dex_data:
            created_at, pump_peak = parse_pump_timestamp(dex_data)

        if created_at is None:
            print(
                "\n[!] Token not found on DexScreener (may have been delisted).\n"
                "    Re-run with --pump-time to set the pump timestamp manually:\n"
                f"    python solana_callers.py {ca} --pump-time 2026-03-23T01:39:00Z\n"
                "\n    Or omit it to search X for all tweets mentioning this CA.\n"
            )

    # Step 2 — Scrape X
    client = await get_twikit_client()
    if created_at and pump_peak:
        window_start = created_at - timedelta(hours=1)
        window_end = pump_peak + timedelta(minutes=30)
    else:
        # No pump timestamp — search without a time window
        window_start = datetime.min.replace(tzinfo=timezone.utc)
        window_end = datetime.now(tz=timezone.utc)
    tweets = await scrape_tweets(client, ca, window_start, window_end)

    if not tweets:
        print("No tweets found. Exiting.")
        return

    # Step 3 — Score
    ranked = score_and_rank(tweets, pump_peak or datetime.now(tz=timezone.utc))

    # Step 4 — Output
    save_csv(ranked, args.output)
    print_top(ranked, n=args.top)


if __name__ == "__main__":
    asyncio.run(main())
