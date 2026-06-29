import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime
import time

# ==========================================================
# NSE AI PRO V13 INSTITUTIONAL
# ADVANCED AI SCANNER
# PART 1
# ==========================================================

def show_scanner(fyers):

    st.title("🚀 NSE AI PRO V13 Institutional Scanner")

    # ======================================================
    # Sidebar
    # ======================================================

    st.sidebar.header("⚙ Scanner Filters")

    scanner_type = st.sidebar.selectbox(

        "Scanner Type",

        [

            "AI Institutional",

            "Intraday",

            "Swing",

            "Momentum",

            "Breakout",

            "Volume Breakout",

            "Golden Cross",

            "Smart Money",

            "52 Week High",

            "52 Week Low"

        ]

    )

    market = st.sidebar.selectbox(

        "Market",

        [

            "NIFTY50",

            "NIFTY100",

            "NIFTY200",

            "NIFTY500",

            "F&O"

        ]

    )

    min_rvol = st.sidebar.slider(

        "Minimum RVOL",

        1.0,

        10.0,

        2.0,

        0.1

    )

    min_ai = st.sidebar.slider(

        "Minimum AI Score",

        0,

        100,

        70

    )

    only_golden = st.sidebar.checkbox(

        "Golden Cross Only"

    )

    only_smart = st.sidebar.checkbox(

        "Smart Money Only"

    )

    only_52high = st.sidebar.checkbox(

        "Near 52 Week High"

    )

    only_fnO = st.sidebar.checkbox(

        "F&O Stocks Only"

    )

    auto_refresh = st.sidebar.checkbox(

        "Auto Refresh"

    )

    refresh = st.sidebar.slider(

        "Refresh Seconds",

        5,

        60,

        10

    )

    st.divider()

    # ======================================================
    # Dashboard
    # ======================================================

    c1,c2,c3,c4,c5,c6 = st.columns(6)

    c1.metric("Stocks","0")

    c2.metric("BUY","0")

    c3.metric("SELL","0")

    c4.metric("Golden","0")

    c5.metric("Smart","0")

    c6.metric("52W High","0")

    st.divider()

    st.subheader("📊 AI Scanner")

    scan_button = st.button(

        "🚀 Run AI Scanner",

        use_container_width=True

    )

    result_placeholder = st.empty()

    if auto_refresh:

        time.sleep(refresh)

        st.rerun()
# ==========================================================
# NSE AI PRO V13 INSTITUTIONAL
# PART 2
# LIVE DATA | RVOL | EMA | GOLDEN CROSS | 52 WEEK
# ==========================================================

    if scan_button:

        progress = st.progress(0)

        scanner_data = []

        # ==========================================
        # Sample Symbols
        # (Later replace with NIFTY500/F&O list)
        # ==========================================

        symbols = [

            "NSE:RELIANCE-EQ",

            "NSE:TCS-EQ",

            "NSE:INFY-EQ",

            "NSE:HDFCBANK-EQ",

            "NSE:ICICIBANK-EQ"

        ]

        total = len(symbols)

        for i, symbol in enumerate(symbols):

            try:

                history = fyers.history({

                    "symbol": symbol,

                    "resolution": "D",

                    "date_format": "1",

                    "range_from": "2025-01-01",

                    "range_to": "2026-12-31",

                    "cont_flag": "1"

                })

                if history.get("s") != "ok":

                    continue

                candles = history["candles"]

                df = pd.DataFrame(

                    candles,

                    columns=[

                        "Time",

                        "Open",

                        "High",

                        "Low",

                        "Close",

                        "Volume"

                    ]

                )

                if len(df) < 220:

                    continue

                close = df["Close"]

                volume = df["Volume"]

                # =====================================
                # EMA
                # =====================================

                ema20 = close.ewm(
                    span=20,
                    adjust=False
                ).mean().iloc[-1]

                ema50 = close.ewm(
                    span=50,
                    adjust=False
                ).mean().iloc[-1]

                ema200 = close.ewm(
                    span=200,
                    adjust=False
                ).mean().iloc[-1]

                golden_cross = ema50 > ema200

                death_cross = ema50 < ema200

                # =====================================
                # RVOL
                # =====================================

                avg_vol = volume.tail(20).mean()

                rvol = volume.iloc[-1] / avg_vol if avg_vol else 0

                # =====================================
                # 52 Week
                # =====================================

                high52 = df["High"].tail(252).max()

                low52 = df["Low"].tail(252).min()

                last
