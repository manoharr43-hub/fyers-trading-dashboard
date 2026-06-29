import streamlit as st
import pandas as pd
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

# ── Configuration ────────────────────────────────────────────────────────────
SYMBOLS = ["NSE:RELIANCE-EQ", "NSE:TCS-EQ", "NSE:INFY-EQ", "NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ", "NSE:WIPRO-EQ"]
DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO = datetime.today().strftime("%Y-%m-%d")

# ── Helper Functions ─────────────────────────────────────────────────────────
def _fetch_symbol(fyers, symbol: str) -> dict | None:
    try:
        resp = fyers.history({
            "symbol": symbol, "resolution": "D", "date_format": "1",
            "range_from": DATE_FROM, "range_to": DATE_TO, "cont_flag": "1"
        })
        if resp.get("s") == "ok" and "candles" in resp:
            df = pd.DataFrame(resp["candles"], columns=["Time", "Open", "High", "Low", "Close", "Volume"])
            return _analyse(symbol, df)
    except Exception as e:
        return None
    return None

def _analyse(symbol: str, df: pd.DataFrame) -> dict:
    close, volume = df["Close"], df["Volume"]
    ema20, ema50, ema200 = close.ewm(span=20).mean().iloc[-1], close.ewm(span=50).mean().iloc[-1], close.ewm(span=200).mean().iloc[-1]
    rvol = volume.iloc[-1] / volume.tail(20).mean()
    trend_score = sum([close.iloc[-1] > ema20, close.iloc[-1] > ema50, close.iloc[-1] > ema200]) / 3
    roc = (close.iloc[-1] / close.iloc[-10] - 1) * 100
    
    ai_score = min(round((rvol * 15) + (trend_score * 40) + min(max(roc, 0), 10) * 2 + 20, 1), 100)
    return {
        "Symbol": symbol.replace("NSE:", "").replace("-EQ", ""),
        "Close": round(close.iloc[-1], 2),
        "AI Score": ai_score,
        "Smart Money": "🏦 Institutional" if ai_score > 70 else "⚖️ Neutral" if ai_score > 45 else "🔻 Distribution",
        "Signal": "🟢 BUY" if ai_score > 65 else "🔴 SELL" if ai_score < 40 else "🟡 HOLD"
    }

# ── Main Application ──────────────────────────────────────────────────────────
def show_scanner(fyers):
    st.set_page_config(layout="wide")
    st.title("🚀 NSE AI PRO V13 — Institutional Scanner")

    if st.button("🚀 Run Multi-threaded Scan"):
        with st.spinner("Fetching data from all symbols..."):
            # Multi-threading for speed
            with ThreadPoolExecutor() as executor:
                results = list(executor.map(lambda s: _fetch_symbol(fyers, s), SYMBOLS))
                scan_df = pd.DataFrame([r for r in results if r])
            
            st.session_state["scan_df"] = scan_df

    if "scan_df" in st.session_state:
        df = st.session_state["scan_df"]
        st.dataframe(df, use_container_width=True)
        st.bar_chart(df.set_index("Symbol")["AI Score"])

# Fyers ఆబ్జెక్ట్‌ను ఇక్కడ పాస్ చేయండి
# show_scanner(fyers)
