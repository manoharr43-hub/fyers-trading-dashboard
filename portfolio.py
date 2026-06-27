import streamlit as st
import pandas as pd


def show_portfolio(fyers):

    st.title("💼 Portfolio")

    tab1, tab2, tab3 = st.tabs(
        ["📈 Holdings", "📉 Positions", "💰 Funds"]
    )

    # =====================================
    # Holdings
    # =====================================
    with tab1:

        try:

            data = fyers.holdings()

            holdings = data.get("holdings", [])

            if holdings:

                df = pd.DataFrame(holdings)

                st.subheader("Holdings")

                st.dataframe(
                    df,
                    use_container_width=True,
                    hide_index=True
                )

                # Portfolio Summary
                total_investment = 0
                total_value = 0
                total_pnl = 0

                for row in holdings:

                    qty = float(row.get("quantity", 0))
                    cost = float(row.get("costPrice", 0))
                    ltp = float(row.get("ltp", 0))

                    investment = qty * cost
                    value = qty * ltp
                    pnl = value - investment

                    total_investment += investment
                    total_value += value
                    total_pnl += pnl

                c1, c2, c3 = st.columns(3)

                c1.metric(
                    "Investment",
                    f"₹ {total_investment:,.2f}"
                )

                c2.metric(
                    "Current Value",
                    f"₹ {total_value:,.2f}"
                )

                c3.metric(
                    "Total P&L",
                    f"₹ {total_pnl:,.2f}"
                )

                csv = df.to_csv(index=False)

                st.download_button(
                    "⬇ Export Holdings",
                    csv,
                    "holdings.csv",
                    "text/csv"
                )

            else:

                st.info("No Holdings")

        except Exception as e:

            st.error(e)

    # =====================================
    # Positions
    # =====================================
    with tab2:

        try:

            data = fyers.positions()

            positions = data.get("netPositions", [])

            if positions:

                df = pd.DataFrame(positions)

                st.subheader("Open Positions")

                st.dataframe(
                    df,
                    use_container_width=True,
                    hide_index=True
                )

                total_pnl = 0

                for row in positions:
                    total_pnl += float(row.get("pl", 0))

                st.metric(
                    "Today's P&L",
                    f"₹ {total_pnl:,.2f}"
                )

                csv = df.to_csv(index=False)

                st.download_button(
                    "⬇ Export Positions",
                    csv,
                    "positions.csv",
                    "text/csv"
                )

            else:

                st.info("No Open Positions")

        except Exception as e:

            st.error(e)

    # =====================================
    # Funds
    # =====================================
    with tab3:

        try:

            funds = fyers.funds()

            st.subheader("Available Funds")

            st.json(funds)

        except Exception as e:

            st.error(e)
