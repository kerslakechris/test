# Outcome Tracker — Debug & Recovery Spec

## Observed Symptoms

From the running dashboard:
- **Total calls:** 3482
- **Pending:** 1469
- **Completed:** 0
- Watchlist win rates: 0% across all callers
- Avg multiples: 0.0x across all callers

The math is the first clue: `3482 - 1469 = 2013` calls are *not* pending but also *not* counted as completed. They're stuck in some intermediate or error state (likely `no_data`, `null`, or a typo that doesn't match the classification check).

This spec covers instrumenting the code to find the failure, hypothesizing the root cause, and applying the fix.

---

## Phase 1: Instrumentation

Before guessing, add visibility. Implement these changes in `outcomes.py` (or wherever the outcome resolution happens).

### 1a. Structured logging

Add a per-call log that captures every step. Use Python's `logging` module with a custom formatter, write to `outcomes_debug.log` in the project root.

```python
import logging

logger = logging.getLogger("outcomes")
handler = logging.FileHandler("outcomes_debug.log")
handler.setFormatter(logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s"
))
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)
```

Log at each step of the outcome resolution pipeline:

```python
logger.info(f"[{ca[:8]}] START outcome check for @{caller} tweet_time={tweet_time}")
logger.debug(f"[{ca[:8]}] Calling DexScreener for pair lookup...")
logger.debug(f"[{ca[:8]}] Pair found: {pair_address}, liquidity={liquidity}")
logger.debug(f"[{ca[:8]}] Fetching candles from={from_ts} to={to_ts}")
logger.debug(f"[{ca[:8]}] Got {len(candles)} candles")
logger.debug(f"[{ca[:8]}] Entry price: {entry_price}")
logger.debug(f"[{ca[:8]}] Best 24h multiple: {mult_24h}")
logger.info(f"[{ca[:8]}] COMPLETE status={status} mult_24h={mult_24h}")
# On any failure:
logger.error(f"[{ca[:8]}] FAIL at step={step} reason={reason}", exc_info=True)
```

### 1b. Status counters

Add a counter dict that tracks every status produced in a batch run:

```python
status_counts = defaultdict(int)
# At end of each call:
status_counts[result.get("status", "null_status")] += 1
# At end of batch:
logger.info(f"BATCH SUMMARY: {dict(status_counts)}")
```

The expected output should look like:
```
BATCH SUMMARY: {'win': 12, 'loss': 30, 'rug': 8, 'no_data': 5, 'pending': 0}
```

If the actual output has `'null_status': 50` or `'error': 50`, you've found the silent failure.

### 1c. Sanity-check the existing data

Add a one-time read-only diagnostic that audits the current watchlist.json:

```python
def audit_watchlist():
    wl = load_watchlist()
    status_dist = defaultdict(int)
    sample_problems = []
    for username, entry in wl.items():
        for call in entry.get("calls", []):
            outcome = call.get("outcome")
            if outcome is None:
                status_dist["NO_OUTCOME_FIELD"] += 1
                if len(sample_problems) < 5:
                    sample_problems.append(("no_outcome", username, call.get("ca")))
            else:
                status_dist[outcome.get("status", "MISSING_STATUS")] += 1
                if len(sample_problems) < 10 and outcome.get("status") not in ("win", "loss", "rug", "big_win", "moonshot", "pending"):
                    sample_problems.append((outcome.get("status"), username, call.get("ca")))
    print("Status distribution:", dict(status_dist))
    print("Sample problem records:", sample_problems)
```

Wire this to a CLI flag: `python solana_callers.py --audit-outcomes`

This is the **first thing to run.** It tells you exactly where the 2013 mystery calls went.

---

## Phase 2: Single-Call Isolation Mode

Add a CLI flag that runs the outcome check for one specific call with verbose output:

```bash
python solana_callers.py --debug-outcome --user <username> --ca <CA>
```

This should:
1. Skip caching
2. Print every step to stdout in addition to the log file
3. Print the raw JSON returned by every API call
4. Print the candle array (first 5 + last 5)
5. Print all intermediate computations
6. Save the full debug trace to `debug_<ca>.json`

Pick a known-pumped token from your watchlist (one where you remember it 10x'd) and run this. The exact step where it fails will be obvious.

---

## Phase 3: Hypothesis Testing

Run the audit and debug-outcome commands, then check against these hypotheses in order. Each has a likely diagnostic signature and a fix.

### Hypothesis 1: DexScreener candle endpoint is broken or returns empty

**Signature in logs:**
```
[abc12345] Got 0 candles
[abc12345] FAIL at step=fetch_candles reason=empty_response
```
OR HTTP 404/410 on the `io.dexscreener.com/dex/chart` endpoint.

**Why likely:** The `io.dexscreener.com` endpoint is undocumented and not part of their stable API. They can change or remove it without notice.

**Fix:** Switch to Birdeye's documented OHLCV endpoint:
```
GET https://public-api.birdeye.so/defi/ohlcv
    ?address={pair_address}
    &type=1m
    &time_from={unix_seconds}
    &time_to={unix_seconds}
Header: X-API-KEY: <key>
```

Free tier gives 1 req/sec with 10k req/month — enough for a few hundred outcome checks per day.

Add `BIRDEYE_API_KEY` to `.env` and a `CANDLE_PROVIDER=birdeye` switch. Make the candle fetcher pluggable so both providers can be tried in sequence.

### Hypothesis 2: Pair address lookup is failing for old/migrated tokens

**Signature in logs:**
```
[abc12345] DexScreener returned 0 pairs
[abc12345] FAIL at step=pair_lookup
```

**Why likely:** Pump.fun tokens migrate to Raydium when they graduate. The originally-saved CA may have moved, or DexScreener may no longer index dead pairs.

**Fix:** Handle the empty case explicitly. For tokens with no current pair, try:
1. Solscan historical lookup
2. Mark as `no_data` (not `pending`) so it doesn't keep getting retried
3. Add a `LAST_CHECKED` field so the retry backoff actually works

```python
if not pairs:
    return {
        "status": "no_data",
        "reason": "no_pair_found",
        "checked_at": now_iso(),
    }
```

### Hypothesis 3: Status writes happen but don't get classified

**Signature in audit output:**
```
Status distribution: {'pending': 1469, '': 2013}
```
OR
```
Status distribution: {'pending': 1469, None: 2013}
```

**Why likely:** A code path returns an empty dict or None instead of a proper outcome object. The "Completed" counter probably only counts statuses in a specific set like `{"win", "big_win", "moonshot", "loss", "rug"}`.

**Fix:** Make the counter logic explicit:
```python
TERMINAL_STATUSES = {"win", "big_win", "moonshot", "loss", "rug", "no_data"}
completed = sum(
    1 for entry in wl.values()
    for call in entry["calls"]
    if call.get("outcome", {}).get("status") in TERMINAL_STATUSES
)
```

Also verify the outcome dict is fully populated before writing:
```python
assert outcome.get("status") in ALL_VALID_STATUSES, f"Invalid status: {outcome}"
```

### Hypothesis 4: Exceptions are being swallowed

**Signature:** Nothing in the log, no errors visible, but no completions either.

**Why likely:** A `try/except` block with bare `except: pass` somewhere in the loop. Easy to miss in async code.

**Fix:** Search for bare excepts:
```bash
grep -rn "except.*:" outcomes.py | grep -v "Exception as e"
grep -rn "except:" .
```

Replace every bare `except:` with `except Exception as e:` and log the exception:
```python
except Exception as e:
    logger.exception(f"[{ca[:8]}] Unexpected error in outcome check")
    return {"status": "error", "reason": str(e), "checked_at": now_iso()}
```

### Hypothesis 5: Async tasks not actually awaited

**Signature:** Logs show "START" but no corresponding "COMPLETE" or "FAIL." Tasks are getting created but not run to completion.

**Why likely:** Common bug pattern in asyncio code:
```python
# Bug:
tasks = [check_outcome(call) for call in calls]  # creates coroutines but doesn't await
# Fix:
results = await asyncio.gather(*[check_outcome(c) for c in calls])
```

**Fix:** Wrap in `asyncio.gather` with `return_exceptions=True` so one bad call doesn't poison the batch:
```python
results = await asyncio.gather(
    *[check_outcome(c) for c in batch],
    return_exceptions=True
)
for r in results:
    if isinstance(r, Exception):
        logger.error(f"Task failed: {r}")
```

### Hypothesis 6: File write race condition

**Signature:** Diagnostic command works in isolation (writes complete), but batch run leaves data missing.

**Why likely:** Multiple concurrent writers to `watchlist.json` — the last write wins and earlier writes are lost.

**Fix:** Serialize writes through a single mutation point. Don't have each outcome check write to the file directly. Instead:
1. Each check returns its result
2. Main loop collects all results
3. Single write at end of batch (or every N completions for crash safety)

```python
async def batch_update(calls):
    results = await asyncio.gather(*[check_outcome(c) for c in calls], return_exceptions=True)
    wl = load_watchlist()
    for original, result in zip(calls, results):
        if not isinstance(result, Exception):
            # write back to wl in-memory
            update_call_in_wl(wl, original, result)
    save_watchlist(wl)  # one write
```

### Hypothesis 7: Outcome check gated by misconfigured delay

**Signature in logs:**
```
[abc12345] Skipping — call too recent (age=0.5h < min=24h)
```

**Why likely:** `OUTCOME_CHECK_DELAY_MIN` is set to a high value, so every newly-added call is skipped on first run.

**Fix:** Verify the env var. The spec recommended 60 minutes, but if you set it to 24h or 1440 minutes, nothing will resolve until calls age out. Lower to 60 minutes for production:
```
OUTCOME_CHECK_DELAY_MIN=60
```

### Hypothesis 8: Tweet times stored as strings, compared as datetimes

**Signature in logs:**
```
[abc12345] FAIL at step=compute_window reason=TypeError
TypeError: '<' not supported between 'str' and 'datetime.datetime'
```

**Why likely:** Tweet times in watchlist.json are ISO strings (since JSON can't store datetimes). If they're not parsed back to datetimes before window math, every call silently errors.

**Fix:** Always parse on read:
```python
from datetime import datetime
def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

tweet_dt = parse_iso(call["tweet_time"])
```

---

## Phase 4: Recovery & Backfill

Once the root cause is fixed, the 2013 broken records need to be recovered.

### 4a. Reset stuck calls

```bash
python solana_callers.py --reset-outcomes --status-not-in win,loss,big_win,moonshot,rug,no_data
```

This resets every call with an invalid/missing status back to `pending`, so the next outcome run will retry them with the fixed code.

### 4b. Throttled batch recovery

The default outcome run hits API rate limits if it tries to resolve 2000 calls at once. Add a throttle:

```bash
python solana_callers.py --update-outcomes --batch-size 50 --delay-ms 200
```

This processes 50 calls per batch with 200ms between requests. ~17 minutes to resolve 2000 calls on free Birdeye, much safer.

### 4c. Resume on crash

Save progress checkpoint every batch:
```python
checkpoint = {"last_processed_idx": idx, "last_save": now_iso()}
with open(".outcome_progress", "w") as f:
    json.dump(checkpoint, f)
```

On resume, skip everything before `last_processed_idx`.

---

## Phase 5: Validation

After the fix, run all three of these and confirm:

```bash
# 1. Audit should show no NO_OUTCOME_FIELD or null statuses for old calls
python solana_callers.py --audit-outcomes

# 2. Debug a known winner — should show win or big_win
python solana_callers.py --debug-outcome --user <topcaller> --ca <known_pumped_CA>

# 3. Watchlist should now show non-zero win rates
python solana_callers.py --watchlist --sort-by win_rate
```

If the top caller's win rate is now something realistic (e.g., 35%, 60%) instead of 0%, the pipeline is healthy.

---

## Quick-Start Order

1. **Add the logging from Phase 1a** (10 min)
2. **Add and run `--audit-outcomes`** (10 min + immediate result)
3. **Add `--debug-outcome` and run on one known token** (15 min + immediate result)
4. The audit output + one debug trace will narrow it to one or two hypotheses. Apply the fix.
5. Reset stuck calls and run throttled recovery.

Most likely outcome: it's **Hypothesis 1 or 2** (DexScreener endpoint issue) or **Hypothesis 3** (status field never gets populated correctly). Both are quick fixes once spotted.

---

## Definition of Done

- `outcomes_debug.log` produces structured per-call traces
- `--audit-outcomes` command reports current status distribution
- `--debug-outcome` command runs one call with full verbose output
- Root cause identified and documented
- Fix applied
- All 1469 pending calls re-run and resolved (or marked `no_data` with reason)
- "Completed" counter in dashboard moves from 0 to a meaningful number
- At least one watchlist caller shows a non-zero win rate
