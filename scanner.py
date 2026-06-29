import streamlit as st
import pandas as pd
import time
from datetime import datetime, timedelta


# ── Helpers ────────────────────────────────────────────────────────────────────

SYMBOLS = [
    "NSE:RELIANCE-EQ",
    "NSE:TCS-EQ",
    "NSE:INFY-EQ",
    "NSE:HDFCBANK-EQ",
    "NSE:ICICIBANK-EQ",
    "NSE:WIPRO-EQ",
]

DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO   = datetime.today().strftime("%Y-%m-%d")


def _fetch_symbol(fyers, symbol: str) -> dict | None:
    """Fetch daily OHLCV history for one symbol. Returns None on failure."""
    try:
        resp = fyers.history({
            "symbol": symbol,
            "resolution": "D",
            "date_format": "1",
            "range_from": DATE_FROM,
            "range_to": DATE_TO,
            "cont_flag": "1",
        })
        if resp.get("s") != "ok" or not resp.get("candles"):
            return None

        df = pd.DataFrame(
            resp["candles"],
            columns=["Time", "Open", "High", "Low", "Close", "Volume"],
        )
        return df
    except Exception as exc:
        st.warning(f"⚠️ Could not fetch {symbol}: {exc}")
        return None


def _analyse(symbol: str, df: pd.DataFrame) -> dict:
    """Compute indicators and return a result row."""
    close  = df["Close"]
    volume = df["Volume"]

    ema20  = close.ewm(span=20,  adjust=False).mean().iloc[-1]
    ema50  = close.ewm(span=50,  adjust=False).mean().iloc[-1]
    ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]

    avg_vol = volume.tail(20).mean()
    rvol    = volume.iloc[-1] / avg_vol if avg_vol else 0

    # Trend score: +1 for each EMA the close is above
    trend_score = sum([
        close.iloc[-1] > ema20,
        close.iloc[-1] > ema50,
        close.iloc[-1] > ema200,
    ]) / 3  # 0 → 1

    # Momentum: 10-day rate-of-change
    roc = (close.iloc[-1] / close.iloc[-10] - 1) * 100 if len(close) >= 10 else 0

    # Composite AI score (0–100)
    ai_score = round(
        (rvol * 15) + (trend_score * 40) + min(max(roc, 0), 10) * 2 + 20,
        1,
    )
    ai_score = min(ai_score, 100)  # cap

    smart_money = (
        "🏦 Institutional Accumulation" if ai_score > 70
        else "⚖️ Neutral"               if ai_score > 45
        else "🔻 Distribution"
    )

    signal = "🟢 BUY" if ai_score > 65 else "🔴 SELL" if ai_score < 40 else "🟡 HOLD"

    return {
        "Symbol":      symbol.replace("NSE:", "").replace("-EQ", ""),
        "Close":       round(close.iloc[-1], 2),
        "EMA 20":      round(ema20, 2),
        "EMA 200":     round(ema200, 2),
        "RVOL":        round(rvol, 2),
        "ROC (10d) %": round(roc, 2),
        "AI Score":    ai_score,
        "Smart Money": smart_money,
        "Signal":      signal,
    }


def _colour_row(row):
    """Row-level background colour based on signal."""
    if "BUY"  in row["Signal"]: return ["background-color:#0d3321; color:#d4f5e0"] * len(row)
    if "SELL" in row["Signal"]: return ["background-color:#3d0d0d; color:#f5d4d4"] * len(row)
    return ["background-color:#2a2a1a; color:#f5f0d4"] * len(row)


def _colour_score(val):
    if val >= 65: return "color:#00e676; font-weight:bold"
    if val <= 40: return "color:#ff5252; font-weight:bold"
    return "color:#ffeb3b"


def _run_scan(fyers, symbols: list[str]) -> pd.DataFrame:
    results = []
    progress = st.progress(0, text="Initialising scan…")

    for i, symbol in enumerate(symbols):
        progress.progress((i + 1) / len(symbols), text=f"Scanning {symbol}…")
        df = _fetch_symbol(fyers, symbol)
        if df is not None and len(df) >= 20:
            results.append(_analyse(symbol, df))

    progress.empty()
    return pd.DataFrame(results) if results else pd.DataFrame()


# ── Main entry point ───────────────────────────────────────────────────────────

def show_scanner(fyers):
    st.title("🚀 NSE AI PRO V13 — Institutional Scanner")

    # ── Sidebar ────────────────────────────────────────────────────────────────
    with st.sidebar:
        st.header("⚙️ Scanner Filters")

        scanner_type = st.selectbox(
            "Scanner Type",
            ["AI Institutional", "Intraday", "Smart Money"],
        )

        custom_symbols = st.multiselect(
            "Symbols to scan",
            options=SYMBOLS,
            default=SYMBOLS[:4],
            format_func=lambda s: s.replace("NSE:", "").replace("-EQ", ""),
        )

        st.divider()
        auto_refresh  = st.checkbox("🔄 Auto Refresh")
        refresh_rate  = st.slider("Refresh every (seconds)", 5, 120, 30,
                                  disabled=not auto_refresh)

        st.divider()
        st.caption(f"Mode: **{scanner_type}**")
        st.caption(f"Data window: {DATE_FROM} → {DATE_TO}")

    # ── Top metric strip (filled after scan) ───────────────────────────────────
    metric_cols = st.columns(4)
    metric_placeholders = [c.empty() for c in metric_cols]

    # ── Scan button ────────────────────────────────────────────────────────────
    run = st.button("🚀 Run AI Scanner", use_container_width=True, type="primary")

    scan_df: pd.DataFrame = st.session_state.get("scan_df", pd.DataFrame())

    if run:
        with st.spinner("Running scan — fetching market data…"):
            scan_df = _run_scan(fyers, custom_symbols or SYMBOLS[:4])
        st.session_state["scan_df"] = scan_df
        st.session_state["scan_time"] = datetime.now().strftime("%H:%M:%S")

    # ── Results ────────────────────────────────────────────────────────────────
    if not scan_df.empty:
        # Metric cards
        buy_count  = (scan_df["Signal"].str.contains("BUY")).sum()
        sell_count = (scan_df["Signal"].str.contains("SELL")).sum()
        avg_score  = scan_df["AI Score"].mean()
        top_symbol = scan_df.loc[scan_df["AI Score"].idxmax(), "Symbol"]

        metric_placeholders[0].metric("📊 Symbols Scanned", len(scan_df))
        metric_placeholders[1].metric("🟢 BUY Signals",    buy_count)
        metric_placeholders[2].metric("🔴 SELL Signals",   sell_count)
        metric_placeholders[3].metric("⭐ Top Pick",        top_symbol,
                                       delta=f"Score {avg_score:.1f}")

        scan_time = st.session_state.get("scan_time", "")
        st.caption(f"Last scan: {scan_time}  |  Mode: {scanner_type}")

        # ── Scanner table ──────────────────────────────────────────────────────
        st.subheader("🤖 AI Scanner Results")
        styled = (
            scan_df.style
            .apply(_colour_row, axis=1)
            .applymap(_colour_score, subset=["AI Score"])
            .format({
                "Close": "₹{:.2f}",
                "EMA 20": "₹{:.2f}",
                "EMA 200": "₹{:.2f}",
                "RVOL": "{:.2f}x",
                "ROC (10d) %": "{:+.2f}%",
                "AI Score": "{:.1f}",
            })
        )
        st.dataframe(styled, use_container_width=True, height=280)

        # ── Smart Money breakdown ──────────────────────────────────────────────
        st.subheader("💰 Smart Money Analysis")
        smart_cols = ["Symbol", "AI Score", "RVOL", "ROC (10d) %", "Smart Money", "Signal"]
        smart_styled = (
            scan_df[smart_cols].style
            .apply(_colour_row, axis=1)
            .applymap(_colour_score, subset=["AI Score"])
            .format({"AI Score": "{:.1f}", "RVOL": "{:.2f}x", "ROC (10d) %": "{:+.2f}%"})
        )
        st.dataframe(smart_styled, use_container_width=True, height=280)

        # ── AI Score bar chart ─────────────────────────────────────────────────
        st.subheader("📈 AI Score Comparison")
        chart_df = scan_df[["Symbol", "AI Score"]].set_index("Symbol")
        st.bar_chart(chart_df)

        st.success("✅ Analysis complete.")

    elif run:
        st.error("❌ No data returned. Check API credentials or symbol list.")

    # ── Auto-refresh countdown ─────────────────────────────────────────────────
    if auto_refresh:
        countdown = st.empty()
        for remaining in range(refresh_rate, 0, -1):
            countdown.info(f"🔄 Auto-refreshing in {remaining}s…")
            time.sleep(1)
        countdown.empty()
        st.rerun()
