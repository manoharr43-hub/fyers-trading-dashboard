import streamlit as st
import pandas as pd


def show_orders(fyers):

    st.title("📋 Orders & Trades")

    tab1, tab2 = st.tabs(
        ["📋 Order Book", "📑 Trade Book"]
    )

    # =====================================
    # ORDER BOOK
    # =====================================
    with tab1:

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

                # Summary
                st.divider()

                col1, col2, col3 = st.columns(3)

                col1.metric(
                    "Total Orders",
                    len(df)
                )

                if "status" in df.columns:

                    pending = len(
                        df[df["status"] == 6]
                    )

                    completed = len(
                        df[df["status"] == 2]
                    )

                    col2.metric(
                        "Pending",
                        pending
                    )

                    col3.metric(
                        "Completed",
                        completed
                    )

                st.download_button(
                    "⬇ Download Order Book",
                    df.to_csv(index=False),
                    "orderbook.csv",
                    "text/csv"
                )

            else:

                st.info("No Orders Found")

        except Exception as e:

            st.error(e)

    # =====================================
    # TRADE BOOK
    # =====================================
    with tab2:

        st.subheader("Trade Book")

        try:

            trades = fyers.tradebook()

            if trades.get("tradeBook"):

                df = pd.DataFrame(
                    trades["tradeBook"]
                )

                st.dataframe(
                    df,
                    use_container_width=True
                )

                st.metric(
                    "Total Trades",
                    len(df)
                )

                st.download_button(
                    "⬇ Download Trade Book",
                    df.to_csv(index=False),
                    "tradebook.csv",
                    "text/csv"
                )

            else:

                st.info("No Trades Found")

        except Exception as e:

            st.error(e)

    st.divider()

    # =====================================
    # REFRESH
    # =====================================

    if st.button(
        "🔄 Refresh Orders",
        use_container_width=True
    ):
        st.rerun()
