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
# ==========================================================
# LOAD OPTION CHAIN DATA
# ==========================================================

response = fetch_option_chain()

if response is None:
    st.stop()

data = response["data"]

spot_price = data.get("ltp", 0)

options = data.get("options", [])

if len(options) == 0:
    st.warning("No Option Chain Data Available")
    st.stop()

# ==========================================================
# DATAFRAME
# ==========================================================

df = pd.DataFrame(options)

numeric_columns = [
    "strike_price",
    "ce_ltp",
    "ce_oi",
    "ce_volume",
    "ce_chng_oi",
    "pe_ltp",
    "pe_oi",
    "pe_volume",
    "pe_chng_oi",
    "ce_iv",
    "pe_iv"
]

for col in numeric_columns:
    if col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

df = df.sort_values("strike_price").reset_index(drop=True)

# ==========================================================
# CALCULATIONS
# ==========================================================

pcr = calculate_pcr(df)

max_pain = calculate_max_pain(df)

atm_index = (df["strike_price"] - spot_price).abs().idxmin()

atm = df.loc[atm_index, "strike_price"]

support = df.loc[df["pe_oi"].idxmax(), "strike_price"]

resistance = df.loc[df["ce_oi"].idxmax(), "strike_price"]

total_ce = int(df["ce_oi"].sum())

total_pe = int(df["pe_oi"].sum())

signal_text = signal(pcr)

# ==========================================================
# KPI DASHBOARD
# ==========================================================

st.markdown("## 📊 Live Market Overview")

col1, col2, col3, col4, col5 = st.columns(5)

with col1:
    st.metric(
        "Spot Price",
        f"₹{spot_price:,.2f}"
    )

with col2:
    st.metric(
        "ATM Strike",
        f"{atm:,.0f}"
    )

with col3:
    st.metric(
        "PCR",
        pcr
    )

with col4:
    st.metric(
        "Max Pain",
        f"{max_pain:,.0f}"
    )

with col5:
    st.metric(
        "Signal",
        signal_text
    )

# ==========================================================
# OI SUMMARY
# ==========================================================

left, right = st.columns(2)

with left:

    st.success(f"""
### 🟢 CALL SIDE

Total CE OI : **{total_ce:,}**

Resistance : **{resistance:,.0f}**
""")

with right:

    st.error(f"""
### 🔴 PUT SIDE

Total PE OI : **{total_pe:,}**

Support : **{support:,.0f}**
""")

st.divider()

# ==========================================================
# MARKET SUMMARY
# ==========================================================

summary = f"""
### 📈 Market Summary

- Spot Price : **₹{spot_price:,.2f}**
- ATM Strike : **{atm:,.0f}**
- PCR : **{pcr}**
- Max Pain : **{max_pain:,.0f}**
- Strong Support : **{support:,.0f}**
- Strong Resistance : **{resistance:,.0f}**
- Market Signal : **{signal_text}**
"""

st.markdown(summary)
