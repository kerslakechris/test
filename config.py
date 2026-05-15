"""
Centralised configuration — reads .env at import time and exposes every
tunable value as a class attribute on Config.

Usage:
    from config import Config
    Config.WATCHLIST_FILE  # "watchlist.json"
    Config.validate()      # warn about misconfigured values
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── X (Twitter) auth ──────────────────────────────────────────────────
    X_USERNAME: str | None = os.getenv("X_USERNAME")
    X_EMAIL: str | None = os.getenv("X_EMAIL")
    X_PASSWORD: str | None = os.getenv("X_PASSWORD")

    # ── API keys ──────────────────────────────────────────────────────────
    BIRDEYE_API_KEY: str | None = os.getenv("BIRDEYE_API_KEY")
    HELIUS_API_KEY: str | None = os.getenv("HELIUS_API_KEY")
    SOLSCAN_API_KEY: str | None = os.getenv("SOLSCAN_API_KEY")
    TWITTER_SEARCH_QID: str | None = os.getenv("TWITTER_SEARCH_QID")
    TELEGRAM_BOT_TOKEN: str | None = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID: str | None = os.getenv("TELEGRAM_CHAT_ID")
    DISCORD_WEBHOOK_URL: str | None = os.getenv("DISCORD_WEBHOOK_URL")

    # ── Storage / paths ───────────────────────────────────────────────────
    WATCHLIST_FILE: str = os.getenv("WATCHLIST_FILE", "watchlist.json")
    COOKIES_FILE: str = os.getenv("COOKIES_FILE", "cookies.json")
    WALLET_LINKS_FILE: str = os.getenv("WALLET_LINKS_FILE", "wallet_links.json")
    OUTCOMES_LOG_FILE: str = os.getenv("OUTCOMES_LOG_FILE", "outcomes_debug.log")
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # ── Candle data source ────────────────────────────────────────────────
    # "dexscreener" (currently 403-blocked) | "birdeye" (requires API key)
    CANDLE_PROVIDER: str = os.getenv("CANDLE_PROVIDER", "dexscreener")
    CANDLE_FETCH_DAYS: int = int(os.getenv("CANDLE_FETCH_DAYS", "7"))

    # ── Outcome tracking ──────────────────────────────────────────────────
    OUTCOME_CHECK_ENABLED: bool = (
        os.getenv("OUTCOME_CHECK_ENABLED", "true").lower() == "true"
    )
    OUTCOME_CHECK_DELAY_MIN: int = int(os.getenv("OUTCOME_CHECK_DELAY_MIN", "60"))
    OUTCOME_CHECK_BACKOFF_HOURS: int = int(
        os.getenv("OUTCOME_CHECK_BACKOFF_HOURS", "24")
    )
    OUTCOME_BATCH_SIZE: int = int(os.getenv("OUTCOME_BATCH_SIZE", "50"))
    OUTCOME_DELAY_MS: int = int(os.getenv("OUTCOME_DELAY_MS", "1100"))
    INTER_OUTCOME_SLEEP_S: float = float(os.getenv("INTER_OUTCOME_SLEEP_S", "0.5"))

    # ── HTTP / retry ──────────────────────────────────────────────────────
    HTTP_TIMEOUT_PAIR: int = int(os.getenv("HTTP_TIMEOUT_PAIR", "15"))
    HTTP_TIMEOUT_CANDLES: int = int(os.getenv("HTTP_TIMEOUT_CANDLES", "20"))
    API_RETRY_ATTEMPTS: int = int(os.getenv("API_RETRY_ATTEMPTS", "4"))

    # ── Pump window / tweet search ────────────────────────────────────────
    PUMP_PEAK_OFFSET_MIN: int = int(os.getenv("PUMP_PEAK_OFFSET_MIN", "15"))
    DEFAULT_WINDOW_BEFORE_MIN: int = int(os.getenv("DEFAULT_WINDOW_BEFORE_MIN", "60"))
    DEFAULT_WINDOW_AFTER_MIN: int = int(os.getenv("DEFAULT_WINDOW_AFTER_MIN", "30"))
    DEFAULT_TWEET_LIMIT: int = int(os.getenv("DEFAULT_TWEET_LIMIT", "200"))
    TWEETS_PER_PAGE: int = int(os.getenv("TWEETS_PER_PAGE", "20"))
    MAX_SEARCH_PAGES: int = int(os.getenv("MAX_SEARCH_PAGES", "10"))

    # ── Caller scoring weights ────────────────────────────────────────────
    WEIGHT_FOLLOWERS: float = float(os.getenv("WEIGHT_FOLLOWERS", "0.4"))
    WEIGHT_LIKES: float = float(os.getenv("WEIGHT_LIKES", "0.4"))
    WEIGHT_RETWEETS: float = float(os.getenv("WEIGHT_RETWEETS", "0.2"))

    # ── Outcome classification thresholds ─────────────────────────────────
    THRESHOLD_MOONSHOT: float = float(os.getenv("THRESHOLD_MOONSHOT", "10.0"))
    THRESHOLD_BIG_WIN: float = float(os.getenv("THRESHOLD_BIG_WIN", "5.0"))
    THRESHOLD_WIN: float = float(os.getenv("THRESHOLD_WIN", "2.0"))
    THRESHOLD_RUG_DRAWDOWN: float = float(os.getenv("THRESHOLD_RUG_DRAWDOWN", "-0.50"))

    # ── Alerts ────────────────────────────────────────────────────────────
    ALERT_MIN_CALLER_SCORE: int = int(os.getenv("ALERT_MIN_CALLER_SCORE", "1000"))
    ALERT_MIN_WIN_RATE: float = float(os.getenv("ALERT_MIN_WIN_RATE", "0.5"))
    ALERT_QUIET_HOURS_START: int = int(os.getenv("ALERT_QUIET_HOURS_START", "23"))
    ALERT_QUIET_HOURS_END: int = int(os.getenv("ALERT_QUIET_HOURS_END", "7"))

    # ── Wallet linking (future) ───────────────────────────────────────────
    WALLET_LINKING_ENABLED: bool = (
        os.getenv("WALLET_LINKING_ENABLED", "true").lower() == "true"
    )
    WALLET_LINK_MIN_CALLS: int = int(os.getenv("WALLET_LINK_MIN_CALLS", "3"))
    WALLET_LINK_BUY_WINDOW_MIN: int = int(os.getenv("WALLET_LINK_BUY_WINDOW_MIN", "5"))

    @classmethod
    def validate(cls) -> None:
        """Warn about misconfigured values. Does not raise — just logs."""
        log = logging.getLogger("config")

        if cls.CANDLE_PROVIDER == "birdeye" and not cls.BIRDEYE_API_KEY:
            log.warning(
                "CANDLE_PROVIDER=birdeye but BIRDEYE_API_KEY is not set — "
                "candle fetches will fail"
            )

        weight_sum = cls.WEIGHT_FOLLOWERS + cls.WEIGHT_LIKES + cls.WEIGHT_RETWEETS
        if abs(weight_sum - 1.0) > 0.01:
            log.warning(
                f"Score weights sum to {weight_sum:.3f} (expected 1.0) — "
                "check WEIGHT_FOLLOWERS + WEIGHT_LIKES + WEIGHT_RETWEETS"
            )

        if cls.THRESHOLD_MOONSHOT <= cls.THRESHOLD_BIG_WIN:
            log.warning("THRESHOLD_MOONSHOT must be > THRESHOLD_BIG_WIN")
        if cls.THRESHOLD_BIG_WIN <= cls.THRESHOLD_WIN:
            log.warning("THRESHOLD_BIG_WIN must be > THRESHOLD_WIN")

        if cls.OUTCOME_CHECK_DELAY_MIN < 15:
            log.warning(
                f"OUTCOME_CHECK_DELAY_MIN={cls.OUTCOME_CHECK_DELAY_MIN} is very low — "
                "outcomes checked before enough price history exists"
            )
