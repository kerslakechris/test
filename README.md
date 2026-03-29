# Solana Token Early Caller Finder

Identifies influential X (Twitter) accounts that posted a Solana token contract address before or during its initial price pump.

## How It Works

1. **Pump timestamp** — Queries the DexScreener API for the token's pair creation time and estimates the peak volume window (+15 min).
2. **X scraping** — Uses [twikit](https://github.com/d60/twikit) to search for tweets containing the CA in a window from 1 hour before creation to 30 minutes after the estimated peak.
3. **Scoring** — Ranks tweets by influence score: `(followers × 0.4) + (likes × 0.4) + (retweets × 0.2)`, filtered to posts before peak volume.
4. **Output** — Saves all results to CSV and prints the top callers to console.

## Requirements

- Python 3.10+
- A Twitter/X account

## Installation

```bash
pip install -r requirements.txt
```

## Setup

On first run, twikit logs in with your credentials and saves a `cookies.json` file. Subsequent runs reuse the cookies automatically.

Set your credentials as environment variables before the first run:

```bash
export X_USERNAME="your_username"
export X_EMAIL="your_email@example.com"
export X_PASSWORD="your_password"
```

## Usage

```bash
python solana_callers.py <CONTRACT_ADDRESS> [OPTIONS]
```

### Arguments

| Argument | Description |
|---|---|
| `ca` | Solana token contract address (required) |
| `-o`, `--output` | Output CSV file path (default: `callers.csv`) |
| `--top` | Number of top callers to print to console (default: `5`) |

### Examples

```bash
# Basic usage
python solana_callers.py EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v

# Custom output file and show top 10
python solana_callers.py EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v -o results.csv --top 10
```

## Output

**Console:**
```
================================================================================
 Top 5 Influential Callers
================================================================================
 1. @caller_one          followers=82000     likes=312   RTs=88    score=33128.4
 2. @caller_two          followers=45000     likes=201   RTs=44    score=18089.0
 ...
```

**CSV columns:** `username`, `followers`, `tweet_time`, `likes`, `retweets`, `score`, `tweet_url`

## Notes

- The pump peak is estimated as 15 minutes after pair creation. For tokens with unusual pump timings this heuristic may need adjusting.
- `cookies.json` stores your session — keep it private and add it to `.gitignore`.
- twikit's search uses X's `Latest` feed, so very old tokens may have limited tweet history available.
