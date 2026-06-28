import streamlit as st
import pandas as pd

def show_option_chain(fyers):
    st.title("📊 NSE AI PRO V12 - Institutional Option Chain")

    symbol_map = {
        "NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX", "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        "SENSEX": "BSE:SENSEX-INDEX", "RELIANCE": "NSE:RELIANCE-EQ"
    }
    
    selected_symbol = st.sidebar.selectbox("Select Index/Stock", list(symbol_map.keys()))
    strike_count = st.sidebar.slider("Strike Count", 5, 20, 10)

    if st.button("🔄 Load Institutional Data"):
        try:
            res = fyers.optionchain({"symbol": symbol_map[selected_symbol], "strikecount": strike_count})
            data = res.get("data", {}).get("optionsChain", [])
            if data:
                st.session_state.oc_df = pd.DataFrame(data)
                st.rerun()
        except Exception as e:
            st.error(f"Error: {e}")

    # FIX: కేవలం డేటా ఉంటేనే ఎక్స్‌పైరీ సెలెక్ట్ బాక్స్ చూపిస్తాం
    if "oc_df" in st.session_state and not st.session_state.oc_df.empty:
        df = st.session_state.oc_df
        
        if 'expiry' in df.columns:
            unique_expiries = df['expiry'].unique()
            # డిఫాల్ట్ వాల్యూ సెట్ చేస్తున్నాం
            selected_expiry = st.sidebar.selectbox("📅 Select Expiry", unique_expiries, index=0)
            
            # ఫిల్టరింగ్
            df_filtered = df[df['expiry'] == selected_expiry].copy()
            df_filtered['oi'] = pd.to_numeric(df_filtered['oi'], errors='coerce').fillna(0)
            
            st.subheader(f"🔥 Analysis - {selected_expiry}")
            st.dataframe(df_filtered[['strike_price', 'option_type', 'oi', 'ltp', 'volume']], use_container_width=True)
    else:
        st.info("డేటా లోడ్ చేయడానికి బటన్ నొక్కండి.")
