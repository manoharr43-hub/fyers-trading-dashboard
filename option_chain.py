import streamlit as st
import pandas as pd
import time

def show_option_chain(fyers):
    st.title("📊 NSE AI PRO V12 - Institutional Option Chain")

    # 1. Sidebar Settings
    index = st.sidebar.selectbox("Select Index", ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"])
    strike_count = st.sidebar.slider("Strike Count", 5, 30, 10)
    auto_refresh = st.sidebar.checkbox("Auto Refresh")

    symbol_map = {
        "NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX", "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX"
    }

    # 2. Load Data
    if st.button("🔄 Load Institutional Option Chain"):
        with st.spinner("Fetching Data..."):
            try:
                res = fyers.optionchain({"symbol": symbol_map[index], "strikecount": strike_count})
                df = pd.DataFrame(res["data"]["optionsChain"])
                st.session_state.oc_df = df
            except Exception as e: 
                st.error(f"Error fetching data: {e}")

    # 3. Analysis Dashboard
    if "oc_df" in st.session_state:
        df = st.session_state.oc_df
        
        # డేటా క్లీనింగ్: కాలమ్స్ నంబర్లుగా మార్చడం
        numeric_cols = ['ce_oi', 'pe_oi', 'ce_ltp', 'pe_ltp', 'strike_price']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

        # కాలమ్స్ క్రమబద్ధీకరించడం (Expiry ముందు ఉండేలా)
        cols = ['expiry'] + [c for c in df.columns if c != 'expiry']
        df = df[cols]

        # Calculations
        total_ce_oi = df['ce_oi'].sum()
        total_pe_oi = df['pe_oi'].sum()
        pcr = total_pe_oi / total_ce_oi if total_ce_oi != 0 else 0
        
        # Metrics UI
        c1, c2, c3 = st.columns(3)
        c1.metric("PCR Ratio", round(pcr, 2))
        c2.metric("Total CE OI", int(total_ce_oi))
        c3.metric("Total PE OI", int(total_pe_oi))

        # OI Heatmap
        st.subheader("🔥 OI Heatmap")
        st.dataframe(df.style.background_gradient(cmap='RdYlGn', subset=['ce_oi', 'pe_oi']), use_container_width=True)

        # AI Signals
        st.subheader("🤖 AI Institutional Signals")
        sentiment = "🟢 Bullish" if pcr > 1.0 else "🔴 Bearish"
        st.info(f"Market Sentiment: {sentiment}")

    if auto_refresh:
        time.sleep(10)
        st.rerun()

    st.caption("NSE AI PRO V12 | Institutional Edition")
