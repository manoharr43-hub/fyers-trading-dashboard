"""
volume_scanner.py — 15-Minute Top / Bottom Volume Scanner (ADD-ON MODULE)
===========================================================================
Purely additive companion to option_chain.py. Nothing in option_chain.py
is modified by importing this file — it reuses option_chain.py's own
`fetch_underlying_candles()`, `INDEX_SYMBOL_CANDIDATES`, and
`get_stock_symbol_candidates()` instead of redefining them, so there is
only one source of truth for symbol handling and candle fetching.

WHAT IT DOES
------------
Scans a configurable watchlist of indices + F&O stocks on the 15-minute
timeframe and, for each symbol, compares the latest completed 15m bar's
volume against that same symbol's own trailing 20-bar average volume
("Relative Volume"). The whole watchlist is then ranked so you can see,
at a glance, which symbols currently have unusually HIGH ("Top") or
unusually LOW ("Bottom") 15-minute volume — a lightweight cross-market
volume scanner rather than a single-symbol chart.

HOW TO WIRE THIS INTO option_chain.py (3 small, additive edits — nothing
existing is removed, renamed, or reordered):

1) Near your other imports at the top of option_chain.py, add:

       from volume_scanner import render_volume_scanner_tab

2) In show_option_chain(), change the tabs line from:

       tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(
           ["📋 Chain Table", "📊 OI Analysis", "📈 IV Skew", "🔥 Big Move Ready",
            "🤖 AI Trade Signals", "⚡ Gamma Build-up", "🎯 AI Scalping Engine"]
       )

   to (adds an 8th tab, everything else identical):

       tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs(
           ["📋 Chain Table", "📊 OI Analysis", "📈 IV Skew", "🔥 Big Move Ready",
            "🤖 AI Trade Signals", "⚡ Gamma Build-up", "🎯 AI Scalping Engine",
            "📡 15m Volume Scanner"]
       )

3) After the existing `with tab7:` block (the AI Scalping Engine tab),
   add:

       with tab8:
           render_volume_scanner_tab(fyers)

That's it — every existing function, tab, column, and computation in
option_chain.py is untouched.
"""

from __future__ import annotations

import logging

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from option_chain import (
    INDEX_SYMBOL_CANDIDATES,
    get_stock_symbol_candidates,
    fetch_underlying_candles,
)

logger = logging.getLogger("options_chain_dashboard")

# ══════════════════════════════════════════════════════════════════════════
# 1. DEFAULT WATCHLIST  (indices from option_chain.py's own list + a
#    starter set of liquid F&O stocks). Fully editable in the sidebar —
#    this is only the default selection, not a hard limit.
# ══════════════════════════════════════════════════════════════════════════

DEFAULT_INDEX_WATCHLIST = list(INDEX_SYMBOL_CANDIDATES.keys())

DEFAULT_STOCK_WATCHLIST = [
    "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK",
    "SBIN", "AXISBANK", "KOTAKBANK", "LT", "ITC",
    "BHARTIARTL", "TATAMOTORS", "TATASTEEL", "MARUTI", "ADANIENT",
]

VOLUME_SCAN_LOOKBACK_BARS = 20  # trailing bars used for the "average volume" baseline


def _symbol_candidates_for(label: str, is_index: bool) -> list:
    if is_index:
        return INDEX_SYMBOL_CANDIDATES.get(label, [])
    return get_stock_symbol_candidates(label)


# ══════════════════════════════════════════════════════════════════════════
# 2. SCAN ENGINE
# ══════════════════════════════════════════════════════════════════════════

def _relative_volume_label(rel_vol: float) -> tuple:
    """Returns (label, css_key). Thresholds are intentionally simple and
    symmetric around 1.0x (last bar volume == its own 20-bar average)."""
    if rel_vol >= 2.5:
        return "🔥 Extreme Top", "strongbuy"
    if rel_vol >= 1.5:
        return "🟢 Top", "buy"
    if rel_vol <= 0.3:
        return "⚫ Extreme Bottom", "strongsell"
    if rel_vol <= 0.6:
        return "🔴 Bottom", "sell"
    return "🟡 Normal", "hold"


def scan_symbol_15m_volume(fyers, label: str, is_index: bool) -> dict:
    """Fetches 15m candles for one symbol and computes its latest-bar
    Relative Volume vs its own trailing average. Never raises — a fetch
    failure for one symbol just produces a row with available=False so
    one bad symbol can't take down the whole scan."""
    candidates = _symbol_candidates_for(label, is_index)
    if not candidates:
        return {"Symbol": label, "available": False, "reason": "No symbol mapping found"}

    try:
        c15 = fetch_underlying_candles(fyers, candidates, resolution="15", lookback_days=5)
    except Exception as e:  # noqa: BLE001 - one symbol's failure must never break the scan
        logger.error("15m Volume Scanner: candle fetch raised for %s: %s", label, e)
        return {"Symbol": label, "available": False, "reason": str(e)}

    if c15 is None or c15.empty or len(c15) < 3:
        return {"Symbol": label, "available": False, "reason": "No/insufficient 15m candle data"}

    window = c15.tail(VOLUME_SCAN_LOOKBACK_BARS + 1)
    if len(window) < 3:
        return {"Symbol": label, "available": False, "reason": "Not enough bars for a baseline"}

    last_bar = window.iloc[-1]
    baseline = window.iloc[:-1]["volume"]
    avg_vol = float(baseline.mean()) if len(baseline) and baseline.mean() > 0 else 0.0
    last_vol = float(last_bar["volume"])
    rel_vol = (last_vol / avg_vol) if avg_vol > 0 else 0.0

    prev_close = float(window.iloc[-2]["close"]) if len(window) >= 2 else float(last_bar["open"])
    change_pct = ((float(last_bar["close"]) - prev_close) / prev_close * 100) if prev_close else 0.0

    label_txt, css_key = _relative_volume_label(rel_vol)

    return {
        "Symbol": label,
        "Type": "Index" if is_index else "Stock",
        "available": True,
        "LTP": round(float(last_bar["close"]), 2),
        "Change %": round(change_pct, 2),
        "Last 15m Volume": int(last_vol),
        "Avg 15m Volume (20-bar)": int(avg_vol),
        "Relative Volume": round(rel_vol, 2),
        "Volume Signal": label_txt,
        "Signal Key": css_key,
        "Candle Time": last_bar["time"],
    }


def run_volume_scan(fyers, index_watchlist: list, stock_watchlist: list) -> pd.DataFrame:
    rows = []
    for label in index_watchlist:
        rows.append(scan_symbol_15m_volume(fyers, label, is_index=True))
    for label in stock_watchlist:
        rows.append(scan_symbol_15m_volume(fyers, label, is_index=False))

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    ok = df[df["available"] == True].copy()  # noqa: E712
    failed = df[df["available"] == False].copy()  # noqa: E712

    if not ok.empty:
        ok.sort_values("Relative Volume", ascending=False, inplace=True)
        ok.reset_index(drop=True, inplace=True)

    st.session_state["oc_volscan_failed"] = failed[["Symbol", "reason"]].to_dict("records") if not failed.empty else []
    return ok


# ══════════════════════════════════════════════════════════════════════════
# 3. RENDERING
# ══════════════════════════════════════════════════════════════════════════

SIGNAL_ROW_CSS = {
    "strongbuy": "gamma-row-strongbuy",
    "buy": "gamma-row-buy",
    "hold": "gamma-row-hold",
    "sell": "gamma-row-sell",
    "strongsell": "gamma-row-strongsell",
}


def render_volume_scan_html_table(df: pd.DataFrame) -> str:
    cols = [
        ("Symbol", "Symbol"), ("Type", "Type"), ("LTP", "LTP"),
        ("Change %", "Change %"), ("Last 15m Volume", "Last 15m Vol"),
        ("Avg 15m Volume (20-bar)", "Avg 15m Vol"), ("Relative Volume", "Rel. Vol (x)"),
        ("Volume Signal", "Signal"),
    ]
    header_html = "".join(f"<th>{label}</th>" for _, label in cols)
    rows_html = []
    for _, row in df.iterrows():
        css = SIGNAL_ROW_CSS.get(row.get("Signal Key", "hold"), "gamma-row-hold")
        cells = []
        for key, _ in cols:
            val = row.get(key, "")
            if key == "LTP":
                val = f"{val:,.2f}"
            elif key == "Change %":
                val = f"{val:+.2f}%"
            elif key in ("Last 15m Volume", "Avg 15m Volume (20-bar)"):
                val = f"{val:,.0f}"
            elif key == "Relative Volume":
                val = f"{val:.2f}x"
            cells.append(f"<td>{val}</td>")
        rows_html.append(f'<tr class="{css}">{"".join(cells)}</tr>')
    return f"""
    <div style="max-height:560px; overflow-y:auto; border:1px solid #30363d; border-radius:8px;">
    <table class="gamma-table">
        <thead><tr>{header_html}</tr></thead>
        <tbody>{''.join(rows_html)}</tbody>
    </table>
    </div>
    """


def render_volume_scanner_tab(fyers):
    st.markdown("##### 📡 15-Minute Top / Bottom Volume Scanner")
    st.caption(
        "Compares each symbol's latest completed 15-minute bar volume against its own trailing "
        f"{VOLUME_SCAN_LOOKBACK_BARS}-bar average ('Relative Volume'). ≥1.5x = unusually high "
        "volume (🟢 Top / 🔥 Extreme Top), ≤0.6x = unusually low volume (🔴 Bottom / ⚫ Extreme "
        "Bottom). This is a same-symbol-vs-itself comparison, not a cross-symbol size comparison — "
        "a small-float stock and NIFTY are each compared only to their own history."
    )

    with st.expander("⚙️ Watchlist", expanded=False):
        idx_wl = st.multiselect(
            "Indices", DEFAULT_INDEX_WATCHLIST, default=DEFAULT_INDEX_WATCHLIST, key="volscan_idx_wl"
        )
        stock_wl_text = st.text_area(
            "F&O Stocks (comma-separated)",
            value=", ".join(DEFAULT_STOCK_WATCHLIST),
            key="volscan_stock_wl_text",
            help="Enter NSE trading symbols, e.g. RELIANCE, TCS, INFY",
        )
        stock_wl = [s.strip().upper() for s in stock_wl_text.split(",") if s.strip()]

    scan_btn = st.button("🔄 Run 15m Volume Scan", use_container_width=True, key="volscan_run_btn")

    if scan_btn:
        with st.spinner(f"Scanning {len(idx_wl) + len(stock_wl)} symbols on the 15m timeframe …"):
            scan_df = run_volume_scan(fyers, idx_wl, stock_wl)
        st.session_state["oc_volscan_df"] = scan_df

    scan_df = st.session_state.get("oc_volscan_df")
    if scan_df is None:
        st.info("👆 Choose your watchlist above and click **Run 15m Volume Scan**.")
        return
    if scan_df.empty:
        st.warning("No symbols returned usable 15m candle data.")
    else:
        top_n = 10
        top_df = scan_df.head(top_n)
        bottom_df = scan_df.tail(top_n).sort_values("Relative Volume")

        c1, c2, c3 = st.columns(3)
        c1.metric("Symbols Scanned", len(scan_df))
        c2.metric("Highest Rel. Volume", f"{scan_df['Relative Volume'].max():.2f}x" if len(scan_df) else "—")
        c3.metric("Lowest Rel. Volume", f"{scan_df['Relative Volume'].min():.2f}x" if len(scan_df) else "—")

        st.markdown("<br>", unsafe_allow_html=True)
        top_col, bottom_col = st.columns(2)
        with top_col:
            st.markdown("**🟢 Top Volume (highest Relative Volume)**")
            st.markdown(render_volume_scan_html_table(top_df), unsafe_allow_html=True)
        with bottom_col:
            st.markdown("**🔴 Bottom Volume (lowest Relative Volume)**")
            st.markdown(render_volume_scan_html_table(bottom_df), unsafe_allow_html=True)

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("**Full Watchlist Scan**")
        st.markdown(render_volume_scan_html_table(scan_df), unsafe_allow_html=True)

    failed = st.session_state.get("oc_volscan_failed") or []
    if failed:
        with st.expander(f"⚠️ {len(failed)} symbol(s) skipped (no usable data)", expanded=False):
            for f in failed:
                st.caption(f"- {f['Symbol']}: {f['reason']}")

    st.caption(
        "Uses the same fetch_underlying_candles() FYERS history call as the AI Scalping Engine tab, "
        "so it needs a valid FYERS session with history access. This is a volume-activity screen, not "
        "a trade signal — always confirm with price action and your own risk management before acting."
    )
