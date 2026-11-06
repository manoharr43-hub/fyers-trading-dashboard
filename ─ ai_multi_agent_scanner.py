
import streamlit as st
import pandas as pd
import numpy as np
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed
import io

# Reuse existing FYERS data and symbol functions.
from scanner import (
    _fetch_timeframe_data,
    load_nse_equity_symbols,
    load_fo_stocks,
)

IST = ZoneInfo("Asia/Kolkata")


def _rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def _prepare_closed_candles(df, minutes):
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()
    required = ["Open", "High", "Low", "Close", "Volume"]
    if not all(c in x.columns for c in required):
        return pd.DataFrame()

    for c in required:
        x[c] = pd.to_numeric(x[c], errors="coerce")

    x = x.dropna(subset=required).sort_values("Time").reset_index(drop=True)

    if "Time" in x.columns:
        t = pd.to_datetime(x["Time"], errors="coerce")
        if t.dt.tz is None:
            t = t.dt.tz_localize(IST)
        else:
            t = t.dt.tz_convert(IST)

        now = pd.Timestamp(datetime.now(IST))
        # Only fully completed candles are used.
        x = x.loc[(t + pd.Timedelta(minutes=minutes)) <= now].copy()
        x["Time"] = t.loc[x.index]

    return x.reset_index(drop=True)


def _analyze_timeframe(df, timeframe):
    if df is None or len(df) < 30:
        return {
            "trend": "DATA_UNAVAILABLE",
            "score": 0,
            "confidence": 0,
            "reason": "Insufficient completed candles",
            "rvol": 0,
            "rsi": 50,
            "close": None,
        }

    close = df["Close"]
    volume = df["Volume"]

    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    rsi = _rsi(close)

    avg_volume = volume.iloc[-21:-1].mean()
    rvol = float(volume.iloc[-1] / avg_volume) if avg_volume and avg_volume > 0 else 0

    last = float(close.iloc[-1])
    score = 0
    reasons = []

    # Trend agent
    if last > ema9.iloc[-1] > ema21.iloc[-1]:
        score += 20
        reasons.append("Price above EMA9/21")
    elif last < ema9.iloc[-1] < ema21.iloc[-1]:
        score -= 20
        reasons.append("Price below EMA9/21")

    if ema21.iloc[-1] > ema50.iloc[-1]:
        score += 15
        reasons.append("EMA21 above EMA50")
    elif ema21.iloc[-1] < ema50.iloc[-1]:
        score -= 15
        reasons.append("EMA21 below EMA50")

    # Momentum agent
    rv = float(rsi.iloc[-1])
    if 55 <= rv < 75:
        score += 15
        reasons.append("Bullish RSI momentum")
    elif 25 < rv <= 45:
        score -= 15
        reasons.append("Bearish RSI momentum")

    # Volume agent
    if rvol >= 1.5:
        direction = 1 if close.iloc[-1] > df["Open"].iloc[-1] else -1
        score += 10 * direction
        reasons.append(f"Relative volume {rvol:.2f}x")

    # Market structure agent: confirmed previous range break
    previous_high = float(df["High"].iloc[-21:-1].max())
    previous_low = float(df["Low"].iloc[-21:-1].min())

    if last > previous_high:
        score += 20
        reasons.append("20-candle range breakout")
    elif last < previous_low:
        score -= 20
        reasons.append("20-candle range breakdown")

    # VWAP estimate using available candle data
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    total_vol = volume.sum()
    vwap = float((typical * volume).sum() / total_vol) if total_vol > 0 else last

    if last > vwap:
        score += 10
        reasons.append("Above volume-weighted average")
    elif last < vwap:
        score -= 10
        reasons.append("Below volume-weighted average")

    score = int(max(-100, min(100, score)))

    if score >= 35:
        trend = "BULLISH"
    elif score <= -35:
        trend = "BEARISH"
    else:
        trend = "SIDEWAYS"

    confidence = min(95, abs(score) + 25) if trend != "SIDEWAYS" else 40

    return {
        "trend": trend,
        "score": score,
        "confidence": confidence,
        "reason": " | ".join(reasons) or "No strong confirmation",
        "rvol": round(rvol, 2),
        "rsi": round(rv, 2),
        "close": round(last, 2),
        "vwap": round(vwap, 2),
        "candle_time": str(df["Time"].iloc[-1]),
    }


def _final_decision(a5, a15, a60):
    agents = [a5, a15, a60]

    if any(a["trend"] == "DATA_UNAVAILABLE" for a in agents):
        return "NO TRADE", 0, "One or more timeframe datasets unavailable"

    # 5M 30%, 15M 40%, 1H 30%.
    combined = (
        a5["score"] * 0.30
        + a15["score"] * 0.40
        + a60["score"] * 0.30
    )

    aligned_bull = all(a["trend"] == "BULLISH" for a in agents)
    aligned_bear = all(a["trend"] == "BEARISH" for a in agents)

    if aligned_bull and combined >= 35:
        signal = "BUY"
    elif aligned_bear and combined <= -35:
        signal = "SELL"
    else:
        signal = "HOLD"

    confidence = int(min(95, abs(combined) + 20))
    if signal == "HOLD":
        confidence = min(confidence, 60)

    reason = (
        f"5M={a5['trend']} | 15M={a15['trend']} | "
        f"1H={a60['trend']} | Combined score={combined:.1f}"
    )
    return signal, confidence, reason


def _scan_one(fyers, symbol):
    try:
        raw5 = _fetch_timeframe_data(fyers, symbol, "5", 10)
        raw15 = _fetch_timeframe_data(fyers, symbol, "15", 20)
        raw60 = _fetch_timeframe_data(fyers, symbol, "60", 45)

        d5 = _prepare_closed_candles(raw5, 5)
        d15 = _prepare_closed_candles(raw15, 15)
        d60 = _prepare_closed_candles(raw60, 60)

        a5 = _analyze_timeframe(d5, "5M")
        a15 = _analyze_timeframe(d15, "15M")
        a60 = _analyze_timeframe(d60, "1H")

        signal, confidence, reason = _final_decision(a5, a15, a60)

        return {
            "SYMBOL": symbol.replace("NSE:", "").replace("-EQ", ""),
            "LTP": a5.get("close"),
            "5M TREND": a5["trend"],
            "15M TREND": a15["trend"],
            "1H TREND": a60["trend"],
            "5M RSI": a5["rsi"],
            "15M RSI": a15["rsi"],
            "1H RSI": a60["rsi"],
            "5M RVOL": a5["rvol"],
            "15M RVOL": a15["rvol"],
            "AI SIGNAL": signal,
            "AI CONFIDENCE %": confidence,
            "REASON": reason,
            "5M EVIDENCE": a5["reason"],
            "15M EVIDENCE": a15["reason"],
            "1H EVIDENCE": a60["reason"],
            "STATUS": "OK",
        }

    except Exception as e:
        return {
            "SYMBOL": symbol,
            "AI SIGNAL": "NO TRADE",
            "AI CONFIDENCE %": 0,
            "STATUS": f"ERROR: {type(e).__name__}: {str(e)[:120]}",
        }


def show_ai_multi_agent_scanner(fyers):
    st.title("🤖 AI MULTI-AGENT SCANNER")
    st.caption(
        "Independent FYERS scanner | Completed 5M / 15M / 1H candles | "
        "NSE & F&O stocks | Asia/Kolkata"
    )

    market = st.radio(
        "Select Universe",
        ["F&O STOCKS", "NSE STOCKS"],
        horizontal=True,
        key="multi_agent_universe",
    )

    try:
        symbols = (
            load_fo_stocks()
            if market == "F&O STOCKS"
            else load_nse_equity_symbols()
        )
    except Exception as e:
        st.error(f"Symbol loading failed: {e}")
        return

    if not symbols:
        st.error("No symbols available. Check FYERS connection.")
        return

    # F&O scanner must contain stocks only, not index symbols.
    if market == "F&O STOCKS":
        excluded = {
            "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
            "NIFTYNEXT50", "SENSEX", "BANKEX"
        }
        symbols = [
            s for s in symbols
            if s.split(":")[-1].replace("-EQ", "").replace("-INDEX", "")
            not in excluded
        ]

    c1, c2 = st.columns(2)
    with c1:
        limit = st.number_input(
            "Scan limit",
            min_value=1,
            max_value=len(symbols),
            value=min(50, len(symbols)),
            step=10,
            key="multi_agent_limit",
        )
    with c2:
        workers = st.selectbox(
            "Parallel workers", [1, 2, 3, 4], index=1,
            key="multi_agent_workers",
        )

    st.info(
        f"Universe: {market} | Available: {len(symbols)} | "
        f"Selected: {limit}"
    )

    if st.button(
        "🚀 RUN AI MULTI-AGENT SCAN",
        type="primary",
        use_container_width=True,
        key="multi_agent_run",
    ):
        selected = symbols[:int(limit)]
        rows = []
        errors = []
        progress = st.progress(0.0, text="Starting scan...")

        with ThreadPoolExecutor(max_workers=int(workers)) as pool:
            futures = {
                pool.submit(_scan_one, fyers, symbol): symbol
                for symbol in selected
            }

            for i, future in enumerate(as_completed(futures), 1):
                try:
                    row = future.result()
                    if row.get("STATUS") == "OK":
                        rows.append(row)
                    else:
                        errors.append(row)
                except Exception as e:
                    errors.append({
                        "SYMBOL": futures[future],
                        "ERROR": str(e),
                    })

                progress.progress(
                    i / len(selected),
                    text=f"Scanned {i}/{len(selected)} | Signals: {len(rows)}",
                )

        progress.empty()

        result = pd.DataFrame(rows)
        if not result.empty:
            result = result.sort_values(
                "AI CONFIDENCE %",
                ascending=False,
            ).reset_index(drop=True)

        st.session_state["multi_agent_results"] = result
        st.session_state["multi_agent_errors"] = errors
        st.session_state["multi_agent_scan_time"] = datetime.now(IST).strftime(
            "%d-%b-%Y %H:%M:%S IST"
        )

    result = st.session_state.get("multi_agent_results")

    if result is None:
        st.info("Select universe and press RUN AI MULTI-AGENT SCAN.")
        return

    st.caption(
        f"Last scan: {st.session_state.get('multi_agent_scan_time', 'N/A')}"
    )

    if result.empty:
        st.warning("No usable completed-candle data returned by FYERS.")
    else:
        buys = int(result["AI SIGNAL"].eq("BUY").sum())
        sells = int(result["AI SIGNAL"].eq("SELL").sum())
        holds = int(result["AI SIGNAL"].eq("HOLD").sum())

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Stocks scanned", len(result))
        m2.metric("BUY", buys)
        m3.metric("SELL", sells)
        m4.metric("HOLD", holds)

        choice = st.multiselect(
            "Filter signals",
            ["BUY", "SELL", "HOLD", "NO TRADE"],
            default=["BUY", "SELL", "HOLD"],
            key="multi_agent_filter",
        )

        view = result[result["AI SIGNAL"].isin(choice)].copy()
        st.dataframe(view, use_container_width=True, hide_index=True, height=520)

        csv_data = view.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "📥 Download CSV",
            csv_data,
            "AI_MULTI_AGENT_SIGNALS.csv",
            "text/csv",
            key="multi_agent_csv",
        )

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            view.to_excel(writer, index=False, sheet_name="AI Signals")

        st.download_button(
            "📊 Download Excel",
            output.getvalue(),
            "AI_MULTI_AGENT_SIGNALS.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="multi_agent_excel",
        )

    errors = st.session_state.get("multi_agent_errors", [])
    if errors:
        with st.expander(f"Data/API errors ({len(errors)})"):
            st.dataframe(pd.DataFrame(errors), use_container_width=True)

    st.warning(
        "Signals are technical research outputs, not guaranteed predictions "
        "or trading instructions. Check data quality and paper-test before use. "
        "This module does not place orders."
    )
