import streamlit as st
import pandas as pd
import numpy as np


# =====================================
# EMA
# =====================================
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


# =====================================
# RSI
# =====================================
def rsi(close, period=14):

    delta = close.diff()

    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)

    gain = pd.Series(gain).rolling(period).mean()
    loss = pd.Series(loss).rolling(period).mean()

    rs = gain / loss

    return 100 - (100 / (1 + rs))


# =====================================
# AI Scanner
# =====================================
def show_scanner(fyers):

    st.title("🤖 AI Stock Scanner")

    symbols = st.text_area(
        "Enter Symbols (One Per Line)",
        """NSE:RELIANCE-EQ
NSE:TCS-EQ
NSE:HDFCBANK-EQ
NSE:INFY-EQ
NSE:ICICIBANK-EQ"""
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

    if st.button("🔍 Scan Stocks"):

        rows = []

        for symbol in symbols.splitlines():

            symbol = symbol.strip()

            if symbol == "":
                continue

            try:

                data = {
                    "symbol": symbol,
                    "resolution": resolution,
                    "date_format": "1",
                    "range_from": "2026-06-01",
                    "range_to": "2026-06-30",
                    "cont_flag": "1"
                }

                response = fyers.history(data)

                candles = response.get("candles", [])

                if len(candles) == 0:
                    continue

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

                df["EMA20"] = ema(df["Close"], 20)
                df["EMA50"] = ema(df["Close"], 50)
                df["RSI"] = rsi(df["Close"])

                last = df.iloc[-1]

                signal = "HOLD"

                if (
                    last["EMA20"] > last["EMA50"]
                    and last["RSI"] > 60
                ):
                    signal = "BUY"

                elif (
                    last["EMA20"] < last["EMA50"]
                    and last["RSI"] < 40
                ):
                    signal = "SELL"

                rows.append({
                    "Symbol": symbol,
                    "Close": round(last["Close"],2),
                    "EMA20": round(last["EMA20"],2),
                    "EMA50": round(last["EMA50"],2),
                    "RSI": round(last["RSI"],2),
                    "Signal": signal
                })

            except Exception as e:

                st.error(f"{symbol} : {e}")

        if len(rows):

            result = pd.DataFrame(rows)

            st.success("Scan Completed")

            st.dataframe(
                result,
                use_container_width=True,
                hide_index=True
            )

            csv = result.to_csv(index=False)

            st.download_button(
                "⬇ Download CSV",
                csv,
                "scanner.csv",
                "text/csv"
            )

        else:

            st.warning("No Stocks Found")
