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
    # Part 2: Data Loading & Table Display
    st.subheader("📊 Live Option Chain Data")
    
    # 1. Load Data Button
    if st.button("🔄 Load Institutional Data", use_container_width=True):
        with st.spinner("Fetching Data..."):
            try:
                res = fyers.optionchain({"symbol": symbol_map[index], "strikecount": strike_count})
                data = res.get("data", {}).get("optionsChain", [])
                st.session_state.oc_df = pd.DataFrame(data)
                st.rerun() # డేటా లోడ్ అయ్యాక పేజీని రీఫ్రెష్ చేస్తుంది
            except Exception as e:
                st.error(f"Error: {e}")

    # 2. Display logic (డేటా ఉంటేనే చూపుతుంది)
    if "oc_df" in st.session_state and not st.session_state.oc_df.empty:
        df = st.session_state.oc_df
        
        # Expiry Selection Box (టాప్‌లో)
        if 'expiry' in df.columns:
            selected_expiry = st.selectbox("📅 Select Expiry", df['expiry'].unique())
            df = df[df['expiry'] == selected_expiry].copy()
        
        # Calculations (Totals)
        df['oi'] = pd.to_numeric(df['oi'], errors='coerce').fillna(0)
        ce_total = df[df['option_type'] == 'CE']['oi'].sum()
        pe_total = df[df['option_type'] == 'PE']['oi'].sum()

        # Top Metrics
        c1, c2 = st.columns(2)
        c1.metric("Total CE OI", f"{int(ce_total):,}")
        c2.metric("Total PE OI", f"{int(pe_total):,}")

        # Color Coding (CE=Red, PE=Green)
        def highlight_rows(row):
            color = '#ffcccc' if row.get('option_type') == 'CE' else '#ccffcc'
            return [f'background-color: {color}'] * len(row)

        st.dataframe(df.style.apply(highlight_rows, axis=1), use_container_width=True)
        
    else:
        st.info("👈 సెట్టింగ్స్ మార్చి, బటన్ క్లిక్ చేసి డేటాను లోడ్ చేయండి.")
        
