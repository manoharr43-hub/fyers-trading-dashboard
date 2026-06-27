import streamlit as st
import pandas as pd
import time


# ======================================
# MARKET WATCH
# ======================================
def show_market(fyers):

    st.title("📊 Live Market Watch")

    # -----------------------------
    # Default Watchlist
    # -----------------------------
    default_symbols = [
        "NSE:NIFTY50-INDEX",
        "NSE:NIFTYBANK-INDEX",
        "NSE:FINNIFTY-INDEX",
        "NSE:RELIANCE-EQ",
        "NSE:TCS-EQ",
        "NSE:HDFCBANK-EQ",
        "NSE:INFY-EQ",
        "NSE:ICICIBANK-EQ",
        "NSE:SBIN-EQ"
    ]

    symbols = st.multiselect(
        "Select Symbols",
        default_symbols,
        default=default_symbols
    )

    custom = st.text_input(
        "Custom Symbol",
        placeholder="Example: NSE:ITC-EQ"
    )

    if custom:
        symbols.append(custom)

    col1, col2 = st.columns(2)

    auto_refresh = col1.checkbox("Auto Refresh")

    refresh_time = col2.slider(
        "Refresh (Seconds)",
        2,
        30,
        5
    )

    if st.button("Refresh Quotes"):
        load_quotes(fyers, symbols)

    if auto_refresh:

        placeholder = st.empty()

        while auto_refresh:

            with placeholder.container():

                load_quotes(fyers, symbols)

            time.sleep(refresh_time)

            st.rerun()


# ======================================
# LOAD QUOTES
# ======================================
def load_quotes(fyers, symbols):

    if len(symbols) == 0:
        st.warning("Select at least one symbol.")
        return

    try:

        data = fyers.quotes(
            {
                "symbols": ",".join(symbols)
            }
        )

        rows = []

        for item in data["d"]:

            q = item["v"]

            rows.append({

                "Symbol": item["n"],

                "LTP": q.get("lp"),

                "Open": q.get("o"),

                "High": q.get("h"),

                "Low": q.get("l"),

                "Prev Close": q.get("prev_close"),

                "Change": q.get("ch"),

                "% Change": q.get("chp"),

                "Volume": q.get("volume")

            })

        df = pd.DataFrame(rows)

        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True
        )

        st.subheader("JSON Response")

        st.json(data)

    except Exception as e:

        st.error(e)
