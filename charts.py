import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import time

# ==========================================================
# NSE AI PRO V12 Institutional
# CHARTS - PART 1
# ==========================================================

def show_charts(fyers):

    st.title("📈 Advanced Trading Charts")

    # =====================================
    # Sidebar
    # =====================================

    st.sidebar.header("⚙ Chart Settings")

    symbol = st.sidebar.text_input(
        "Symbol",
        "NSE:RELIANCE-EQ"
    )

    resolution = st.sidebar.selectbox(

        "Timeframe",

        [

            "1",
            "3",
            "5",
            "10",
            "15",
            "30",
            "60",
            "120",
            "240",
            "D",
            "W",
            "M"

        ]

    )

    days = st.sidebar.slider(

        "History (Days)",

        5,

        365,

        100

    )

    auto_refresh = st.sidebar.checkbox(
        "Auto Refresh"
    )

    refresh_time = st.sidebar.slider(

        "Refresh Seconds",

        5,

        60,

        10

    )

    st.divider()

    # =====================================
    # Download Data
    # =====================================

    if st.button(
        "📊 Load Chart",
        use_container_width=True
    ):

        try:

            data = fyers.history({

                "symbol": symbol,

                "resolution": resolution,

                "date_format": "0",

                "range_from": str(days),

                "range_to": "0",

                "cont_flag": "1"

            })

            if data.get("s") != "ok":

                st.error(data)

                st.stop()

            candles = data["candles"]

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

            df["Time"] = pd.to_datetime(
                df["Time"],
                unit="s"
            )

            st.success("Historical Data Loaded")

            st.dataframe(
                df.tail(),
                use_container_width=True
            )

            chart_placeholder = st.empty()
