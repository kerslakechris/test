# Solana Token Early Caller Finder

Find the influential X (Twitter) accounts that posted a Solana token's contract address **before or during its first price pump** — then track them across multiple tokens to identify consistent early callers.

## How It Works

```
   CA input
      │
      ▼
┌─────────────┐     ┌──────────────┐     ┌───────────┐     ┌─────────┐     ┌───────────┐
│ DexScreener │────▶│  X / Twitter │────▶│   Score   │────▶│  CSV +  │────▶│ Watchlist │
│  pump time  │     │    search    │     │  & rank   │     │ console │     │  update   │
└─────────────┘     └──────────────┘     └───────────┘     └─────────┘     └───────────┘
```

1. **Pump timestamp** — Queries the DexScreener API for pair creation time; estimates peak volume at +15 min.
2. **X search** — Uses [twikit](https://github.com/d60/twikit) (with automatic patches for X's latest auth changes) to search for tweets containing the CA. Searches both "Latest" and "Top" feeds, up to 200 tweets.
3. **Scoring** — Ranks by influence: `(followers × 0.4) + (likes × 0.4) + (retweets × 0.2)`, filtered to pre-peak tweets.
4. **Output** — Saves ranked results to CSV and prints the top callers.
5. **Watchlist** — Merges results into `watchlist.json`, building a profile of each caller across every token you investigate.

## Requirements

- Python 3.10+
- An X (Twitter) account

## Installation

```bash
git clone https://github.com/kerslakechris/test.git
cd test
pip install -r requirements.txt
```

### Dependencies

| Package | Purpose |
|---|---|
| `twikit` | X/Twitter scraping via GraphQL |
| `aiohttp` | Async HTTP for DexScreener API |
| `python-dotenv` | Load `.env` credentials |

## Setup — X Authentication

You need an authenticated X session stored in `cookies.json`. There are two ways to set this up:

### Option A — Browser cookies (recommended)

X frequently blocks programmatic login. Importing cookies from your browser is the most reliable method.

1. Log in to [x.com](https://x.com) in your browser
2. Open **DevTools** (F12) → **Application** → **Cookies** → `https://x.com`
3. Run the setup command:

```bash
python solana_callers.py setup-cookies
```

4. Paste the cookie values when prompted:
   - **`auth_token`** — required
   - **`ct0`** — required
   - `kdt`, `twid`, `guest_id` — optional but recommended

Cookies expire after ~24 hours. Re-run `setup-cookies` when you get a 401 error.

### Option B — Credentials via .env

Create a `.env` file from the template:

```bash
cp .env.example .env
```

Then edit `.env`:

```
X_USERNAME="your_username"
X_EMAIL="your_email@example.com"
X_PASSWORD="your_password"
```

The script will attempt to log in and save `cookies.json` automatically. If X blocks the login (common), fall back to Option A.

## Usage

```bash
python solana_callers.py <CONTRACT_ADDRESS> [OPTIONS]
```

### Arguments

| Argument | Description | Default |
|---|---|---|
| `ca` | Solana token contract address | required* |
| `-o`, `--output` | CSV output file path | `callers.csv` |
| `--top` | Number of top callers to print | `5` |
| `--pump-time` | Manual pump timestamp (ISO 8601) | auto from DexScreener |
| `--update-outcomes` | Update outcome data for watchlist calls | — |
| `--user` | Only update outcomes for this username | all |
| `--force` | Re-check all outcomes, not just pending | — |
| `--batch-size` | Max outcome checks per run (0 = unlimited) | `0` |
| `--watchlist` | Print watchlist and exit | — |
| `--sort-by` | Sort watchlist: `tokens_called`, `win_rate`, `avg_multiple`, `total_score` | `tokens_called` |
| `--min-calls` | Filter: minimum completed calls | `0` |
| `--min-win-rate` | Filter: minimum win rate (0.0–1.0) | `0.0` |

*Not required when using `--update-outcomes` or `--watchlist`.

### Commands

| Command | Description |
|---|---|
| `setup-cookies` | Import browser cookies for X authentication |

### Examples

```bash
# Basic — auto-detect pump time from DexScreener
python solana_callers.py ACtfUWtgvaXrQGNMiohTusi5jcx5RJf5zwu9aAxkpump

# Manual pump time (for delisted tokens)
python solana_callers.py 49bgrAJimCAGHG8o8VhSV7w1DhopLBrWutzYbqSFpump \
  --pump-time 2026-03-23T01:39:00Z

# Custom output and show top 10
python solana_callers.py ACtfUWtgvaXrQGNMiohTusi5jcx5RJf5zwu9aAxkpump \
  -o results.csv --top 10

# Investigate multiple tokens (watchlist builds up)
python solana_callers.py <TOKEN_1>
python solana_callers.py <TOKEN_2>
python solana_callers.py <TOKEN_3>

# Update outcomes for all pending calls
python solana_callers.py --update-outcomes

# Force re-check outcomes for a specific caller
python solana_callers.py --update-outcomes --user alpha_caller --force

# Batch update (useful for large watchlists)
python solana_callers.py --update-outcomes --batch-size 50

# View watchlist ranked by win rate, high performers only
python solana_callers.py --watchlist --sort-by win_rate --min-calls 5 --min-win-rate 0.5
```

## Output

### Console

```
Pair:          LOCKDOWN/SOL
DEX:           pumpfun
Created at:    2026-03-23T01:39:24+00:00
Est. peak:     2026-03-23T01:54:24+00:00
[twikit] Loaded saved cookies.

Searching X: ACtfUWtgvaXrQGNMiohTusi5jcx5RJf5zwu9aAxkpump
[twikit] Total raw tweets collected: 47
[twikit] Tweets in target window: 12

================================================================================
 Top 5 Influential Callers
================================================================================
 1. @alpha_caller        followers=82000     likes=312   RTs=88    score=33128.4
 2. @degen_scout         followers=45000     likes=201   RTs=44    score=18089.0
 3. @sol_sniper          followers=23000     likes=89    RTs=31    score=9241.8
 4. @memecoin_alerts     followers=15200     likes=44    RTs=12    score=6100.0
 5. @pump_watcher        followers=4410      likes=3     RTs=0     score=1765.2

==========================================================================================
 Watchlist — Top 5 Callers (sorted by win_rate)
==========================================================================================
 1. @alpha_caller        tokens=3   WR=67%  avg=4.2x  (3 rated)
 2. @degen_scout         tokens=2   WR=50%  avg=3.1x  (2 rated)
 3. @sol_sniper          tokens=2   WR=50%  avg=2.8x  (2 rated)
```

### CSV (`callers.csv`)

| Column | Description |
|---|---|
| `username` | X handle |
| `followers` | Follower count at time of tweet |
| `tweet_time` | ISO 8601 timestamp |
| `likes` | Like count |
| `retweets` | Retweet count |
| `score` | Influence score |
| `tweet_url` | Direct link to tweet |

### Watchlist (`watchlist.json`)

Persists across every run. Per caller:

```json
{
  "alpha_caller": {
    "tokens_called": 3,
    "completed_calls": 3,
    "pending_calls": 0,
    "wins": 1,
    "big_wins": 1,
    "moonshots": 0,
    "losses": 1,
    "rugs": 0,
    "win_rate": 0.6667,
    "avg_multiple_24h": 4.2,
    "median_multiple_24h": 3.8,
    "best_call": { "ca": "ACtf...", "multiple": 8.2, "tweet_url": "..." },
    "worst_call": { "ca": "49bg...", "multiple": 0.4, "tweet_url": "..." },
    "first_seen": "2026-03-20T14:22:00+00:00",
    "last_seen": "2026-04-01T09:15:00+00:00",
    "calls": [
      {
        "ca": "ACtfUWtgva...",
        "tweet_time": "2026-03-23T01:35:00+00:00",
        "tweet_url": "https://x.com/alpha_caller/status/...",
        "score": 33128.4,
        "followers": 82000,
        "likes": 312,
        "retweets": 88,
        "outcome": {
          "status": "big_win",
          "entry_price": 0.0000234,
          "checked_at": "2026-03-24T01:40:00+00:00",
          "windows": {
            "15m":  { "multiple": 1.4, "drawdown": -0.12, "final": 0.0000287 },
            "1h":   { "multiple": 2.1, "drawdown": -0.18, "final": 0.0000412 },
            "6h":   { "multiple": 3.8, "drawdown": -0.18, "final": 0.0000356 },
            "24h":  { "multiple": 8.2, "drawdown": -0.31, "final": 0.0000189 },
            "7d":   { "multiple": 8.2, "drawdown": -0.78, "final": 0.0000051 }
          },
          "ath": { "multiple": 8.2, "minutes_to_ath": 187 }
        }
      }
    ]
  }
}
```

The watchlist deduplicates by tweet URL, so re-running the same token is safe.

### Outcome Labels

| Label | Threshold |
|---|---|
| `moonshot` | Best 24h multiple >= 10x |
| `big_win` | Best 24h multiple >= 5x |
| `win` | Best 24h multiple >= 2x |
| `loss` | Best 24h multiple < 1.5x |
| `rug` | Multiple < 1x AND drawdown > 50% in 1h |
| `pending` | Less than 24h since call |
| `no_data` | No candle data available |

## Scoring Formula

```
score = (followers × 0.4) + (likes × 0.4) + (retweets × 0.2)
```

Only tweets posted **before the estimated peak** are scored. If no pre-peak tweets are found (e.g. X's search index dropped them), all returned tweets are scored instead.

## Troubleshooting

| Problem | Solution |
|---|---|
| `Couldn't get KEY_BYTE indices` | The script patches this automatically. Make sure you're on the latest version (`git pull`). |
| `401 Unauthorized` | Cookies expired. Re-run `python solana_callers.py setup-cookies`. |
| `All search query IDs returned 404` | X rotated GraphQL endpoints. Get the current ID from browser DevTools (Network → filter `SearchTimeline`), then set `TWITTER_SEARCH_QID=<id>` in `.env`. |
| `No pairs found on DexScreener` | Token was delisted. Use `--pump-time` to set the timestamp manually. |
| `No tweets found` | X's search index doesn't reliably cover long random strings (CAs) or old tweets. Try a more recent token. |
| `ModuleNotFoundError: aiohttp` | Run `pip install -r requirements.txt` inside your virtual environment. |
| `git pull` conflicts | Run `git stash && git pull && git stash pop`, or `git checkout -- solana_callers.py && git pull` to discard local changes. |

## Environment Variables

All optional. Set in `.env` or export in your shell.

| Variable | Purpose | Default |
|---|---|---|
| `X_USERNAME` | X username (login fallback) | — |
| `X_EMAIL` | X email (login fallback) | — |
| `X_PASSWORD` | X password (login fallback) | — |
| `TWITTER_SEARCH_QID` | Override GraphQL search query ID when X rotates endpoints | — |
| `OUTCOME_CHECK_ENABLED` | Auto-check outcomes after each CA lookup | `true` |
| `OUTCOME_CHECK_DELAY_MIN` | Minutes to wait before checking a call's outcome | `60` |
| `OUTCOME_CHECK_BACKOFF_HOURS` | Hours between re-checks for pending calls | `24` |

## Files

| File | Tracked | Description |
|---|---|---|
| `solana_callers.py` | Yes | Main script — CA lookup, scoring, watchlist |
| `outcomes.py` | Yes | Outcome tracking — candle fetching, classification, aggregates |
| `requirements.txt` | Yes | Python dependencies |
| `.env.example` | Yes | Template for credentials and config |
| `.env` | No | Your credentials (gitignored) |
| `cookies.json` | No | X session cookies (gitignored) |
| `watchlist.json` | No | Persistent caller database with outcomes (gitignored) |
| `callers.csv` | No | Output from last run |
