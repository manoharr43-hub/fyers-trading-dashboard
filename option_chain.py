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

    if st.button("🔄 Load Institutional Option Chain"):
        try:
            # 1. API కాల్ చేయడానికి ముందు ప్రింట్ చేయడం (Logging)
            symbol = symbol_map[selected_symbol]
            st.write(f"Fetching data for: {symbol}...")
            
            res = fyers.optionchain({"symbol": symbol, "strikecount": strike_count})
            
            # 2. రెస్పాన్స్ ని చెక్ చేయడం
            st.write("API Response Status:", res.get("s"))
            
            data = res.get("data", {}).get("optionsChain", [])
            
            if data:
                st.session_state.oc_df = pd.DataFrame(data)
                st.success(f"Successfully loaded {len(data)} records!")
                st.rerun()
            else:
                st.error("API నుండి డేటా రాలేదు (Empty Data). ఫ్యర్స్ సర్వర్ రెస్పాన్స్ చూడండి:")
                st.json(res) # రెస్పాన్స్ లో అసలు ఏం వచ్చిందో చూద్దాం
                
        except Exception as e:
            st.error(f"API కనెక్టివిటీ లోపం: {e}")

    # డేటా ఉంటేనే టేబుల్ చూపించడం
    if "oc_df" in st.session_state and not st.session_state.oc_df.empty:
        st.dataframe(st.session_state.oc_df)
