import streamlit as st
import pandas as pd

def show_dashboard(fyers):
    st.title("🏠 Institutional Trading Dashboard")

    # 1. MARKET OVERVIEW (Metrics)
    st.subheader("📊 Market Overview")
    try:
        quote = fyers.quotes({"symbols": "NSE:NIFTY50-INDEX,NSE:NIFTYBANK-INDEX,NSE:FINNIFTY-INDEX"})
        data = quote.get("d", [])
        if data:
            c1, c2, c3 = st.columns(3)
            for i, col in enumerate([c1, c2, c3]):
                val = data[i]["v"]
                col.metric(val["symbol"].replace("NSE:", ""), val["lp"], val["ch"])
    except Exception as e:
        st.warning("Market data unavailable.")

    st.divider()

    # 2. PORTFOLIO & POSITIONS (Summary Cards)
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("💰 Holdings Summary")
        try:
            holdings = fyers.holdings()
            if holdings.get("holdings"):
                df_h = pd.DataFrame(holdings["holdings"])
                st.metric("Total Holdings", len(df_h))
            else: st.info("No holdings.")
        except: st.error("Error loading holdings.")
    
    with c2:
        st.subheader("📈 Current Positions")
        try:
            pos = fyers.positions()
            if pos.get("netPositions"):
                df_p = pd.DataFrame(pos["netPositions"])
                total_pl = df_p["pl"].sum()
                st.metric("Net P&L", f"₹{total_pl:,.2f}", delta=f"{total_pl:,.2f}")
            else: st.info("No open positions.")
        except: st.error("Error loading positions.")

    st.divider()

    # 3. FUNDS (Clean View)
    st.subheader("💳 Funds Status")
    try:
        funds = fyers.funds()
        if funds.get("fund_limit"):
            df_f = pd.DataFrame(funds["fund_limit"])
            st.dataframe(df_f, use_container_width=True)
    except: st.error("Funds data unavailable.")

    # 4. QUICK ACTIONS
    if st.button("🔄 Refresh Data"):
        st.rerun()
