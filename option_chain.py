import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from fyers_apiv3 import fyersModel

# Fyers API - ఇక్కడ మీ వివరాలు ఎంటర్ చేయండి
client_id = "YOUR_CLIENT_ID"
access_token = "YOUR_ACCESS_TOKEN"
fyers = fyersModel.FyersModel(client_id=client_id, token=access_token)

st.set_page_config(layout="wide", page_title="NSE AI PRO")
st.title("📊 NSE AI PRO - Master Options Chain")

# డేటా ఫెచ్ చేయడానికి కాష్ ఉపయోగించడం (API లిమిట్స్ తగ్గించడానికి)
@st.cache_data(ttl=60) 
def get_option_chain(symbol):
    try:
        data = {"symbol": symbol, "strikecount": 20}
        response = fyers.optionchain(data=data)
        if response and 'data' in response:
            return pd.DataFrame(response['data']['options'])
        return pd.DataFrame()
    except Exception as e:
        st.error(f"API Error: {e}")
        return pd.DataFrame()

# Sidebar
option_type = st.sidebar.radio("Select Type", ["Indices", "F&O Stocks"])
if option_type == "Indices":
    symbol = st.sidebar.selectbox("Select Index", 
        ["NSE:NIFTY-INDEX", "NSE:BANKNIFTY-INDEX", "NSE:FINNIFTY-INDEX", "NSE:MIDCPNIFTY-INDEX", "BSE:SENSEX-INDEX"])
else:
    symbol = st.sidebar.text_input("Enter Symbol (e.g., NSE:RELIANCE-EQ)", "NSE:RELIANCE-EQ")

# Main Execution
if st.button("Fetch Live Data"):
    df = get_option_chain(symbol)
    
    if not df.empty:
        # Key Metrics
        col1, col2, col3 = st.columns(3)
        pcr = df['pe_oi'].sum() / df['ce_oi'].sum() if df['ce_oi'].sum() != 0 else 0
        col1.metric("Total CE OI", f"{df['ce_oi'].sum():,}")
        col2.metric("Total PE OI", f"{df['pe_oi'].sum():,}")
        col3.metric("PCR Ratio", round(pcr, 2))

        # Table with Color Highlight
        st.subheader("Live Option Chain")
        st.dataframe(df[['strike_price', 'ce_ltp', 'ce_oi', 'pe_oi', 'pe_ltp']].style.background_gradient(subset=['ce_oi', 'pe_oi'], cmap='Greens'), use_container_width=True)

        # OI Chart
        fig = go.Figure()
        fig.add_trace(go.Bar(x=df['strike_price'], y=df['ce_oi'], name='CE OI (Resistance)', marker_color='red'))
        fig.add_trace(go.Bar(x=df['strike_price'], y=df['pe_oi'], name='PE OI (Support)', marker_color='green'))
        fig.update_layout(title='Open Interest Analysis', barmode='group')
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning("డేటా అందుబాటులో లేదు. మీ API టోకెన్ మరియు సింబల్ చెక్ చేయండి.")


