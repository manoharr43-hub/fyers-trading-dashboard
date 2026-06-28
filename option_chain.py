import streamlit as st
import pandas as pd
import time

def show_option_chain(fyers):
    st.title("📊 NSE AI PRO V12 - Institutional Option Chain")

    # --- Part 1: Settings ---
    symbol_map = {
        "NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX", 
        "FINNIFTY": "NSE:FINNIFTY-INDEX", "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX", 
        "SENSEX": "BSE:SENSEX-INDEX", "BANKEX": "BSE:BANKEX-INDEX"
    }
    
    index = st.sidebar.selectbox("Select Index", list(symbol_map.keys()))
    strike_count = st.sidebar.slider("Strike Count", 5, 30, 10)

    # --- Part 2: Load Data & UI ---
    if st.button("🔄 Load Institutional Option Chain", use_container_width=True):
        with st.spinner("Fetching Data..."):
            try:
                res = fyers.optionchain({"symbol": symbol_map[index], "strikecount": strike_count})
                data = res.get("data", {}).get("optionsChain", [])
                st.session_state.oc_df = pd.DataFrame(data)
                st.rerun()
            except Exception as e:
                st.error(f"Error: {e}")

    if "oc_df" in st.session_state and not st.session_state.oc_df.empty:
        df = st.session_state.oc_df
        
        # Expiry Selection Box (టాప్‌లో)
        if 'expiry' in df.columns:
            selected_expiry = st.selectbox("📅 Select Expiry", df['expiry'].unique())
            df = df[df['expiry'] == selected_expiry].copy()
        
        # Calculations
        df['oi'] = pd.to_numeric(df['oi'], errors='coerce').fillna(0)
        ce_total = df[df['option_type'] == 'CE']['oi'].sum()
        pe_total = df[df['option_type'] == 'PE']['oi'].sum()

        # Top Metrics
        c1, c2 = st.columns(2)
        c1.metric("Total CE OI", f"{int(ce_total):,}")
        c2.metric("Total PE OI", f"{int(pe_total):,}")

        # Color Coding
        def highlight_rows(row):
            color = '#ffcccc' if row['option_type'] == 'CE' else '#ccffcc'
            return [f'background-color: {color}'] * len(row)

        st.dataframe(df.style.apply(highlight_rows, axis=1), use_container_width=True)

        # --- Part 3: OI Analysis ---
        st.divider()
        st.subheader("📊 Open Interest Analysis")
        # Logic to calculate PCR, Support, Resistance
        if ce_total > 0:
            pcr = pe_total / ce_total
            st.metric("PCR", round(pcr, 2))

        # --- Part 4, 5, 6: Advanced Institutional View ---
        st.divider()
        st.subheader("🏦 Institutional OI Analysis")
        # Add your Max Pain, Heatmap, and Export Logic here as before
        
        # --- Export ---
        csv = df.to_csv(index=False)
        st.download_button("⬇ Download Report", csv, f"{index}_Report.csv", "text/csv")
        
    else:
        st.info("👈 ఎడమ వైపు సెట్టింగ్స్ మార్చి, డేటాను లోడ్ చేయడానికి బటన్ క్లిక్ చేయండి.")

    st.sidebar.divider()
    st.sidebar.markdown("### 🚀 NSE AI PRO V12")
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
