import streamlit as st
import pandas as pd
import time

# ==========================================================
# NSE AI PRO V12 INSTITUTIONAL
# OPTION CHAIN V2 - PART 1
# ==========================================================

def show_option_chain(fyers):

    st.title("📊 NSE AI PRO V12 Institutional Option Chain")

    # ==========================================
    # Sidebar
    # ==========================================

    st.sidebar.header("⚙ Option Chain Settings")

    asset_type = st.sidebar.radio(

        "Market",

        [

            "INDEX",

            "F&O STOCK"

        ]

    )

    # ==========================================
    # Index List
    # ==========================================

    index_symbols = {

        "NIFTY":"NSE:NIFTY50-INDEX",

        "BANKNIFTY":"NSE:NIFTYBANK-INDEX",

        "FINNIFTY":"NSE:FINNIFTY-INDEX",

        "MIDCPNIFTY":"NSE:MIDCPNIFTY-INDEX",

        "SENSEX":"BSE:SENSEX-INDEX",

        "BANKEX":"BSE:BANKEX-INDEX"

    }

    # ==========================================
    # Popular F&O Stocks
    # ==========================================

    fo_symbols = {

        "RELIANCE":"NSE:RELIANCE-EQ",

        "TCS":"NSE:TCS-EQ",

        "INFY":"NSE:INFY-EQ",

        "HDFCBANK":"NSE:HDFCBANK-EQ",

        "ICICIBANK":"NSE:ICICIBANK-EQ",

        "SBIN":"NSE:SBIN-EQ",

        "AXISBANK":"NSE:AXISBANK-EQ",

        "LT":"NSE:LT-EQ",

        "BHARTIARTL":"NSE:BHARTIARTL-EQ",

        "MARUTI":"NSE:MARUTI-EQ",

        "TATAMOTORS":"NSE:TATAMOTORS-EQ",

        "ADANIENT":"NSE:ADANIENT-EQ"

    }

    if asset_type=="INDEX":

        name = st.sidebar.selectbox(

            "Select Index",

            list(index_symbols.keys())

        )

        symbol = index_symbols[name]

    else:

        search = st.sidebar.text_input(

            "Search Stock"

        )

        stock_list = sorted(fo_symbols.keys())

        if search:

            stock_list = [

                x for x in stock_list

                if search.upper() in x

            ]

        name = st.sidebar.selectbox(

            "Select Stock",

            stock_list

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

        False

    )

    refresh_time = st.sidebar.slider(

        "Refresh Time",

        5,

        60,

        10

    )

    # ==========================================
    # Live Spot
    # ==========================================

    st.subheader("📈 Live Spot")

    try:

        quote = fyers.quotes({

            "symbols":symbol

        })

        q = quote["d"][0]["v"]

        spot = q["lp"]

        col1,col2,col3,col4 = st.columns(4)

        col1.metric(

            "Spot",

            spot,

            q["ch"]

        )

        col2.metric(

            "High",

            q["high_price"]

        )

        col3.metric(

            "Low",

            q["low_price"]

        )

        col4.metric(

            "Volume",

            f"{q['volume']:,}"

        )

    except Exception as e:

        st.error(e)

    st.divider()

    # ==========================================
    # Dynamic Expiry
    # ==========================================

    st.subheader("📅 Expiry")

    expiry_placeholder = st.empty()

    expiry = expiry_placeholder.selectbox(

        "Expiry",

        [

            "Loading Expiry..."

        ]

    )

    st.info(

        "✔ Live Expiry dates will load automatically in Part 2."

    )

    # ==========================================
    # ATM
    # ==========================================

    st.subheader("🎯 ATM Strike")

    atm_placeholder = st.empty()

    atm_placeholder.metric(

        "ATM Strike",

        "--"

    )

    st.divider()

    # ==========================================
    # Summary Cards
    # ==========================================

    c1,c2,c3,c4,c5,c6 = st.columns(6)

    c1.metric("Total CE OI","--")

    c2.metric("Total PE OI","--")

    c3.metric("CE OI Change","--")

    c4.metric("PE OI Change","--")

    c5.metric("PCR","--")

    c6.metric("Max Pain","--")

    st.divider()

    option_placeholder = st.empty()

    if auto_refresh:

        time.sleep(refresh_time)

        st.rerun()
# ==========================================================
# OPTION CHAIN V2 - PART 2A
# LIVE EXPIRY | OPTION CHAIN API | PCR | ATM
# ==========================================================

    st.subheader("📊 Live Option Chain")

    if st.button("🔄 Load Option Chain", use_container_width=True):

        with st.spinner("Loading Option Chain..."):

            try:

                # -----------------------------------------
                # Live Option Chain API
                # -----------------------------------------

                response = fyers.optionchain({

                    "symbol": symbol,

                    "strikecount": strike_count

                })

                if response.get("s") != "ok":

                    st.error(response)

                    st.stop()

                option_data = response.get("data", {})

                # -----------------------------------------
                # Dynamic Expiry
                # -----------------------------------------

                expiry_list = []

                if isinstance(option_data, dict):

                    expiry_list = option_data.get("expiryData", [])

                    if not expiry_list:

                        expiry_list = option_data.get("expiryDates", [])

                if expiry_list:

                    expiry = st.selectbox(

                        "Select Expiry",

                        expiry_list

                    )

                # -----------------------------------------
                # Option Chain Table
                # -----------------------------------------

                chain = None

                if isinstance(option_data, dict):

                    chain = option_data.get("optionsChain")

                    if chain is None:

                        chain = option_data.get("optionChain")

                    if chain is None:

                        chain = option_data.get("chain")

                if chain is None:

                    st.warning("Option Chain data not found.")

                    st.stop()

                df = pd.DataFrame(chain)

                if df.empty:

                    st.warning("Empty Option Chain")

                    st.stop()

                # -----------------------------------------
                # Auto Detect Columns
                # -----------------------------------------

                strike_col = None

                ce_oi_col = None

                pe_oi_col = None

                ce_change_col = None

                pe_change_col = None

                for c in df.columns:

                    x = str(c).lower()

                    if "strike" in x:

                        strike_col = c

                    elif "ce" in x and "oi" in x and "change" not in x:

                        ce_oi_col = c

                    elif "pe" in x and "oi" in x and "change" not in x:

                        pe_oi_col = c

                    elif "ce" in x and "change" in x:

                        ce_change_col = c

                    elif "pe" in x and "change" in x:

                        pe_change_col = c

                # -----------------------------------------
                # Numeric Conversion
                # -----------------------------------------

                for col in [

                    ce_oi_col,

                    pe_oi_col,

                    ce_change_col,

                    pe_change_col

                ]:

                    if col:

                        df[col] = pd.to_numeric(

                            df[col],

                            errors="coerce"

                        ).fillna(0)

                # -----------------------------------------
                # Total OI
                # -----------------------------------------

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

                # -----------------------------------------
                # ATM Strike
                # -----------------------------------------

                atm = "--"

                if strike_col:

                    df["DIFF"] = (

                        df[strike_col] - spot

                    ).abs()

                    atm = df.loc[

                        df["DIFF"].idxmin(),

                        strike_col

                    ]

                # -----------------------------------------
                # Dashboard
                # -----------------------------------------

                m1, m2, m3, m4, m5, m6 = st.columns(6)

                m1.metric(

                    "Total CE OI",

                                   # ==========================================================
                # OPTION CHAIN V2 - PART 2B
                # COLOR TABLE | ATM HIGHLIGHT | CSV EXPORT
                # ==========================================================

                # ---------- Highest OI ----------
                max_ce = (
                    df[ce_oi_col].max()
                    if ce_oi_col else 0
                )

                max_pe = (
                    df[pe_oi_col].max()
                    if pe_oi_col else 0
                )

                # ---------- Table Style ----------
                def highlight_row(row):

                    styles = [""] * len(row)

                    # ATM Highlight
                    if strike_col and row[strike_col] == atm:
                        for i in range(len(styles)):
                            styles[i] = "background-color:#FFF59D;"

                    # Highest CE OI
                    if ce_oi_col:
                        idx = df.columns.get_loc(ce_oi_col)
                        if row[ce_oi_col] == max_ce:
                            styles[idx] = (
                                "background-color:#ff5252;"
                                "color:white;"
                                "font-weight:bold;"
                            )

                    # Highest PE OI
                    if pe_oi_col:
                        idx = df.columns.get_loc(pe_oi_col)
                        if row[pe_oi_col] == max_pe:
                            styles[idx] = (
                                "background-color:#4caf50;"
                                "color:white;"
                                "font-weight:bold;"
                            )

                    # CE Change OI
                    if ce_change_col:
                        idx = df.columns.get_loc(ce_change_col)
                        if row[ce_change_col] > 0:
                            styles[idx] = (
                                "background-color:#ffcdd2;"
                            )

                    # PE Change OI
                    if pe_change_col:
                        idx = df.columns.get_loc(pe_change_col)
                        if row[pe_change_col] > 0:
                            styles[idx] = (
                                "background-color:#c8e6c9;"
                            )

                    return styles

                st.subheader("📊 Live Option Chain")

                styled_df = df.style.apply(
                    highlight_row,
                    axis=1
                )

                st.dataframe(
                    styled_df,
                    use_container_width=True,
                    height=700
                )

                # ---------- Top OI Summary ----------

                col1, col2 = st.columns(2)

                if ce_oi_col and strike_col:

                    ce_row = df.loc[
                        df[ce_oi_col].idxmax()
                    ]

                    col1.error(
                        f"🔴 Highest CE OI : "
                        f"{ce_row[strike_col]} | "
                        f"{int(ce_row[ce_oi_col]):,}"
                    )

                if pe_oi_col and strike_col:

                    pe_row = df.loc[
                        df[pe_oi_col].idxmax()
                    ]

                    col2.success(
                        f"🟢 Highest PE
                # ==========================================================
                # OPTION CHAIN V2 - PART 3
                # OI BUILD-UP | MAX PAIN | SUPPORT | RESISTANCE
                # ==========================================================

                st.divider()

                st.subheader("🏦 Institutional OI Analysis")

                try:

                    # -----------------------------
                    # Highest CE & PE OI
                    # -----------------------------

                    resistance = None
                    support = None

                    if ce_oi_col:

                        ce_row = df.loc[
                            df[ce_oi_col].idxmax()
                        ]

                        resistance = ce_row[strike_col]

                    if pe_oi_col:

                        pe_row = df.loc[
                            df[pe_oi_col].idxmax()
                        ]

                        support = pe_row[strike_col]

                    # -----------------------------
                    # Max Pain
                    # -----------------------------

                    df["TOTAL_OI"] = 0

                    if ce_oi_col:
                        df["TOTAL_OI"] += df[ce_oi_col]

                    if pe_oi_col:
                        df["TOTAL_OI"] += df[pe_oi_col]

                    max_pain = df.loc[
                        df["TOTAL_OI"].idxmax()
                    ][strike_col]

                    # -----------------------------
                    # OI Build-Up
                    # -----------------------------

                    build_up = "Neutral"

                    if total_pe_change > total_ce_change:

                        build_up = "Long Build-up"

                    elif total_ce_change > total_pe_change:

                        build_up = "Short Build-up"

                    # -----------------------------
                    # Market Bias
                    # -----------------------------

                    if pcr >= 1.30:

                        bias = "Bullish"

                    elif pcr <= 0.70:

                        bias = "Bearish"

                    else:

                        bias = "Neutral"

                    # -----------------------------
                    # Dashboard
                    # -----------------------------

                    a,b,c,d = st.columns(4)

                    a.metric(

                        "Support",

                        support

                    )

                    b.metric(

                        "Resistance",

                        resistance

                    )

                    c.metric(

                        "Max Pain",

                        max_pain

                    )

                    d.metric(

                        "OI Build-up",

                        build_up

                    )

                    st.divider()

                    # -----------------------------
                    # Institutional View
                    # -----------------------------

                    if bias=="Bullish":

                        st.success("""

🟢 Institutional View

• Heavy Put Writing

• Strong Support

• Bullish Bias

• Buy On Dips

""")

                    elif bias=="Bearish":

                        st.error("""

🔴 Institutional View

• Heavy Call Writing

• Strong Resistance

• Bearish Bias

• Sell On Rise

""")

                    else:

                        st.info("""

🟡 Institutional View

• Balanced OI

• Sideways Market

• Wait For Breakout

""")

                    # -----------------------------
                    # OI Summary
                    # -----------------------------

                    summary = pd.DataFrame({

                        "Indicator":[

                            "Spot",

                            "ATM",

                            "PCR",

                            "Support",

                            "Resistance",

                            "Max Pain",

                            "Market Bias",

                            "OI Build-up"

                        ],

                        "Value":[

                            spot,

                            atm
                # ==========================================================
                # OPTION CHAIN V2 - PART 4
                # CHARTS | AI SCORE | SMART MONEY | LIVE ALERTS
                # ==========================================================

                import plotly.express as px

                st.divider()
                st.subheader("📊 OI Charts")

                try:

                    # -----------------------------
                    # CE vs PE OI Chart
                    # -----------------------------

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

                    # -----------------------------
                    # OI Change Chart
                    # -----------------------------

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

                    st.warning(e)

                # ==================================================
                # AI SCORE
                # ==================================================

                st.divider()

                st.subheader("🤖 AI Institutional Score")

                score = 50

                if pcr > 1.20:

                    score += 20

                elif pcr < 0.80:

                    score -= 20

                if build_up == "Long Build-up":

                    score += 20

                elif build_up == "Short Build-up":

                    score -= 20

                score = max(0, min(score, 100))

                col1, col2 = st.columns(2)

                col1.metric(

                    "Institutional Score",

                    f"{score}/100"

                )

                if score >= 80:

                    signal = "🟢 STRONG BUY"

                elif score >= 60:

                    signal = "🟢 BUY"

                elif score >= 40:

                    signal = "🟡 HOLD"

                else:

                    signal = "🔴 SELL"

                col2.metric(

                    "AI Signal",

                    signal

                )

                # ==================================================
                # SMART MONEY
                # ==================================================

                st.divider()

                st.subheader("💰 Smart Money Flow")

                if score >= 80:

                    st.success("""

Large Put Writing Detected

Institutional Buying Seen

Smart Money Entering

""")

                elif score <= 30:

                    st.error("""

Heavy Call Writing

Institutional Selling

Smart Money Exiting

""")

                else:

                      # ==========================================================
                # OPTION CHAIN V2 - PART 5
                # FINAL ADVANCED DASHBOARD
                # ==========================================================

                st.divider()
                st.subheader("📊 Advanced Institutional Dashboard")

                # ------------------------------------------
                # Market Strength
                # ------------------------------------------

                if pcr >= 1.30:
                    market_strength = "🟢 Strong Bullish"

                elif pcr >= 1.00:
                    market_strength = "🟢 Bullish"

                elif pcr >= 0.80:
                    market_strength = "🟡 Sideways"

                elif pcr >= 0.60:
                    market_strength = "🔴 Bearish"

                else:
                    market_strength = "🔴 Strong Bearish"

                c1, c2, c3 = st.columns(3)

                c1.metric(
                    "Market Strength",
                    market_strength
                )

                c2.metric(
                    "Spot Price",
                    round(spot, 2)
                )

                c3.metric(
                    "ATM Strike",
                    atm
                )

                st.divider()

                # ------------------------------------------
                # OI Interpretation
                # ------------------------------------------

                st.subheader("📈 OI Interpretation")

                if total_pe > total_ce:

                    st.success("""
🟢 Heavy Put Writing

• Strong Support

• Institutions Bullish

• Buy on Dips
""")

                elif total_ce > total_pe:

                    st.error("""
🔴 Heavy Call Writing

• Strong Resistance

• Institutions Bearish

• Sell on Rise
""")

                else:

                    st.info("""
🟡 Balanced OI

No Clear Direction
""")

                # ------------------------------------------
                # Support & Resistance Table
                # ------------------------------------------

                st.divider()

                sr = pd.DataFrame({

                    "Level":[

                        "Support",

                        "ATM",

                        "Resistance",

                        "Max Pain"

                    ],

                    "Strike":[

                        support,

                        atm,

                        resistance,

                        max_pain

                    ]

                })

                st.subheader("🎯 Important Levels")

                st.dataframe(
                    sr,
                    use_container_width=True
                )

                # ------------------------------------------
                # Institutional Checklist
                # ------------------------------------------

                st.divider()

                checklist = pd.DataFrame({

                    "Parameter":[

                        "PCR",

                        "Long Build-up",

                        "Support",

                        "Resistance",

                        "Max Pain",

                        "AI Score"

                    ],

                    "Status":[

                        round(pcr,2),

                        build_up,

                        support,

                        resistance,

                        max_pain,

                        score

                    ]

                })

                st.subheader("✅ Institutional Checklist")

                st.dataframe(

                    checklist,

                    use_container_width=True

                )

                # ------------------------------------------
                # Excel Ready Export
                # ------------------------------------------

                st.download_button(

                    "📥 Download Complete Option Chain",

                    df.to_csv(index=False),

                    file_name=f"{name}_Institutional_OptionChain.csv",

                    mime="text/csv"

                )

        # ==========================================================
# OPTION CHAIN V2 - PART 6 (ULTIMATE)
# LIVE DASHBOARD | AUTO REFRESH | FINAL SUMMARY
# ==========================================================

                st.divider()
                st.subheader("📡 Live Market Dashboard")

                # -------------------------------------
                # Market Status
                # -------------------------------------

                market_status = "🟡 SIDEWAYS"

                if pcr > 1.20:
                    market_status = "🟢 BULLISH"

                elif pcr < 0.80:
                    market_status = "🔴 BEARISH"

                c1, c2, c3, c4 = st.columns(4)

                c1.metric("Spot", round(spot, 2))
                c2.metric("ATM", atm)
                c3.metric("PCR", round(pcr, 2))
                c4.metric("Status", market_status)

                st.divider()

                # -------------------------------------
                # Top CE / PE Writing
                # -------------------------------------

                st.subheader("🏦 Top Writing Levels")

                left, right = st.columns(2)

                if ce_oi_col:

                    top_ce = df.nlargest(5, ce_oi_col)[
                        [strike_col, ce_oi_col]
                    ]

                    left.error("🔴 Top 5 CE Writing")
                    left.dataframe(
                        top_ce,
                        use_container_width=True
                    )

                if pe_oi_col:

                    top_pe = df.nlargest(5, pe_oi_col)[
                        [strike_col, pe_oi_col]
                    ]

                    right.success("🟢 Top 5 PE Writing")
                    right.dataframe(
                        top_pe,
                        use_container_width=True
                    )

                st.divider()

                # -------------------------------------
                # Trading View
                # -------------------------------------

                st.subheader("📈 Trading View")

                if score >= 80:

                    st.success("""
✅ Strong Bullish

• Buy CE

• Bull Call Spread

• Bull Put Spread

• Buy On Dips
""")

                elif score >= 60:

                    st.success("""
🟢 Bullish

Trend Positive

Look For Buy Opportunity
""")

                elif score >= 40:

                    st.warning("""
🟡 Neutral

Wait For Breakout
""")

                else:

                    st.error("""
🔴 Bearish

Buy PE

Sell On Rise

Bear Call Spread
""")

                st.divider()

                # -------------------------------------
                # Dashboard Footer
                # -------------------------------------

                st.success("✅ NSE AI PRO V12 Institutional Option Chain Ready")

                st.caption(
                    "Version : V12 Institutional Edition"
                )

                st.caption(
                    "Powered By FYERS API V3"
                )

# ==========================================================
# AUTO REFRESH
# ==========================================================

    if auto_refresh:

        with st.spinner("Refreshing Option Chain..."):

            time.sleep(refresh_time)

        st.rerun()                    
# ==========================================================
# OPTION CHAIN V2 - PART 7
# WATCHLIST | ALERTS | FAVORITES | SESSION STATS
# ==========================================================

                st.divider()
                st.subheader("⭐ Watchlist & Alerts")

                # -----------------------------
                # Save Current Symbol
                # -----------------------------
                if "watchlist" not in st.session_state:
                    st.session_state.watchlist = []

                col1, col2 = st.columns(2)

                if col1.button("⭐ Add to Watchlist"):

                    if symbol not in st.session_state.watchlist:

                        st.session_state.watchlist.append(symbol)

                        st.success(f"{symbol} added.")

                if col2.button("🗑 Clear Watchlist"):

                    st.session_state.watchlist = []

                    st.success("Watchlist Cleared")

                if st.session_state.watchlist:

                    st.write("### ⭐ My Watchlist")

                    st.dataframe(

                        pd.DataFrame({

                            "Symbols": st.session_state.watchlist

                        }),

                        use_container_width=True

                    )

                # -----------------------------------------
                # Market Alert
                # -----------------------------------------

                st.divider()

                st.subheader("🚨 Instant Alert")

                if pcr >= 1.30:

                    st.success("🟢 Strong Bullish Option Chain")

                elif pcr <= 0.70:

                    st.error("🔴 Strong Bearish Option Chain")

                else:

                    st.warning("🟡 Sideways Market")

                # -----------------------------------------
                # Session Statistics
                # -----------------------------------------

                st.divider()

                st.subheader("📊 Session Statistics")

                c1, c2, c3 = st.columns(3)

                c1.metric(

                    "Total Strikes",

                    len(df)

                )

                c2.metric(

                    "Current Symbol",

                    name

                )

                c3.metric(

                    "Expiry",

                    expiry

                )

                # -----------------------------------------
                # Export Summary
                # -----------------------------------------

                export_df = pd.DataFrame({

                    "Spot":[spot],

                    "ATM":[atm],

                    "PCR":[pcr],

                    "Support":[support],

                    "Resistance":[resistance],

                    "Max Pain":[max_pain],

                    "AI Score":[score]

                })

                st.download_button(

                    "📥 Export Dashboard Summary",

                    export_df.to_csv(index=False),

                    file_name="dashboard_summary.csv",

                    mime="text/csv"

                )

                st.divider()

                st.success("✅ Option Chain V2 Ultimate Ready")
