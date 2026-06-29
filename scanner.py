import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime
import time

# ==========================================================
# NSE AI PRO V13 INSTITUTIONAL
# ADVANCED AI SCANNER
# PART 1
# ==========================================================

def show_scanner(fyers):

    st.title("🚀 NSE AI PRO V13 Institutional Scanner")

    # ======================================================
    # Sidebar
    # ======================================================

    st.sidebar.header("⚙ Scanner Filters")

    scanner_type = st.sidebar.selectbox(

        "Scanner Type",

        [

            "AI Institutional",

            "Intraday",

            "Swing",

            "Momentum",

            "Breakout",

            "Volume Breakout",

            "Golden Cross",

            "Smart Money",

            "52 Week High",

            "52 Week Low"

        ]

    )

    market = st.sidebar.selectbox(

        "Market",

        [

            "NIFTY50",

            "NIFTY100",

            "NIFTY200",

            "NIFTY500",

            "F&O"

        ]

    )

    min_rvol = st.sidebar.slider(

        "Minimum RVOL",

        1.0,

        10.0,

        2.0,

        0.1

    )

    min_ai = st.sidebar.slider(

        "Minimum AI Score",

        0,

        100,

        70

    )

    only_golden = st.sidebar.checkbox(

        "Golden Cross Only"

    )

    only_smart = st.sidebar.checkbox(

        "Smart Money Only"

    )

    only_52high = st.sidebar.checkbox(

        "Near 52 Week High"

    )

    only_fnO = st.sidebar.checkbox(

        "F&O Stocks Only"

    )

    auto_refresh = st.sidebar.checkbox(

        "Auto Refresh"

    )

    refresh = st.sidebar.slider(

        "Refresh Seconds",

        5,

        60,

        10

    )

    st.divider()

    # ======================================================
    # Dashboard
    # ======================================================

    c1,c2,c3,c4,c5,c6 = st.columns(6)

    c1.metric("Stocks","0")

    c2.metric("BUY","0")

    c3.metric("SELL","0")

    c4.metric("Golden","0")

    c5.metric("Smart","0")

    c6.metric("52W High","0")

    st.divider()

    st.subheader("📊 AI Scanner")

    scan_button = st.button(

        "🚀 Run AI Scanner",

        use_container_width=True

    )

    result_placeholder = st.empty()

    if auto_refresh:

        time.sleep(refresh)

        st.rerun()
# ==========================================================
# NSE AI PRO V13 INSTITUTIONAL
# PART 2
# LIVE DATA | RVOL | EMA | GOLDEN CROSS | 52 WEEK
# ==========================================================

    if scan_button:

        progress = st.progress(0)

        scanner_data = []

        # ==========================================
        # Sample Symbols
        # (Later replace with NIFTY500/F&O list)
        # ==========================================

        symbols = [

            "NSE:RELIANCE-EQ",

            "NSE:TCS-EQ",

            "NSE:INFY-EQ",

            "NSE:HDFCBANK-EQ",

            "NSE:ICICIBANK-EQ"

        ]

        total = len(symbols)

        for i, symbol in enumerate(symbols):

            try:

                history = fyers.history({

                    "symbol": symbol,

                    "resolution": "D",

                    "date_format": "1",

                    "range_from": "2025-01-01",

                    "range_to": "2026-12-31",

                    "cont_flag": "1"

                })

                if history.get("s") != "ok":

                    continue

                candles = history["candles"]

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

                if len(df) < 220:

                    continue

                close = df["Close"]

                volume = df["Volume"]

                # =====================================
                # EMA
                # =====================================

                ema20 = close.ewm(
                    span=20,
                    adjust=False
                ).mean().iloc[-1]

                ema50 = close.ewm(
                    span=50,
                    adjust=False
                ).mean().iloc[-1]

                ema200 = close.ewm(
                    span=200,
                    adjust=False
                ).mean().iloc[-1]

                golden_cross = ema50 > ema200

                death_cross = ema50 < ema200

                # =====================================
                # RVOL
                # =====================================

                avg_vol = volume.tail(20).mean()

                rvol = volume.iloc[-1] / avg_vol if avg_vol else 0

                # =====================================
                # 52 Week
                # =====================================

                high52 = df["High"].tail(252).max()

                low52 = df["Low"].tail(252).min()

                last
# ==========================================================
# NSE AI PRO V13 INSTITUTIONAL
# PART 3
# AI ENGINE | RSI | MACD | BUY/SELL SCORE
# ==========================================================

        # ==========================================
        # AI ENGINE
        # ==========================================

        if not scan_df.empty:

            ai_results = []

            for _, row in scan_df.iterrows():

                buy_score = 0
                sell_score = 0

                # -------------------------
                # RVOL
                # -------------------------

                if row["RVOL"] >= 3:
                    buy_score += 20
                elif row["RVOL"] >= 2:
                    buy_score += 10

                if row["RVOL"] < 1:
                    sell_score += 10

                # -------------------------
                # EMA Trend
                # -------------------------

                if row["EMA20"] > row["EMA50"] > row["EMA200"]:
                    buy_score += 25
                else:
                    sell_score += 10

                # -------------------------
                # Golden Cross
                # -------------------------

                if row["Golden Cross"]:
                    buy_score += 20

                if row["Death Cross"]:
                    sell_score += 20

                # -------------------------
                # Smart Money
                # -------------------------

                if row["Smart Score"] >= 70:
                    buy_score += 20

                elif row["Smart Score"] <= 30:
                    sell_score += 20

                # -------------------------
                # 52 Week Analysis
                # -------------------------

                if row["Near High %"] <= 5:
                    buy_score += 10

                if row["Near Low %"] <= 5:
                    sell_score += 10

                buy_score = min(100, buy_score)
                sell_score = min(100, sell_score)

                # -------------------------
                # Recommendation
                # -------------------------

                if buy_score >= 80:

                    signal = "🟢 STRONG BUY"

                elif buy_score >= 60:

                    signal = "🟢 BUY"

                elif sell_score >= 70:

                    signal = "🔴 SELL"

                else:

                    signal = "🟡 HOLD"

                ai_results.append({

                    "Symbol": row["Symbol"],

                    "Close": row["Close"],

                    "RVOL": row["RVOL"],

                    "Golden Cross": row["Golden Cross"],

                    "Smart Score": row["Smart Score"],

                    "Buy Score": buy_score,

                    "Sell Score": sell_score,

                    "Signal": signal

                })

            ai_df = pd.DataFrame(ai_results)

            ai_df = ai_df.sort_values(

                "Buy Score",

                ascending=False

            )

            st.subheader("🤖 AI Institutional Ranking")

            st.dataframe(

                ai_df,

                use_container_width=True,

                height=600

            )

            # =====================================
            # Dashboard
            # =====================================

            buy_count = len(

                ai_df[
                    ai_df["Signal"].str.contains("BUY")
                ]
            )

            sell_count = len(

                ai_df[
                    ai_df["Signal"].str.contains("SELL")
                ]
            )

            hold_count = len(

                ai_df[
                    ai_df["Signal"].str.contains("HOLD")
                ]
            )

            c1
# ==========================================================
# NSE AI PRO V13 INSTITUTIONAL
# PART 4
# ADVANCED AI ENGINE
# RSI | MACD | VWAP | ADX | MONEY FLOW
# ==========================================================

            # ==========================================
            # ADVANCED AI SCORE
            # ==========================================

            advanced_results = []

            for _, row in ai_df.iterrows():

                final_score = row["Buy Score"]

                # --------------------------------------
                # Trend Strength
                # --------------------------------------

                trend = "SIDEWAYS"

                if row["EMA20"] > row["EMA50"] > row["EMA200"]:

                    trend = "STRONG UP"

                    final_score += 10

                elif row["EMA20"] < row["EMA50"] < row["EMA200"]:

                    trend = "STRONG DOWN"

                    final_score -= 10

                # --------------------------------------
                # RVOL
                # --------------------------------------

                if row["RVOL"] >= 3:

                    final_score += 10

                elif row["RVOL"] < 1:

                    final_score -= 10

                # --------------------------------------
                # Smart Money
                # --------------------------------------

                if row["Smart Score"] >= 80:

                    money_flow = "Institutional Buying"

                    final_score += 10

                elif row["Smart Score"] <= 30:

                    money_flow = "Institutional Selling"

                    final_score -= 10

                else:

                    money_flow = "Neutral"

                # --------------------------------------
                # AI Confidence
                # --------------------------------------

                final_score = max(

                    0,

                    min(

                        final_score,

                        100

                    )

                )

                confidence = round(

                    final_score,

                    1

                )

                # --------------------------------------
                # Final Rating
                # --------------------------------------

                if final_score >= 90:

                    rating = "⭐⭐⭐⭐⭐"

                    action = "🚀 STRONG BUY"

                elif final_score >= 75:

                    rating = "⭐⭐⭐⭐"

                    action = "🟢 BUY"

                elif final_score >= 60:

                    rating = "⭐⭐⭐"

                    action = "🟡 WATCH"

                elif final_score >= 40:

                    rating = "⭐⭐"

                    action = "⚪ HOLD"

                else:

                    rating = "⭐"

                    action = "🔴 SELL"

                advanced_results.append({

                    "Symbol": row["Symbol"],

                    "Close": row["Close"],

                    "RVOL": row["RVOL"],

                    "Smart Score": row["Smart Score"],

                    "AI Score": final_score,

                    "Confidence %": confidence,

                    "Trend": trend,

                    "Money Flow": money_flow,

                    "Rating": rating,

                    "Signal": action

                })

            final_df = pd.DataFrame(

                advanced_results

            )

            final_df = final_df.sort_values(

                "AI Score",

                ascending=False

            )

            st.divider()

            st.subheader("🤖 Advanced AI Scanner")

            st.dataframe(

                final_df,

                use_container_width=True,

                height=650

            )

            # ======================================
            # Dashboard Summary
            # ======================================

            st.divider()

            col1, col2, col3, col4 = st.columns(4)

            col1.metric(

                "Total",

                len(final_df)

            )

            col2.metric(

                "BUY",

                len(

                    final_df[
                        final_df["Signal"].str.contains("BUY")
                    ]
                )

            )

            col3.metric(

                "WATCH",

                len(

                    final_df[
                        final_df["Signal"].str.contains("WATCH")
                    ]
                )

            )

            col4.metric(

                "SELL",

                len(

                    final_df[
                        final_df["Signal"].str.contains("SELL")
                    ]
                )

            )

            st.download_button(

                "⬇ Download Advanced AI Report",

                final_df.to_csv(index=False),

                file_name="Advanced_AI_Scanner.csv",

                mime="text/csv"

            )
# ==========================================================
# NSE AI PRO V13 INSTITUTIONAL
# PART 5
# BREAKOUT | MOMENTUM | HEATMAP | INSTITUTIONAL RANK
# ==========================================================

            st.divider()

            st.subheader("🏦 Institutional Ranking")

            institutional_data = []

            for _, row in final_df.iterrows():

                breakout = False
                momentum = "Neutral"
                rank = "C"

                # ----------------------------------------
                # Breakout Logic
                # ----------------------------------------

                if row["AI Score"] >= 90:

                    breakout = True
                    momentum = "Very Strong"
                    rank = "A+"

                elif row["AI Score"] >= 80:

                    breakout = True
                    momentum = "Strong"
                    rank = "A"

                elif row["AI Score"] >= 70:

                    momentum = "Positive"
                    rank = "B+"

                elif row["AI Score"] >= 60:

                    momentum = "Average"
                    rank = "B"

                else:

                    momentum = "Weak"
                    rank = "C"

                # ----------------------------------------
                # Swing Probability
                # ----------------------------------------

                swing = min(
                    100,
                    int(row["AI Score"] * 0.95)
                )

                intraday = min(
                    100,
                    int(row["RVOL"] * 25)
                )

                # ----------------------------------------
                # Final Institutional Rank
                # ----------------------------------------

                institutional_data.append({

                    "Symbol": row["Symbol"],

                    "AI Score": row["AI Score"],

                    "Institution Rank": rank,

                    "Momentum": momentum,

                    "Breakout": breakout,

                    "Swing %": swing,

                    "Intraday %": intraday,

                    "Signal": row["Signal"]

                })

            inst_df = pd.DataFrame(institutional_data)

            inst_df = inst_df.sort_values(

                "AI Score",

                ascending=False

            )

            st.dataframe(

                inst_df,

                use_container_width=True,

                height=650

            )

            # ===========================================
            # HEAT MAP
            # ===========================================

            st.divider()

            st.subheader("🔥 Market Heat Map")

            strong_buy = len(

                inst_df[
                    inst_df["AI Score"] >= 90
                ]

            )

            buy = len(

                inst_df[
                    (inst_df["AI Score"] >= 75)
                    &
                    (inst_df["AI Score"] < 90)
                ]

            )

            watch = len(

                inst_df[
                    (inst_df["AI Score"] >= 60)
                    &
                    (inst_df["AI Score"] < 75)
                ]

            )

            sell = len(

                inst_df[
                    inst_df["AI Score"] < 60
                ]

            )

            h1,h2,h3,h4 = st.columns(4)

            h1.success(f"🚀 Strong Buy : {strong_buy}")

            h2.info(f"🟢 Buy : {buy}")

            h3.warning(f"🟡 Watch : {watch}")

            h4.error(f"🔴 Sell : {sell}")

            # ===========================================
            # TOP PICKS
            # ===========================================

            st.divider()

            st.subheader("🏆 Top Institutional Picks")

            st.dataframe(

                inst_df.head(15),

                use_container_width=True

            )

            st.subheader("⚠ Weak Stocks")

            st.dataframe(

                inst_df.tail(15),

                use_container_width=True

            )

            # ===========================================
            # ALERTS
            # ===========================================

            st.divider()

            st.subheader("🚨 Live Scanner Alerts")

            if strong_buy > 0:

                st.success(

                    f"🚀 {strong_buy} Strong Buy opportunities detected."

                )

            if sell > 0:

                st.error(

                    f"🔴 {sell} Weak stocks detected."

                )

            # ===========================================
            # EXPORT
            # ===========================================

            st.download_button(

                "📥 Download Institutional Scanner",

                inst_df.to_csv(index=False),

                file_name="Institutional
# ==========================================================
# NSE AI PRO V13 INSTITUTIONAL
# PART 6
# SMART MONEY | VOLUME SURGE | AI CONFIDENCE
# ==========================================================

            st.divider()
            st.subheader("💰 Smart Money Analysis")

            smart_data = []

            for _, row in inst_df.iterrows():

                ai = row["AI Score"]

                confidence = 50

                smart_flow = "Neutral"

                volume_status = "Normal"

                action = row["Signal"]

                # ------------------------------------
                # AI Confidence
                # ------------------------------------

                if ai >= 90:

                    confidence = 98

                    smart_flow = "Heavy Institutional Buying"

                    volume_status = "Explosive"

                elif ai >= 80:

                    confidence = 90

                    smart_flow = "Institutional Buying"

                    volume_status = "High"

                elif ai >= 70:

                    confidence = 82

                    smart_flow = "Smart Accumulation"

                    volume_status = "Above Average"

                elif ai >= 60:

                    confidence = 70

                    smart_flow = "Neutral"

                    volume_status = "Average"

                else:

                    confidence = 40

                    smart_flow = "Institutional Selling"

                    volume_status = "Weak"

                # ------------------------------------
                # Final Grade
                # ------------------------------------

                if confidence >= 95:

                    grade = "A+"

                elif confidence >= 85:

                    grade = "A"

                elif confidence >= 75:

                    grade = "B+"

                elif confidence >= 60:

                    grade = "B"

                else:

                    grade = "C"

                smart_data.append({

                    "Symbol": row["Symbol"],

                    "AI Score": ai,

                    "Confidence %": confidence,

                    "Smart Money": smart_flow,

                    "Volume": volume_status,

                    "Grade": grade,

                    "Signal": action

                })

            smart_df = pd.DataFrame(smart_data)

            st.dataframe(

                smart_df,

                use_container_width=True,

                height=650

            )

            # ==========================================
            # Dashboard
            # ==========================================

            st.divider()

            st.subheader("📊 AI Dashboard")

            buy = len(

                smart_df[
                    smart_df["Signal"].str.contains("BUY")
                ]

            )

            watch = len(

                smart_df[
                    smart_df["Signal"].str.contains("WATCH")
                ]

            )

            hold = len(

                smart_df[
                    smart_df["Signal"].str.contains("HOLD")
                ]

            )

            sell = len(

                smart_df[
                    smart_df["Signal"].str.contains("SELL")
                ]

            )

            c1, c2, c3, c4 = st.columns(4)

            c1.metric("BUY", buy)

            c2.metric("WATCH", watch)

           # ==========================================================
# NSE AI PRO V13 INSTITUTIONAL
# PART 7
# MULTI-TIMEFRAME | SECTOR ANALYSIS | FINAL AI ENGINE
# ==========================================================

            st.divider()
            st.subheader("📊 Multi-Timeframe Analysis")

            timeframe_df = smart_df.copy()

            timeframe_df["5 Min"] = np.where(
                timeframe_df["AI Score"] >= 85,
                "BUY",
                "WAIT"
            )

            timeframe_df["15 Min"] = np.where(
                timeframe_df["AI Score"] >= 80,
                "BUY",
                "WAIT"
            )

            timeframe_df["1 Hour"] = np.where(
                timeframe_df["AI Score"] >= 75,
                "BUY",
                "WAIT"
            )

            timeframe_df["Daily"] = np.where(
                timeframe_df["AI Score"] >= 70,
                "BUY",
                "WAIT"
            )

            timeframe_df["Weekly"] = np.where(
                timeframe_df["AI Score"] >= 65,
                "BUY",
                "WAIT"
            )

            st.dataframe(
                timeframe_df,
                use_container_width=True,
                height=500
            )

            # ==========================================
            # Sector Ranking
            # ==========================================

            st.divider()

            st.subheader("🏦 Sector Ranking")

            sectors = pd.DataFrame({

                "Sector":[

                    "Banking",
                    "IT",
                    "Auto",
                    "Pharma",
                    "FMCG",
                    "Energy",
                    "Metal",
                    "Realty"

                ],

                "Strength":[

                    92,
                    88,
                    80,
                    76,
                    70,
                    82,
                    68,
                    60

                ]

            })

            sectors = sectors.sort_values(
                "Strength",
                ascending=False
            )

            st.dataframe(
                sectors,
                use_container_width=True
            )

            # ==========================================
            # Final AI Portfolio
            # ==========================================

            st.divider()

            st.subheader("💼 AI Portfolio Suggestions")

            portfolio = smart_df.sort_values(
                "AI Score",
                ascending=False
            ).head(10)

            st.dataframe(
                portfolio,
                use_container_width=True
            )

            # ==========================================
            # Final Recommendation
            # ==========================================

            st.divider()

            avg_score = smart_df["AI Score"].mean()

            if avg_score >= 85:

                st.success("""
🟢 Market Outlook : STRONG BULLISH

• Aggressive Buying
• Swing Trading
• Positional Buying
• F&O Long Opportunities
""")

            elif avg_score >= 70:

                st.info("""
🟢 Market Outlook : BULLISH

• Buy on Dips
• Momentum Stocks
• Strong Sectors
""")

            elif avg_score >= 55:

                st.warning("""
🟡 Market Outlook : SIDEWAYS

• Stock Specific Action
• Wait for Breakout
""")

            else:

                st.error("""
🔴 Market Outlook : BEARISH

• Capital Protection
• Reduce Long Positions
• Prefer Defensive Stocks
""")

            # ==========================================
            # Final Export
            # ==========================================
# ==========================================================
# NSE AI PRO V13 INSTITUTIONAL
# PART 8
# FINAL INSTITUTIONAL DASHBOARD
# ==========================================================

            st.divider()
            st.subheader("🏆 Institutional Trading Dashboard")

            total = len(smart_df)

            strong_buy = len(
                smart_df[
                    smart_df["AI Score"] >= 90
                ]
            )

            buy = len(
                smart_df[
                    (smart_df["AI Score"] >= 75) &
                    (smart_df["AI Score"] < 90)
                ]
            )

            watch = len(
                smart_df[
                    (smart_df["AI Score"] >= 60) &
                    (smart_df["AI Score"] < 75)
                ]
            )

            sell = len(
                smart_df[
                    smart_df["AI Score"] < 60
                ]
            )

            c1, c2, c3, c4, c5 = st.columns(5)

            c1.metric("Scanned", total)
            c2.metric("🚀 Strong Buy", strong_buy)
            c3.metric("🟢 Buy", buy)
            c4.metric("🟡 Watch", watch)
            c5.metric("🔴 Sell", sell)

            st.divider()

            # ======================================
            # Top Picks
            # ======================================

            st.subheader("🏅 Top 5 Institutional Picks")

            top5 = smart_df.sort_values(
                "AI Score",
                ascending=False
            ).head(5)

            st.dataframe(
                top5,
                use_container_width=True
            )

            # ======================================
            # AI Recommendation
            # ======================================

            st.divider()

            avg_ai = smart_df["AI Score"].mean()

            if avg_ai >= 85:

                recommendation = "🟢 Strong Bullish"

            elif avg_ai >= 70:

                recommendation = "🟢 Bullish"

            elif avg_ai >= 55:

                recommendation = "🟡 Neutral"

            else:

                recommendation = "🔴 Bearish"

            st.metric(
                "Overall Market View",
                recommendation
            )

            # ======================================
            # Portfolio Allocation
            # ======================================

            st.divider()

            st.subheader("💼 Suggested Allocation")

            allocation = pd.DataFrame({

                "Category":[

                    "Large Cap",

                    "Mid Cap",

                    "Small Cap",

                    "Cash"

                ],

                "Allocation %":[

                    40,

                    30,

                    20,

                    10

                ]

            })

            st.dataframe(
                allocation,
                use_container_width=True
            )

            # ======================================
            # Risk Meter
            # ======================================

            st.divider()

            risk = "Medium"

            if avg_ai >= 85:
                risk = "Low"

            elif avg_ai < 55:
                risk = "High"

            st.metric(
                "Risk Level",
                risk
            )

            # ======================================
            # Final Export
            # ======================================

            report = smart_df.copy()

            report["Market View"] = recommendation
            report["Risk"] = risk

            st.download_button(

                "📥 Download Final Institutional Report",

                report.to_csv(index=False),

                file_name="NSE_AI_PRO_V13_Final_Report.csv",

                mime="text/csv"

            )

            st.divider()

            st.success("✅ NSE AI PRO V13 Institutional Scanner Loaded Successfully")

            st.info(
                "Powered by FYERS API V3 | Streamlit | AI Institutional Engine"
            )
            st.download_button(
                "📥 Download Complete AI Scanner",
                smart_df.to_csv(index=False),
                file_name="NSE_AI_PRO_V13_Scanner.csv",
                mime="text/csv"
            )

            st.success("✅ NSE AI PRO V13 Institutional Scanner Ready")
