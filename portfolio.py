import streamlit as st
import pandas as pd


def show_portfolio(fyers):

    st.title("💼 Portfolio")

    # =====================================
    # Holdings
    # =====================================

    st.subheader("📦 Holdings")

    try:

        holdings = fyers.holdings()

        if holdings.get("holdings"):

            df = pd.DataFrame(holdings["holdings"])

            st.dataframe(
                df,
                use_container_width=True
            )

            st.download_button(
                "⬇ Download Holdings CSV",
                df.to_csv(index=False),
                "holdings.csv",
                "text/csv"
            )

        else:

            st.info("No Holdings Available")

    except Exception as e:

        st.error(e)

    st.divider()

    # =====================================
    # Open Positions
    # =====================================

    st.subheader("📈 Open Positions")

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

            st.download_button(
                "⬇ Download Positions CSV",
                df.to_csv(index=False),
                "positions.csv",
                "text/csv"
            )

        else:

            st.info("No Open Positions")

    except Exception as e:

        st.error(e)

    st.divider()

    # =====================================
    # Portfolio Summary
    # =====================================

    st.subheader("📊 Portfolio Summary")

    try:

        holdings = fyers.holdings()

        if holdings.get("holdings"):

            df = pd.DataFrame(
                holdings["holdings"]
            )

            st.write("Total Holdings :", len(df))

            if "marketVal" in df.columns:
                st.metric(
                    "Market Value",
                    f"₹ {df['marketVal'].sum():,.2f}"
                )

            if "costPrice" in df.columns:
                st.metric(
                    "Investment",
                    f"₹ {df['costPrice'].sum():,.2f}"
                )

        else:

            st.info("Portfolio Empty")

    except Exception as e:

        st.error(e)

    st.divider()

    # =====================================
    # Refresh
    # =====================================

    if st.button(
        "🔄 Refresh Portfolio",
        use_container_width=True
    ):
        st.rerun()
