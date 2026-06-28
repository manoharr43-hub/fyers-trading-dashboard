import streamlit as st
import pandas as pd
import time

# ==========================================================
# NSE AI PRO V12 INSTITUTIONAL
# SCANNER - PART 1
# ==========================================================

def show_scanner(fyers):

    st.title("🚀 NSE AI PRO V12 Institutional Scanner")

    # ===========================================
    # Sidebar
    # ===========================================

    st.sidebar.header("⚙ Scanner Settings")

    scanner_type = st.sidebar.selectbox(

        "Scanner",

        [

            "AI Scanner",

            "Intraday Scanner",

            "Swing Scanner",

            "Breakout Scanner",

            "Volume Breakout",

            "Momentum Scanner",

            "RSI Scanner",

            "EMA Scanner",

            "MACD Scanner",

            "Supertrend Scanner"

        ]

    )

    market = st.sidebar.selectbox(

        "Market",

        [

            "NIFTY50",

            "NIFTY100",

            "NIFTY200",

            "NIFTY500",

            "F&O",

            "CUSTOM"

        ]

    )

    refresh = st.sidebar.checkbox(
        "Auto Refresh",
        False
    )

    refresh_sec = st.sidebar.slider(
        "Refresh (Seconds)",
        5,
        60,
        10
    )

    st.divider()

    # ===========================================
    # Manual Symbols
    # ===========================================

    custom_symbols = st.text_area(

        "Custom Symbols (Comma Separated)",

        "NSE:RELIANCE-EQ,NSE:TCS-EQ,NSE:INFY-EQ"

    )

    if market == "CUSTOM":

        symbols = [

            x.strip()

            for x in custom_symbols.split(",")

            if x.strip()

        ]

    else:

        # Placeholder
        symbols = []

    st.success(f"Scanner : {scanner_type}")

    st.info(f"Selected Market : {market}")

    st.write(f"Total Symbols : {len(symbols)}")

    st.divider()

    # ===========================================
    # Placeholder
    # ===========================================

    result_placeholder = st.empty()

    if refresh:

        time.sleep(refresh_sec)

        st.rerun()
# ==========================================================
# SCANNER - PART 2
# LIVE QUOTES | MARKET DATA | SCANNER TABLE
# ==========================================================

    st.subheader("📈 Live Scanner")

    if st.button("🚀 Run Scanner", use_container_width=True):

        results = []

        progress = st.progress(0)

        if len(symbols) == 0:

            st.warning(
                "Default symbols list will be added in Part 3."
            )

        total = max(len(symbols), 1)

        for i, symbol in enumerate(symbols):

            try:

                quote = fyers.quotes({

                    "symbols": symbol

                })

                if quote.get("s") != "ok":
                    continue

                q = quote["d"][0]["v"]

                row = {

                    "Symbol": symbol,

                    "LTP": q.get("lp"),

                    "Open": q.get("open_price"),

                    "High": q.get("high_price"),

                    "Low": q.get("low_price"),

                    "Prev Close": q.get("prev_close_price"),

                    "Change": q.get("ch"),

                    "Change %": q.get("chp"),

                    "Volume": q.get("volume"),

                    "Signal": "WAIT"

                }

                results.append(row)

            except Exception:

                pass

            progress.progress((i + 1) / total)

        progress.empty()

        if len(results):

            df = pd.DataFrame(results)

            # ===================================
            # Basic Signal
            # ===================================

            def signal(row):

                if row["Change %"] is None:
                    return "WAIT"

                if row["Change %"] >= 2:
                    return "BUY"

                elif row["Change %"] <= -2:
                    return "SELL"

                return "WAIT"

            df["Signal"] = df.apply(signal, axis=1)

            st.success(f"Scanned {len(df)} Stocks")

            st.dataframe(

                df,

                use_container_width=True,

                height=550

            )

            # ==============================
            # Summary
            # ==============================

            buy = len(df[df.Signal == "BUY"])

            sell = len(df[df.Signal == "SELL"])

            wait = len(df[df.Signal == "WAIT"])

            c1, c2, c3 = st.columns(3)

            c1.metric("BUY", buy)

            c2.metric("SELL", sell)

            c3.metric("WAIT", wait)

            # ==============================
            # CSV Download
            # ==============================

            st.download_button(

                "⬇ Download Scanner Report",

                df.to_csv(index=False),

                file_name="scanner_report.csv",

                mime="text/csv"

            )

        else:

            st.warning("No market data received.")
