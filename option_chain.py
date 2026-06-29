import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import time

# ==========================================================
# NSE AI PRO V13 INSTITUTIONAL
# OPTION CHAIN PRO (FYERS API V3)
# PART 1
# ==========================================================

def show_option_chain(fyers):

    st.title("📊 NSE AI PRO V13 Institutional Option Chain")

    # ==========================================
    # Sidebar
    # ==========================================

    st.sidebar.header("⚙ Settings")

    market_type = st.sidebar.radio(
        "Market",
        ["INDEX", "F&O STOCK"]
    )

    index_symbols = {
        "NIFTY": "NSE:NIFTY50-INDEX",
        "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX",
        "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        "SENSEX": "BSE:SENSEX-INDEX",
        "BANKEX": "BSE:BANKEX-INDEX"
    }

    fo_symbols = {
        "RELIANCE": "NSE:RELIANCE-EQ",
        "TCS": "NSE:TCS-EQ",
        "INFY": "NSE:INFY-EQ",
        "HDFCBANK": "NSE:HDFCBANK-EQ",
        "ICICIBANK": "NSE:ICICIBANK-EQ",
        "SBIN": "NSE:SBIN-EQ",
        "AXISBANK": "NSE:AXISBANK-EQ",
        "LT": "NSE:LT-EQ",
        "MARUTI": "NSE:MARUTI-EQ",
        "BHARTIARTL": "NSE:BHARTIARTL-EQ",
        "TATAMOTORS": "NSE:TATAMOTORS-EQ",
        "ADANIENT": "NSE:ADANIENT-EQ"
    }

    if market_type == "INDEX":

        name = st.sidebar.selectbox(
            "Select Index",
            list(index_symbols.keys())
        )

        symbol = index_symbols[name]

    else:

        search = st.sidebar.text_input(
            "Search Stock"
        )

        stocks = sorted(fo_symbols.keys())

        if search:
            stocks = [
                x for x in stocks
                if search.upper() in x
            ]

        name = st.sidebar.selectbox(
            "Select Stock",
            stocks
        )

        symbol = fo_symbols[name]

    strike_count = st.sidebar.slider(
        "Strike Count",
        5,
        30,
        15
    )

    auto_refresh = st.sidebar.checkbox(
        "Auto Refresh",
        value=False
    )

    refresh_time = st.sidebar.slider(
        "Refresh Time (Seconds)",
        5,
        60,
        10
    )

    st.divider()

    # ==========================================
    # Live Spot Quote
    # ==========================================

    try:

        quote = fyers.quotes({
            "symbols": symbol
        })

        q = quote["d"][0]["v"]

        spot = float(q["lp"])

        c1, c2, c3, c4 = st.columns(4)

        c1.metric("Spot", spot, q["ch"])
        c2.metric("High", q["high_price"])
        c3.metric("Low", q["low_price"])
        c4.metric("Volume", f"{int(q['volume']):,}")

    except Exception as e:

        st.error(f"Quote Error : {e}")
        return

    st.divider()

    # ==========================================
    # Expiry Placeholder
    # ==========================================

    st.subheader("📅 Expiry")

    expiry = st.selectbox(
        "Expiry",
        ["Loading..."]
    )

    st.info(
        "Live expiry dates will load in Part 2."
    )

    st.divider()

    # ==========================================
    # Dashboard Cards
    # ==========================================

    a, b, c, d, e, f = st.columns(6)

    a.metric("CE OI", "--")
    b.metric("PE OI", "--")
    c.metric("PCR", "--")
    d.metric("ATM", "--")
    e.metric("Max Pain", "--")
    f.metric("AI Score", "--")

    st.divider()

    load = st.button(
        "🚀 Load Option Chain",
        use_container_width=True
    )

    if not load:
        return

    # ===== PART 2 STARTS BELOW =====
