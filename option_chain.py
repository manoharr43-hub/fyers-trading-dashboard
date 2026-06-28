import streamlit as st
import pandas as pd

def show_option_chain(fyers):
    st.title("📊 NSE AI PRO V12 - Institutional Option Chain")

    # ఇండెక్స్ మరియు స్టాక్స్ మ్యాపింగ్
    symbol_map = {
        "NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX", "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        "SENSEX": "BSE:SENSEX-INDEX", "RELIANCE": "NSE:RELIANCE-EQ"
    }
    
    selected_symbol = st.sidebar.selectbox("Select Index/Stock", list(symbol_map.keys()))
    strike_count = st.sidebar.slider("Strike Count", 5, 20, 10)

    if st.button("🔄 Load Data"):
        try:
            res = fyers.optionchain({"symbol": symbol_map[selected_symbol], "strikecount": strike_count})
            data = res.get("data", {}).get("optionsChain", [])
            st.session_state.oc_df = pd.DataFrame(data)
            quote = fyers.quotes({"symbols": symbol_map[selected_symbol]})
            st.session_state.spot_price = quote["d"][0]["v"]["lp"]
            st.rerun()
        except Exception as e:
            st.error(f"Error: {e}")

    if "oc_df" in st.session_state:
        df = st.session_state.oc_df
        if 'expiry' in df.columns:
            selected_expiry = st.sidebar.selectbox("📅 Select Expiry", df['expiry'].unique())
            df = df[df['expiry'] == selected_expiry]

        df['oi'] = pd.to_numeric(df['oi'], errors='coerce').fillna(0)
        ce = df[df['option_type'] == 'CE']
        pe = df[df['option_type'] == 'PE']

        # మెట్రిక్స్
        c1, c2, c3 = st.columns(3)
        c1.metric("Spot Price", st.session_state.get("spot_price", "N/A"))
        c2.metric("Total CE OI", f"{int(ce['oi'].sum()):,}")
        c3.metric("Total PE OI", f"{int(pe['oi'].sum()):,}")

        st.dataframe(df[['strike_price', 'option_type', 'oi', 'ltp', 'volume']], use_container_width=True)
