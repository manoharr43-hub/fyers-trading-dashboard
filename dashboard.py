import streamlit as st
import pandas as pd


# ===============================
# Dashboard
# ===============================
def show_dashboard(fyers):

    st.title("📈 FYERS Trading Dashboard")

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        [
            "🏠 Dashboard",
            "💰 Portfolio",
            "📋 Orders",
            "📊 Market",
            "👤 Profile"
        ]
    )

    # =====================================
    # Dashboard
    # =====================================
    with tab1:

        st.subheader("Market Overview")

        col1, col2, col3 = st.columns(3)

        try:

            quote = fyers.quotes(
                {
                    "symbols": "NSE:NIFTY50-INDEX,NSE:NIFTYBANK-INDEX,NSE:INDIAVIX-INDEX"
                }
            )

            data = quote["d"]

            col1.metric(
                "NIFTY",
                data[0]["v"]["lp"],
                data[0]["v"]["ch"]
            )

            col2.metric(
                "BANKNIFTY",
                data[1]["v"]["lp"],
                data[1]["v"]["ch"]
            )

            col3.metric(
                "INDIA VIX",
                data[2]["v"]["lp"],
                data[2]["v"]["ch"]
            )

        except Exception as e:
            st.error(e)

    # =====================================
    # Portfolio
    # =====================================
    with tab2:

        st.subheader("Holdings")

        try:

            holdings = fyers.holdings()

            if holdings.get("holdings"):

                df = pd.DataFrame(holdings["holdings"])

                st.dataframe(
                    df,
                    use_container_width=True
                )

            else:
                st.info("No Holdings")

        except Exception as e:
            st.error(e)

        st.divider()

        st.subheader("Open Positions")

        try:

            positions = fyers.positions()

            if positions.get("netPositions"):

                df = pd.DataFrame(
                    positions["netPositions"]
                )

                st.dataframe(
                    df,
                    use_container_width=True
                )

            else:
                st.info("No Positions")

        except Exception as e:
            st.error(e)

    # =====================================
    # Orders
    # =====================================
    with tab3:

        st.subheader("Order Book")

        try:

            orders = fyers.orderbook()

            if orders.get("orderBook"):

                df = pd.DataFrame(
                    orders["orderBook"]
                )

                st.dataframe(
                    df,
                    use_container_width=True
                )

            else:
                st.info("No Orders")

        except Exception as e:
            st.error(e)

    # =====================================
    # Market Watch
    # =====================================
    with tab4:

        st.subheader("Live Quotes")

        symbol = st.text_input(
            "Enter Symbol",
            "NSE:RELIANCE-EQ"
        )

        if st.button("Get Quote"):

            try:

                quote = fyers.quotes(
                    {
                        "symbols": symbol
                    }
                )

                st.json(quote)

            except Exception as e:
                st.error(e)

    # =====================================
    # Profile
    # =====================================
    with tab5:

        st.subheader("Profile")

        try:

            profile = fyers.get_profile()

            st.json(profile)

        except Exception as e:
            st.error(e)

        st.divider()

        st.subheader("Funds")

        try:

            funds = fyers.funds()

            st.json(funds)

        except Exception as e:
            st.error(e)
