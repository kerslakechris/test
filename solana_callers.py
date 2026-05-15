#!/usr/bin/env python3
"""
Identify influential X (Twitter) callers who posted a Solana token CA
before or during its initial price pump.
"""

import argparse
import asyncio
import csv
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiohttp

from config import Config

Config.validate()

COOKIES_FILE = Config.COOKIES_FILE

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
    env_qid = Config.TWITTER_SEARCH_QID
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

from outcomes import (  # noqa: E402
    audit_watchlist,
    update_pending_outcomes,
    recompute_caller_aggregates,
)


# ── Step 1: Get pump timestamp from DexScreener ─────────────────────────

async def fetch_dexscreener_data(session: aiohttp.ClientSession, ca: str) -> dict | None:
    """Fetch token pair data from DexScreener. Returns None if token not found."""
    urls = [
        f"https://api.dexscreener.com/latest/dex/tokens/{ca}",
        f"https://api.dexscreener.com/token-pairs/v1/solana/{ca}",
    ]
    for url in urls:
        for attempt in range(Config.API_RETRY_ATTEMPTS):
            try:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=Config.HTTP_TIMEOUT_PAIR)
                ) as resp:
                    if resp.status == 429:
                        wait = 2 ** (attempt + 1)
                        print(f"[DexScreener] Rate-limited, retrying in {wait}s...")
                        await asyncio.sleep(wait)
                        continue
                    if resp.status == 404:
                        break  # try next URL
                    resp.raise_for_status()
                    data = await resp.json()
                    if isinstance(data, list):
                        pairs = data
                    elif isinstance(data, dict):
                        pairs = data.get("pairs") or []
                    else:
                        pairs = []
                    if pairs:
                        return {"pairs": pairs}
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                wait = 2 ** (attempt + 1)
                print(f"[DexScreener] Request error ({exc}), retrying in {wait}s...")
                await asyncio.sleep(wait)
    return None


def parse_pump_timestamp(data: dict) -> tuple[datetime, datetime, str]:
    """
    Return (pair_created_at, estimated_pump_peak, token_name) from DexScreener.
    """
    pairs = data.get("pairs") or []
    if not pairs:
        return None, None, ""

    pair = max(pairs, key=lambda p: float(p.get("volume", {}).get("h24", 0) or 0))

    created_ms = pair.get("pairCreatedAt")
    if created_ms is None:
        sys.exit("Pair creation time not available in DexScreener response.")

    created_at = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
    pump_peak = created_at + timedelta(minutes=Config.PUMP_PEAK_OFFSET_MIN)

    base = pair.get("baseToken", {}).get("symbol", "?")
    quote = pair.get("quoteToken", {}).get("symbol", "?")
    token_name = f"{base}/{quote}"

    print(f"Pair:          {token_name}")
    print(f"DEX:           {pair.get('dexId', '?')}")
    print(f"Created at:    {created_at.isoformat()}")
    print(f"Est. peak:     {pump_peak.isoformat()}")
    return created_at, pump_peak, token_name


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

    username = Config.X_USERNAME
    email = Config.X_EMAIL
    password = Config.X_PASSWORD
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
    max_tweets: int = Config.DEFAULT_TWEET_LIMIT,
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
            results = await client.search_tweet(
                query, product=product, count=Config.TWEETS_PER_PAGE
            )

            page = 0
            while results and len(all_tweets) < max_tweets and page < Config.MAX_SEARCH_PAGES:
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
            (t["followers"] * Config.WEIGHT_FOLLOWERS)
            + (t["likes"] * Config.WEIGHT_LIKES)
            + (t["retweets"] * Config.WEIGHT_RETWEETS),
            2,
        )

    scored.sort(key=lambda t: t["score"], reverse=True)
    return scored


def add_pump_timing(
    tweets: list[dict],
    created_at: datetime | None,
    pump_peak: datetime | None,
) -> list[dict]:
    """Tag each tweet with its timing relative to the pump."""
    if not created_at or not pump_peak:
        for t in tweets:
            t["pump_timing"] = ""
        return tweets

    for t in tweets:
        dt = t["tweet_time_dt"]
        minutes = int((dt - created_at).total_seconds() / 60)
        sign = "+" if minutes >= 0 else ""

        if dt < created_at:
            label = "PRE-PUMP"
        elif dt <= pump_peak:
            label = "DURING"
        else:
            label = "AFTER"

        t["pump_timing"] = f"{label} ({sign}{minutes}m)"

    return tweets


# ── Step 4: Output ───────────────────────────────────────────────────────

CSV_COLUMNS = [
    "username", "followers", "tweet_time", "likes", "retweets",
    "score", "pump_timing", "tweet_url",
]


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
        timing = r.get("pump_timing", "")
        print(
            f" {i}. @{r['username']:<20} "
            f"followers={r['followers']:<10} "
            f"likes={r['likes']:<6} "
            f"RTs={r['retweets']:<6} "
            f"score={r['score']:<10} "
            f"{timing:<18} "
            f"{r['tweet_url']}"
        )
    if not rows:
        print(" No qualifying tweets found.")
    print()


# ── Step 5: Watchlist ────────────────────────────────────────────────────

WATCHLIST_FILE = Config.WATCHLIST_FILE


def load_watchlist() -> dict:
    if Path(WATCHLIST_FILE).exists():
        with open(WATCHLIST_FILE) as f:
            return json.load(f)
    return {}


def save_watchlist(watchlist: dict) -> None:
    with open(WATCHLIST_FILE, "w") as f:
        json.dump(watchlist, f, indent=2)


def update_watchlist(ranked: list[dict], ca: str, token_name: str = "") -> dict:
    """
    Merge scored tweets into the persistent watchlist.

    Watchlist structure per username:
    {
      "tokens_called": 3,
      "total_score": 1234.5,
      "avg_score": 411.5,
      "first_seen": "2026-03-23T01:30:00+00:00",
      "last_seen": "2026-04-15T12:00:00+00:00",
      "calls": [
        {
          "ca": "49bgr...",
          "tweet_time": "...",
          "tweet_url": "...",
          "score": 400.2,
          "followers": 5000,
          "likes": 10,
          "retweets": 2
        }
      ]
    }
    """
    watchlist = load_watchlist()
    now = datetime.now(tz=timezone.utc).isoformat()

    for tweet in ranked:
        username = tweet["username"]
        entry = watchlist.get(username, {
            "tokens_called": 0,
            "total_score": 0,
            "avg_score": 0,
            "first_seen": tweet["tweet_time"],
            "last_seen": tweet["tweet_time"],
            "calls": [],
        })

        # Skip if this exact tweet was already recorded
        existing_urls = {c["tweet_url"] for c in entry["calls"]}
        if tweet["tweet_url"] in existing_urls:
            watchlist[username] = entry
            continue

        # Check if this is a new token for this caller
        existing_cas = {c["ca"] for c in entry["calls"]}
        if ca not in existing_cas:
            entry["tokens_called"] += 1

        entry["calls"].append({
            "ca": ca,
            "token_name": token_name,
            "tweet_time": tweet["tweet_time"],
            "tweet_url": tweet["tweet_url"],
            "score": tweet.get("score", 0),
            "followers": tweet["followers"],
            "likes": tweet["likes"],
            "retweets": tweet["retweets"],
            "pump_timing": tweet.get("pump_timing", ""),
        })

        entry["total_score"] = round(
            sum(c["score"] for c in entry["calls"]), 2
        )
        entry["avg_score"] = round(
            entry["total_score"] / len(entry["calls"]), 2
        )

        # Update first/last seen
        tweet_ts = tweet["tweet_time"]
        if tweet_ts < entry["first_seen"]:
            entry["first_seen"] = tweet_ts
        if tweet_ts > entry["last_seen"]:
            entry["last_seen"] = tweet_ts

        watchlist[username] = entry

    save_watchlist(watchlist)
    return watchlist


def print_watchlist_summary(
    watchlist: dict,
    n: int = 10,
    sort_by: str = "tokens_called",
    min_calls: int = 0,
    min_win_rate: float = 0.0,
) -> None:
    if not watchlist:
        return

    items = list(watchlist.items())

    # Apply filters
    if min_calls > 0:
        items = [(u, d) for u, d in items if d.get("completed_calls", 0) >= min_calls]
    if min_win_rate > 0:
        items = [(u, d) for u, d in items if d.get("win_rate", 0) >= min_win_rate]

    if not items:
        print("\nNo callers match the filters.")
        return

    # Sort key
    sort_keys = {
        "tokens_called": lambda kv: (kv[1].get("tokens_called", 0), kv[1].get("avg_score", 0)),
        "win_rate": lambda kv: (kv[1].get("win_rate", 0), kv[1].get("avg_multiple_24h", 0)),
        "avg_multiple": lambda kv: kv[1].get("avg_multiple_24h", 0),
        "total_score": lambda kv: kv[1].get("total_score", 0),
    }
    key_fn = sort_keys.get(sort_by, sort_keys["tokens_called"])
    ranked = sorted(items, key=key_fn, reverse=True)

    has_outcomes = any(d.get("completed_calls", 0) > 0 for _, d in ranked)

    print(f"\n{'=' * 90}")
    print(f" Watchlist — Top {min(n, len(ranked))} Callers (sorted by {sort_by})")
    print(f"{'=' * 90}")
    for i, (username, data) in enumerate(ranked[:n], 1):
        line = (
            f" {i}. @{username:<20} "
            f"tokens={data.get('tokens_called', 0):<4} "
        )
        if has_outcomes:
            wr = data.get("win_rate", 0)
            avg_m = data.get("avg_multiple_24h", 0)
            cc = data.get("completed_calls", 0)
            line += (
                f"WR={wr:.0%}  "
                f"avg={avg_m:<6.1f}x "
                f"({cc} rated) "
            )
        else:
            line += (
                f"avg_score={data.get('avg_score', 0):<10} "
                f"calls={len(data.get('calls', []))}"
            )
        print(line)
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
    parser.add_argument("ca", nargs="?", help="Solana token contract address")
    parser.add_argument("-o", "--output", default="callers.csv", help="Output CSV path (default: callers.csv)")
    parser.add_argument("--top", type=int, default=5, help="Number of top callers to print (default: 5)")
    parser.add_argument(
        "--pump-time",
        help="Manual pump timestamp (ISO format, e.g. 2026-03-23T01:39:00Z). "
             "Use when DexScreener no longer has the token.",
    )

    # Outcome tracking
    parser.add_argument(
        "--update-outcomes", action="store_true",
        help="Update outcome data for pending calls in the watchlist",
    )
    parser.add_argument("--user", help="Only update outcomes for this username")
    parser.add_argument(
        "--force", action="store_true",
        help="Force re-check all outcomes, not just pending",
    )
    parser.add_argument(
        "--batch-size", type=int, default=0,
        help="Max number of outcome checks per run (0 = unlimited)",
    )
    parser.add_argument(
        "--audit-outcomes", action="store_true",
        help="Print status distribution of all calls in watchlist and exit",
    )

    # Watchlist display
    parser.add_argument("--watchlist", action="store_true", help="Print watchlist and exit")
    parser.add_argument(
        "--sort-by", default="tokens_called",
        choices=["tokens_called", "win_rate", "avg_multiple", "total_score"],
        help="Sort watchlist by this field (default: tokens_called)",
    )
    parser.add_argument("--min-calls", type=int, default=0, help="Filter: minimum completed calls")
    parser.add_argument("--min-win-rate", type=float, default=0.0, help="Filter: minimum win rate (0.0-1.0)")

    args = parser.parse_args()

    # ── Watchlist display mode ──
    if args.watchlist:
        watchlist = load_watchlist()
        print_watchlist_summary(
            watchlist, n=args.top * 2,
            sort_by=args.sort_by,
            min_calls=args.min_calls,
            min_win_rate=args.min_win_rate,
        )
        return

    # ── Audit mode ──
    if args.audit_outcomes:
        watchlist = load_watchlist()
        if not watchlist:
            print("Watchlist is empty.")
            return
        audit_watchlist(watchlist)
        return

    # ── Standalone outcome update ──
    if args.update_outcomes:
        watchlist = load_watchlist()
        if not watchlist:
            print("Watchlist is empty. Run a CA lookup first.")
            return
        print("[*] Updating outcomes...")
        watchlist = await update_pending_outcomes(
            watchlist,
            user_filter=args.user,
            force=args.force,
            batch_size=args.batch_size,
        )
        save_watchlist(watchlist)
        print_watchlist_summary(
            watchlist, sort_by="win_rate",
            min_calls=args.min_calls,
            min_win_rate=args.min_win_rate,
        )
        print(f"Watchlist saved: {WATCHLIST_FILE}")
        return

    # ── Normal CA lookup flow ──
    if not args.ca:
        parser.error("the following arguments are required: ca")

    ca: str = args.ca
    created_at = None
    pump_peak = None
    token_name = ""

    # Step 1 — Get pump timestamp
    if args.pump_time:
        try:
            created_at = datetime.fromisoformat(args.pump_time.replace("Z", "+00:00"))
        except ValueError:
            parser.error(f"Invalid --pump-time format: {args.pump_time}")
        pump_peak = created_at + timedelta(minutes=Config.PUMP_PEAK_OFFSET_MIN)
        print(f"Using manual pump time: {created_at.isoformat()}")
        print(f"Est. peak:             {pump_peak.isoformat()}")
    else:
        async with aiohttp.ClientSession() as session:
            dex_data = await fetch_dexscreener_data(session, ca)

        if dex_data:
            created_at, pump_peak, token_name = parse_pump_timestamp(dex_data)

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
        window_start = created_at - timedelta(minutes=Config.DEFAULT_WINDOW_BEFORE_MIN)
        window_end = pump_peak + timedelta(minutes=Config.DEFAULT_WINDOW_AFTER_MIN)
    else:
        window_start = datetime.min.replace(tzinfo=timezone.utc)
        window_end = datetime.now(tz=timezone.utc)
    tweets = await scrape_tweets(client, ca, window_start, window_end)

    if not tweets:
        print("No tweets found. Exiting.")
        return

    # Step 3 — Score & timing
    ranked = score_and_rank(tweets, pump_peak or datetime.now(tz=timezone.utc))
    add_pump_timing(ranked, created_at, pump_peak)

    if token_name:
        print(f"Token:         {token_name}")

    # Step 4 — Output
    save_csv(ranked, args.output)
    print_top(ranked, n=args.top)

    # Step 5 — Update watchlist
    watchlist = update_watchlist(ranked, ca, token_name)

    # Step 6 — Auto outcome check
    if Config.OUTCOME_CHECK_ENABLED:
        print("[*] Checking outcomes for eligible calls...")
        watchlist = await update_pending_outcomes(watchlist)
        save_watchlist(watchlist)

    print_watchlist_summary(watchlist, sort_by="win_rate")
    print(f"Watchlist updated: {WATCHLIST_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
