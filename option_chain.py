import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from fyers_apiv3 import fyersModel

# Fyers Auth
client_id = "YOUR_CLIENT_ID"
access_token = "YOUR_ACCESS_TOKEN"
fyers = fyersModel.FyersModel(client_id=client_id, token=access_token)

st.set_page_config(layout="wide")
st.title("📊 Master Options Chain Dashboard")

# 1. Selection Sidebar
option_type = st.sidebar.radio("Select Type", ["Indices", "F&O Stocks"])

if option_type == "Indices":
    symbol = st.sidebar.selectbox("Select Index", 
        ["NSE:NIFTY-INDEX", "NSE:BANKNIFTY-INDEX", "NSE:FINNIFTY-INDEX", "NSE:MIDCPNIFTY-INDEX", "BSE:SENSEX-INDEX"])
else:
    # F&O Stocks list (ఉదాహరణకు కొన్ని)
    symbol = st.sidebar.text_input("Enter Stock Symbol (e.g., NSE:RELIANCE-EQ)", "NSE:RELIANCE-EQ")

def get_option_chain(symbol):
    data = {"symbol": symbol, "strikecount": 20}
    response = fyers.optionchain(data=data)
    return pd.DataFrame(response['data']['options'])

# Data Processing
if st.button("Fetch Data"):
    df = get_option_chain(symbol)
    
    # 2. Key Metrics
    col1, col2, col3 = st.columns(3)
    pcr = df['pe_oi'].sum() / df['ce_oi'].sum()
    col1.metric("Total CE OI", f"{df['ce_oi'].sum():,}")
    col2.metric("Total PE OI", f"{df['pe_oi'].sum():,}")
    col3.metric("PCR Ratio", round(pcr, 2))

    # 3. Highlight Table
    st.subheader("Live Option Chain")
    st.dataframe(df[['strike_price', 'ce_ltp', 'ce_oi', 'pe_oi', 'pe_ltp']].style.format("{:.0f}"), use_container_width=True)

    # 4. OI Chart
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df['strike_price'], y=df['ce_oi'], name='CE OI (Resistance)', marker_color='red'))
    fig.add_trace(go.Bar(x=df['strike_price'], y=df['pe_oi'], name='PE OI (Support)', marker_color='green'))
    fig.update_layout(title='Open Interest Analysis', barmode='group')
    st.plotly_chart(fig, use_container_width=True)
    
    # 5. CSV Export
    csv = df.to_csv(index=False)
    st.download_button("Download Data as CSV", csv, "option_chain.csv", "text/csv")

