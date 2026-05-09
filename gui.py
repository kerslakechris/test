#!/usr/bin/env python3
"""Streamlit GUI for Solana Token Early Caller Finder."""

import asyncio
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Solana Early Callers",
    layout="wide",
    initial_sidebar_state="collapsed",
)

from solana_callers import (
    COOKIES_FILE,
    WATCHLIST_FILE,
    fetch_dexscreener_data,
    get_twikit_client,
    load_watchlist,
    parse_pump_timestamp,
    save_csv,
    save_watchlist,
    score_and_rank,
    scrape_tweets,
    update_watchlist,
)
from outcomes import update_pending_outcomes


# ── Helpers ─────────────────────────────────────────────────


def run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def capture_async(coro):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        result = run_async(coro)
    except SystemExit as e:
        result = None
        buf.write(f"\nExit: {e}\n")
    except Exception as e:
        result = None
        buf.write(f"\nError: {e}\n")
    finally:
        sys.stdout = old
    return result, buf.getvalue()


def format_number(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


# ── Layout ──────────────────────────────────────────────────

st.title("Solana Token Early Caller Finder")
st.caption(
    "Find influential X accounts that posted a token's CA before or during the pump"
)

tab_lookup, tab_watchlist, tab_outcomes, tab_settings = st.tabs(
    ["Token Lookup", "Watchlist", "Update Outcomes", "Settings"]
)


# ═════════════════════════════════════════════════════════════
#  TOKEN LOOKUP
# ═════════════════════════════════════════════════════════════

with tab_lookup:
    if not Path(COOKIES_FILE).exists():
        st.warning(
            "No cookies.json found. Go to the **Settings** tab to set up X authentication first."
        )

    col_input, col_opts = st.columns([3, 1])

    with col_input:
        ca = st.text_input(
            "Contract Address",
            placeholder="Paste Solana token CA here...",
            key="ca_input",
        )

    with col_opts:
        top_n = st.number_input("Top callers", min_value=1, max_value=100, value=10)
        output_file = st.text_input("Output CSV", value="callers.csv")

    with st.expander("Advanced Options"):
        pump_time_str = st.text_input(
            "Manual pump time (ISO 8601)",
            placeholder="e.g. 2026-03-23T01:39:00Z",
            help="Set manually when the token is no longer listed on DexScreener.",
        )
        auto_outcomes = st.checkbox(
            "Auto-check outcomes after search",
            value=os.getenv("OUTCOME_CHECK_ENABLED", "true").lower() == "true",
        )

    search_btn = st.button(
        "Search", type="primary", disabled=not ca, use_container_width=True
    )

    if search_btn and ca:

        async def do_search():
            created_at = None
            pump_peak = None

            if pump_time_str:
                try:
                    created_at = datetime.fromisoformat(
                        pump_time_str.replace("Z", "+00:00")
                    )
                except ValueError:
                    print(f"Invalid pump time format: {pump_time_str}")
                    return None
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
                        "[!] Token not found on DexScreener. "
                        "Use manual pump time or search all tweets."
                    )

            client = await get_twikit_client()
            if created_at and pump_peak:
                window_start = created_at - timedelta(hours=1)
                window_end = pump_peak + timedelta(minutes=30)
            else:
                window_start = datetime.min.replace(tzinfo=timezone.utc)
                window_end = datetime.now(tz=timezone.utc)

            tweets = await scrape_tweets(client, ca, window_start, window_end)
            if not tweets:
                print("No tweets found.")
                return None

            ranked = score_and_rank(
                tweets, pump_peak or datetime.now(tz=timezone.utc)
            )
            save_csv(ranked, output_file)
            watchlist = update_watchlist(ranked, ca)

            if auto_outcomes:
                print("\n[*] Checking outcomes for eligible calls...")
                watchlist = await update_pending_outcomes(watchlist)
                save_watchlist(watchlist)

            return ranked

        with st.status("Searching...", expanded=True) as status:
            results, log = capture_async(do_search())
            st.code(log, language="text")
            if results:
                status.update(
                    label=f"Found {len(results)} callers", state="complete"
                )
            else:
                status.update(label="Search completed", state="error")

        if results:
            st.session_state["last_results"] = results

    if "last_results" in st.session_state and st.session_state["last_results"]:
        results = st.session_state["last_results"]

        st.divider()
        st.subheader(f"Top {min(top_n, len(results))} Callers")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Found", len(results))
        m2.metric("Highest Score", f"{results[0]['score']:,.1f}")
        m3.metric("Top Followers", format_number(results[0]["followers"]))
        avg_score = sum(r["score"] for r in results) / len(results)
        m4.metric("Avg Score", f"{avg_score:,.1f}")

        display_cols = [
            "username",
            "followers",
            "tweet_time",
            "likes",
            "retweets",
            "score",
            "tweet_url",
        ]
        df = pd.DataFrame(results[:top_n])
        df = df[[c for c in display_cols if c in df.columns]].copy()
        if "username" in df.columns:
            df["username"] = df["username"].apply(lambda x: f"@{x}")

        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "username": st.column_config.TextColumn("Caller", width="medium"),
                "followers": st.column_config.NumberColumn("Followers", format="%d"),
                "tweet_time": st.column_config.TextColumn("Tweet Time"),
                "likes": st.column_config.NumberColumn("Likes"),
                "retweets": st.column_config.NumberColumn("RTs"),
                "score": st.column_config.NumberColumn("Score", format="%.1f"),
                "tweet_url": st.column_config.LinkColumn("Tweet", display_text="View"),
            },
        )


# ═════════════════════════════════════════════════════════════
#  WATCHLIST
# ═════════════════════════════════════════════════════════════

with tab_watchlist:
    watchlist = load_watchlist()

    if not watchlist:
        st.info("Watchlist is empty. Run a token lookup first to start building it.")
    else:
        f1, f2, f3, f4 = st.columns(4)
        with f1:
            sort_by = st.selectbox(
                "Sort by",
                ["tokens_called", "win_rate", "avg_multiple", "total_score"],
                format_func=lambda x: x.replace("_", " ").title(),
            )
        with f2:
            min_calls = st.number_input("Min completed calls", min_value=0, value=0)
        with f3:
            min_wr = st.slider("Min win rate", 0.0, 1.0, 0.0, 0.05)
        with f4:
            show_n = st.number_input(
                "Show top N", min_value=1, max_value=500, value=25
            )

        items = list(watchlist.items())
        if min_calls > 0:
            items = [
                (u, d)
                for u, d in items
                if d.get("completed_calls", 0) >= min_calls
            ]
        if min_wr > 0:
            items = [
                (u, d) for u, d in items if d.get("win_rate", 0) >= min_wr
            ]

        sort_keys = {
            "tokens_called": lambda kv: (
                kv[1].get("tokens_called", 0),
                kv[1].get("avg_score", 0),
            ),
            "win_rate": lambda kv: (
                kv[1].get("win_rate", 0),
                kv[1].get("avg_multiple_24h", 0),
            ),
            "avg_multiple": lambda kv: kv[1].get("avg_multiple_24h", 0),
            "total_score": lambda kv: kv[1].get("total_score", 0),
        }
        items.sort(
            key=sort_keys.get(sort_by, sort_keys["tokens_called"]), reverse=True
        )
        items = items[:show_n]

        if not items:
            st.warning("No callers match the current filters.")
        else:
            has_outcomes = any(
                d.get("completed_calls", 0) > 0 for _, d in items
            )

            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric("Total Callers", len(watchlist))
            mc2.metric("Showing", len(items))
            if has_outcomes:
                wr_vals = [
                    d.get("win_rate", 0)
                    for _, d in items
                    if d.get("completed_calls", 0) > 0
                ]
                mc3.metric(
                    "Avg Win Rate",
                    f"{(sum(wr_vals) / len(wr_vals) * 100) if wr_vals else 0:.0f}%",
                )
                mc4.metric(
                    "Total Rated",
                    sum(d.get("completed_calls", 0) for _, d in items),
                )
            else:
                total_tokens = sum(
                    d.get("tokens_called", 0) for _, d in items
                )
                mc3.metric("Total Tokens Called", total_tokens)

            rows = []
            for username, data in items:
                row = {
                    "Caller": f"@{username}",
                    "Tokens": data.get("tokens_called", 0),
                    "Calls": len(data.get("calls", [])),
                }
                if has_outcomes:
                    row["Win Rate"] = f"{data.get('win_rate', 0):.0%}"
                    row["Avg 24h"] = f"{data.get('avg_multiple_24h', 0):.1f}x"
                    row["Median 24h"] = (
                        f"{data.get('median_multiple_24h', 0):.1f}x"
                    )
                    row["Rated"] = data.get("completed_calls", 0)
                    row["Wins"] = (
                        data.get("wins", 0)
                        + data.get("big_wins", 0)
                        + data.get("moonshots", 0)
                    )
                    row["Losses"] = data.get("losses", 0)
                    row["Rugs"] = data.get("rugs", 0)
                    best = data.get("best_call")
                    row["Best"] = (
                        f"{best['multiple']:.1f}x" if best else "-"
                    )
                else:
                    row["Avg Score"] = f"{data.get('avg_score', 0):.1f}"
                    row["Total Score"] = f"{data.get('total_score', 0):.1f}"
                row["First Seen"] = data.get("first_seen", "-")[:10]
                row["Last Seen"] = data.get("last_seen", "-")[:10]
                rows.append(row)

            st.dataframe(
                pd.DataFrame(rows), use_container_width=True, hide_index=True
            )

            st.divider()
            st.subheader("Caller Details")

            for username, data in items:
                calls = data.get("calls", [])
                label = f"@{username} — {data.get('tokens_called', 0)} tokens"
                if has_outcomes and data.get("completed_calls", 0) > 0:
                    label += f" | WR {data.get('win_rate', 0):.0%} | Avg {data.get('avg_multiple_24h', 0):.1f}x"

                with st.expander(label):
                    d1, d2, d3, d4 = st.columns(4)
                    d1.metric("First Seen", data.get("first_seen", "-")[:10])
                    d2.metric("Last Seen", data.get("last_seen", "-")[:10])
                    d3.metric(
                        "Win Rate", f"{data.get('win_rate', 0):.0%}"
                    )
                    d4.metric(
                        "Avg Multiple",
                        f"{data.get('avg_multiple_24h', 0):.1f}x",
                    )

                    best = data.get("best_call")
                    worst = data.get("worst_call")
                    if best or worst:
                        bc, wc = st.columns(2)
                        if best:
                            bc.metric(
                                "Best Call",
                                f"{best['multiple']:.1f}x",
                                help=best.get("ca", "")[:16],
                            )
                        if worst:
                            wc.metric(
                                "Worst Call",
                                f"{worst['multiple']:.1f}x",
                                help=worst.get("ca", "")[:16],
                            )

                    if calls:
                        call_rows = []
                        for c in calls:
                            outcome = c.get("outcome") or {}
                            status = outcome.get("status", "pending")
                            m24 = (
                                outcome.get("windows", {})
                                .get("24h", {})
                                .get("multiple")
                            )
                            ath = outcome.get("ath", {}).get("multiple")
                            entry_p = outcome.get("entry_price")
                            call_rows.append(
                                {
                                    "CA": c.get("ca", "")[:16] + "...",
                                    "Tweet Time": c.get("tweet_time", "")[
                                        :19
                                    ],
                                    "Score": round(c.get("score", 0), 1),
                                    "Followers": c.get("followers", 0),
                                    "Status": status,
                                    "24h": (
                                        f"{m24:.1f}x"
                                        if isinstance(m24, (int, float))
                                        else "-"
                                    ),
                                    "ATH": (
                                        f"{ath:.1f}x"
                                        if isinstance(ath, (int, float))
                                        else "-"
                                    ),
                                    "Entry": (
                                        f"{entry_p}"
                                        if entry_p is not None
                                        else "-"
                                    ),
                                    "Tweet": c.get("tweet_url", ""),
                                }
                            )
                        st.dataframe(
                            pd.DataFrame(call_rows),
                            use_container_width=True,
                            hide_index=True,
                            column_config={
                                "Tweet": st.column_config.LinkColumn(
                                    "Tweet", display_text="View"
                                ),
                            },
                        )


# ═════════════════════════════════════════════════════════════
#  UPDATE OUTCOMES
# ═════════════════════════════════════════════════════════════

with tab_outcomes:
    watchlist_out = load_watchlist()

    if not watchlist_out:
        st.info("Watchlist is empty. Run a token lookup first.")
    else:
        total_calls = sum(
            len(d.get("calls", [])) for d in watchlist_out.values()
        )
        pending = sum(
            1
            for d in watchlist_out.values()
            for c in d.get("calls", [])
            if not c.get("outcome")
            or c.get("outcome", {}).get("status") in ("pending", None)
        )
        completed = sum(
            d.get("completed_calls", 0) for d in watchlist_out.values()
        )

        oc1, oc2, oc3 = st.columns(3)
        oc1.metric("Total Calls", total_calls)
        oc2.metric("Pending", pending)
        oc3.metric("Completed", completed)

        st.divider()

        o1, o2, o3 = st.columns(3)
        with o1:
            usernames = ["All"] + sorted(watchlist_out.keys())
            user_filter = st.selectbox("Filter by caller", usernames)
        with o2:
            batch_size = st.number_input(
                "Batch size (0 = unlimited)", min_value=0, value=0
            )
        with o3:
            force = st.checkbox("Force re-check all (not just pending)")

        update_btn = st.button(
            "Update Outcomes", type="primary", use_container_width=True
        )

        if update_btn:

            async def do_update():
                wl = load_watchlist()
                uf = None if user_filter == "All" else user_filter
                wl = await update_pending_outcomes(
                    wl, user_filter=uf, force=force, batch_size=batch_size
                )
                save_watchlist(wl)
                return wl

            with st.status("Updating outcomes...", expanded=True) as status:
                result, log = capture_async(do_update())
                st.code(log, language="text")
                if result is not None:
                    status.update(
                        label="Outcomes updated", state="complete"
                    )
                else:
                    status.update(label="Update failed", state="error")

            if result is not None:
                st.success("Watchlist saved with updated outcomes.")

        # Outcome breakdown
        if completed > 0:
            st.divider()
            st.subheader("Outcome Breakdown")

            moonshots = sum(
                d.get("moonshots", 0) for d in watchlist_out.values()
            )
            big_wins = sum(
                d.get("big_wins", 0) for d in watchlist_out.values()
            )
            wins = sum(d.get("wins", 0) for d in watchlist_out.values())
            losses = sum(d.get("losses", 0) for d in watchlist_out.values())
            rugs = sum(d.get("rugs", 0) for d in watchlist_out.values())

            b1, b2, b3, b4, b5 = st.columns(5)
            b1.metric("Moonshots (10x+)", moonshots)
            b2.metric("Big Wins (5x+)", big_wins)
            b3.metric("Wins (2x+)", wins)
            b4.metric("Losses (<1.5x)", losses)
            b5.metric("Rugs", rugs)


# ═════════════════════════════════════════════════════════════
#  SETTINGS
# ═════════════════════════════════════════════════════════════

with tab_settings:
    st.subheader("X (Twitter) Authentication")

    cookies_exist = Path(COOKIES_FILE).exists()
    if cookies_exist:
        st.success(f"{COOKIES_FILE} exists")
        with open(COOKIES_FILE) as f:
            cookie_data = json.load(f)
        cookie_keys = list(cookie_data.keys())
        st.text(f"Cookie keys present: {', '.join(cookie_keys)}")
        if st.button("Delete cookies"):
            os.remove(COOKIES_FILE)
            st.rerun()
    else:
        st.warning(f"No {COOKIES_FILE} found")

    st.markdown("---")
    st.markdown("**Import cookies from your browser:**")
    st.markdown(
        "1. Log in to x.com in your browser\n"
        "2. Open DevTools (F12) > Application > Cookies > `https://x.com`\n"
        "3. Copy the values below"
    )

    with st.form("cookie_form"):
        auth_token = st.text_input("auth_token (required)", type="password")
        ct0 = st.text_input("ct0 (required)", type="password")
        kdt = st.text_input("kdt (optional)")
        twid = st.text_input("twid (optional)")
        guest_id = st.text_input("guest_id (optional)")
        save_btn = st.form_submit_button("Save Cookies", type="primary")

        if save_btn:
            if not auth_token or not ct0:
                st.error("auth_token and ct0 are both required.")
            else:
                cookies = {"auth_token": auth_token, "ct0": ct0}
                for key, val in [
                    ("kdt", kdt),
                    ("twid", twid),
                    ("guest_id", guest_id),
                ]:
                    if val:
                        cookies[key] = val
                with open(COOKIES_FILE, "w") as f:
                    json.dump(cookies, f, indent=2)
                st.success(f"Cookies saved to {COOKIES_FILE}")
                st.rerun()

    st.divider()
    st.subheader("Environment Configuration")

    env_path = Path(".env")
    env_example_path = Path(".env.example")

    if env_path.exists():
        with open(env_path) as f:
            env_lines = f.readlines()
        safe_lines = []
        for line in env_lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0]
                if key in ("X_PASSWORD",):
                    safe_lines.append(f"{key}=********\n")
                else:
                    safe_lines.append(line)
            else:
                safe_lines.append(line)
        st.code("".join(safe_lines), language="bash")
    else:
        st.info("No .env file found.")
        if env_example_path.exists():
            st.markdown("Create one from the template:")
            st.code("cp .env.example .env", language="bash")

    st.divider()
    st.subheader("File Status")

    file_checks = [
        ("solana_callers.py", "Main script"),
        ("outcomes.py", "Outcome tracking"),
        ("requirements.txt", "Dependencies"),
        (COOKIES_FILE, "X session cookies"),
        (WATCHLIST_FILE, "Persistent caller database"),
        (".env", "Credentials and config"),
    ]
    for fname, desc in file_checks:
        exists = Path(fname).exists()
        icon = "+" if exists else "-"
        status_text = "found" if exists else "missing"
        st.text(f"  [{icon}] {fname:<25} {desc:<35} {status_text}")
