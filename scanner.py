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
# ==========================================================
# SCANNER - PART 4
# VOLUME BREAKOUT | PRICE BREAKOUT | RVOL | GAP SCANNER
# ==========================================================

    st.divider()
    st.subheader("🚀 Breakout Scanner")

    if len(results):

        breakout_results = []

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

                candles = history["candles"]

                if len(candles) < 21:
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

                latest = df.iloc[-1]

                avg_volume = df["volume"].tail(20).mean()

                rvol = latest["volume"] / avg_volume if avg_volume > 0 else 0

                breakout = latest["close"] > df["high"].tail(20).max()

                breakdown = latest["close"] < df["low"].tail(20).min()

                gap = (
                    (latest["open"] - df.iloc[-2]["close"])
                    / df.iloc[-2]["close"]
                ) * 100

                signal = "WAIT"

                if breakout and rvol >= 2:

                    signal = "BUY"

                elif breakdown and rvol >= 2:

                    signal = "SELL"

                breakout_results.append({

                    "Symbol": symbol,

                    "Close": round(latest["close"], 2),

                    "RVOL": round(rvol, 2),

                    "Gap %": round(gap, 2),

                    "Breakout": breakout,

                    "Breakdown": breakdown,

                    "Signal": signal

                })

            except Exception:

                pass

            progress.progress((i + 1) / total)

        progress.empty()

        if breakout_results:

            breakout_df = pd.DataFrame(
                breakout_results
            )

            st.dataframe(

                breakout_df,

                use_container_width=True,

                height=550

            )

            # -----------------------
            # Summary
            # -----------------------

            buy = len(
                breakout_df[
                    breakout_df.Signal == "BUY"
                ]
            )

            sell = len(
                breakout_df[
                    breakout_df.Signal == "SELL"
                ]
            )

            wait = len(
                breakout_df[
                    breakout_df.Signal == "WAIT"
                ]
            )

            c1, c2, c3 = st.columns(3)

            c1.metric("BUY", buy)

            c2.metric("SELL", sell)

            c3.metric("WAIT", wait)

            # -----------------------
            # Download
            # -----------------------

            st.download_button(

                "⬇ Download Breakout Scanner",

                breakout_df.to_csv(index=False),

                file_name="breakout_scanner.csv",

                mime="text/csv"

            )

        else:

            st.warning("No breakout stocks found.")
# ==========================================================
# SCANNER - PART 5
# AI SCORE | INSTITUTIONAL RANKING | TOP PICKS
# ==========================================================

    st.divider()
    st.subheader("🤖 AI Institutional Scanner")

    if len(breakout_results):

        ai_results = []

        for row in breakout_results:

            score = 50

            # RVOL Score
            if row["RVOL"] >= 3:
                score += 20
            elif row["RVOL"] >= 2:
                score += 10

            # Breakout Score
            if row["Breakout"]:
                score += 20

            if row["Breakdown"]:
                score -= 20

            # Gap Score
            if row["Gap %"] > 1:
                score += 5

            elif row["Gap %"] < -1:
                score -= 5

            score = max(0, min(score, 100))

            # ----------------------------------
            # AI Recommendation
            # ----------------------------------

            if score >= 90:
                recommendation = "⭐⭐⭐⭐⭐ STRONG BUY"

            elif score >= 75:
                recommendation = "⭐⭐⭐⭐ BUY"

            elif score >= 60:
                recommendation = "⭐⭐⭐ WATCH"

            elif score >= 40:
                recommendation = "⭐⭐ HOLD"

            else:
                recommendation = "⭐ SELL"

            ai_results.append({

                "Symbol": row["Symbol"],

                "Close": row["Close"],

                "RVOL": row["RVOL"],

                "Gap %": row["Gap %"],

                "AI Score": score,

                "Recommendation": recommendation

            })

        ai_df = pd.DataFrame(ai_results)

        ai_df = ai_df.sort_values(
            "AI Score",
            ascending=False
        )

        st.dataframe(
            ai_df,
            use_container_width=True,
            height=600
        )

        # =====================================
        # TOP 10 BUY
        # =====================================

        st.divider()

        st.subheader("🟢 Top 10 Buy Stocks")

        buy_df = ai_df.head(10)

        st.dataframe(
            buy_df,
            use_container_width=True
        )

        # =====================================
        # TOP 10 SELL
        # =====================================

        st.subheader("🔴 Lowest Ranked Stocks")

        sell_df = ai_df.tail(10)

        st.dataframe(
            sell_df,
            use_container_width=True
        )

        # =====================================
        # Dashboard Metrics
        # =====================================

        st.divider()

        c1, c2, c3, c4 = st.columns(4)

        c1.metric(
            "Scanned",
            len(ai_df)
        )

        c2.metric(
            "BUY Candidates",
            len(ai_df[ai_df["AI Score"] >= 75])
        )

        c3.metric(
            "WATCH",
            len(
                ai_df[
                    (ai_df["AI Score"] >= 60) &
                    (ai_df["AI Score"] < 75)
                ]
            )
        )

        c4.metric(
            "SELL",
            len(ai_df[ai_df["AI Score"] < 40])
        )

        # =====================================
        # Export
        # =====================================

        st.download_button(

            "⬇ Download AI Scanner Report",

            ai_df.to_csv(index=False),

            file_name="AI_Scanner_Report.csv",

            mime="text/csv"

        )

    else:

        st.info("Run the scanner first to generate AI rankings.")
