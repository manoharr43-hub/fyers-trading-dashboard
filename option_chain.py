import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from fyers_apiv3 import fyersModel

# --- Fyers API Configuration ---
# మీ access_token ను ఇక్కడ అప్‌డేట్ చేయండి
fyers = fyersModel.FyersModel(client_id="YOUR_CLIENT_ID", token="YOUR_ACCESS_TOKEN")

st.set_page_config(layout="wide")
st.title("📈 NSE AI PRO - Institutional Option Chain")

# --- Sidebar Filters ---
st.sidebar.header("Navigation")
market_type = st.sidebar.radio("Market", ["INDEX", "F&O STOCK"])
symbol_input = st.sidebar.text_input("Enter Symbol", "NSE:NIFTY-INDEX" if market_type == "INDEX" else "NSE:RELIANCE-EQ")
strike_count = st.sidebar.slider("Strike Count", 5, 30, 15)

def fetch_option_chain(symbol):
    try:
        data = {"symbol": symbol, "strikecount": strike_count}
        response = fyers.optionchain(data=data)
        if response['s'] == 'ok':
            return pd.DataFrame(response['data']['options'])
        return pd.DataFrame()
    except Exception as e:
        st.error(f"Error: {e}")
        return pd.DataFrame()

# --- Main Dashboard ---
if st.button("🚀 Fetch Live Data"):
    df = fetch_option_chain(symbol_input)
    
    if not df.empty:
        # 1. Dashboard Metrics
        c1, c2, c3 = st.columns(3)
        pcr = df['pe_oi'].sum() / df['ce_oi'].sum() if df['ce_oi'].sum() > 0 else 0
        c1.metric("Total CE OI", f"{df['ce_oi'].sum():,}")
        c2.metric("Total PE OI", f"{df['pe_oi'].sum():,}")
        c3.metric("PCR", round(pcr, 2))

        # 2. Institutional AI Scoring (Logic)
        df['AI Score'] = (df['pe_oi'] / (df['ce_oi'] + 1) * 100).clip(0, 100)
        
        # 3. Data Table with Highlights
        st.subheader("Live Option Chain Data")
        st.dataframe(df.style.background_gradient(subset=['ce_oi', 'pe_oi'], cmap='Greens'), use_container_width=True)

        # 4. OI Charts
        fig = go.Figure()
        fig.add_trace(go.Bar(x=df['strike_price'], y=df['ce_oi'], name='CE OI', marker_color='red'))
        fig.add_trace(go.Bar(x=df['strike_price'], y=df['pe_oi'], name='PE OI', marker_color='green'))
        fig.update_layout(title='Open Interest Distribution', barmode='group')
        st.plotly_chart(fig, use_container_width=True)

        # 5. Smart Money Logic
        st.subheader("💰 Smart Money & Trading Suggestions")
        if pcr > 1.2:
            st.success("Smart Money Suggestion: Bullish (Support observed in PE side)")
        elif pcr < 0.8:
            st.error("Smart Money Suggestion: Bearish (Resistance observed in CE side)")
        else:
            st.warning("Neutral Trend: Wait for Breakout")
    else:
        st.warning("Data loading... please check symbol format (e.g., NSE:NIFTY-INDEX)")

# 
