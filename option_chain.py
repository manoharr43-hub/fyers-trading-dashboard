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
