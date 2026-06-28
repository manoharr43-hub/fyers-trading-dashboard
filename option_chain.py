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

    # 1. Load Data logic
    if st.button("🔄 Load Institutional Option Chain"):
        with st.spinner("Fetching data from FYERS..."):
            try:
                res = fyers.optionchain({"symbol": symbol_map[selected_symbol], "strikecount": strike_count})
                data = res.get("data", {}).get("optionsChain", [])
                
                if data:
                    df = pd.DataFrame(data)
                    st.session_state.oc_df = df
                    # Spot Price
                    quote = fyers.quotes({"symbols": symbol_map[selected_symbol]})
                    st.session_state.spot_price = quote["d"][0]["v"]["lp"]
                    st.success("Data Loaded Successfully!")
                    st.rerun()
                else:
                    st.error("API నుండి డేటా రాలేదు. ఫ్యర్స్ API ని ఒకసారి చెక్ చేయండి.")
            except Exception as e:
                st.error(f"Error: {e}")

    # 2. Display Data logic
    if "oc_df" in st.session_state and not st.session_state.oc_df.empty:
        df = st.session_state.oc_df
        
        # Expiry సెలక్షన్
        if 'expiry' in df.columns:
            unique_expiries = df['expiry'].unique()
            selected_expiry = st.sidebar.selectbox("📅 Select Expiry", unique_expiries)
            df_filtered = df[df['expiry'] == selected_expiry].copy()

            # Data Cleaning
            df_filtered['oi'] = pd.to_numeric(df_filtered['oi'], errors='coerce').fillna(0)
            ce_df = df_filtered[df_filtered['option_type'] == 'CE']
            pe_df = df_filtered[df_filtered['option_type'] == 'PE']

            # Metrics
            c1, c2, c3 = st.columns(3)
            c1.metric("Spot Price", st.session_state.get("spot_price", "N/A"))
            c2.metric("Total CE OI", f"{int(ce_df['oi'].sum()):,}")
            c3.metric("Total PE OI", f"{int(pe_df['oi'].sum()):,}")

            # Styling
            def style_df(row):
                color = '#ffcccc' if row['option_type'] == 'CE' else '#ccffcc'
                return [f'background-color: {color}'] * len(row)

            st.subheader(f"🔥 Analysis - {selected_expiry}")
            st.dataframe(df_filtered[['strike_price', 'option_type', 'oi', 'ltp', 'volume']].style.apply(style_df, axis=1), use_container_width=True)
    else:
        st.warning("డేటా లోడ్ అవ్వలేదు. పైన ఉన్న బటన్ నొక్కండి.")
