import streamlit as st
import pandas as pd
import time

# ==========================================================
# NSE AI PRO V12 INSTITUTIONAL
# OPTION CHAIN V2 - PART 1
# ==========================================================

def show_option_chain(fyers):

    st.title("📊 NSE AI PRO V12 Institutional Option Chain")

    # ==========================================
    # Sidebar
    # ==========================================

    st.sidebar.header("⚙ Option Chain Settings")

    asset_type = st.sidebar.radio(

        "Market",

        [

            "INDEX",

            "F&O STOCK"

        ]

    )

    # ==========================================
    # Index List
    # ==========================================

    index_symbols = {

        "NIFTY":"NSE:NIFTY50-INDEX",

        "BANKNIFTY":"NSE:NIFTYBANK-INDEX",

        "FINNIFTY":"NSE:FINNIFTY-INDEX",

        "MIDCPNIFTY":"NSE:MIDCPNIFTY-INDEX",

        "SENSEX":"BSE:SENSEX-INDEX",

        "BANKEX":"BSE:BANKEX-INDEX"

    }

    # ==========================================
    # Popular F&O Stocks
    # ==========================================

    fo_symbols = {

        "RELIANCE":"NSE:RELIANCE-EQ",

        "TCS":"NSE:TCS-EQ",

        "INFY":"NSE:INFY-EQ",

        "HDFCBANK":"NSE:HDFCBANK-EQ",

        "ICICIBANK":"NSE:ICICIBANK-EQ",

        "SBIN":"NSE:SBIN-EQ",

        "AXISBANK":"NSE:AXISBANK-EQ",

        "LT":"NSE:LT-EQ",

        "BHARTIARTL":"NSE:BHARTIARTL-EQ",

        "MARUTI":"NSE:MARUTI-EQ",

        "TATAMOTORS":"NSE:TATAMOTORS-EQ",

        "ADANIENT":"NSE:ADANIENT-EQ"

    }

    if asset_type=="INDEX":

        name = st.sidebar.selectbox(

            "Select Index",

            list(index_symbols.keys())

        )

        symbol = index_symbols[name]

    else:

        search = st.sidebar.text_input(

            "Search Stock"

        )

        stock_list = sorted(fo_symbols.keys())

        if search:

            stock_list = [

                x for x in stock_list

                if search.upper() in x

            ]

        name = st.sidebar.selectbox(

            "Select Stock",

            stock_list

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

        False

    )

    refresh_time = st.sidebar.slider(

        "Refresh Time",

        5,

        60,

        10

    )

    # ==========================================
    # Live Spot
    # ==========================================

    st.subheader("📈 Live Spot")

    try:

        quote = fyers.quotes({

            "symbols":symbol

        })

        q = quote["d"][0]["v"]

        spot = q["lp"]

        col1,col2,col3,col4 = st.columns(4)

        col1.metric(

            "Spot",

            spot,

            q["ch"]

        )

        col2.metric(

            "High",

            q["high_price"]

        )

        col3.metric(

            "Low",

            q["low_price"]

        )

        col4.metric(

            "Volume",

            f"{q['volume']:,}"

        )

    except Exception as e:

        st.error(e)

    st.divider()

    # ==========================================
    # Dynamic Expiry
    # ==========================================

    st.subheader("📅 Expiry")

    expiry_placeholder = st.empty()

    expiry = expiry_placeholder.selectbox(

        "Expiry",

        [

            "Loading Expiry..."

        ]

    )

    st.info(

        "✔ Live Expiry dates will load automatically in Part 2."

    )

    # ==========================================
    # ATM
    # ==========================================

    st.subheader("🎯 ATM Strike")

    atm_placeholder = st.empty()

    atm_placeholder.metric(

        "ATM Strike",

        "--"

    )

    st.divider()

    # ==========================================
    # Summary Cards
    # ==========================================

    c1,c2,c3,c4,c5,c6 = st.columns(6)

    c1.metric("Total CE OI","--")

    c2.metric("Total PE OI","--")

    c3.metric("CE OI Change","--")

    c4.metric("PE OI Change","--")

    c5.metric("PCR","--")

    c6.metric("Max Pain","--")

    st.divider()

    option_placeholder = st.empty()

    if auto_refresh:

        time.sleep(refresh_time)

        st.rerun()
