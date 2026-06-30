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
# ==========================================================
# OPTION CHAIN TABLE
# ==========================================================

st.markdown("## 📋 Live Option Chain")

display_cols = [
    "ce_oi",
    "ce_chng_oi",
    "ce_volume",
    "ce_ltp",
    "strike_price",
    "pe_ltp",
    "pe_volume",
    "pe_chng_oi",
    "pe_oi"
]

display_df = df[display_cols].copy()

display_df.columns = [
    "CE OI",
    "CE ΔOI",
    "CE Volume",
    "CE LTP",
    "Strike",
    "PE LTP",
    "PE Volume",
    "PE ΔOI",
    "PE OI"
]

# Highlight ATM Strike
def highlight_atm(row):
    if row["Strike"] == atm:
        return ["background-color:#FFD700;color:black;font-weight:bold"] * len(row)
    return [""] * len(row)

styled_df = (
    display_df.style
    .apply(highlight_atm, axis=1)
    .background_gradient(subset=["CE OI"], cmap="Greens")
    .background_gradient(subset=["PE OI"], cmap="Reds")
    .format("{:,.0f}")
)

st.dataframe(
    styled_df,
    use_container_width=True,
    height=650
)

st.divider()

# ==========================================================
# OPEN INTEREST BAR CHART
# ==========================================================

st.markdown("## 📊 Open Interest Analysis")

fig = go.Figure()

fig.add_trace(
    go.Bar(
        y=df["strike_price"],
        x=-df["ce_oi"],
        orientation="h",
        name="CE OI"
    )
)

fig.add_trace(
    go.Bar(
        y=df["strike_price"],
        x=df["pe_oi"],
        orientation="h",
        name="PE OI"
    )
)

fig.add_vline(
    x=0,
    line_width=2
)

fig.update_layout(

    barmode="overlay",

    height=700,

    template="plotly_dark",

    title="Call vs Put Open Interest",

    yaxis_title="Strike",

    xaxis_title="Open Interest"
)

st.plotly_chart(
    fig,
    use_container_width=True
)

# ==========================================================
# OI HEATMAP
# ==========================================================

st.markdown("## 🔥 OI Heatmap")

heat_df = df[[
    "strike_price",
    "ce_oi",
    "pe_oi",
    "ce_chng_oi",
    "pe_chng_oi"
]].copy()

heat_df.columns = [
    "Strike",
    "CE OI",
    "PE OI",
    "CE ΔOI",
    "PE ΔOI"
]

st.dataframe(

    heat_df.style

    .background_gradient(
        subset=["CE OI"],
        cmap="Greens"
    )

    .background_gradient(
        subset=["PE OI"],
        cmap="Reds"
    )

    .background_gradient(
        subset=["CE ΔOI"],
        cmap="Blues"
    )

    .background_gradient(
        subset=["PE ΔOI"],
        cmap="Oranges"
    ),

    use_container_width=True,

    height=500

)

st.divider()
# ==========================================================
# PCR GAUGE
# ==========================================================

st.markdown("## 📊 Put Call Ratio (PCR)")

gauge = go.Figure(go.Indicator(
    mode="gauge+number",
    value=pcr,
    title={"text": "PCR"},
    gauge={
        "axis": {"range": [0, 3]},
        "bar": {"color": "deepskyblue"},
        "steps": [
            {"range": [0, 0.7], "color": "red"},
            {"range": [0.7, 1.3], "color": "gold"},
            {"range": [1.3, 3], "color": "green"},
        ],
        "threshold": {
            "line": {"color": "white", "width": 4},
            "value": pcr
        }
    }
))

gauge.update_layout(
    template="plotly_dark",
    height=350
)

st.plotly_chart(
    gauge,
    use_container_width=True
)

st.divider()

# ==========================================================
# IMPLIED VOLATILITY
# ==========================================================

st.markdown("## 📈 IV Skew")

iv_chart = go.Figure()

if "ce_iv" in df.columns:

    iv_chart.add_trace(

        go.Scatter(

            x=df["strike_price"],

            y=df["ce_iv"],

            mode="lines+markers",

            name="CE IV"

        )

    )

if "pe_iv" in df.columns:

    iv_chart.add_trace(

        go.Scatter(

            x=df["strike_price"],

            y=df["pe_iv"],

            mode="lines+markers",

            name="PE IV"

        )

    )

iv_chart.update_layout(

    template="plotly_dark",

    title="Implied Volatility",

    xaxis_title="Strike",

    yaxis_title="IV %",

    height=500

)

st.plotly_chart(
    iv_chart,
    use_container_width=True
)

st.divider()

# ==========================================================
# MAX PAIN VISUALIZATION
# ==========================================================

st.markdown("## 🎯 Max Pain Analysis")

pain_chart = go.Figure()

pain_chart.add_trace(

    go.Bar(

        x=df["strike_price"],

        y=df["ce_oi"],

        name="CE OI"

    )

)

pain_chart.add_trace(

    go.Bar(

        x=df["strike_price"],

        y=df["pe_oi"],

        name="PE OI"

    )

)

pain_chart.add_vline(

    x=max_pain,

    line_dash="dash",

    line_color="yellow",

    annotation_text=f"Max Pain {max_pain}"

)

pain_chart.update_layout(

    template="plotly_dark",

    barmode="group",

    height=550,

    xaxis_title="Strike",

    yaxis_title="Open Interest"

)

st.plotly_chart(
    pain_chart,
    use_container_width=True
)

st.divider()

# ==========================================================
# SUPPORT & RESISTANCE
# ==========================================================

st.markdown("## 🛡️ Support & Resistance")

sr = go.Figure()

sr.add_trace(

    go.Scatter(

        x=df["strike_price"],

        y=df["pe_oi"],

        mode="lines+markers",

        name="Support (PE)"

    )

)

sr.add_trace(

    go.Scatter(

        x=df["strike_price"],

        y=df["ce_oi"],

        mode="lines+markers",

        name="Resistance (CE)"

    )

)

sr.add_vline(

    x=support,

    line_color="green",

    annotation_text=f"Support {support}"

)

sr.add_vline(

    x=resistance,

    line_color="red",

    annotation_text=f"Resistance {resistance}"

)

sr.add_vline(

    x=atm,

    line_dash="dot",

    line_color="white",

    annotation_text="ATM"

)

sr.update_layout(

    template="plotly_dark",

    height=550,

    xaxis_title="Strike",

    yaxis_title="Open Interest"

)

st.plotly_chart(
    sr,
    use_container_width=True
)

st.divider()
# ==========================================================
# OI CHANGE ANALYSIS
# ==========================================================

st.markdown("## 🔥 OI Change Analysis")

analysis_df = df.copy()

# ----------------------------------------------------------
# BUILD-UP DETECTION
# ----------------------------------------------------------

def build_up(row):

    ce_signal = "Neutral"
    pe_signal = "Neutral"

    # CALL SIDE
    if row["ce_chng_oi"] > 0 and row["ce_ltp"] < row["ce_ltp"]:
        ce_signal = "Short Build-up"

    elif row["ce_chng_oi"] < 0:
        ce_signal = "Short Covering"

    # PUT SIDE
    if row["pe_chng_oi"] > 0:
        pe_signal = "Long Build-up"

    elif row["pe_chng_oi"] < 0:
        pe_signal = "Long Unwinding"

    return pd.Series([ce_signal, pe_signal])

analysis_df[["CE Signal","PE Signal"]] = analysis_df.apply(
    build_up,
    axis=1
)

display = analysis_df[[
    "strike_price",
    "ce_oi",
    "ce_chng_oi",
    "CE Signal",
    "pe_oi",
    "pe_chng_oi",
    "PE Signal"
]]

display.columns = [
    "Strike",
    "CE OI",
    "CE ΔOI",
    "CE Signal",
    "PE OI",
    "PE ΔOI",
    "PE Signal"
]

st.dataframe(
    display,
    use_container_width=True,
    height=500
)

st.divider()

# ==========================================================
# TOP WRITING STRIKES
# ==========================================================

st.markdown("## 🏦 Highest OI Writing")

left,right = st.columns(2)

with left:

    st.subheader("📉 Top CE Writing")

    ce_top = (
        df.nlargest(5,"ce_oi")[
            ["strike_price","ce_oi","ce_chng_oi"]
        ]
    )

    st.dataframe(
        ce_top,
        use_container_width=True
    )

with right:

    st.subheader("📈 Top PE Writing")

    pe_top = (
        df.nlargest(5,"pe_oi")[
            ["strike_price","pe_oi","pe_chng_oi"]
        ]
    )

    st.dataframe(
        pe_top,
        use_container_width=True
    )

st.divider()

# ==========================================================
# AI MARKET SIGNAL
# ==========================================================

st.markdown("## 🤖 AI Trading Signal")

ai_signal = "SIDEWAYS"

reason = []

if pcr > 1.30:

    ai_signal = "BUY"

    reason.append("PCR Bullish")

if support > atm:

    reason.append("Support Above ATM")

if resistance < atm:

    reason.append("Resistance Broken")

if pcr < 0.70:

    ai_signal = "SELL"

    reason.append("PCR Bearish")

if ai_signal == "BUY":

    st.success(f"""
### 🟢 AI BUY SIGNAL

Reasons

{chr(10).join(reason)}
""")

elif ai_signal == "SELL":

    st.error(f"""
### 🔴 AI SELL SIGNAL

Reasons

{chr(10).join(reason)}
""")

else:

    st.warning("""
### 🟡 WAIT

Market is sideways.

No strong confirmation.
""")

st.divider()

# ==========================================================
# SMART MONEY SUMMARY
# ==========================================================

st.markdown("## 💰 Smart Money Summary")

summary = pd.DataFrame({

    "Parameter":[

        "Spot Price",
        "ATM",
        "PCR",
        "Support",
        "Resistance",
        "Max Pain",
        "AI Signal"

    ],

    "Value":[

        f"{spot_price:.2f}",
        atm,
        pcr,
        support,
        resistance,
        max_pain,
        ai_signal

    ]

})

st.table(summary)

# ==========================================================
# AUTO REFRESH
# ==========================================================

if auto_refresh:

    time.sleep(refresh_time)

    st.rerun()
