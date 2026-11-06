
import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


# ============================================================
# AI MARKET INTELLIGENCE
# FYERS MARKET DATA + TECHNICAL ANALYSIS
# Existing app.py integration:
# show_ai_market_intelligence(client)
# ============================================================

IST = ZoneInfo("Asia/Kolkata")


def _calculate_indicators(df):
    """Calculate technical indicators using completed candles."""

    df = df.copy()

    df["EMA_9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["EMA_21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["EMA_50"] = df["close"].ewm(span=50, adjust=False).mean()

    delta = df["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / 14, min_periods=14, adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / 14, min_periods=14, adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))
    df["RSI"] = df["RSI"].fillna(50)

    df["VOLUME_AVG"] = df["volume"].rolling(20).mean()

    df["RVOL"] = (
        df["volume"] / df["VOLUME_AVG"].replace(0, np.nan)
    ).fillna(0)

    df["HIGH_20"] = df["high"].shift(1).rolling(20).max()
    df["LOW_20"] = df["low"].shift(1).rolling(20).min()

    return df


def _analyze_market(df):

    latest = df.iloc[-1]

    price = float(latest["close"])
    ema9 = float(latest["EMA_9"])
    ema21 = float(latest["EMA_21"])
    ema50 = float(latest["EMA_50"])
    rsi = float(latest["RSI"])
    rvol = float(latest["RVOL"])

    score = 0
    reasons = []

    # Trend analysis
    if ema9 > ema21 > ema50:
        score += 35
        reasons.append("Bullish EMA alignment")

    elif ema9 < ema21 < ema50:
        score -= 35
        reasons.append("Bearish EMA alignment")

    elif ema9 > ema21:
        score += 15
        reasons.append("Short-term bullish trend")

    elif ema9 < ema21:
        score -= 15
        reasons.append("Short-term bearish trend")

    # RSI analysis
    if rsi >= 60:
        score += 20
        reasons.append("Strong RSI momentum")

    elif rsi <= 40:
        score -= 20
        reasons.append("Weak RSI momentum")

    # Volume analysis
    if rvol >= 1.5:
        if latest["close"] > latest["open"]:
            score += 15
            reasons.append("High volume buying pressure")

        elif latest["close"] < latest["open"]:
            score -= 15
            reasons.append("High volume selling pressure")

    # Breakout / breakdown
    high20 = latest["HIGH_20"]
    low20 = latest["LOW_20"]

    if pd.notna(high20) and price > float(high20):
        score += 20
        reasons.append("20-candle breakout")

    elif pd.notna(low20) and price < float(low20):
        score -= 20
        reasons.append("20-candle breakdown")

    # Final signal
    if score >= 35:
        signal = "BUY"
        trend = "BULLISH"

    elif score <= -35:
        signal = "SELL"
        trend = "BEARISH"

    else:
        signal = "WAIT"
        trend = "SIDEWAYS / MIXED"

    confidence = min(95, max(30, 50 + abs(score)))

    return {
        "price": price,
        "ema9": ema9,
        "ema21": ema21,
        "ema50": ema50,
        "rsi": rsi,
        "rvol": rvol,
        "score": score,
        "signal": signal,
        "trend": trend,
        "confidence": confidence,
        "reasons": reasons,
        "support": float(df["low"].tail(20).min()),
        "resistance": float(df["high"].tail(20).max())
    }


def _fetch_fyers_data(client, symbol, resolution, days):

    if client is None:
        raise ValueError("FYERS client is not connected.")

    today = datetime.now(IST).date()
    start_date = today - timedelta(days=days)

    data = {
        "symbol": symbol,
        "resolution": str(resolution),
        "date_format": "1",
        "range_from": start_date.strftime("%Y-%m-%d"),
        "range_to": today.strftime("%Y-%m-%d"),
        "cont_flag": "1"
    }

    response = client.history(data)

    if not isinstance(response, dict):
        raise ValueError("Invalid FYERS response.")

    if response.get("s") != "ok":
        raise ValueError(
            response.get("message", "FYERS data fetch failed.")
        )

    candles = response.get("candles", [])

    if not candles:
        raise ValueError("No candle data received from FYERS.")

    df = pd.DataFrame(
        candles,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]
    )

    df["datetime"] = pd.to_datetime(
        df["timestamp"], unit="s", utc=True
    ).dt.tz_convert("Asia/Kolkata")

    numeric_columns = [
        "open", "high", "low", "close", "volume"
    ]

    for col in numeric_columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=numeric_columns)
    df = df.sort_values("timestamp").drop_duplicates(
        subset=["timestamp"]
    ).reset_index(drop=True)

    # Exclude the currently forming candle.
    now = datetime.now(IST)
    candle_minutes = int(resolution)

    if candle_minutes > 0:
        candle_close_time = (
            df["datetime"] +
            pd.to_timedelta(candle_minutes, unit="m")
        )

        df = df[candle_close_time <= now].copy()

    if len(df) < 55:
        raise ValueError(
            "Not enough completed candles. "
            "Try a larger historical period."
        )

    return df.reset_index(drop=True)


# ============================================================
# MAIN STREAMLIT PAGE
# ============================================================

def show_ai_market_intelligence(client):

    st.title("🧠 AI MARKET INTELLIGENCE")

    st.caption(
        "FYERS-based technical market analysis "
        "| EMA | RSI | Relative Volume | Breakout"
    )

    if client is None:
        st.error("FYERS client is not connected. Please login first.")
        return

    st.divider()

    col1, col2, col3 = st.columns(3)

    with col1:
        symbol = st.text_input(
            "FYERS Symbol",
            value="NSE:RELIANCE-EQ"
        ).strip().upper()

    with col2:
        timeframe = st.selectbox(
            "Timeframe",
            options=["1", "5", "15", "30", "60"],
            index=1,
            format_func=lambda x: {
                "1": "1 Minute",
                "5": "5 Minutes",
                "15": "15 Minutes",
                "30": "30 Minutes",
                "60": "1 Hour"
            }[x]
        )

    with col3:
        history_days = st.selectbox(
            "Historical Days",
            options=[5, 10, 15, 30, 60],
            index=2
        )

    run_analysis = st.button(
        "🚀 RUN MARKET ANALYSIS",
        type="primary",
        use_container_width=True
    )

    if run_analysis:

        if not symbol:
            st.warning("Please enter a valid FYERS symbol.")
            return

        try:
            with st.spinner("Fetching FYERS market data..."):

                df = _fetch_fyers_data(
                    client,
                    symbol,
                    timeframe,
                    history_days
                )

                df = _calculate_indicators(df)

                result = _analyze_market(df)

            st.session_state["market_intelligence_result"] = result
            st.session_state["market_intelligence_df"] = df
            st.session_state["market_intelligence_symbol"] = symbol

        except Exception as e:
            st.error(f"Market analysis error: {e}")
            return

    result = st.session_state.get(
        "market_intelligence_result"
    )

    df = st.session_state.get(
        "market_intelligence_df"
    )

    saved_symbol = st.session_state.get(
        "market_intelligence_symbol"
    )

    if result is None or df is None:
        st.info("Enter a symbol and click RUN MARKET ANALYSIS.")
        return

    st.divider()

    st.subheader(f"📊 Analysis: {saved_symbol}")

    c1, c2, c3, c4 = st.columns(4)

    c1.metric(
        "Current Price",
        f"{result['price']:.2f}"
    )

    c2.metric(
        "Market Trend",
        result["trend"]
    )

    c3.metric(
        "RSI",
        f"{result['rsi']:.2f}"
    )

    c4.metric(
        "Relative Volume",
        f"{result['rvol']:.2f}x"
    )

    st.divider()

    st.subheader("🎯 Market Signal")

    if result["signal"] == "BUY":
        st.success("🟢 BUY — Bullish conditions detected")

    elif result["signal"] == "SELL":
        st.error("🔴 SELL — Bearish conditions detected")

    else:
        st.warning("🟡 WAIT — No strong directional confirmation")

    c1, c2 = st.columns(2)

    c1.metric(
        "Technical Score",
        f"{result['score']}/90"
    )

    c2.metric(
        "Signal Confidence",
        f"{result['confidence']}%"
    )

    st.caption(
        "Confidence is a rule-based technical score, "
        "not a statistically validated probability."
    )

    st.divider()

    st.subheader("📈 EMA Analysis")

    ema_data = pd.DataFrame({
        "Indicator": ["EMA 9", "EMA 21", "EMA 50"],
        "Value": [
            result["ema9"],
            result["ema21"],
            result["ema50"]
        ]
    })

    st.dataframe(
        ema_data,
        use_container_width=True,
        hide_index=True
    )

    st.subheader("🧱 Support & Resistance")

    c1, c2 = st.columns(2)

    c1.metric(
        "Support",
        f"{result['support']:.2f}"
    )

    c2.metric(
        "Resistance",
        f"{result['resistance']:.2f}"
    )

    st.divider()

    st.subheader("🔍 Market Intelligence Findings")

    if result["reasons"]:
        for reason in result["reasons"]:
            st.write("•", reason)
    else:
        st.write("No strong technical confirmation.")

    st.divider()

    st.subheader("📉 Price & EMA Chart")

    chart_df = df.set_index("datetime")[
        ["close", "EMA_9", "EMA_21", "EMA_50"]
    ]

    st.line_chart(
        chart_df,
        use_container_width=True
    )

    st.subheader("📋 Recent Candle Data")

    display_df = df.tail(20).copy()

    display_columns = [
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "RSI",
        "RVOL"
    ]

    st.dataframe(
        display_df[display_columns].sort_values(
            "datetime", ascending=False
        ),
        use_container_width=True,
        hide_index=True
    )

    csv_data = df.to_csv(index=False).encode("utf-8")

    st.download_button(
        "📥 DOWNLOAD MARKET DATA CSV",
        data=csv_data,
        file_name="market_intelligence.csv",
        mime="text/csv",
        use_container_width=True
    )

    st.warning(
        "This is technical market analysis, not guaranteed "
        "buy/sell advice. Verify signals and manage risk "
        "before placing any trade."
    )
