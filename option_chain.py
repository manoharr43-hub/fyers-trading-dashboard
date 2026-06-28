import streamlit as st
import pandas as pd

def show_option_chain(fyers):
    st.title("📊 NSE AI PRO V12 - Full F&O Option Chain")

    # 1. Index/Stock Mapping
    symbol_map = {
        "NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "SENSEX": "BSE:SENSEX-INDEX", "RELIANCE": "NSE:RELIANCE-EQ"
    }
    
    selected_symbol = st.sidebar.selectbox("Select Index/Stock", list(symbol_map.keys()))
    strike_count = st.sidebar.slider("Strike Count", 5, 20, 10)

    if st.button("🔄 Load Data"):
        res = fyers.optionchain({"symbol": symbol_map[selected_symbol], "strikecount": strike_count})
        st.session_state.oc_df = pd.DataFrame(res.get("data", {}).get("optionsChain", []))
        st.rerun()

    if "oc_df" in st.session_state:
        df = st.session_state.oc_df
        
        # 2. Expiry Box Highlight
        if 'expiry' in df.columns:
            unique_expiries = df['expiry'].unique()
            selected_expiry = st.sidebar.selectbox("📅 Select Expiry Date", unique_expiries)
            df = df[df['expiry'] == selected_expiry]

        # 3. Data Cleaning (CE/PE separation)
        df['oi'] = pd.to_numeric(df['oi'], errors='coerce').fillna(0)
        ce_df = df[df['option_type'] == 'CE']
        pe_df = df[df['option_type'] == 'PE']

        # 4. Display Logic
        st.subheader(f"🔥 OI Analysis - {selected_expiry}")
        
        # Color coding: CE (Red-ish), PE (Green-ish)
        def highlight_cols(x):
            return 'background-color: #ffcccc' if x.name == 'CE' else 'background-color: #ccffcc'

        st.dataframe(df[['strike_price', 'option_type', 'oi', 'ltp', 'volume']], use_container_width=True)

    st.caption("NSE AI PRO V12 | Institutional Edition")
