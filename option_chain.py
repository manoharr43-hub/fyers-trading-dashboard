import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import numpy as np
from plotly.subplots import make_subplots

st.set_page_config(page_title="Options Chain Dashboard V2", page_icon="📊", layout="wide")

# Sidebar Config
with st.sidebar:
    st.markdown("### ⚙️ Configuration")
    symbol_map = {
        "NIFTY 50": "NSE:NIFTY50-OPT",
        "BANKNIFTY": "NSE:NIFTYBANK-OPT",
    }
    selected_key = st.selectbox("Index", list(symbol_map.keys()))
    symbol = symbol_map[selected_key]

    expiry = st.text_input("Expiry Date (YYYY-MM-DD)", "2026-07-02")
    strike_count = st.slider("Strikes Around ATM", 5, 30, 20, step=5)
    auto_refresh = st.checkbox("Auto Refresh (5 min)", True)
    fetch_btn = st.button("🔄 Fetch Live Data", use_container_width=True)

if auto_refresh:
    st_autorefresh(interval=5*60*1000, key="refresh")

# Dummy API call (replace with fyers.optionchain)
def get_data(symbol, expiry, strike_count):
    # Simulated response
    return pd.DataFrame({
        "strike_price": [16000, 16100, 16200],
        "ce_oi": [12000, 15000, 18000],
        "pe_oi": [10000, 14000, 20000],
    })

if fetch_btn:
    df = get_data(symbol, expiry, strike_count)
    if df.empty:
        st.error("⚠️ No options data. Market may be closed or symbol unavailable.")
    else:
        total_ce = df["ce_oi"].sum()
        total_pe = df["pe_oi"].sum()
        pcr = total_pe / total_ce if total_ce > 0 else 0

        st.metric("Total CE OI", f"{total_ce:,}")
        st.metric("Total PE OI", f"{total_pe:,}")
        st.metric("PCR", f"{pcr:.2f}")

        # PCR Trend (dummy)
        pcr_trend = pd.DataFrame({"time": ["9:15","9:30","9:45"], "pcr":[0.8,1.2,1.5]})
        st.line_chart(pcr_trend.set_index("time"))
