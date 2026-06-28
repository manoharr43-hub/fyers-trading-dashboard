import streamlit as st
import pandas as pd

def show_option_chain(fyers):
    st.title("📊 NSE AI PRO V12 - Institutional Option Chain")

    # 1. Index/Stock Mapping
    symbol_map = {
        "NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX", "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        "SENSEX": "BSE:SENSEX-INDEX", "RELIANCE": "NSE:RELIANCE-EQ"
    }
    
    selected_symbol = st.sidebar.selectbox("Select Index/Stock", list(symbol_map.keys()))
    strike_count = st.sidebar.slider("Strike Count", 5, 20, 10)

    # 2. Load Data Button
    if st.button("🔄 Load Institutional Option Chain"):
        try:
            res = fyers.optionchain({"symbol": symbol_map[selected_symbol], "strikecount": strike_count})
            data = res.get("data", {}).get("optionsChain", [])
            st.session_state.oc_df = pd.DataFrame(data)
            # Spot Price పొందడం
            quote = fyers.quotes({"symbols": symbol_map[selected_symbol]})
            st.session_state.spot_price = quote["d"][0]["v"]["lp"]
            st.rerun()
        except Exception as e:
            st.error(f"Error Loading Data: {e}")

    # 3. Analysis Dashboard
    if "oc_df" in st.session_state:
        df = st.session_state.oc_df
        
        # Expiry Selection Box
        if 'expiry' in df.columns:
            unique_expiries = df['expiry'].unique()
            selected_expiry = st.sidebar.selectbox("📅 Select Expiry", unique_expiries)
            df = df[df['expiry'] == selected_expiry]

        # Data Cleaning
        df['oi'] = pd.to_numeric(df['oi'], errors='coerce').fillna(0)
        ce_df = df[df['option_type'] == 'CE']
        pe_df = df[df['option_type'] == 'PE']

        # Top Metrics
        c1, c2, c3 = st.columns(3)
        c1.metric("Spot Price", st.session_state.get("spot_price", "N/A"))
        c2.metric("Total CE OI", f"{int(ce_df['oi'].sum()):,}")
        c3.metric("Total PE OI", f"{int(pe_df['oi'].sum()):,}")

        # Colored Data Table
        def style_df(row):
            color = '#ffcccc' if row['option_type'] == 'CE' else '#ccffcc'
            return [f'background-color: {color}'] * len(row)

        st.subheader(f"🔥 Analysis - {selected_expiry}")
        st.dataframe(df[['strike_price', 'option_type', 'oi', 'ltp', 'volume']].style.apply(style_df, axis=1), use_container_width=True)

    st.caption("NSE AI PRO V12 | Institutional Edition")
