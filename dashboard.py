import streamlit as st
import pandas as pd


def show_dashboard(fyers):

    st.header("📈 FYERS Trading Dashboard")

    # ===============================
    # MARKET OVERVIEW
    # ===============================
    st.subheader("📊 Market Overview")

    col1, col2, col3 = st.columns(3)

    try:
        quote = fyers.quotes({
            "symbols": "NSE:NIFTY50-INDEX,NSE:NIFTYBANK-INDEX,NSE:FINNIFTY-INDEX"
        })

        data = quote["d"]

        col1.metric(
            "NIFTY 50",
            data[0]["v"]["lp"],
            data[0]["v"]["ch"]
        )

        col2.metric(
            "BANKNIFTY",
            data[1]["v"]["lp"],
            data[1]["v"]["ch"]
        )

        col3.metric(
            "FINNIFTY",
            data[2]["v"]["lp"],
            data[2]["v"]["ch"]
        )

    except Exception as e:
        st.warning(e)

    st.divider()

    # ===============================
    # PORTFOLIO
    # ===============================

    st.subheader("💰 Holdings")

    try:

        holdings = fyers.holdings()

        if holdings.get("holdings"):
            df = pd.DataFrame(holdings["holdings"])
            st.dataframe(df, use_container_width=True)
        else:
            st.info("No Holdings Found")

    except Exception as e:
        st.error(e)

    st.divider()

    # ===============================
    # POSITIONS
    # ===============================

    st.subheader("📈 Open Positions")

    try:

        positions = fyers.positions()

        if positions.get("netPositions"):
            df = pd.DataFrame(positions["netPositions"])
            st.dataframe(df, use_container_width=True)
        else:
            st.info("No Open Positions")

    except Exception as e:
        st.error(e)

    st.divider()

    # ===============================
    # ORDERS
    # ===============================

    st.subheader("📋 Orders")

    try:

        orders = fyers.orderbook()

        if orders.get("orderBook"):
            df = pd.DataFrame(orders["orderBook"])
            st.dataframe(df, use_container_width=True)
        else:
            st.info("No Orders")

    except Exception as e:
        st.error(e)

    st.divider()

    # ===============================
    # PROFILE
    # ===============================

    st.subheader("👤 Profile")

    try:

        profile = fyers.get_profile()

        if profile.get("s") == "ok":
            st.json(profile["data"])
        else:
            st.json(profile)

    except Exception as e:
        st.error(e)

    st.divider()

    # ===============================
    # FUNDS
    # ===============================

    st.subheader("💳 Available Funds")

    try:

        funds = fyers.funds()

        st.json(funds)

    except Exception as e:
        st.error(e)
