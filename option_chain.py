import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from fyers_apiv3 import fyersModel
import time

# ==========================================================
# PAGE CONFIG
# ==========================================================
st.set_page_config(
    page_title="📊 Professional Option Chain",
    page_icon="📈",
    layout="wide"
)

# ==========================================================
# CUSTOM CSS
# ==========================================================
st.markdown("""
<style>

.stApp{
    background:#0d1117;
    color:white;
}

div[data-testid="metric-container"]{
    background:#161b22;
    border-radius:10px;
    border:1px solid #30363d;
    padding:10px;
}

.sidebar .sidebar-content{
    background:#161b22;
}

</style>
""",unsafe_allow_html=True)

# ==========================================================
# HEADER
# ==========================================================
st.title("📊 Professional Option Chain Dashboard")
st.caption("Powered by FYERS API V3")

# ==========================================================
# SIDEBAR
# ==========================================================
st.sidebar.header("Settings")

symbol = st.sidebar.selectbox(
    "Index",
    [
        "NSE:NIFTY50-INDEX",
        "NSE:NIFTYBANK-INDEX",
        "BSE:SENSEX-INDEX",
        "BSE:BANKEX-INDEX"
    ]
)

strike_count = st.sidebar.slider(
    "Strike Count",
    5,
    30,
    20
)

auto_refresh = st.sidebar.checkbox("Auto Refresh")

refresh_time = st.sidebar.slider(
    "Refresh Seconds",
    5,
    60,
    10
)

# ==========================================================
# FYERS SESSION
# ==========================================================

CLIENT_ID = st.secrets["FYERS_CLIENT_ID"]
TOKEN = st.secrets["FYERS_ACCESS_TOKEN"]

fyers = fyersModel.FyersModel(
    client_id=CLIENT_ID,
    token=TOKEN,
    is_async=False
)

# ==========================================================
# FETCH OPTION CHAIN
# ==========================================================

def fetch_option_chain():

    data = {
        "symbol":symbol,
        "strikecount":strike_count
    }

    response = fyers.optionchain(data=data)

    if response["s"]!="ok":
        st.error(response["message"])
        return None

    return response

# ==========================================================
# CALCULATE PCR
# ==========================================================

def calculate_pcr(df):

    ce=df["ce_oi"].sum()

    pe=df["pe_oi"].sum()

    if ce==0:
        return 0

    return round(pe/ce,2)

# ==========================================================
# CALCULATE MAX PAIN
# ==========================================================

def calculate_max_pain(df):

    strikes=df["strike_price"].tolist()

    pain=[]

    for strike in strikes:

        ce=((strike-df["strike_price"]).clip(lower=0)*df["ce_oi"]).sum()

        pe=((df["strike_price"]-strike).clip(lower=0)*df["pe_oi"]).sum()

        pain.append(ce+pe)

    return strikes[np.argmin(pain)]

# ==========================================================
# MARKET SIGNAL
# ==========================================================

def signal(pcr):

    if pcr>1.3:
        return "🟢 Bullish"

    elif pcr<0.7:
        return "🔴 Bearish"

    return "🟡 Neutral"
