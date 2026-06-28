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

    # 2. Live Spot Price
    try:
        quote = fyers.quotes({"symbols": symbol_map[index]})
        q = quote["d"][0]["v"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Spot Price", q["lp"])
        c2.metric("High", q["high_price"])
        c3.metric("Low", q["low_price"])
    except: st.warning("Spot price loading...")

    # 3. Load Data
    if st.button("🔄 Load Institutional Option Chain"):
        with st.spinner("Fetching Institutional Data..."):
            try:
                res = fyers.optionchain({"symbol": symbol_map[index], "strikecount": strike_count})
                df = pd.DataFrame(res["data"]["optionsChain"])
                st.session_state.oc_df = df
            except Exception as e: st.error(f"Error fetching data: {e}")

    # 4. Analysis Dashboard
    if "oc_df" in st.session_state:
        df = st.session_state.oc_df
        
        # కాలమ్ పేర్లు సరిచూసుకోవడం (KeyError నివారించడానికి)
        ce_col = 'ce_oi' if 'ce_oi' in df.columns else 'oi'
        pe_col = 'pe_oi' if 'pe_oi' in df.columns else 'oi'
        
        # Calculations
        total_ce_oi = df[ce_col].sum()
        total_pe_oi = df[pe_col].sum()
        pcr = total_pe_oi / total_ce_oi if total_ce_oi != 0 else 0
        
        # UI Metrics
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("PCR", round(pcr, 2))
        c2.metric("Max Pain", df.loc[(df[ce_col] + df[pe_col]).idxmax(), 'strike_price'])
        c3.success(f"Support: {df.loc[df[pe_col].idxmax(), 'strike_price']}")
        c4.error(f"Resistance: {df.loc[df[ce_col].idxmax(), 'strike_price']}")

        # OI Heatmap with Expiry
        st.subheader("🔥 OI Heatmap")
        display_cols = ['expiry', 'strike_price', ce_col, pe_col] if 'expiry' in df.columns else ['strike_price', ce_col, pe_col]
        st.dataframe(df[display_cols].style.background_gradient(cmap='RdYlGn'), use_container_width=True)

        # AI Signal
        st.subheader("🤖 AI Institutional Signals")
        status = "🟢 Bullish" if pcr > 1.2 else "🔴 Bearish"
        st.metric("Institutional Score", f"{int(50 + (20 if pcr > 1.2 else -20))}/100", status)
        
        # Download
        st.download_button("⬇ Download Report", df.to_csv(), f"{index}_Report.csv", "text/csv")

    if auto_refresh:
        time.sleep(10)
        st.rerun()

    st.divider()
    st.caption("NSE AI PRO V12 | Institutional Edition")
