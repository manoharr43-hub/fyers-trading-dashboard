import streamlit as st
import pandas as pd


def show_market(fyers):

    st.title("📊 Live Market")

    symbol = st.text_input(
        "Enter Symbol",
        "NSE:RELIANCE-EQ"
    )

    col1, col2 = st.columns(2)

    # ===========================
    # LIVE QUOTE
    # ===========================
    with col1:

        if st.button("📈 Get Quote", use_container_width=True):

            try:

                quote = fyers.quotes({
                    "symbols": symbol
                })

                if quote.get("s") == "ok":

                    data = quote["d"][0]["v"]

                    st.metric(
                        "Last Price",
                        data["lp"],
                        data["ch"]
                    )

                    st.json(data)

                else:
                    st.error(quote)

            except Exception as e:
                st.error(e)

    # ===========================
    # MARKET DEPTH
    # ===========================
    with col2:

        if st.button("📚 Market Depth", use_container_width=True):

            try:

                depth = fyers.depth({
                    "symbol": symbol,
                    "ohlcv_flag": "1"
                })

                st.json(depth)

            except Exception as e:

                st.error(e)

    st.divider()

    # ===========================
    # HISTORICAL DATA
    # ===========================
    st.subheader("📉 Historical Data")

    if st.button("Load History"):

        try:

            history = fyers.history({

                "symbol": symbol,

                "resolution": "5",

                "date_format": "1",

                "range_from": "2026-01-01",

                "range_to": "2026-12-31",

                "cont_flag": "1"

            })

            if history.get("candles"):

                df = pd.DataFrame(

                    history["candles"],

                    columns=[
                        "Timestamp",
                        "Open",
                        "High",
                        "Low",
                        "Close",
                        "Volume"
                    ]
                )

                st.dataframe(
                    df,
                    use_container_width=True
                )

            else:

                st.warning("No Data")

        except Exception as e:

            st.error(e)
