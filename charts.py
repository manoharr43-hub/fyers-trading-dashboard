import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timedelta

# టెక్నికల్ ఇండికేటర్ ఫంక్షన్స్ (Pandas ద్వారా)
def get_ema(data, period):
    return data.ewm(span=period, adjust=False).mean()


def get_rsi(data, period=14):
    delta = data.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    # When loss is 0 for a stretch, rs becomes inf and rsi correctly -> 100.
    # When both gain and loss are 0 (flat price), rs is 0/0 = NaN; treat as neutral 50.
    return rsi.fillna(50)


RESOLUTION_DAYS = {
    "D": 365,    # 1 year of daily candles
    "60": 30,    # 30 days of 1h candles
    "15": 10,    # 10 days of 15m candles
    "5": 5,      # 5 days of 5m candles
}


def fetch_history(fyers, symbol: str, resolution: str):
    """Fetches candle history with a dynamic, resolution-appropriate date
    range ending today, and returns (df, error_message)."""
    days_back = RESOLUTION_DAYS.get(resolution, 365)
    range_to = datetime.today().strftime("%Y-%m-%d")
    range_from = (datetime.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    try:
        history = fyers.history({
            "symbol": symbol, "resolution": resolution,
            "date_format": "1", "range_from": range_from,
            "range_to": range_to, "cont_flag": "1"
        })
    except Exception as e:
        return None, f"API call failed: {e}"

    if not isinstance(history, dict):
        return None, "No response from API."

    if history.get("s") != "ok":
        return None, history.get("message", f"API returned status '{history.get('s')}' with no message.")

    candles = history.get("candles")
    if not candles:
        return None, (
            f"No candle data returned for {symbol} between {range_from} and {range_to}. "
            "Market may be closed, the symbol may be invalid, or there's no data in this range."
        )

    df = pd.DataFrame(candles, columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"])

    # Fyers returns epoch seconds (UTC). Convert to an actual datetime and
    # shift to IST so the chart's x-axis shows real trading-session times
    # rather than raw integers or UTC-shifted candles.
    df["Timestamp"] = pd.to_datetime(df["Timestamp"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")

    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    df.reset_index(drop=True, inplace=True)

    return df, None


def show_charts(fyers):
    st.title("📊 Institutional Charting Engine")

    symbol = st.sidebar.text_input("Enter Symbol", "NSE:RELIANCE-EQ")
    resolution_label = st.sidebar.selectbox(
        "Timeframe",
        ["1 Day", "1 Hour", "15 Min", "5 Min"],
        index=0,
    )
    resolution_map = {"1 Day": "D", "1 Hour": "60", "15 Min": "15", "5 Min": "5"}
    resolution = resolution_map[resolution_label]

    ema_period = st.sidebar.number_input("EMA Period", min_value=2, max_value=200, value=20)

    if st.button("🚀 Load Chart"):
        with st.spinner("Fetching data…"):
            df, error = fetch_history(fyers, symbol, resolution)

        if error:
            st.error(error)
            return

        if len(df) < ema_period:
            st.warning(
                f"Only {len(df)} candles returned, fewer than the EMA period ({ema_period}). "
                "Indicator values at the start of the chart will be unreliable."
            )

        df[f"EMA_{ema_period}"] = get_ema(df["Close"], ema_period)
        df["RSI"] = get_rsi(df["Close"], 14)

        fig = make_subplots(
            rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.06,
            row_heights=[0.7, 0.3],
            subplot_titles=(f"{symbol} — {resolution_label}", "RSI (14)"),
        )

        fig.add_trace(go.Candlestick(
            x=df["Timestamp"], open=df["Open"], high=df["High"],
            low=df["Low"], close=df["Close"], name="Price",
        ), row=1, col=1)

        fig.add_trace(go.Scatter(
            x=df["Timestamp"], y=df[f"EMA_{ema_period}"],
            name=f"EMA {ema_period}", line=dict(color="orange", width=1.5),
        ), row=1, col=1)

        fig.add_trace(go.Scatter(
            x=df["Timestamp"], y=df["RSI"], name="RSI",
            line=dict(color="purple", width=1.5),
        ), row=2, col=1)

        # Overbought/oversold reference lines on the RSI panel
        fig.add_hline(y=70, line_dash="dot", line_color="red", row=2, col=1)
        fig.add_hline(y=30, line_dash="dot", line_color="green", row=2, col=1)

        fig.update_layout(
            height=650,
            xaxis_rangeslider_visible=False,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        fig.update_yaxes(range=[0, 100], row=2, col=1)

        # Daily/longer timeframes show weekend gaps as empty space unless
        # we explicitly tell Plotly to skip non-trading days.
        if resolution == "D":
            fig.update_xaxes(
                rangebreaks=[dict(bounds=["sat", "mon"])],
                row=1, col=1,
            )
            fig.update_xaxes(
                rangebreaks=[dict(bounds=["sat", "mon"])],
                row=2, col=1,
            )

        st.plotly_chart(fig, use_container_width=True)

        with st.expander("📋 Raw candle data"):
            st.dataframe(df, use_container_width=True)
