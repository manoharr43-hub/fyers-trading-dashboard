import streamlit as st

def show_option_chain(fyers):
    # Part 1: Settings & Navigation
    st.title("📊 NSE AI PRO V12 - Institutional Option Chain")
    
    st.sidebar.header("⚙️ Option Chain Settings")
    
    # 1. Index/Stock Selection
    symbol_map = {
        "NIFTY": "NSE:NIFTY50-INDEX", 
        "BANKNIFTY": "NSE:NIFTYBANK-INDEX", 
        "FINNIFTY": "NSE:FINNIFTY-INDEX", 
        "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX", 
        "SENSEX": "BSE:SENSEX-INDEX", 
        "BANKEX": "BSE:BANKEX-INDEX"
    }
    
    index = st.sidebar.selectbox("Select Index", list(symbol_map.keys()))
    
    # 2. Strike Count Selection
    strike_count = st.sidebar.slider("Strike Count", min_value=5, max_value=30, value=10)
    
    # 3. Auto Refresh Toggle
    auto_refresh = st.sidebar.checkbox("Auto Refresh", value=False)
    refresh_time = st.sidebar.slider("Refresh Seconds", 5, 60, 10)
    
    # 4. Spot Price Display Section
    st.subheader("📈 Market Spot Price")
    try:
        quote = fyers.quotes({"symbols": symbol_map[index]})
        if quote.get("s") == "ok":
            q = quote["d"][0]["v"]
            c1, c2, c3 = st.columns(3)
            c1.metric("Spot Price", q.get("lp", 0), q.get("ch", 0))
            c2.metric("Day High", q.get("high_price", "-"))
            c3.metric("Day Low", q.get("low_price", "-"))
        else:
            st.error("Could not fetch spot price.")
    except Exception as e:
        st.error(f"Error: {e}")
        
    st.divider()
    
    # ఇక్కడి నుండి Part 2 (Load Data) మొదలవుతుంది
