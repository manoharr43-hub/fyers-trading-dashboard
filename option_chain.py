import streamlit as st
import pandas as pd
import time

# ==========================================================
# OPTION CHAIN - PART 1
# NSE AI PRO V12 Institutional Edition
# ==========================================================


def show_option_chain(fyers):

    st.title("📊 NSE AI PRO V12 - Institutional Option Chain")

    # ---------------------------------------
    # Sidebar Settings
    # ---------------------------------------

    st.sidebar.header("⚙️ Option Chain Settings")

    index = st.sidebar.selectbox(

        "Select Index",

        [
            "NIFTY",
            "BANKNIFTY",
            "FINNIFTY",
            "MIDCPNIFTY",
            "SENSEX",
            "BANKEX"
        ]

    )

    symbol_map = {

        "NIFTY": "NSE:NIFTY50-INDEX",

        "BANKNIFTY": "NSE:NIFTYBANK-INDEX",

        "FINNIFTY": "NSE:FINNIFTY-INDEX",

        "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",

        "SENSEX": "BSE:SENSEX-INDEX",

        "BANKEX": "BSE:BANKEX-INDEX"

    }

    strike_count = st.sidebar.slider(

        "Strike Count",

        min_value=5,

        max_value=30,

        value=10

    )

    auto_refresh = st.sidebar.checkbox(

        "Auto Refresh",

        value=False

    )

    refresh_time = st.sidebar.slider(

        "Refresh Seconds",

        5,

        60,

        10

    )

    # ---------------------------------------
    # Spot Price
    # ---------------------------------------

    st.subheader("📈 Spot Price")

    try:

        quote = fyers.quotes({

            "symbols": symbol_map[index]

        })

        if quote.get("s") == "ok":

            q = quote["d"][0]["v"]

            c1, c2, c3 = st.columns(3)

            c1.metric(
                "Spot",
                q.get("lp", 0),
                q.get("ch", 0)
            )

            c2.metric(
                "High",
                q.get("high_price", "-")
            )

            c3.metric(
                "Low",
                q.get("low_price", "-")
            )

        else:

            st.error(quote)

    except Exception as e:

        st.error(e)

    st.divider()

    # ---------------------------------------
    # Expiry Selection
    # ---------------------------------------

    st.subheader("📅 Expiry")

    expiry = st.selectbox(

        "Select Expiry",

        [

            "Current Expiry"

        ]

    )

    st.info(
        "Expiry list will be loaded automatically from FYERS API in Part 2."
    )

    st.divider()

    # ---------------------------------------
    # Placeholder
    # ---------------------------------------

    option_placeholder = st.empty()

    if auto_refresh:

        time.sleep(refresh_time)

        st.rerun()
