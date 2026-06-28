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
        try:
            res = fyers.optionchain({"symbol": symbol_map[selected_symbol], "strikecount": strike_count})
            data = res.get("data", {}).get("optionsChain", [])
            df = pd.DataFrame(data)
            st.session_state.oc_df = df
            st.rerun()
        except Exception as e:
            st.error(f"API Error: {e}")

    if "oc_df" in st.session_state:
        df = st.session_state.oc_df
        
        # ఆటోమేటిక్ కాలమ్ మ్యాపింగ్ (KeyError రాకుండా)
        col_map = {c.lower(): c for c in df.columns}
        sp_col = col_map.get('strike_price') or col_map.get('strikeprice') or df.columns[0]
        oi_col = col_map.get('oi') or df.columns[2]

        # Expiry సెలక్షన్
        if 'expiry' in df.columns:
            selected_expiry = st.sidebar.selectbox("📅 Select Expiry", df['expiry'].unique())
            df = df[df['expiry'] == selected_expiry]

        # డిస్‌ప్లే డేటా
        st.subheader(f"🔥 OI Analysis")
        st.dataframe(df[[sp_col, 'option_type', oi_col, 'ltp', 'volume']], use_container_width=True)

    st.caption("NSE AI PRO V12 | Institutional Edition")
