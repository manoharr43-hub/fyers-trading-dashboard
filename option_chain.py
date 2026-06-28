import streamlit as st
import pandas as pd

def show_option_chain(fyers):
    st.title("📊 NSE AI PRO V12 - Institutional Option Chain")

    # 1. Configuration
    symbol_map = {
        "NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX", "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        "SENSEX": "BSE:SENSEX-INDEX", "RELIANCE": "NSE:RELIANCE-EQ"
    }
    
    selected_symbol = st.sidebar.selectbox("Select Index/Stock", list(symbol_map.keys()))
    strike_count = st.sidebar.slider("Strike Count", 5, 20, 10)

    # 2. Data Fetching
    if st.button("🔄 Load Institutional Data"):
        try:
            res = fyers.optionchain({"symbol": symbol_map[selected_symbol], "strikecount": strike_count})
            
            # API రెస్పాన్స్ నుండి డేటా తీసుకోవడం
            data = res.get("data", {}).get("optionsChain", [])
            
            if data:
                df = pd.DataFrame(data)
                st.session_state.oc_df = df
                st.success("Data Loaded Successfully!")
                st.rerun()
            else:
                st.warning("API నుండి డేటా రాలేదు. ఫ్యర్స్ కనెక్షన్ సరిచూసుకోండి.")
                
        except Exception as e:
            st.error(f"Error: {e}")

    # 3. Data Analysis Dashboard
    if "oc_df" in st.session_state and not st.session_state.oc_df.empty:
        df = st.session_state.oc_df
        
        # డేటా క్లీనింగ్
        if 'expiry' in df.columns:
            selected_expiry = st.sidebar.selectbox("📅 Select Expiry", df['expiry'].unique())
            df = df[df['expiry'] == selected_expiry]
            
        # UI Metrics
        st.subheader(f"🔥 Analysis - {selected_expiry}")
        
        # టేబుల్ చూపించడం
        st.dataframe(df[['strike_price', 'option_type', 'oi', 'ltp', 'volume']], use_container_width=True)
        
    else:
        st.info("పైన ఉన్న బటన్ నొక్కి డేటాను లోడ్ చేయండి.")

    st.caption("NSE AI PRO V12 | Institutional Edition")
