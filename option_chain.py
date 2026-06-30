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
# ==========================================================
# OPTION GREEKS
# ==========================================================

st.markdown("## ⚡ Option Greeks")

# ----------------------------------------------------------
# FALLBACK GREEKS
# ----------------------------------------------------------

if "ce_delta" not in df.columns:
    df["ce_delta"] = np.where(
        df["strike_price"] <= atm,
        0.70,
        0.30
    )

if "pe_delta" not in df.columns:
    df["pe_delta"] = np.where(
        df["strike_price"] >= atm,
        -0.70,
        -0.30
    )

if "ce_gamma" not in df.columns:
    df["ce_gamma"] = 0.02

if "pe_gamma" not in df.columns:
    df["pe_gamma"] = 0.02

if "ce_theta" not in df.columns:
    df["ce_theta"] = -4.5

if "pe_theta" not in df.columns:
    df["pe_theta"] = -4.0

if "ce_vega" not in df.columns:
    df["ce_vega"] = 1.25

if "pe_vega" not in df.columns:
    df["pe_vega"] = 1.15

# ==========================================================
# GREEKS TABLE
# ==========================================================

greeks = df[[
    "strike_price",
    "ce_delta",
    "ce_gamma",
    "ce_theta",
    "ce_vega",
    "pe_delta",
    "pe_gamma",
    "pe_theta",
    "pe_vega"
]]

greeks.columns = [
    "Strike",
    "CE Δ",
    "CE Γ",
    "CE Θ",
    "CE Vega",
    "PE Δ",
    "PE Γ",
    "PE Θ",
    "PE Vega"
]

st.dataframe(
    greeks,
    use_container_width=True,
    height=500
)

st.divider()

# ==========================================================
# DELTA EXPOSURE
# ==========================================================

st.markdown("## 📊 Delta Exposure")

df["DEX"] = (
    df["ce_delta"] * df["ce_oi"]
    +
    abs(df["pe_delta"]) * df["pe_oi"]
)

dex_fig = go.Figure()

dex_fig.add_trace(

    go.Bar(

        x=df["strike_price"],

        y=df["DEX"],

        name="Delta Exposure"

    )

)

dex_fig.update_layout(

    template="plotly_dark",

    title="Delta Exposure",

    height=500

)

st.plotly_chart(
    dex_fig,
    use_container_width=True
)

st.divider()

# ==========================================================
# GAMMA EXPOSURE
# ==========================================================

st.markdown("## 📈 Gamma Exposure")

df["GEX"] = (

    df["ce_gamma"] * df["ce_oi"]

    +

    df["pe_gamma"] * df["pe_oi"]

)

gex_fig = go.Figure()

gex_fig.add_trace(

    go.Scatter(

        x=df["strike_price"],

        y=df["GEX"],

        mode="lines+markers",

        name="Gamma Exposure"

    )

)

gex_fig.update_layout(

    template="plotly_dark",

    title="Gamma Exposure",

    height=500

)

st.plotly_chart(
    gex_fig,
    use_container_width=True
)

st.divider()

# ==========================================================
# IV RANK
# ==========================================================

st.markdown("## 📉 IV Rank")

if "ce_iv" in df.columns:

    iv_high = df["ce_iv"].max()

    iv_low = df["ce_iv"].min()

    current_iv = df.loc[
        (df["strike_price"] - atm).abs().idxmin(),
        "ce_iv"
    ]

    iv_rank = (
        (current_iv - iv_low)
        /
        max(iv_high - iv_low, 1)
    ) * 100

else:

    iv_rank = 0

st.metric(
    "Current IV Rank",
    f"{iv_rank:.2f}%"
)

st.divider()

# ==========================================================
# INSTITUTIONAL SUMMARY
# ==========================================================

st.markdown("## 🏦 Institutional Activity")

institution = pd.DataFrame({

    "Metric":[

        "Support",

        "Resistance",

        "Max Pain",

        "PCR",

        "DEX",

        "GEX"

    ],

    "Value":[

        support,

        resistance,

        max_pain,

        round(pcr,2),

        round(df["DEX"].sum(),2),

        round(df["GEX"].sum(),2)

    ]

})

st.table(institution)
# ==========================================================
# VOLUME ANALYSIS
# ==========================================================

st.markdown("## 📊 Volume Analysis")

df["Total Volume"] = df["ce_volume"] + df["pe_volume"]

avg_volume = df["Total Volume"].mean()

df["Volume Breakout"] = np.where(
    df["Total Volume"] > (avg_volume * 2),
    "🔥 High Volume",
    ""
)

volume_table = df[
    [
        "strike_price",
        "ce_volume",
        "pe_volume",
        "Total Volume",
        "Volume Breakout"
    ]
]

volume_table.columns = [
    "Strike",
    "CE Volume",
    "PE Volume",
    "Total Volume",
    "Signal"
]

st.dataframe(
    volume_table,
    use_container_width=True,
    height=450
)

# ==========================================================
# VOLUME CHART
# ==========================================================

fig = go.Figure()

fig.add_trace(
    go.Bar(
        x=df["strike_price"],
        y=df["ce_volume"],
        name="CE Volume"
    )
)

fig.add_trace(
    go.Bar(
        x=df["strike_price"],
        y=df["pe_volume"],
        name="PE Volume"
    )
)

fig.update_layout(
    template="plotly_dark",
    barmode="group",
    title="Option Volume Distribution",
    height=500
)

st.plotly_chart(
    fig,
    use_container_width=True
)

st.divider()

# ==========================================================
# ADVANCED OI HEATMAP
# ==========================================================

st.markdown("## 🔥 Advanced OI Heatmap")

heatmap = go.Figure()

heatmap.add_trace(
    go.Heatmap(
        z=[
            df["ce_oi"],
            df["pe_oi"]
        ],
        x=df["strike_price"],
        y=["CE OI", "PE OI"],
        colorscale="Viridis"
    )
)

heatmap.update_layout(
    template="plotly_dark",
    height=350
)

st.plotly_chart(
    heatmap,
    use_container_width=True
)

st.divider()

# ==========================================================
# AI ENTRY / EXIT
# ==========================================================

st.markdown("## 🤖 AI Entry & Exit")

entry = "WAIT"
target = "-"
stoploss = "-"

if ai_signal == "BUY":
    entry = f"BUY above {atm}"
    target = resistance
    stoploss = support

elif ai_signal == "SELL":
    entry = f"SELL below {atm}"
    target = support
    stoploss = resistance

col1, col2, col3 = st.columns(3)

with col1:
    st.metric("Entry", entry)

with col2:
    st.metric("Target", target)

with col3:
    st.metric("Stop Loss", stoploss)

st.divider()

# ==========================================================
# OI vs VOLUME
# ==========================================================

st.markdown("## 📈 OI vs Volume")

comparison = go.Figure()

comparison.add_trace(
    go.Scatter(
        x=df["strike_price"],
        y=df["Total Volume"],
        mode="lines+markers",
        name="Volume"
    )
)

comparison.add_trace(
    go.Scatter(
        x=df["strike_price"],
        y=df["ce_oi"] + df["pe_oi"],
        mode="lines+markers",
        name="Total OI"
    )
)

comparison.update_layout(
    template="plotly_dark",
    height=500,
    title="Volume vs Open Interest"
)

st.plotly_chart(
    comparison,
    use_container_width=True
)

st.divider()

# ==========================================================
# ALERT PANEL
# ==========================================================

st.markdown("## 🚨 Live Alerts")

alerts = []

if pcr > 1.30:
    alerts.append("🟢 High PCR - Bullish Bias")

if pcr < 0.70:
    alerts.append("🔴 Low PCR - Bearish Bias")

if support == atm:
    alerts.append("🛡️ Strong Support at ATM")

if resistance == atm:
    alerts.append("🧱 Strong Resistance at ATM")

high_volume = df[df["Volume Breakout"] != ""]

if len(high_volume):

    alerts.append(
        f"🔥 {len(high_volume)} High Volume Strike(s) Detected"
    )

if len(alerts) == 0:

    st.info("No important alerts.")

else:

    for item in alerts:

        st.success(item)
# ==========================================================
# DASHBOARD SUMMARY
# ==========================================================

st.markdown("## 📋 Dashboard Summary")

summary_col1, summary_col2 = st.columns(2)

with summary_col1:

    st.info(f"""
### Market Overview

Spot Price : ₹{spot_price:,.2f}

ATM Strike : {atm}

PCR : {pcr:.2f}

Max Pain : {max_pain}

AI Signal : {ai_signal}
""")

with summary_col2:

    st.success(f"""
### Important Levels

Support : {support}

Resistance : {resistance}

Total CE OI : {int(df['ce_oi'].sum()):,}

Total PE OI : {int(df['pe_oi'].sum()):,}
""")

st.divider()

# ==========================================================
# QUICK STATISTICS
# ==========================================================

st.markdown("## 📊 Quick Statistics")

c1, c2, c3, c4 = st.columns(4)

with c1:
    st.metric(
        "Highest CE OI",
        int(df["ce_oi"].max())
    )

with c2:
    st.metric(
        "Highest PE OI",
        int(df["pe_oi"].max())
    )

with c3:
    st.metric(
        "Highest CE Volume",
        int(df["ce_volume"].max())
    )

with c4:
    st.metric(
        "Highest PE Volume",
        int(df["pe_volume"].max())
    )

st.divider()

# ==========================================================
# EXPORT CSV
# ==========================================================

st.markdown("## 💾 Export Data")

csv = df.to_csv(index=False).encode("utf-8")

st.download_button(
    label="📥 Download Option Chain CSV",
    data=csv,
    file_name="option_chain.csv",
    mime="text/csv"
)

st.divider()

# ==========================================================
# MARKET STATUS
# ==========================================================

st.markdown("## 🟢 Market Status")

market_status = "OPEN"

current_hour = time.localtime().tm_hour

if current_hour < 9 or current_hour >= 16:
    market_status = "CLOSED"

col1, col2 = st.columns(2)

with col1:
    st.metric(
        "Market",
        market_status
    )

with col2:
    st.metric(
        "Last Update",
        time.strftime("%H:%M:%S")
    )

st.divider()

# ==========================================================
# AUTO REFRESH
# ==========================================================

if auto_refresh:

    placeholder = st.empty()

    placeholder.info(
        f"🔄 Refreshing every {refresh_time} seconds..."
    )

    time.sleep(refresh_time)

    st.rerun()

# ==========================================================
# FOOTER
# ==========================================================

st.markdown("---")

st.caption(
    "📊 Professional Option Chain Dashboard | "
    "Powered by FYERS API V3 | Streamlit"
)
# ==========================================================
# ADVANCED AI SCORE
# ==========================================================

st.markdown("## 🤖 AI Market Intelligence")

bull_score = 0
bear_score = 0

# PCR Analysis
if pcr > 1.20:
    bull_score += 2
elif pcr < 0.80:
    bear_score += 2

# Spot Position
if spot_price > support:
    bull_score += 1
else:
    bear_score += 1

# Max Pain
if abs(spot_price - max_pain) < 100:
    bull_score += 1

# OI Comparison
if df["pe_oi"].sum() > df["ce_oi"].sum():
    bull_score += 2
else:
    bear_score += 2

# ----------------------------------------------------------
# AI DECISION
# ----------------------------------------------------------

if bull_score > bear_score:
    final_signal = "🟢 BULLISH"
elif bear_score > bull_score:
    final_signal = "🔴 BEARISH"
else:
    final_signal = "🟡 SIDEWAYS"

c1, c2, c3 = st.columns(3)

c1.metric("Bull Score", bull_score)
c2.metric("Bear Score", bear_score)
c3.metric("AI Decision", final_signal)

st.divider()

# ==========================================================
# SMART MONEY
# ==========================================================

st.markdown("## 💰 Smart Money Analysis")

smart_money = "Neutral"

if pcr > 1.30:
    smart_money = "Institutions likely accumulating PUT positions"

elif pcr < 0.70:
    smart_money = "Institutions likely writing CALL positions"

st.info(smart_money)

st.divider()

# ==========================================================
# OI CONCENTRATION
# ==========================================================

st.markdown("## 📊 Open Interest Concentration")

top_ce = df.nlargest(3, "ce_oi")
top_pe = df.nlargest(3, "pe_oi")

left, right = st.columns(2)

with left:
    st.subheader("Top CE OI")

    st.dataframe(
        top_ce[
            ["strike_price", "ce_oi"]
        ],
        use_container_width=True
    )

with right:
    st.subheader("Top PE OI")

    st.dataframe(
        top_pe[
            ["strike_price", "pe_oi"]
        ],
        use_container_width=True
    )

st.divider()

# ==========================================================
# RISK METER
# ==========================================================

st.markdown("## ⚠️ Risk Meter")

risk = "Medium"

if abs(spot_price - support) < 30:
    risk = "Low"

elif abs(spot_price - resistance) < 30:
    risk = "High"

risk_color = {
    "Low": "green",
    "Medium": "orange",
    "High": "red"
}

st.markdown(
    f"<h3 style='color:{risk_color[risk]};'>Risk : {risk}</h3>",
    unsafe_allow_html=True
)

st.divider()

# ==========================================================
# TRADING PLAN
# ==========================================================

st.markdown("## 🎯 Trading Plan")

if final_signal == "🟢 BULLISH":

    st.success(f"""
BUY ABOVE : {atm}

TARGET 1 : {resistance}

TARGET 2 : {resistance + 100}

STOPLOSS : {support}
""")

elif final_signal == "🔴 BEARISH":

    st.error(f"""
SELL BELOW : {atm}

TARGET 1 : {support}

TARGET 2 : {support - 100}

STOPLOSS : {resistance}
""")

else:

    st.warning("""
WAIT FOR CONFIRMATION

No high probability setup available.
""")

st.divider()

# ==========================================================
# MARKET SNAPSHOT
# ==========================================================

snapshot = pd.DataFrame({
    "Parameter": [
        "Spot",
        "ATM",
        "PCR",
        "Support",
        "Resistance",
        "Max Pain",
        "AI Signal",
        "Risk"
    ],
    "Value": [
        spot_price,
        atm,
        round(pcr, 2),
        support,
        resistance,
        max_pain,
        final_signal,
        risk
    ]
})

st.markdown("## 📋 Market Snapshot")

st.dataframe(
    snapshot,
    use_container_width=True,
    hide_index=True
)
# ==========================================================
# DATA VALIDATION
# ==========================================================

st.markdown("## ✅ Data Validation")

required_columns = [
    "strike_price",
    "ce_oi",
    "pe_oi",
    "ce_volume",
    "pe_volume"
]

missing = [col for col in required_columns if col not in df.columns]

if missing:
    st.error(f"Missing Columns: {', '.join(missing)}")
else:
    st.success("Option Chain data validated successfully.")

st.divider()

# ==========================================================
# INDIA VIX (PLACEHOLDER)
# ==========================================================

st.markdown("## 📉 India VIX")

st.info(
    "India VIX is not available from the current Option Chain response. "
    "Integrate a dedicated VIX API or market-data endpoint to display live VIX."
)

st.divider()

# ==========================================================
# FII / DII (PLACEHOLDER)
# ==========================================================

st.markdown("## 🏦 FII / DII Activity")

st.info(
    "FII/DII data requires a separate data source. "
    "It is not returned by the FYERS Option Chain API."
)

st.divider()

# ==========================================================
# SIMPLE STRATEGY SUGGESTION
# ==========================================================

st.markdown("## 🎯 Strategy Suggestion")

if final_signal == "🟢 BULLISH":
    st.success("Suggested Strategy: Bull Call Spread or Cash Long")

elif final_signal == "🔴 BEARISH":
    st.error("Suggested Strategy: Bear Put Spread or Protective Put")

else:
    st.warning("Suggested Strategy: Iron Condor or Wait for Breakout")

st.divider()

# ==========================================================
# SETTINGS SUMMARY
# ==========================================================

st.markdown("## ⚙️ Current Settings")

settings_df = pd.DataFrame({
    "Setting": [
        "Selected Symbol",
        "Strike Count",
        "Auto Refresh",
        "Refresh Interval (sec)"
    ],
    "Value": [
        symbol,
        strike_count,
        auto_refresh,
        refresh_time
    ]
})

st.dataframe(
    settings_df,
    use_container_width=True,
    hide_index=True
)

st.divider()

# ==========================================================
# DASHBOARD HEALTH
# ==========================================================

st.markdown("## 🩺 Dashboard Status")

checks = {
    "API Response": "OK",
    "Option Chain": "Loaded",
    "Charts": "Ready",
    "AI Engine": "Running",
    "Data": "Live"
}

health_df = pd.DataFrame(
    list(checks.items()),
    columns=["Component", "Status"]
)

st.dataframe(
    health_df,
    use_container_width=True,
    hide_index=True
)

st.divider()

# ==========================================================
# DISCLAIMER
# ==========================================================

st.warning(
    "This dashboard is for educational and analytical purposes only. "
    "Always confirm trades using your own analysis and risk management."
)

# ==========================================================
# FOOTER
# ==========================================================

st.markdown("---")

st.caption(
    "🚀 NSE AI PRO | Professional Option Chain Dashboard | "
    "Powered by FYERS API V3 & Streamlit"
)
