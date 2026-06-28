import streamlit as st
import pandas as pd
import time

def show_option_chain(fyers):
    st.title("📊 NSE AI PRO V12 - Institutional Option Chain")

    # 1. Sidebar
    index = st.sidebar.selectbox("Select Index", ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"])
    strike_count = st.sidebar.slider("Strike Count", 5, 30, 10)
    auto_refresh = st.sidebar.checkbox("Auto Refresh")

    symbol_map = {
        "NIFTY": "NSE:NIFTY50-INDEX", "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "FINNIFTY": "NSE:FINNIFTY-INDEX", "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX"
    }

    # 2. Live Spot Price
    try:
        quote = fyers.quotes({"symbols": symbol_map[index]})
        q = quote["d"][0]["v"]
        st.metric("Spot Price", q["lp"])
    except: st.warning("Spot price loading...")

    # 3. Load Data
    if st.button("🔄 Load Institutional Option Chain"):
        with st.spinner("Fetching Data..."):
            try:
                res = fyers.optionchain({"symbol": symbol_map[index], "strikecount": strike_count})
                df = pd.DataFrame(res["data"]["optionsChain"])
                st.session_state.oc_df = df
            except Exception as e: st.error(f"Error: {e}")

    # 4. Analysis Dashboard (Error-Proof)
    if "oc_df" in st.session_state:
        df = st.session_state.oc_df
        
        # కాలమ్ పేర్లను ఆటోమేటిక్ గా గుర్తించడం
        ce_cols = [c for c in df.columns if 'ce' in c.lower() and 'oi' in c.lower()]
        pe_cols = [c for c in df.columns if 'pe' in c.lower() and 'oi' in c.lower()]
        
        ce_name = ce_cols[0] if ce_cols else (df.columns[1] if len(df.columns) > 1 else df.columns[0])
        pe_name = pe_cols[0] if pe_cols else (df.columns[2] if len(df.columns) > 2 else df.columns[0])

        # Calculations
        total_ce_oi = df[ce_name].sum()
        total_pe_oi = df[pe_name].sum()
        pcr = total_pe_oi / total_ce_oi if total_ce_oi != 0 else 0
        
        # UI Metrics
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("PCR", round(pcr, 2))
        c2.metric("Max Pain", "N/A") # కాలమ్స్ ని బట్టి ఇది మార్చుకోవచ్చు
        c3.success("Ready")
        c4.error("Data Loaded")

        # OI Heatmap
        st.subheader("🔥 OI Heatmap")
        st.dataframe(df.style.background_gradient(cmap='RdYlGn'), use_container_width=True)

        # AI Signal
        status = "🟢 Bullish" if pcr > 1.2 else "🔴 Bearish"
        st.metric("Institutional Score", f"{int(50 + (20 if pcr > 1.2 else -20))}/100", status)

    if auto_refresh:
        time.sleep(10)
        st.rerun()

    st.caption("NSE AI PRO V12 | Institutional Edition")
