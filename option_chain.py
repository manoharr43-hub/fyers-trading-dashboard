import streamlit as st
import pandas as pd

def show_option_chain(fyers):
    st.title("📊 NSE AI PRO V12 - Full F&O Option Chain")

    # 1. Sidebar: Index and Expiry Selection
    index_options = {
        "NIFTY": "NSE:NIFTY50-INDEX", 
        "BANKNIFTY": "NSE:NIFTYBANK-INDEX", 
        "FINNIFTY": "NSE:FINNIFTY-INDEX", 
        "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        "RELIANCE": "NSE:RELIANCE-EQ", # F&O Stocks ಉದಾಹరణ
        "HDFCBANK": "NSE:HDFCBANK-EQ"
    }
    
    selected_index = st.sidebar.selectbox("Select Index/Stock", list(index_options.keys()))
    strike_count = st.sidebar.slider("Strike Count", 5, 30, 10)

    # 2. Load Data
    if st.button("🔄 Load Institutional Option Chain"):
        try:
            res = fyers.optionchain({"symbol": index_options[selected_index], "strikecount": strike_count})
            df = pd.DataFrame(res["data"]["optionsChain"])
            st.session_state.oc_df = df
        except Exception as e: st.error(f"Error: {e}")

    # 3. Analysis Dashboard
    if "oc_df" in st.session_state:
        df = st.session_state.oc_df
        
        # Expiry Selection Box
        if 'expiry' in df.columns:
            unique_expiries = df['expiry'].unique()
            selected_expiry = st.sidebar.selectbox("Select Expiry Date", unique_expiries)
            df = df[df['expiry'] == selected_expiry]

        # Cleanup numeric data
        numeric_cols = ['ce_oi', 'pe_oi', 'strike_price']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

        # OI Heatmap
        st.subheader(f"🔥 OI Heatmap - {selected_index} ({selected_expiry})")
        st.dataframe(df.style.background_gradient(cmap='RdYlGn', subset=['ce_oi', 'pe_oi']), use_container_width=True)

        # PCR Logic
        total_ce = df['ce_oi'].sum()
        total_pe = df['pe_oi'].sum()
        pcr = total_pe / total_ce if total_ce != 0 else 0
        st.metric("PCR Ratio", round(pcr, 2))

    st.caption("NSE AI PRO V12 | Institutional Edition")
