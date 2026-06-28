import streamlit as st
import pandas as pd
import time

# ==========================================================
# OPTION CHAIN - PART 1
# NSE AI PRO V12 Institutional Edition
# ==========================================================


def show_option_chain(fyers):

    st.title("📊 NSE AI PRO V12 - Institutional Option Chain")

    # ---------------------------------------
    # Sidebar Settings
    # ---------------------------------------

    st.sidebar.header("⚙️ Option Chain Settings")

    index = st.sidebar.selectbox(

        "Select Index",

        [
            "NIFTY",
            "BANKNIFTY",
            "FINNIFTY",
            "MIDCPNIFTY",
            "SENSEX",
            "BANKEX"
        ]

    )

    symbol_map = {

        "NIFTY": "NSE:NIFTY50-INDEX",

        "BANKNIFTY": "NSE:NIFTYBANK-INDEX",

        "FINNIFTY": "NSE:FINNIFTY-INDEX",

        "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",

        "SENSEX": "BSE:SENSEX-INDEX",

        "BANKEX": "BSE:BANKEX-INDEX"

    }

    strike_count = st.sidebar.slider(

        "Strike Count",

        min_value=5,

        max_value=30,

        value=10

    )

    auto_refresh = st.sidebar.checkbox(

        "Auto Refresh",

        value=False

    )

    refresh_time = st.sidebar.slider(

        "Refresh Seconds",

        5,

        60,

        10

    )

    # ---------------------------------------
    # Spot Price
    # ---------------------------------------

    st.subheader("📈 Spot Price")

    try:

        quote = fyers.quotes({

            "symbols": symbol_map[index]

        })

        if quote.get("s") == "ok":

            q = quote["d"][0]["v"]

            c1, c2, c3 = st.columns(3)

            c1.metric(
                "Spot",
                q.get("lp", 0),
                q.get("ch", 0)
            )

            c2.metric(
                "High",
                q.get("high_price", "-")
            )

            c3.metric(
                "Low",
                q.get("low_price", "-")
            )

        else:

            st.error(quote)

    except Exception as e:

        st.error(e)

    st.divider()

    # ---------------------------------------
    # Expiry Selection
    # ---------------------------------------

    st.subheader("📅 Expiry")

    expiry = st.selectbox(

        "Select Expiry",

        [

            "Current Expiry"

        ]

    )

    st.info(
        "Expiry list will be loaded automatically from FYERS API in Part 2."
    )

    st.divider()

    # ---------------------------------------
    # Placeholder
    # ---------------------------------------

    option_placeholder = st.empty()

    if auto_refresh:

        time.sleep(refresh_time)

        st.rerun()
# ==========================================================
# OPTION CHAIN - PART 2
# Live Option Chain Data
# ==========================================================

    st.subheader("📊 Live Option Chain")

    if st.button("🔄 Load Option Chain", use_container_width=True):

        with st.spinner("Loading Option Chain..."):

            try:

                response = fyers.optionchain({

                    "symbol": symbol_map[index],

                    "strikecount": strike_count

                })

                if response.get("s") != "ok":

                    st.error(response)
                    st.stop()

                st.success("✅ Option Chain Loaded")

                data = response.get("data", {})

                # -----------------------------------
                # Display Raw Response (Debug)
                # -----------------------------------

                with st.expander("📦 Raw API Response"):

                    st.json(data)

                # -----------------------------------
                # Option Chain Table
                # -----------------------------------

                option_data = None

                if isinstance(data, list):

                    option_data = data

                elif isinstance(data, dict):

                    if "optionsChain" in data:
                        option_data = data["optionsChain"]

                    elif "optionChain" in data:
                        option_data = data["optionChain"]

                    elif "chain" in data:
                        option_data = data["chain"]

                if option_data:

                    df = pd.DataFrame(option_data)

                    st.dataframe(
                        df,
                        use_container_width=True,
                        height=600
                    )

                    csv = df.to_csv(index=False)

                    st.download_button(

                        "⬇ Download Option Chain CSV",

                        csv,

                        file_name=f"{index}_option_chain.csv",

                        mime="text/csv"

                    )

                    # -----------------------------------
                    # Summary
                    # -----------------------------------

                    st.divider()

                    c1, c2, c3 = st.columns(3)

                    c1.metric(
                        "Total Strikes",
                        len(df)
                    )

                    c2.metric(
                        "Index",
                        index
                    )

                    c3.metric(
                        "Strike Count",
                        strike_count
                    )

                else:

                    st.warning(
                        "⚠ Option Chain table not found in API response."
                    )

            except Exception as e:

                st.exception(e)
# ==========================================================
# OPTION CHAIN - PART 3
# PCR | OI Analysis | Support | Resistance
# ==========================================================

                st.divider()
                st.subheader("📊 Open Interest Analysis")

                try:

                    analysis_df = df.copy()

                    # Try common column names
                    ce_oi_col = None
                    pe_oi_col = None
                    strike_col = None

                    for col in analysis_df.columns:

                        c = str(col).lower()

                        if "strike" in c:
                            strike_col = col

                        if ce_oi_col is None and "ce" in c and "oi" in c:
                            ce_oi_col = col

                        if pe_oi_col is None and "pe" in c and "oi" in c:
                            pe_oi_col = col

                    if ce_oi_col and pe_oi_col:

                        analysis_df[ce_oi_col] = pd.to_numeric(
                            analysis_df[ce_oi_col],
                            errors="coerce"
                        ).fillna(0)

                        analysis_df[pe_oi_col] = pd.to_numeric(
                            analysis_df[pe_oi_col],
                            errors="coerce"
                        ).fillna(0)

                        total_ce = analysis_df[ce_oi_col].sum()
                        total_pe = analysis_df[pe_oi_col].sum()

                        pcr = (
                            total_pe / total_ce
                            if total_ce != 0 else 0
                        )

                        col1, col2, col3 = st.columns(3)

                        col1.metric(
                            "PCR",
                            round(pcr, 2)
                        )

                        col2.metric(
                            "Total CE OI",
                            f"{int(total_ce):,}"
                        )

                        col3.metric(
                            "Total PE OI",
                            f"{int(total_pe):,}"
                        )

                        st.divider()

                        # Highest CE OI
                        ce_row = analysis_df.loc[
                            analysis_df[ce_oi_col].idxmax()
                        ]

                        # Highest PE OI
                        pe_row = analysis_df.loc[
                            analysis_df[pe_oi_col].idxmax()
                        ]

                        if strike_col:

                            c1, c2 = st.columns(2)

                            c1.success(
                                f"🛑 Resistance : {ce_row[strike_col]}"
                            )

                            c2.success(
                                f"🟢 Support : {pe_row[strike_col]}"
                            )

                        st.subheader("📌 Market Interpretation")

                        if pcr > 1.3:

                            st.success(
                                "Bullish sentiment (High Put Writing)"
                            )

                        elif pcr < 0.7:

                            st.error(
                                "Bearish sentiment (High Call Writing)"
                            )

                        else:

                            st.info(
                                "Neutral Market"
                            )

                    else:

                        st.warning(
                            "OI columns not found in API response."
                        )

                except Exception as e:

                    st.warning(e)
# ==========================================================
# OPTION CHAIN - PART 4
# OI BUILDUP | COI | INSTITUTIONAL ANALYSIS
# ==========================================================

                st.divider()
                st.subheader("🏦 Institutional OI Analysis")

                try:

                    coi_ce = None
                    coi_pe = None
                    ltp_col = None

                    for col in analysis_df.columns:

                        c = str(col).lower()

                        if coi_ce is None and "ce" in c and "change" in c:
                            coi_ce = col

                        if coi_pe is None and "pe" in c and "change" in c:
                            coi_pe = col

                        if ltp_col is None and "ltp" in c:
                            ltp_col = col

                    if coi_ce and coi_pe:

                        analysis_df[coi_ce] = pd.to_numeric(
                            analysis_df[coi_ce],
                            errors="coerce"
                        ).fillna(0)

                        analysis_df[coi_pe] = pd.to_numeric(
                            analysis_df[coi_pe],
                            errors="coerce"
                        ).fillna(0)

                        st.metric(
                            "Total CE Change OI",
                            int(analysis_df[coi_ce].sum())
                        )

                        st.metric(
                            "Total PE Change OI",
                            int(analysis_df[coi_pe].sum())
                        )

                    st.divider()

                    st.subheader("📈 OI Build-up")

                    signal = "Neutral"

                    if pcr > 1.20:
                        signal = "Bullish"

                    elif pcr < 0.80:
                        signal = "Bearish"

                    col1, col2 = st.columns(2)

                    col1.metric(
                        "Market Bias",
                        signal
                    )

                    if signal == "Bullish":

                        col2.success(
                            "Long Build-up Possible"
                        )

                    elif signal == "Bearish":

                        col2.error(
                            "Short Build-up Possible"
                        )

                    else:

                        col2.info(
                            "Sideways Market"
                        )

                    st.divider()

                    st.subheader("🎯 Institutional View")

                    if pcr >= 1.40:

                        st.success(
                            """
Large Put Writing Detected

• Strong Support
• Institutions Bullish
• Buying on Dips
                            """
                        )

                    elif pcr <= 0.60:

                        st.error(
                            """
Heavy Call Writing Detected

• Strong Resistance
• Institutions Bearish
• Sell on Rise
                            """
                        )

                    else:

                        st.info(
                            """
Balanced Open Interest

No Strong Institutional Bias
                            """
                        )

                except Exception as e:

                    st.warning(e)

                st.divider()

                st.subheader("📋 OI Summary")

                summary = pd.DataFrame({

                    "Indicator": [

                        "PCR",
                        "Support",
                        "Resistance",
                        "Market Bias"

                    ],

                    "Value": [

                        round(pcr, 2),

                        pe_row[strike_col] if strike_col else "-",

                        ce_row[strike_col] if strike_col else "-",

                        signal

                    ]

                })

                st.dataframe(
                    summary,
                    use_container_width=True
                )
# ==========================================================
# OPTION CHAIN - PART 5
# MAX PAIN | TOP WRITERS | HEATMAP | EXPORT
# ==========================================================

                st.divider()
                st.subheader("📌 Max Pain Analysis")

                try:

                    if ce_oi_col and pe_oi_col and strike_col:

                        analysis_df["TOTAL_OI"] = (
                            analysis_df[ce_oi_col] +
                            analysis_df[pe_oi_col]
                        )

                        max_pain = analysis_df.loc[
                            analysis_df["TOTAL_OI"].idxmax()
                        ]

                        col1, col2, col3 = st.columns(3)

                        col1.metric(
                            "🎯 Max Pain",
                            max_pain[strike_col]
                        )

                        col2.metric(
                            "Total OI",
                            f"{int(max_pain['TOTAL_OI']):,}"
                        )

                        if pcr > 1:
                            bias = "Bullish"
                        elif pcr < 1:
                            bias = "Bearish"
                        else:
                            bias = "Neutral"

                        col3.metric(
                            "Market Bias",
                            bias
                        )

                except Exception as e:
                    st.warning(e)

                # =====================================
                # TOP CE WRITING
                # =====================================

                st.divider()

                st.subheader("🔴 Top CE Writers")

                try:

                    top_ce = analysis_df.sort_values(
                        ce_oi_col,
                        ascending=False
                    ).head(10)

                    st.dataframe(
                        top_ce,
                        use_container_width=True
                    )

                except:
                    pass

                # =====================================
                # TOP PE WRITING
                # =====================================

                st.subheader("🟢 Top PE Writers")

                try:

                    top_pe = analysis_df.sort_values(
                        pe_oi_col,
                        ascending=False
                    ).head(10)

                    st.dataframe(
                        top_pe,
                        use_container_width=True
                    )

                except:
                    pass

                # =====================================
                # OI HEATMAP
                # =====================================

                st.divider()

                st.subheader("🔥 OI Heatmap")

                try:

                    heat = analysis_df[[
                        strike_col,
                        ce_oi_col,
                        pe_oi_col
                    ]]

                    st.dataframe(
                        heat.style.background_gradient(
                            cmap="RdYlGn"
                        ),
                        use_container_width=True
                    )

                except:
                    pass

                # =====================================
                # EXPORT
                # =====================================

                st.divider()

                st.subheader("📥 Export")

                csv = analysis_df.to_csv(index=False)

                st.download_button(

                    "⬇ Download Institutional Report",

                    csv,

                    file_name=f"{index}_Institutional_Report.csv",

                    mime="text/csv"

                )

                # =====================================
                # DASHBOARD SUMMARY
                # =====================================

                st.divider()

                st.subheader("📊 Dashboard Summary")

                summary = pd.DataFrame({

                    "Metric": [

                        "Spot Price",
                        "PCR",
                        "Support",
                        "Resistance",
                        "Market Bias",
                        "Max Pain"

                    ],

                    "Value": [

                        q.get("lp", "-"),

                        round(pcr, 2),

                        pe_row[strike_col],

                        ce_row[strike_col],

                        bias,

                        max_pain[strike_col]

                    ]

                })

                st.dataframe(
                    summary,
                    use_container_width=True
                )
