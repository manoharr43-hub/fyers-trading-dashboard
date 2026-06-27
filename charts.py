import streamlit as st
import pandas as pd
import plotly.graph_objects as go


# ======================================
# EMA
# ======================================
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


# ======================================
# CHARTS
# ======================================
def show_charts(fyers):

    st.title("📊 Candlestick Charts")

    symbol = st.text_input(
        "Symbol",
        "NSE:RELIANCE-EQ"
    )

    resolution = st.selectbox(
        "Time Frame",
        [
            "5",
            "15",
            "30",
            "60",
            "D"
        ]
    )

    range_from = st.date_input(
        "From Date"
    )

    range_to = st.date_input(
        "To Date"
    )

    if st.button("Load Chart"):

        try:

            data = {
                "symbol": symbol,
                "resolution": resolution,
                "date_format": "1",
                "range_from": str(range_from),
                "range_to": str(range_to),
                "cont_flag": "1"
            }

            response = fyers.history(data)

            candles = response.get("candles", [])

            if not candles:
                st.warning("No historical data found.")
                return

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

            df["EMA20"] = ema(df["Close"], 20)
            df["EMA50"] = ema(df["Close"], 50)

            fig = go.Figure()

            # Candlestick
            fig.add_trace(
                go.Candlestick(
                    x=df["Time"],
                    open=df["Open"],
                    high=df["High"],
                    low=df["Low"],
                    close=df["Close"],
                    name="Candles"
                )
            )

            # EMA20
            fig.add_trace(
                go.Scatter(
                    x=df["Time"],
                    y=df["EMA20"],
                    mode="lines",
                    name="EMA20"
                )
            )

            # EMA50
            fig.add_trace(
                go.Scatter(
                    x=df["Time"],
                    y=df["EMA50"],
                    mode="lines",
                    name="EMA50"
                )
            )

            fig.update_layout(
                title=symbol,
                height=700,
                xaxis_rangeslider_visible=False
            )

            st.plotly_chart(
                fig,
                use_container_width=True
            )

            st.subheader("Historical Data")

            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True
            )

            st.download_button(
                "⬇ Download CSV",
                df.to_csv(index=False),
                "history.csv",
                "text/csv"
            )

        except Exception as e:

            st.error(e)
