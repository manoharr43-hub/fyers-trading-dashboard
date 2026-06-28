import streamlit as st
import pandas as pd
import time

def show_option_chain(fyers):
    st.title("📊 NSE AI PRO V12 - Institutional Option Chain")

    # 1. Sidebar Settings
    index = st.sidebar.selectbox("Select Index", ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"])
    strike_count = st.sidebar.slider("Strike Count", 5, 30, 10)
    
    symbol_map = {
        "NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX", "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX"
    }

    # 2. Load Data
    if st.button("🔄 Load Institutional Option Chain"):
        with st.spinner("Fetching Data..."):
            try:
                res = fyers.optionchain({"symbol": symbol_map[index], "strikecount": strike_count})
                df = pd.DataFrame(res["data"]["optionsChain"])
                st.session_state.oc_df = df
            except Exception as e: st.error(f"Error: {e}")

    # 3. Analysis Dashboard (Safe Mode)
    if "oc_df" in st.session_state:
        df = st.session_state.oc_df
        
        # డైనమిక్ కాలమ్ ఫైండింగ్
        ce_cols = [c for c in df.columns if 'ce' in c.lower() and 'oi' in c.lower()]
        pe_cols = [c for c in df.columns if 'pe' in c.lower() and 'oi' in c.lower()]
        ce_name = ce_cols[0] if ce_cols else df.columns[1]
        pe_name = pe_cols[0] if pe_cols else df.columns[2]

        # డేటా టైప్ కన్వర్షన్ (TypeError నివారించడానికి)
        df[ce_name] = pd.to_numeric(df[ce_name], errors='coerce').fillna(0)
        df[pe_name] = pd.to_numeric(df[pe_name], errors='coerce').fillna(0)

        # Calculations
        total_ce_oi = df[ce_name].sum()
        total_pe_oi = df[pe_name].sum()
        pcr = total_pe_oi / total_ce_oi if total_ce_oi != 0 else 0
        
        # UI Metrics
        c1, c2 = st.columns(2)
        c1.metric("PCR Ratio", round(pcr, 2))
        c2.metric("Market Sentiment", "Bullish" if pcr > 1 else "Bearish")

        # OI Heatmap
        st.subheader("🔥 OI Heatmap")
        st.dataframe(df.style.background_gradient(cmap='RdYlGn'), use_container_width=True)
        
        # AI Logic
        st.info(f"Analysis: Institutional money is moving towards {'Calls' if pcr < 0.9 else 'Puts'}")

    st.divider()
    st.caption("NSE AI PRO V12 | Institutional Edition")
