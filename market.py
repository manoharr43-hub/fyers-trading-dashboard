import streamlit as st
import pandas as pd

from ai_market_intelligence import show_ai_market_intelligence


# =============================================================================
# ORIGINAL FEATURE: Quote / Market Depth / Historical Data
# (Untouched — exactly as in your existing app, just kept under its own name
#  so it isn't overwritten by the new AI wrapper below.)
# =============================================================================

def show_market_tools(fyers):
    st.title("📊 Live Market Analysis")

    symbol = st.text_input("Enter Symbol (e.g., NSE:RELIANCE-EQ)", "NSE:RELIANCE-EQ")
    col1, col2 = st.columns(2)

    # 1. GET QUOTE
    with col1:
        if st.button("📈 Get Quote", use_container_width=True):
            try:
                response = fyers.quotes({"symbols": symbol})
                if response.get("s") == "ok":
                    data = response["d"][0]["v"]
                    st.success(f"LTP: {data.get('lp')}")
                    st.json(data)
                else:
                    st.error("Failed to fetch quote.")
            except Exception as e:
                st.error(f"Error: {e}")

    # 2. MARKET DEPTH
    with col2:
        if st.button("📚 Market Depth", use_container_width=True):
            try:
                depth_resp = fyers.depth({"symbol": symbol, "ohlcv_flag": "0"})

                if depth_resp.get("s") == "ok":
                    data = depth_resp.get("d", {})
                    if "bids" in data and data["bids"]:
                        bid_df = pd.DataFrame(data["bids"])
                        st.write("**Bids (Buyers)**")
                        st.dataframe(bid_df, use_container_width=True)
                    else:
                        st.warning("Market Depth data currently unavailable.")
                else:
                    st.error("Could not fetch market depth.")
            except Exception as e:
                st.error(f"Error: {e}")

    # 3. HISTORICAL DATA
    st.divider()
    st.subheader("📊 Historical Data")
    if st.button("Load History", use_container_width=True):
        try:
            history = fyers.history({
                "symbol": symbol, "resolution": "D",
                "date_format": "1", "range_from": "2026-01-01",
                "range_to": "2026-12-31", "cont_flag": "1"
            })

            if "candles" in history:
                df = pd.DataFrame(history["candles"], columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])
                df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='s')
                st.dataframe(df, use_container_width=True)
                st.line_chart(df.set_index("Timestamp")["Close"])
            else:
                st.info("No historical data found.")
        except Exception as e:
            st.error(f"Error: {e}")


# =============================================================================
# NEW FEATURE: Today's AI Market Summary (quick index snapshot)
# =============================================================================

def market_summary(fyers):
    st.subheader("📊 Today's AI Market Summary")
    symbols = {
        "NIFTY 50": "NSE:NIFTY50-INDEX",
        "BANK NIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX",
        "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        "INDIA VIX": "NSE:INDIAVIX-INDEX"
    }
    rows = []
    for name, symbol in symbols.items():
        try:
            q = fyers.quotes({"symbols": symbol})
            if q["s"] == "ok":
                d = q["d"][0]["v"]
                ltp = d.get("lp", 0)
                openp = d.get("open_price", ltp)
                change = round(ltp - openp, 2)
                if change > 0:
                    trend = "🟢 UP"
                elif change < 0:
                    trend = "🔴 DOWN"
                else:
                    trend = "🟡 NEUTRAL"
                rows.append({
                    "Index": name,
                    "LTP": ltp,
                    "Change": change,
                    "Trend": trend
                })
        except Exception:
            pass

    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True)

    if df.empty:
        st.info("No index data available right now.")
        return

    up = len(df[df.Trend == "🟢 UP"])
    down = len(df[df.Trend == "🔴 DOWN"])
    neutral = len(df[df.Trend == "🟡 NEUTRAL"])
    total = max(len(df), 1)
    up_per = round(up / total * 100)
    down_per = round(down / total * 100)
    neu_per = round(neutral / total * 100)

    c1, c2, c3 = st.columns(3)
    c1.metric("📈 UP", f"{up_per}%")
    c2.metric("📉 DOWN", f"{down_per}%")
    c3.metric("⚖️ NEUTRAL", f"{neu_per}%")

    if up_per > 60:
        st.success("🟢 AI VIEW : Market Bullish")
    elif down_per > 60:
        st.error("🔴 AI VIEW : Market Bearish")
    else:
        st.warning("🟡 AI VIEW : Sideways / Neutral")


# =============================================================================
# MAIN ENTRY POINT — call this one function from app.py
# Keeps everything: original tools + new AI summary + full AI Market
# Intelligence module, as separate tabs. Nothing is removed.
# =============================================================================

def show_market(fyers):
    """
    Single entry point for app.py.
    Replaces the old show_market() call 1-for-1 — no other changes needed
    in app.py.
    """
    tab1, tab2, tab3 = st.tabs([
        "🛠️ Market Tools",
        "📊 AI Market Summary",
        "🧠 AI Market Intelligence",
    ])

    with tab1:
        show_market_tools(fyers)

    with tab2:
        market_summary(fyers)

    with tab3:
        show_ai_market_intelligence(fyers)
