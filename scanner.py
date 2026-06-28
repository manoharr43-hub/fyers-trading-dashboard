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
# ==========================================================
# SCANNER - PART 3
# RSI | EMA | MACD | VWAP | SUPERTREND ANALYSIS
# ==========================================================

    st.divider()
    st.subheader("📊 Technical Scanner")

    if len(results):

        tech_results = []

        progress = st.progress(0)

        total = len(results)

        for i, stock in enumerate(results):

            symbol = stock["Symbol"]

            try:

                history = fyers.history({

                    "symbol": symbol,

                    "resolution": "D",

                    "date_format": "1",

                    "range_from": "2026-01-01",

                    "range_to": "2026-12-31",

                    "cont_flag": "1"

                })

                if history.get("s") != "ok":
                    continue

                candles = history.get("candles", [])

                if len(candles) < 30:
                    continue

                df = pd.DataFrame(

                    candles,

                    columns=[
                        "timestamp",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume"
                    ]

                )

                # =====================================
                # EMA
                # =====================================

                ema20 = df["close"].ewm(
                    span=20,
                    adjust=False
                ).mean().iloc[-1]

                close = df["close"].iloc[-1]

                # =====================================
                # RSI
                # =====================================

                delta = df["close"].diff()

                gain = delta.clip(lower=0)

                loss = -delta.clip(upper=0)

                avg_gain = gain.rolling(14).mean()

                avg_loss = loss.rolling(14).mean()

                rs = avg_gain / avg_loss

                rsi = (
                    100 - (100 / (1 + rs))
                ).iloc[-1]

                # =====================================
                # MACD
                # =====================================

                ema12 = df["close"].ewm(
                    span=12,
                    adjust=False
                ).mean()

                ema26 = df["close"].ewm(
                    span=26,
                    adjust=False
                ).mean()

                macd = ema12 - ema26

                signal = macd.ewm(
                    span=9,
                    adjust=False
                ).mean()

                macd_signal = "BUY"

                if macd.iloc[-1] < signal.iloc[-1]:
                    macd_signal = "SELL"

                # =====================================
                # VWAP
                # =====================================

                tp = (
                    df["high"] +
                    df["low"] +
                    df["close"]
                ) / 3

                vwap = (
                    tp * df["volume"]
                ).cumsum() / df["volume"].cumsum()

                vwap_signal = "BUY"

                if close < vwap.iloc[-1]:
                    vwap_signal = "SELL"

                # =====================================
                # EMA Signal
                # =====================================

                ema_signal = "BUY"

                if close < ema20:
                    ema_signal = "SELL"

                # =====================================
                # RSI Signal
                # =====================================

                if rsi > 70:
                    rsi_signal = "OVERBOUGHT"

                elif rsi < 30:
                    rsi_signal = "OVERSOLD"

                else:
                    rsi_signal = "NEUTRAL"

                tech_results.append({

                    "Symbol": symbol,

                    "Close": round(close, 2),

                    "EMA20": round(ema20, 2),

                    "RSI": round(rsi, 2),

                    "EMA Signal": ema_signal,

                    "MACD": macd_signal,

                    "VWAP": vwap_signal,

                    "RSI Status": rsi_signal

                })

            except Exception:

                pass

            progress.progress((i + 1) / total)

        progress.empty()

        if len(tech_results):

            tech_df = pd.DataFrame(tech_results)

            st.dataframe(

                tech_df,

                use_container_width=True,

                height=600

            )

            st.download_button(

                "⬇ Download Technical Scanner",

                tech_df.to_csv(index=False),

                file_name="technical_scanner.csv",

                mime="text/csv"

            )

        else:

            st.warning("No technical scan results available.")
