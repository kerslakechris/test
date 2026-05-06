"""
Outcome tracking for Solana token calls.

Fetches post-call price data from DexScreener and computes realized
performance metrics (multiples, drawdowns, win/loss classification).
"""

import asyncio
import os
import statistics
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP

import aiohttp

# ── Configuration ────────────────────────────────────────────────────────

OUTCOME_CHECK_DELAY_MIN = int(os.getenv("OUTCOME_CHECK_DELAY_MIN", "60"))
OUTCOME_CHECK_BACKOFF_HOURS = int(os.getenv("OUTCOME_CHECK_BACKOFF_HOURS", "24"))

DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{ca}"
DEXSCREENER_CANDLES_URL = (
    "https://io.dexscreener.com/dex/chart/amm/v3/solana/bars/{pair_address}"
    "?from={from_ms}&to={to_ms}&res=1"
)

WINDOWS = [
    ("15m", timedelta(minutes=15)),
    ("1h", timedelta(hours=1)),
    ("6h", timedelta(hours=6)),
    ("24h", timedelta(hours=24)),
    ("7d", timedelta(days=7)),
]


# ── Pair resolution ─────────────────────────────────────────────────────

async def fetch_pair_address(
    session: aiohttp.ClientSession, ca: str
) -> str | None:
    """Return the most-liquid DEX pair address for the token."""
    url = DEXSCREENER_TOKEN_URL.format(ca=ca)
    for attempt in range(4):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 429:
                    await asyncio.sleep(2 ** (attempt + 1))
                    continue
                if resp.status != 200:
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(2 ** (attempt + 1))
            continue

        pairs = data.get("pairs") or []
        if not pairs:
            return None

        best = max(
            pairs,
            key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0),
        )
        return best.get("pairAddress")

    return None


# ── Candle fetching ──────────────────────────────────────────────────────

async def fetch_candles(
    session: aiohttp.ClientSession,
    pair_address: str,
    from_ts: int,
    to_ts: int,
) -> list[dict]:
    """Fetch 1-minute candles from DexScreener. Returns sorted by time."""
    url = DEXSCREENER_CANDLES_URL.format(
        pair_address=pair_address,
        from_ms=from_ts,
        to_ms=to_ts,
    )
    for attempt in range(4):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 429:
                    wait = 2 ** (attempt + 1)
                    await asyncio.sleep(wait)
                    continue
                if resp.status != 200:
                    return []
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            await asyncio.sleep(2 ** (attempt + 1))
            continue

        bars = data.get("bars", data) if isinstance(data, dict) else data
        if not isinstance(bars, list):
            return []

        bars.sort(key=lambda b: b.get("t", 0))
        return bars

    return []


# ── Outcome computation ─────────────────────────────────────────────────

def _find_entry_price(candles: list[dict], tweet_ts_ms: int) -> Decimal | None:
    """Find entry price: close of the candle containing tweet_time,
    or open of the next candle if no exact match."""
    if not candles:
        return None

    for c in candles:
        if c["t"] >= tweet_ts_ms:
            price = c.get("o") or c.get("c")
            if price and float(price) > 0:
                return Decimal(str(price))
            break

    for c in candles:
        if c["t"] <= tweet_ts_ms:
            price = c.get("c") or c.get("o")
            if price and float(price) > 0:
                return Decimal(str(price))

    first_price = candles[0].get("o") or candles[0].get("c")
    if first_price and float(first_price) > 0:
        return Decimal(str(first_price))
    return None


def _sig_round(val: float, sig: int = 6) -> float:
    """Round to N significant digits for sub-cent prices."""
    if val == 0:
        return 0.0
    d = Decimal(str(val))
    rounded = d.quantize(Decimal(10) ** (d.adjusted() - sig + 1), rounding=ROUND_HALF_UP)
    return float(rounded)


def compute_outcome(candles: list[dict], tweet_time: datetime) -> dict:
    """Compute the full outcome object for one call."""
    tweet_ts_ms = int(tweet_time.timestamp() * 1000)

    if not candles:
        return {"status": "no_data"}

    entry_price = _find_entry_price(candles, tweet_ts_ms)
    if entry_price is None or entry_price <= 0:
        return {"status": "no_data"}

    now = datetime.now(tz=timezone.utc)
    checked_at = now.isoformat()

    windows_result = {}
    ath_multiple = Decimal("1")
    ath_minutes = 0

    for label, delta in WINDOWS:
        window_end_ms = tweet_ts_ms + int(delta.total_seconds() * 1000)

        window_candles = [
            c for c in candles
            if tweet_ts_ms <= c["t"] <= window_end_ms
        ]

        if not window_candles:
            continue

        highs = [Decimal(str(c.get("h", c.get("c", 0)))) for c in window_candles]
        lows = [Decimal(str(c.get("l", c.get("c", 0)))) for c in window_candles]
        final_price = Decimal(str(
            window_candles[-1].get("c", window_candles[-1].get("o", 0))
        ))

        max_price = max(highs) if highs else entry_price
        min_price = min(lows) if lows else entry_price
        # Clamp to avoid division issues
        min_price = max(min_price, Decimal("0"))

        multiple = float(max_price / entry_price) if entry_price > 0 else 1.0
        drawdown = float((min_price - entry_price) / entry_price) if entry_price > 0 else 0.0
        drawdown = max(drawdown, -1.0)  # cap at -100%

        windows_result[label] = {
            "multiple": round(multiple, 2),
            "drawdown": round(drawdown, 4),
            "final": _sig_round(float(final_price)),
        }

        if max_price > ath_multiple * entry_price:
            ath_multiple = max_price / entry_price
            for c in window_candles:
                if Decimal(str(c.get("h", 0))) >= max_price:
                    ath_minutes = max(0, (c["t"] - tweet_ts_ms) // 60000)
                    break

    best_24h = windows_result.get("24h", {}).get("multiple", 1.0)
    drawdown_1h = windows_result.get("1h", {}).get("drawdown", 0.0)

    # Determine if enough time has passed for classification
    hours_since = (now - tweet_time).total_seconds() / 3600
    if hours_since < 24 and "24h" not in windows_result:
        status = "pending"
    else:
        status = classify_outcome(best_24h, drawdown_1h)

    return {
        "status": status,
        "entry_price": _sig_round(float(entry_price)),
        "checked_at": checked_at,
        "windows": windows_result,
        "ath": {
            "multiple": round(float(ath_multiple), 2),
            "minutes_to_ath": ath_minutes,
        },
    }


def classify_outcome(best_multiple_24h: float, drawdown_1h: float) -> str:
    """Classify a call based on best 24h multiple and 1h drawdown."""
    if best_multiple_24h >= 10:
        return "moonshot"
    if best_multiple_24h >= 5:
        return "big_win"
    if best_multiple_24h >= 2:
        return "win"
    if best_multiple_24h < 1.0 and drawdown_1h < -0.50:
        return "rug"
    return "loss"


# ── Per-call update ──────────────────────────────────────────────────────

async def update_call_outcome(
    session: aiohttp.ClientSession,
    call: dict,
    ca: str,
) -> dict:
    """End-to-end: look up pair, fetch candles, compute outcome, return updated call."""
    pair_address = await fetch_pair_address(session, ca)
    if not pair_address:
        call["outcome"] = {"status": "no_data"}
        return call

    tweet_time = datetime.fromisoformat(call["tweet_time"])
    from_ts = int(tweet_time.timestamp() * 1000)
    to_ts = from_ts + int(timedelta(days=7).total_seconds() * 1000)

    candles = await fetch_candles(session, pair_address, from_ts, to_ts)
    call["outcome"] = compute_outcome(candles, tweet_time)
    return call


# ── Caller aggregates ────────────────────────────────────────────────────

_WIN_STATUSES = {"win", "big_win", "moonshot"}
_COMPLETED_STATUSES = {"win", "big_win", "moonshot", "loss", "rug"}


def recompute_caller_aggregates(entry: dict) -> dict:
    """Recompute win_rate, avg_multiple, best/worst_call from calls list."""
    calls = entry.get("calls", [])
    completed = []
    pending = 0
    wins = big_wins = moonshots = losses = rugs = 0
    multiples_24h = []
    best_call = None
    worst_call = None

    for c in calls:
        outcome = c.get("outcome")
        if not outcome:
            pending += 1
            continue

        status = outcome.get("status", "pending")
        if status == "pending":
            pending += 1
            continue
        if status == "no_data":
            continue

        completed.append(c)

        m24 = outcome.get("windows", {}).get("24h", {}).get("multiple")
        ath_m = outcome.get("ath", {}).get("multiple", 1.0)
        effective_m = m24 if m24 is not None else ath_m

        multiples_24h.append(effective_m)

        if status == "moonshot":
            moonshots += 1
        elif status == "big_win":
            big_wins += 1
        elif status == "win":
            wins += 1
        elif status == "rug":
            rugs += 1
        elif status == "loss":
            losses += 1

        if best_call is None or effective_m > best_call["multiple"]:
            best_call = {
                "ca": c["ca"],
                "multiple": round(effective_m, 2),
                "tweet_url": c["tweet_url"],
            }
        if worst_call is None or effective_m < worst_call["multiple"]:
            worst_call = {
                "ca": c["ca"],
                "multiple": round(effective_m, 2),
                "tweet_url": c["tweet_url"],
            }

    total_completed = len(completed)
    total_wins = wins + big_wins + moonshots

    entry["completed_calls"] = total_completed
    entry["pending_calls"] = pending
    entry["wins"] = wins
    entry["big_wins"] = big_wins
    entry["moonshots"] = moonshots
    entry["losses"] = losses
    entry["rugs"] = rugs
    entry["win_rate"] = round(total_wins / total_completed, 4) if total_completed else 0.0
    entry["avg_multiple_24h"] = (
        round(sum(multiples_24h) / len(multiples_24h), 2) if multiples_24h else 0.0
    )
    entry["median_multiple_24h"] = (
        round(statistics.median(multiples_24h), 2) if multiples_24h else 0.0
    )
    entry["best_call"] = best_call
    entry["worst_call"] = worst_call

    return entry


# ── Batch update ─────────────────────────────────────────────────────────

async def update_pending_outcomes(
    watchlist: dict,
    user_filter: str | None = None,
    force: bool = False,
    batch_size: int = 0,
) -> dict:
    """Update outcomes for eligible calls in the watchlist."""
    now = datetime.now(tz=timezone.utc)
    delay = timedelta(minutes=OUTCOME_CHECK_DELAY_MIN)
    backoff = timedelta(hours=OUTCOME_CHECK_BACKOFF_HOURS)

    updated = 0
    skipped = 0
    total = 0

    async with aiohttp.ClientSession() as session:
        for username, entry in watchlist.items():
            if user_filter and username != user_filter:
                continue

            for call in entry.get("calls", []):
                total += 1
                outcome = call.get("outcome")

                if not force and outcome:
                    status = outcome.get("status", "")
                    if status in _COMPLETED_STATUSES:
                        skipped += 1
                        continue

                    checked_at_str = outcome.get("checked_at")
                    if checked_at_str:
                        checked_at = datetime.fromisoformat(checked_at_str)
                        if now - checked_at < backoff:
                            skipped += 1
                            continue

                tweet_time = datetime.fromisoformat(call["tweet_time"])
                if not force and now - tweet_time < delay:
                    skipped += 1
                    continue

                ca = call.get("ca", "")
                if not ca:
                    skipped += 1
                    continue

                print(f"  Checking @{username} → {ca[:12]}...")
                await update_call_outcome(session, call, ca)
                updated += 1

                if batch_size and updated >= batch_size:
                    print(f"  Batch limit ({batch_size}) reached.")
                    break

                await asyncio.sleep(0.5)

            recompute_caller_aggregates(entry)

            if batch_size and updated >= batch_size:
                break

    print(f"\n[outcomes] {updated} updated, {skipped} skipped, {total} total calls")
    return watchlist
