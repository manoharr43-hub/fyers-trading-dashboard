"""
Live Market Analysis (Quote / Market Depth / Historical Data)
================================================================
Streamlit module for a single-symbol FYERS lookup panel: LTP quote,
market depth (bid/ask), and historical OHLCV with a chart.

FIX NOTE (this file):
  Every FYERS SDK call in the original version passed the request body
  as a positional argument — e.g. fyers.quotes({"symbols": symbol}),
  fyers.depth({...}), fyers.history({...}). The fyers-apiv3 SDK's
  methods expect the request body as the `data=` keyword argument (this
  matches how every other FYERS call in this project — optionchain(),
  quotes(), history() — is already invoked elsewhere). Passing it
  positionally either raises a TypeError against some SDK versions or
  silently gets ignored/misrouted against others, so quotes/depth/
  history could fail or return unexpected results depending on the
  installed SDK version. Fixed by calling every endpoint with
  `data=...` explicitly.

Other fixes in this version:
  • Defensive None/type checks before calling .get()/dict-membership on
    an API response — a dropped connection or auth failure can return
    None instead of a dict, which would otherwise raise an unhandled
    AttributeError/TypeError and crash the button's callback.
  • Error messages now surface the FYERS API's own `message`/`code`
    fields instead of a generic "Failed to fetch ..." string, so a
    real cause (auth expired, invalid symbol, etc.) is visible.
  • The historical-data range was hardcoded to a fixed calendar year
    ("2026-01-01" to "2026-12-31"), which silently breaks once the
    real year rolls over and also always requests future dates with
    no data. Replaced with a rolling "last N days up to today" range,
    with the day count adjustable in the UI.
  • Logging added at every stage (request, success, failure) using the
    same logger name/format convention as the rest of this project, so
    log output from this module and option_chain.py interleave cleanly.
  • Spinners added for user feedback while a request is in flight.
"""

import logging
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

# Reuses the same logger name as option_chain.py so log lines from both
# modules interleave in one stream if they're run in the same process.
# Safe to import this module standalone too — the handler is only added
# once, guarded the same way as in option_chain.py.
logger = logging.getLogger("options_chain_dashboard")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def _api_error_message(response) -> str:
    """Extracts a human-readable reason from a FYERS error response,
    falling back gracefully if the response isn't a dict or doesn't
    carry the usual code/message fields."""
    if not isinstance(response, dict):
        return "No response received from FYERS (connection issue, timeout, or invalid session)."
    code = response.get("code", "—")
    message = response.get("message", "Unknown error")
    return f"FYERS API error (code {code}): {message}"


def show_market(fyers):
    st.title("📊 Live Market Analysis")

    symbol = st.text_input("Enter Symbol (e.g., NSE:RELIANCE-EQ)", "NSE:RELIANCE-EQ").strip().upper()
    col1, col2 = st.columns(2)

    # ── 1. GET QUOTE ─────────────────────────────────────────────────────
    with col1:
        if st.button("📈 Get Quote", use_container_width=True):
            if not symbol:
                st.warning("Enter a symbol first.")
            else:
                logger.info("Quote: requesting %s", symbol)
                with st.spinner(f"Fetching quote for {symbol} …"):
                    try:
                        response = fyers.quotes(data={"symbols": symbol})
                    except Exception as e:  # noqa: BLE001 - external SDK, keep resilient
                        logger.error("Quote: exception fetching %s: %s", symbol, e)
                        st.error(f"Error fetching quote: {e}")
                        response = None

                if isinstance(response, dict) and response.get("s") == "ok":
                    d_list = response.get("d") or []
                    if d_list and isinstance(d_list[0], dict):
                        data = d_list[0].get("v", {}) or {}
                        logger.info("Quote: success for %s (LTP=%s)", symbol, data.get("lp"))
                        st.success(f"LTP: {data.get('lp', '—')}")
                        st.json(data)
                    else:
                        logger.error("Quote: 'ok' response but no data payload for %s", symbol)
                        st.warning("FYERS returned no quote data for this symbol.")
                else:
                    reason = _api_error_message(response)
                    logger.error("Quote: failed for %s — %s", symbol, reason)
                    st.error(f"Failed to fetch quote. {reason}")

    # ── 2. MARKET DEPTH ──────────────────────────────────────────────────
    with col2:
        if st.button("📚 Market Depth", use_container_width=True):
            if not symbol:
                st.warning("Enter a symbol first.")
            else:
                logger.info("Depth: requesting %s", symbol)
                # ohlcv_flag "0" returns the depth payload without the extra
                # OHLCV block mixed in, which keeps the bid/ask table clean.
                with st.spinner(f"Fetching market depth for {symbol} …"):
                    try:
                        depth_resp = fyers.depth(data={"symbol": symbol, "ohlcv_flag": "0"})
                    except Exception as e:  # noqa: BLE001
                        logger.error("Depth: exception fetching %s: %s", symbol, e)
                        st.error(f"Error fetching market depth: {e}")
                        depth_resp = None

                if isinstance(depth_resp, dict) and depth_resp.get("s") == "ok":
                    data = depth_resp.get("d", {}) or {}
                    # FYERS nests per-symbol depth under the symbol key on
                    # some SDK/API versions and flat on others — try both.
                    if symbol in data and isinstance(data[symbol], dict):
                        data = data[symbol]

                    bids = data.get("bids") or []
                    asks = data.get("ask") or data.get("asks") or []

                    if bids or asks:
                        logger.info("Depth: success for %s (%d bids, %d asks)", symbol, len(bids), len(asks))
                        depth_col1, depth_col2 = st.columns(2)
                        with depth_col1:
                            st.write("**Bids (Buyers)**")
                            if bids:
                                st.dataframe(pd.DataFrame(bids), use_container_width=True)
                            else:
                                st.caption("No bid data available.")
                        with depth_col2:
                            st.write("**Asks (Sellers)**")
                            if asks:
                                st.dataframe(pd.DataFrame(asks), use_container_width=True)
                            else:
                                st.caption("No ask data available.")
                    else:
                        logger.error("Depth: 'ok' response but no bids/asks for %s", symbol)
                        st.warning("Market Depth data currently unavailable (market may be closed, or no live quotes for this symbol).")
                else:
                    reason = _api_error_message(depth_resp)
                    logger.error("Depth: failed for %s — %s", symbol, reason)
                    st.error(f"Could not fetch market depth. {reason}")

    # ── 3. HISTORICAL DATA ───────────────────────────────────────────────
    st.divider()
    st.subheader("📊 Historical Data")

    lookback_days = st.slider("Lookback (days)", min_value=7, max_value=730, value=180, step=7)

    if st.button("Load History", use_container_width=True):
        if not symbol:
            st.warning("Enter a symbol first.")
        else:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=lookback_days)
            logger.info(
                "History: requesting %s from %s to %s",
                symbol, start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"),
            )
            with st.spinner(f"Loading {lookback_days} days of history for {symbol} …"):
                try:
                    history = fyers.history(data={
                        "symbol": symbol, "resolution": "D",
                        "date_format": "1",
                        "range_from": start_date.strftime("%Y-%m-%d"),
                        "range_to": end_date.strftime("%Y-%m-%d"),
                        "cont_flag": "1",
                    })
                except Exception as e:  # noqa: BLE001
                    logger.error("History: exception fetching %s: %s", symbol, e)
                    st.error(f"Error fetching history: {e}")
                    history = None

            candles = history.get("candles") if isinstance(history, dict) else None

            if candles:
                df = pd.DataFrame(candles, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
                df["Timestamp"] = pd.to_datetime(df["Timestamp"], unit="s")

                logger.info("History: success for %s — %d candles", symbol, len(df))
                st.dataframe(df, use_container_width=True)
                st.line_chart(df.set_index("Timestamp")["Close"])
            elif isinstance(history, dict) and history.get("s") != "ok":
                reason = _api_error_message(history)
                logger.error("History: failed for %s — %s", symbol, reason)
                st.error(f"Could not load historical data. {reason}")
            else:
                logger.info("History: no candles returned for %s in the selected range", symbol)
                st.info("No historical data found for this symbol/date range.")
