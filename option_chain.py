import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import time

# ==========================================================
# NSE AI PRO V13 INSTITUTIONAL
# OPTION CHAIN PRO (FYERS API V3)
# PART 1
# ==========================================================

def show_option_chain(fyers):

    st.title("📊 NSE AI PRO V13 Institutional Option Chain")

    # ==========================================
    # Sidebar
    # ==========================================

    st.sidebar.header("⚙ Settings")

    market_type = st.sidebar.radio(
        "Market",
        ["INDEX", "F&O STOCK"]
    )

    index_symbols = {
        "NIFTY": "NSE:NIFTY50-INDEX",
        "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX",
        "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        "SENSEX": "BSE:SENSEX-INDEX",
        "BANKEX": "BSE:BANKEX-INDEX"
    }

    fo_symbols = {
        "RELIANCE": "NSE:RELIANCE-EQ",
        "TCS": "NSE:TCS-EQ",
        "INFY": "NSE:INFY-EQ",
        "HDFCBANK": "NSE:HDFCBANK-EQ",
        "ICICIBANK": "NSE:ICICIBANK-EQ",
        "SBIN": "NSE:SBIN-EQ",
        "AXISBANK": "NSE:AXISBANK-EQ",
        "LT": "NSE:LT-EQ",
        "MARUTI": "NSE:MARUTI-EQ",
        "BHARTIARTL": "NSE:BHARTIARTL-EQ",
        "TATAMOTORS": "NSE:TATAMOTORS-EQ",
        "ADANIENT": "NSE:ADANIENT-EQ"
    }

    if market_type == "INDEX":

        name = st.sidebar.selectbox(
            "Select Index",
            list(index_symbols.keys())
        )

        symbol = index_symbols[name]

    else:

        search = st.sidebar.text_input(
            "Search Stock"
        )

        stocks = sorted(fo_symbols.keys())

        if search:
            stocks = [
                x for x in stocks
                if search.upper() in x
            ]

        name = st.sidebar.selectbox(
            "Select Stock",
            stocks
        )

        symbol = fo_symbols[name]

    strike_count = st.sidebar.slider(
        "Strike Count",
        5,
        30,
        15
    )

    auto_refresh = st.sidebar.checkbox(
        "Auto Refresh",
        value=False
    )

    refresh_time = st.sidebar.slider(
        "Refresh Time (Seconds)",
        5,
        60,
        10
    )

    st.divider()

    # ==========================================
    # Live Spot Quote
    # ==========================================

    try:

        quote = fyers.quotes({
            "symbols": symbol
        })

        q = quote["d"][0]["v"]

        spot = float(q["lp"])

        c1, c2, c3, c4 = st.columns(4)

        c1.metric("Spot", spot, q["ch"])
        c2.metric("High", q["high_price"])
        c3.metric("Low", q["low_price"])
        c4.metric("Volume", f"{int(q['volume']):,}")

    except Exception as e:

        st.error(f"Quote Error : {e}")
        return

    st.divider()

    # ==========================================
    # Expiry Placeholder
    # ==========================================

    st.subheader("📅 Expiry")

    expiry = st.selectbox(
        "Expiry",
        ["Loading..."]
    )

    st.info(
        "Live expiry dates will load in Part 2."
    )

    st.divider()

    # ==========================================
    # Dashboard Cards
    # ==========================================

    a, b, c, d, e, f = st.columns(6)

    a.metric("CE OI", "--")
    b.metric("PE OI", "--")
    c.metric("PCR", "--")
    d.metric("ATM", "--")
    e.metric("Max Pain", "--")
    f.metric("AI Score", "--")

    st.divider()

    load = st.button(
        "🚀 Load Option Chain",
        use_container_width=True
    )

    if not load:
        return

    # ===== PART 2 STARTS BELOW =====
    # ==========================================================
    # PART 2
    # LOAD OPTION CHAIN DATA
    # ==========================================================

    with st.spinner("Loading Option Chain..."):

        try:

            response = fyers.optionchain({
                "symbol": symbol,
                "strikecount": strike_count
            })

        except Exception as e:

            st.error(f"Option Chain API Error : {e}")
            return

    if not isinstance(response, dict):

        st.error("Invalid API Response")
        return

    if response.get("s") != "ok":

        st.error(response)
        return

    option_data = response.get("data", {})

    # ==========================================
    # Expiry Dates
    # ==========================================

    expiry_list = []

    if isinstance(option_data, dict):

        expiry_list = (
            option_data.get("expiryData")
            or option_data.get("expiryDates")
            or option_data.get("expiries")
            or []
        )

    if expiry_list:

        expiry = st.selectbox(
            "Select Expiry",
            expiry_list
        )

    # ==========================================
    # Option Chain
    # ==========================================

    chain = (
        option_data.get("optionsChain")
        or option_data.get("optionChain")
        or option_data.get("chain")
    )

    if chain is None:

        st.warning("Option Chain data not available.")
        return

    df = pd.DataFrame(chain)

    if df.empty:

        st.warning("Empty Option Chain")
        return

    # ==========================================
    # Auto Detect Columns
    # ==========================================

    strike_col = None
    ce_oi_col = None
    pe_oi_col = None
    ce_change_col = None
    pe_change_col = None

    for col in df.columns:

        x = str(col).lower()

        if "strike" in x:

            strike_col = col

        elif "ce" in x and "oi" in x and "change" not in x:

            ce_oi_col = col

        elif "pe" in x and "oi" in x and "change" not in x:

            pe_oi_col = col

        elif "ce" in x and "change" in x:

            ce_change_col = col

        elif "pe" in x and "change" in x:

            pe_change_col = col

    # ==========================================
    # Convert Numeric
    # ==========================================

    numeric_cols = [
        strike_col,
        ce_oi_col,
        pe_oi_col,
        ce_change_col,
        pe_change_col
    ]

    for col in numeric_cols:

        if col:

            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            ).fillna(0)

    # ==========================================
    # Total OI
    # ==========================================

    total_ce = df[ce_oi_col].sum() if ce_oi_col else 0
    total_pe = df[pe_oi_col].sum() if pe_oi_col else 0

    total_ce_change = (
        df[ce_change_col].sum()
        if ce_change_col else 0
    )

    total_pe_change = (
        df[pe_change_col].sum()
        if pe_change_col else 0
    )

    pcr = round(
        total_pe / total_ce,
        2
    ) if total_ce else 0

    # ==========================================
    # ATM Strike
    # ==========================================

    atm = "--"

    if strike_col:

        df["DIFF"] = (
            df[strike_col] - spot
        ).abs()

        atm = df.loc[
            df["DIFF"].idxmin(),
            strike_col
        ]

    # ===== PART 3 STARTS BELOW =====
    # ==========================================================
    # PART 3
    # DASHBOARD | SUPPORT | RESISTANCE | MAX PAIN
    # ==========================================================

    # ------------------------------------------
    # Highest CE & PE OI
    # ------------------------------------------

    support = "-"
    resistance = "-"
    max_pain = "-"

    if ce_oi_col:

        ce_row = df.loc[df[ce_oi_col].idxmax()]
        resistance = ce_row[strike_col]

    if pe_oi_col:

        pe_row = df.loc[df[pe_oi_col].idxmax()]
        support = pe_row[strike_col]

    # ------------------------------------------
    # Max Pain
    # ------------------------------------------

    df["TOTAL_OI"] = 0

    if ce_oi_col:
        df["TOTAL_OI"] += df[ce_oi_col]

    if pe_oi_col:
        df["TOTAL_OI"] += df[pe_oi_col]

    if strike_col:

        max_pain = df.loc[
            df["TOTAL_OI"].idxmax(),
            strike_col
        ]

    # ------------------------------------------
    # OI Build-up
    # ------------------------------------------

    if total_pe_change > total_ce_change:

        build_up = "🟢 Long Build-up"

    elif total_ce_change > total_pe_change:

        build_up = "🔴 Short Build-up"

    else:

        build_up = "🟡 Neutral"

    # ------------------------------------------
    # Market Bias
    # ------------------------------------------

    if pcr >= 1.30:

        market_bias = "🟢 Bullish"

    elif pcr <= 0.70:

        market_bias = "🔴 Bearish"

    else:

        market_bias = "🟡 Sideways"

    # ------------------------------------------
    # Dashboard Cards
    # ------------------------------------------

    st.subheader("📊 Option Chain Summary")

    c1, c2, c3 = st.columns(3)

    c1.metric(
        "Total CE OI",
        f"{int(total_ce):,}"
    )

    c2.metric(
        "Total PE OI",
        f"{int(total_pe):,}"
    )

    c3.metric(
        "PCR",
        round(pcr, 2)
    )

    c4, c5, c6 = st.columns(3)

    c4.metric(
        "ATM Strike",
        atm
    )

    c5.metric(
        "Support",
        support
    )

    c6.metric(
        "Resistance",
        resistance
    )

    c7, c8 = st.columns(2)

    c7.metric(
        "Max Pain",
        max_pain
    )

    c8.metric(
        "OI Build-up",
        build_up
    )

    st.info(f"📈 Market Bias : {market_bias}")

    st.divider()

    # ===== PART 4 STARTS BELOW =====
    # ==========================================================
    # PART 4
    # OPTION CHAIN TABLE | ATM HIGHLIGHT | OI HIGHLIGHT
    # ==========================================================

    st.subheader("📋 Live Option Chain")

    # ------------------------------------------
    # Highest OI
    # ------------------------------------------

    max_ce = df[ce_oi_col].max() if ce_oi_col else 0
    max_pe = df[pe_oi_col].max() if pe_oi_col else 0

    # ------------------------------------------
    # Highlight Function
    # ------------------------------------------

    def highlight_row(row):

        styles = [""] * len(row)

        # ATM Strike
        if strike_col and row[strike_col] == atm:

            for i in range(len(styles)):
                styles[i] = (
                    "background-color:#FFF59D;"
                    "font-weight:bold;"
                )

        # Highest CE OI
        if ce_oi_col:

            idx = df.columns.get_loc(ce_oi_col)

            if row[ce_oi_col] == max_ce:

                styles[idx] = (
                    "background-color:#ff4d4d;"
                    "color:white;"
                    "font-weight:bold;"
                )

        # Highest PE OI
        if pe_oi_col:

            idx = df.columns.get_loc(pe_oi_col)

            if row[pe_oi_col] == max_pe:

                styles[idx] = (
                    "background-color:#00C853;"
                    "color:white;"
                    "font-weight:bold;"
                )

        # CE Change Highlight
        if ce_change_col:

            idx = df.columns.get_loc(ce_change_col)

            if row[ce_change_col] > 0:

                styles[idx] = (
                    "background-color:#ffcdd2;"
                )

        # PE Change Highlight
        if pe_change_col:

            idx = df.columns.get_loc(pe_change_col)

            if row[pe_change_col] > 0:

                styles[idx] = (
                    "background-color:#C8E6C9;"
                )

        return styles

    styled_df = df.style.apply(
        highlight_row,
        axis=1
    )

    st.dataframe(
        styled_df,
        use_container_width=True,
        height=700
    )

    st.divider()

    # ------------------------------------------
    # Top OI Levels
    # ------------------------------------------

    left, right = st.columns(2)

    if ce_oi_col:

        ce_top = df.nlargest(
            5,
            ce_oi_col
        )[[strike_col, ce_oi_col]]

        left.error("🔴 Top 5 CE OI")

        left.dataframe(
            ce_top,
            use_container_width=True
        )

    if pe_oi_col:

        pe_top = df.nlargest(
            5,
            pe_oi_col
        )[[strike_col, pe_oi_col]]

        right.success("🟢 Top 5 PE OI")

        right.dataframe(
            pe_top,
            use_container_width=True
        )

    st.divider()

    # ===== PART 5 STARTS BELOW =====
    # ==========================================================
    # PART 5
    # OI CHARTS | AI SCORE | SMART MONEY
    # ==========================================================

    st.subheader("📊 Open Interest Analysis")

    try:

        # ------------------------------------------
        # CE vs PE OI Chart
        # ------------------------------------------

        if strike_col and ce_oi_col and pe_oi_col:

            chart_df = df[[
                strike_col,
                ce_oi_col,
                pe_oi_col
            ]].copy()

            fig = px.bar(

                chart_df,

                x=strike_col,

                y=[ce_oi_col, pe_oi_col],

                barmode="group",

                title="CE vs PE Open Interest"

            )

            st.plotly_chart(
                fig,
                use_container_width=True
            )

        # ------------------------------------------
        # OI Change Chart
        # ------------------------------------------

        if ce_change_col and pe_change_col:

            fig2 = px.bar(

                df,

                x=strike_col,

                y=[
                    ce_change_col,
                    pe_change_col
                ],

                barmode="group",

                title="Open Interest Change"

            )

            st.plotly_chart(
                fig2,
                use_container_width=True
            )

    except Exception as e:

        st.warning(f"Chart Error : {e}")

    st.divider()

    # ==========================================================
    # AI SCORE
    # ==========================================================

    ai_score = 50

    if pcr >= 1.30:
        ai_score += 20
    elif pcr <= 0.70:
        ai_score -= 20

    if build_up.startswith("🟢"):
        ai_score += 20
    elif build_up.startswith("🔴"):
        ai_score -= 20

    if total_pe > total_ce:
        ai_score += 10
    else:
        ai_score -= 10

    ai_score = max(0, min(ai_score, 100))

    if ai_score >= 80:
        signal = "🟢 STRONG BUY"

    elif ai_score >= 60:
        signal = "🟢 BUY"

    elif ai_score >= 40:
        signal = "🟡 HOLD"

    else:
        signal = "🔴 SELL"

    left, right = st.columns(2)

    left.metric(
        "🤖 AI Score",
        f"{ai_score}/100"
    )

    right.metric(
        "Signal",
        signal
    )

    st.divider()

    # ==========================================================
    # SMART MONEY
    # ==========================================================

    st.subheader("💰 Smart Money Flow")

    if ai_score >= 80:

        st.success("""
🟢 Heavy Put Writing

🏦 Institutional Buying

📈 Strong Bullish Bias

💰 Smart Money Entering
""")

    elif ai_score >= 60:

        st.info("""
🟢 Moderate Bullish

📊 Positive OI Structure

🏦 Institutions Active
""")

    elif ai_score >= 40:

        st.warning("""
🟡 Neutral Market

⚖
    # ==========================================================
    # PART 6
    # INSTITUTIONAL DASHBOARD | ALERTS | EXPORT
    # ==========================================================

    st.subheader("🏦 Institutional Dashboard")

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "Support",
        support
    )

    col2.metric(
        "Resistance",
        resistance
    )

    col3.metric(
        "Max Pain",
        max_pain
    )

    col4.metric(
        "Market Bias",
        market_bias
    )

    st.divider()

    # ==========================================
    # Trading View
    # ==========================================

    st.subheader("📈 Trading View")

    if ai_score >= 80:

        st.success("""
🟢 STRONG BUY

• Heavy Put Writing

• Strong Support

• Bullish Structure

• Buy on Dips
""")

    elif ai_score >= 60:

        st.info("""
🟢 BUY

• Positive OI

• Bullish Bias

• Momentum Positive
""")

    elif ai_score >= 40:

        st.warning("""
🟡 HOLD

• Wait for Breakout

• Mixed OI

• Neutral Bias
""")

    else:

        st.error("""
🔴 SELL

• Heavy Call Writing

• Bearish Structure

• Sell on Rise
""")

    st.divider()

    # ==========================================
    # Live Alerts
    # ==========================================

    st.subheader("🚨 Live Alerts")

    if pcr >= 1.30:

        st.success("🟢 PCR indicates Strong Bullish Sentiment")

    elif pcr <= 0.70:

        st.error("🔴 PCR indicates Strong Bearish Sentiment")

    else:

        st.warning("🟡 PCR indicates Sideways Market")

    if build_up.startswith("🟢"):

        st.success("📈 Long Build-up Detected")

    elif build_up.startswith("🔴"):

        st.error("📉 Short Build-up Detected")

    else:

        st.info("⚖ No Strong Build-up")

    st.divider()

    # ==========================================
    # Export Option Chain
    # ==========================================

    st.download_button(

        "📥 Download Option Chain CSV",

        data=df.to_csv(index=False),

        file_name=f"{name}_OptionChain.csv",

        mime="text/csv"

    )

    st.success("✅ Option Chain Loaded Successfully")

    # ==========================================
    # Auto Refresh
    # ==========================================

    if auto_refresh:

        time.sleep(refresh_time)

        st.rerun()
            # ==========================================================
    # PART 7
    # HEATMAP | EXPIRY HIGHLIGHT | F&O SEARCH | OI SUMMARY
    # ==========================================================

    st.divider()
    st.subheader("🔥 Institutional OI Dashboard")

    # ==========================================
    # Top Summary
    # ==========================================

    s1, s2, s3, s4 = st.columns(4)

    s1.metric(
        "🔴 Total CE OI",
        f"{int(total_ce):,}"
    )

    s2.metric(
        "🟢 Total PE OI",
        f"{int(total_pe):,}"
    )

    s3.metric(
        "📈 CE OI Change",
        f"{int(total_ce_change):,}"
    )

    s4.metric(
        "📉 PE OI Change",
        f"{int(total_pe_change):,}"
    )

    st.divider()

    # ==========================================
    # Expiry Highlight
    # ==========================================

    if expiry != "Loading...":

        st.success(
            f"📅 Selected Expiry : {expiry}"
        )

    # ==========================================
    # Heatmap
    # ==========================================

    st.subheader("🌡 OI Heatmap")

    heat_df = df.copy()

    if ce_oi_col:

        heat_df = heat_df.style.background_gradient(
            subset=[ce_oi_col],
            cmap="Reds"
        )

    if pe_oi_col:

        heat_df = heat_df.background_gradient(
            subset=[pe_oi_col],
            cmap="Greens"
        )

    st.dataframe(
        heat_df,
        use_container_width=True,
        height=700
    )

    st.divider()

    # ==========================================
    # Top OI Writers
    # ==========================================

    left, right = st.columns(2)

    if ce_oi_col:

        left.subheader("🔴 Top CE Writers")

        left.dataframe(

            df.nlargest(
                10,
                ce_oi_col
            )[
                [strike_col, ce_oi_col]
            ],

            use_container_width=True

        )

    if pe_oi_col:

        right.subheader("🟢 Top PE Writers")

        right.dataframe(

            df.nlargest(
                10,
                pe_oi_col
            )[
                [strike_col, pe_oi_col]
            ],

            use_container_width=True

        )

    st.divider()

    # ==========================================
    # Institutional Rating
    # ==========================================

    st.subheader("🏦 Institutional Rating")

    if ai_score >= 90:

        st.success("⭐⭐⭐⭐⭐ Excellent")

    elif ai_score >= 80:

        st.success("⭐⭐⭐⭐ Very Strong")

    elif ai_score >= 70:

        st.info("⭐⭐⭐ Strong")

    elif ai_score >= 60:

        st.warning("⭐⭐ Moderate")

    else:

        st.error("⭐ Weak")

    st.divider()

    # ==========================================
    # Live F&O Search
    # ==========================================

    st.success(
        "✅ Supports NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX, BANKEX and configured F&O Stocks."
    )

    st.caption("🚀 NSE AI PRO V13 Institutional Option Chain")
        # ==========================================================
    # PART 8
    # PCR METER | SMART MONEY | AI ENTRY/EXIT | BREAKOUT
    # ==========================================================

    st.divider()
    st.subheader("🤖 AI Institutional Analysis")

    # ==========================================
    # PCR Meter
    # ==========================================

    st.write("### 📊 PCR Strength")

    pcr_percent = min(max(pcr / 2, 0), 1)

    st.progress(pcr_percent)

    if pcr >= 1.30:
        st.success(f"PCR : {pcr} → Strong Bullish")

    elif pcr >= 1.00:
        st.info(f"PCR : {pcr} → Bullish")

    elif pcr >= 0.80:
        st.warning(f"PCR : {pcr} → Neutral")

    else:
        st.error(f"PCR : {pcr} → Bearish")

    st.divider()

    # ==========================================
    # Smart Money Index
    # ==========================================

    smart_money = 50

    if total_pe > total_ce:
        smart_money += 20

    if pcr > 1.20:
        smart_money += 15

    if build_up.startswith("🟢"):
        smart_money += 15

    smart_money = min(100, smart_money)

    col1, col2 = st.columns(2)

    col1.metric(
        "💰 Smart Money",
        f"{smart_money}/100"
    )

    if smart_money >= 80:

        money_signal = "Institutional Buying"

    elif smart_money >= 60:

        money_signal = "Accumulation"

    elif smart_money >= 40:

        money_signal = "Neutral"

    else:

        money_signal = "Distribution"

    col2.metric(
        "Flow",
        money_signal
    )

    st.divider()

    # ==========================================
    # Entry / Exit
    # ==========================================

    st.subheader("🎯 AI Entry / Exit")

    if ai_score >= 80:

        st.success(f"""
✅ BUY ZONE

Entry : Above {spot}

Support : {support}

Target : {res
