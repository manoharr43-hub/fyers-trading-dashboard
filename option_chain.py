"""
FYERS Options Chain Dashboard — Pro Edition (AI Upgrade)
=========================================================
Streamlit + FYERS API v3 dashboard with:
  • Fixed multi-index expiry handling (NIFTY / BANKNIFTY / FINNIFTY /
    MIDCPNIFTY / NIFTYNEXT50 / SENSEX / BANKEX) — always resolves the
    nearest available expiry, tags Weekly vs Monthly, auto-refreshes on
    instrument change and after every fetch, and auto-falls-back to the
    next valid expiry if the selected one returns no data. The dashboard
    makes NO assumption about which indices are weekly vs monthly-only —
    every expiry the FYERS API actually returns is shown, unfiltered, and
    the raw API expiry payload is surfaced in the UI for inspection.
  • AI Engine: independent 0-100 CE Score / PE Score per strike, star
    ratings (Strong Buy / Buy / Hold / Avoid / Ignore), AI Confidence,
    and a Big Move table (Strike, CE/PE Score, Overall Score, BUY/SELL
    Probability, Breakout/Breakdown %, Institution/Smart-Money Score,
    Final Signal).
  • AI Trade Signal engine: high-confidence-only Strike/CE/PE cards with
    Entry / SL / T1 / T2 / T3 / Risk-Reward / Reason.
  • Dashboard Summary: Top CE/PE Buy, Best Breakout/Breakdown, highest
    institutional/smart-money/OI/volume/ΔOI, best RR trade, best trade.
  • Excel export (openpyxl) with full conditional-formatting, colored
    headers, auto-width, borders, freeze panes, across Summary / Chain
    Table / Big Move Ready / AI Trade Signals sheets.
  • All original features preserved: PCR, Max Pain, IV chart, OI chart,
    Chain table, Big Move Alerts, Support/Resistance, Strike Signal.
  • Gamma Build-up Analyzer: real-time (session-tracked) per-strike
    Gamma monitoring with Gamma Change / Change % / Trend / Signal /
    Strength / Trade Action / AI Rating, blinking Buy/Sell rows, smart
    alerts, optional audio ping, and a live summary panel.
"""

import io
import math
import time
from collections import defaultdict
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ─── Page Config ────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Options Chain Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Custom CSS ─────────────────────────────────────────────────────────────
st.markdown("""
<style>
.stApp { background-color: #0d1117; }
section[data-testid="stSidebar"] { background-color: #161b22; border-right: 1px solid #30363d; }

div[data-testid="metric-container"] {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 16px 20px;
}
div[data-testid="metric-container"] label { color: #8b949e !important; font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; }
div[data-testid="metric-container"] div[data-testid="stMetricValue"] { color: #e6edf3 !important; font-size: 22px; font-weight: 700; font-family: 'Courier New', monospace; }

h1, h2, h3 { color: #e6edf3 !important; }
.block-title { color: #58a6ff; font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 8px; }

.ce-badge { background: #0d3b2e; color: #3fb950; border: 1px solid #238636; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; }
.pe-badge { background: #3b0d1a; color: #f85149; border: 1px solid #da3633; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; }

button[data-baseweb="tab"] { color: #8b949e !important; }
button[data-baseweb="tab"][aria-selected="true"] { color: #58a6ff !important; border-bottom: 2px solid #58a6ff; }

.stDataFrame { border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }
hr { border-color: #30363d; }

.signal-bull { background: #0d3b2e; color: #3fb950; border: 1px solid #238636; padding: 4px 14px; border-radius: 20px; font-size: 13px; font-weight: 700; display: inline-block; }
.signal-bear { background: #3b0d1a; color: #f85149; border: 1px solid #da3633; padding: 4px 14px; border-radius: 20px; font-size: 13px; font-weight: 700; display: inline-block; }
.signal-neu  { background: #1c2128; color: #d29922; border: 1px solid #9e6a03; padding: 4px 14px; border-radius: 20px; font-size: 13px; font-weight: 700; display: inline-block; }

.badge-explosive { background: #0d3b2e; color: #3fb950; border: 1px solid #238636; padding: 3px 10px; border-radius: 6px; font-size: 12px; font-weight: 700; }
.badge-strong    { background: #123524; color: #7ee787; border: 1px solid #238636; padding: 3px 10px; border-radius: 6px; font-size: 12px; font-weight: 700; }
.badge-watch     { background: #1c2128; color: #d29922; border: 1px solid #9e6a03; padding: 3px 10px; border-radius: 6px; font-size: 12px; font-weight: 700; }
.badge-ignore    { background: #161b22; color: #8b949e; border: 1px solid #30363d; padding: 3px 10px; border-radius: 6px; font-size: 12px; font-weight: 700; }

.intel-card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px 16px; margin-bottom: 8px; }
.intel-label { color: #8b949e; font-size: 11px; text-transform: uppercase; letter-spacing: .08em; }
.intel-value { color: #e6edf3; font-size: 20px; font-weight: 700; font-family: 'Courier New', monospace; }

.rating-strongbuy { background:#0d3b2e; color:#3fb950; border:1px solid #238636; padding:3px 10px; border-radius:6px; font-size:12px; font-weight:700; }
.rating-buy        { background:#123524; color:#7ee787; border:1px solid #238636; padding:3px 10px; border-radius:6px; font-size:12px; font-weight:700; }
.rating-hold       { background:#3a2f05; color:#d29922; border:1px solid #9e6a03; padding:3px 10px; border-radius:6px; font-size:12px; font-weight:700; }
.rating-avoid      { background:#3a2405; color:#e8823a; border:1px solid #b5650a; padding:3px 10px; border-radius:6px; font-size:12px; font-weight:700; }
.rating-ignore     { background:#3b0d1a; color:#f85149; border:1px solid #da3633; padding:3px 10px; border-radius:6px; font-size:12px; font-weight:700; }

/* ── Gamma Build-up Analyzer: blink animations ────────────────────────── */
@keyframes gammaBlinkGreen {
    0%   { background-color: #0d3b2e; }
    50%  { background-color: #1d5c3f; }
    100% { background-color: #0d3b2e; }
}
@keyframes gammaBlinkRed {
    0%   { background-color: #3b0d1a; }
    50%  { background-color: #5c1d2c; }
    100% { background-color: #3b0d1a; }
}
.gamma-table { width: 100%; border-collapse: collapse; font-family: 'Courier New', monospace; font-size: 12.5px; }
.gamma-table th {
    background: #1F4E78; color: #ffffff; padding: 8px 10px; text-align: center;
    position: sticky; top: 0; font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
}
.gamma-table td { padding: 7px 10px; text-align: center; border-bottom: 1px solid #21262d; color: #e6edf3; }
.gamma-row-strongbuy { background-color: #0d3b2e; }
.gamma-row-buy       { background-color: #123524; }
.gamma-row-hold      { background-color: #1c2128; }
.gamma-row-sell      { background-color: #2b1a05; }
.gamma-row-strongsell{ background-color: #3b0d1a; }
.gamma-row-blink-green { animation: gammaBlinkGreen 1.1s infinite; }
.gamma-row-blink-red   { animation: gammaBlinkRed 1.1s infinite; }
.gamma-live-badge {
    background: #238636; color: #fff; padding: 3px 10px; border-radius: 12px;
    font-size: 11px; font-weight: 700; letter-spacing: .05em; animation: gammaBlinkGreen 1.4s infinite;
}

/* ── AI Scalping Engine ───────────────────────────────────────────────── */
.scalp-direction-buy {
    background: linear-gradient(135deg, #0d3b2e, #123524); color: #3fb950;
    border: 1px solid #238636; padding: 14px 20px; border-radius: 10px;
    font-size: 26px; font-weight: 800; text-align: center; letter-spacing: .05em;
}
.scalp-direction-sell {
    background: linear-gradient(135deg, #3b0d1a, #2b0d14); color: #f85149;
    border: 1px solid #da3633; padding: 14px 20px; border-radius: 10px;
    font-size: 26px; font-weight: 800; text-align: center; letter-spacing: .05em;
}
.scalp-direction-wait {
    background: #1c2128; color: #d29922; border: 1px solid #9e6a03;
    padding: 14px 20px; border-radius: 10px; font-size: 22px; font-weight: 800;
    text-align: center; letter-spacing: .04em;
}
.scalp-reason-yes { color: #3fb950; font-family: 'Courier New', monospace; font-size: 13px; }
.scalp-reason-no  { color: #6e7681; font-family: 'Courier New', monospace; font-size: 13px; }
.scalp-warning {
    background: #3a2405; color: #e8823a; border: 1px solid #b5650a;
    padding: 8px 14px; border-radius: 6px; font-size: 13px; margin-bottom: 6px; display: block;
}
.scalp-condition-met { color:#3fb950; }
.scalp-condition-unmet { color:#f85149; }
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════
# 1. SYMBOL HANDLING  (fixes NIFTYNEXT50 / SENSEX / BANKEX / F&O stocks)
# ══════════════════════════════════════════════════════════════════════════

# Each index maps to an ORDERED list of symbol formats to try. FYERS has
# changed / disagreed on exact index symbol spellings across API/SDK
# versions, so every plausible variant is tried until one returns data.
INDEX_SYMBOL_CANDIDATES = {
    "NIFTY":       ["NSE:NIFTY50-INDEX"],
    "BANKNIFTY":   ["NSE:NIFTYBANK-INDEX", "NSE:BANKNIFTY-INDEX"],
    "FINNIFTY":    ["NSE:FINNIFTY-INDEX"],
    "MIDCPNIFTY":  ["NSE:MIDCPNIFTY-INDEX", "NSE:MIDCAPNIFTY-INDEX"],
    "NIFTYNEXT50": ["NSE:NIFTYNEXT50-INDEX", "NSE:NIFTYNXT50-INDEX", "NSE:NIFTY_NEXT_50-INDEX"],
    "SENSEX":      ["BSE:SENSEX-INDEX", "BSE:SENSEX-INDEX50"],
    "BANKEX":      ["BSE:BANKEX-INDEX"],
}

# NOTE: We intentionally make NO assumption here about which indices are
# "weekly" vs "monthly-only" (exchange rules around this have changed more
# than once and vary by index). Whatever expiries the FYERS API actually
# returns for the selected instrument are shown, in full, unfiltered — see
# section 2 below. The sidebar surfaces the raw API payload so this can be
# verified directly rather than trusted to a hard-coded list.


def get_stock_symbol_candidates(stock: str) -> list:
    """Returns ordered symbol variants to try for an F&O stock (RELIANCE,
    TCS, INFY, SBIN, ICICIBANK, HDFCBANK, and every other NSE F&O name)."""
    base = stock.strip().upper()
    if base.endswith("-EQ"):
        base = base[:-3]
    if ":" in base:
        base = base.split(":")[-1]
    return [f"NSE:{base}-EQ", f"NSE:{base}"]


def normalize_symbol(stock: str, with_eq: bool = False) -> str:
    """Kept for backward compatibility with earlier versions of this file."""
    stock = stock.strip().upper()
    if stock.endswith("-EQ"):
        stock = stock[:-3]
    if ":" in stock:
        stock = stock.split(":")[-1]
    return f"NSE:{stock}-EQ" if with_eq else f"NSE:{stock}"


def fetch_optionchain_with_fallback(fyers, symbol_candidates: list, strikecount: int,
                                     expiry_timestamp: str = ""):
    """
    Tries every candidate symbol format until FYERS returns status 'ok'.
    Returns (response, symbol_used, attempts_log).
    """
    attempts = []
    last_response = None
    for sym in symbol_candidates:
        req = {"symbol": sym, "strikecount": int(strikecount)}
        if expiry_timestamp and str(expiry_timestamp).strip():
            req["timestamp"] = str(expiry_timestamp).strip()
        try:
            resp = fyers.optionchain(data=req)
        except Exception as e:  # noqa: BLE001 - external SDK, keep resilient
            attempts.append((sym, f"exception: {e}"))
            continue
        status = resp.get("s") if isinstance(resp, dict) else "no response"
        attempts.append((sym, status))
        last_response = resp
        if isinstance(resp, dict) and resp.get("s") == "ok":
            chain, _ = extract_options_data(resp)
            if chain:  # only accept if it actually carries strikes
                return resp, sym, attempts
    return last_response, symbol_candidates[-1] if symbol_candidates else "", attempts


# ══════════════════════════════════════════════════════════════════════════
# 2. EXPIRY HANDLING  (weekly + monthly, auto refresh, nearest-first,
#    auto fallback to next valid expiry, RAW-API inspection — no hard-coded
#    assumptions about which indices are weekly vs monthly-only)
# ══════════════════════════════════════════════════════════════════════════

def format_expiry_label(ts) -> str:
    try:
        return datetime.fromtimestamp(int(float(ts))).strftime("%d-%b-%Y")
    except (TypeError, ValueError, OSError):
        return str(ts)


def _to_int_ts(ts) -> int:
    """Robust timestamp→int coercion. The original dashboard sorted expiries
    with `int(x) if x.isdigit() else 0`, which silently broke ordering (and
    therefore 'nearest expiry' selection) whenever FYERS returned a
    timestamp with a decimal point or any non-digit character. This handles
    ints, floats, and numeric strings of either form."""
    try:
        return int(float(ts))
    except (TypeError, ValueError):
        return 0


def extract_raw_expiry_payload(response: dict) -> list:
    """
    Returns the RAW expiryData list exactly as received from the FYERS API
    — no filtering, no dedup, no reformatting. This exists purely so the
    UI can display precisely what the API sent back, for inspection.
    """
    data = response.get("data", {}) if isinstance(response, dict) else {}
    raw = data.get("expiryData") or data.get("expirydata") or []
    return raw if isinstance(raw, list) else []


def extract_expiry_list(response: dict) -> list:
    """
    Returns a list of (label, timestamp) tuples, sorted chronologically
    (nearest expiry first), with labels normalised to DD-MMM-YYYY for
    display. This does NOT drop any expiry the API returned — every
    unique timestamp present in the raw payload is kept; only exact
    duplicate (label, ts) pairs are collapsed, and sorting is applied
    purely for chronological ordering in the dropdown.
    """
    raw = extract_raw_expiry_payload(response)
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ts = item.get("expiry") or item.get("timestamp")
        if ts is None:
            continue
        out.append((format_expiry_label(ts), str(ts)))
    # de-duplicate exact repeats while preserving every distinct expiry,
    # then sort by the numeric timestamp (nearest first)
    seen = set()
    deduped = []
    for label, ts in out:
        if ts not in seen:
            seen.add(ts)
            deduped.append((label, ts))
    deduped.sort(key=lambda x: _to_int_ts(x[1]))
    return deduped


def classify_expiries(expiry_list: list) -> list:
    """
    Tags each (label, ts) as ('Weekly' | 'Monthly') purely for display
    convenience in the dropdown. This is a descriptive label derived from
    the data itself (last expiry in a calendar month = Monthly, everything
    else in that month = Weekly) — it is NOT used to filter, hide, or
    exclude any expiry the API returned. Every expiry in expiry_list is
    still present in the output, in the same order.
    """
    if not expiry_list:
        return []
    by_month = defaultdict(list)
    for label, ts in expiry_list:
        dt = datetime.fromtimestamp(_to_int_ts(ts)) if _to_int_ts(ts) else None
        key = (dt.year, dt.month) if dt else ("unknown", ts)
        by_month[key].append((label, ts, dt))
    monthly_ts = set()
    for _, items in by_month.items():
        items_sorted = sorted(items, key=lambda x: _to_int_ts(x[1]))
        monthly_ts.add(items_sorted[-1][1])
    return [(label, ts, "Monthly" if ts in monthly_ts else "Weekly") for label, ts in expiry_list]


def fetch_expiry_list(fyers, symbol_candidates: list) -> tuple:
    """
    Fetches ONLY the expiry list (cheap call, strikecount=2, no timestamp)
    so the sidebar dropdown can be populated automatically the moment an
    instrument is chosen — the user never has to hand-edit a timestamp.
    Returns (expiry_list, symbol_used, raw_expiry_payload) or
    ([], "", []) on failure. raw_expiry_payload is the untouched
    expiryData array from the API response, for on-screen inspection.
    """
    response, used_symbol, _ = fetch_optionchain_with_fallback(
        fyers, symbol_candidates, strikecount=2, expiry_timestamp=""
    )
    if not response or response.get("s") != "ok":
        return [], "", []
    return extract_expiry_list(response), used_symbol, extract_raw_expiry_payload(response)


# ══════════════════════════════════════════════════════════════════════════
# 3. RESPONSE PARSING  (spot price, options payload, wide/long shape fix)
# ══════════════════════════════════════════════════════════════════════════

def extract_options_data(response: dict):
    """FYERS has returned the strikes array under different keys across
    SDK/API versions — try the known variants instead of assuming one."""
    data = response.get("data", {}) if isinstance(response, dict) else {}
    if isinstance(data, dict):
        for key in ("options", "optionsChain", "optionschain", "data"):
            candidate = data.get(key)
            if isinstance(candidate, list) and len(candidate) > 0:
                return candidate, data
    if isinstance(data, list) and len(data) > 0:
        return data, {}
    return [], data


def extract_spot_price(response: dict, data) -> float:
    """Spot/underlying price has shown up under different keys across
    response shapes — try every known variant before giving up."""
    candidates = []
    if isinstance(data, dict):
        candidates.extend([
            data.get("ltp"), data.get("spot_price"), data.get("spotPrice"),
            data.get("underlyingValue"), data.get("underlying_value"),
            data.get("underlyingLtp"), data.get("underlying_ltp"),
        ])
    if isinstance(response, dict):
        candidates.extend([
            response.get("ltp"), response.get("spot_price"), response.get("spotPrice"),
            response.get("underlyingValue"), response.get("underlying_value"),
        ])
    for val in candidates:
        try:
            f = float(val)
            if f > 0:
                return f
        except (TypeError, ValueError):
            continue
    return 0.0


def normalize_chain_shape(options_data: list) -> pd.DataFrame:
    """
    FYERS' optionchain response can come in two shapes:
      1. WIDE  — one row per strike, columns already prefixed ce_/pe_.
      2. LONG  — one row per contract (separate CE/PE rows) with a shared
         'option_type' field. Newer v3 responses commonly use this shape,
         and it is the usual cause of every column silently showing 0.
    This always returns a WIDE dataframe (strike_price, ce_*, pe_*).
    """
    raw = pd.DataFrame(options_data)
    if raw.empty:
        return raw

    if any(c.startswith("ce_") or c.startswith("pe_") for c in raw.columns):
        return raw

    type_col = next(
        (c for c in ("option_type", "optionType", "type", "instrument_type") if c in raw.columns),
        None,
    )
    if type_col is None:
        return raw

    raw[type_col] = raw[type_col].astype(str).str.upper()
    field_map = {
        "oi": "oi", "open_interest": "oi",
        "ltp": "ltp", "last_price": "ltp",
        "volume": "volume", "vol": "volume",
        "chng_oi": "chng_oi", "change_oi": "chng_oi", "oi_change": "chng_oi",
        "iv": "iv", "implied_volatility": "iv",
    }
    raw_renamed = raw.rename(columns={k: v for k, v in field_map.items() if k in raw.columns})
    value_cols = [c for c in ("oi", "ltp", "volume", "chng_oi", "iv") if c in raw_renamed.columns]

    ce_df = raw_renamed[raw_renamed[type_col] == "CE"][["strike_price"] + value_cols].copy()
    pe_df = raw_renamed[raw_renamed[type_col] == "PE"][["strike_price"] + value_cols].copy()
    ce_df.rename(columns={c: f"ce_{c}" for c in value_cols}, inplace=True)
    pe_df.rename(columns={c: f"pe_{c}" for c in value_cols}, inplace=True)

    return pd.merge(ce_df, pe_df, on="strike_price", how="outer")


def ensure_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Guarantees every column the dashboard reads downstream exists and is
    numeric, so nothing ever raises KeyError/IndexError on a thin payload."""
    num_cols = ["strike_price", "ce_ltp", "ce_oi", "ce_volume", "ce_chng_oi",
                "pe_ltp", "pe_oi", "pe_volume", "pe_chng_oi", "ce_iv", "pe_iv"]
    df = df.copy()
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        else:
            df[col] = 0
    return df


# ══════════════════════════════════════════════════════════════════════════
# 4. CORE ANALYTICS  (Max Pain, PCR signal, strike bias, IV via Black-Scholes)
# ══════════════════════════════════════════════════════════════════════════

def calculate_max_pain(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    strikes = df["strike_price"].values
    ce_oi = df["ce_oi"].values
    pe_oi = df["pe_oi"].values
    pain = [
        np.sum(np.maximum(s - strikes, 0) * ce_oi) + np.sum(np.maximum(strikes - s, 0) * pe_oi)
        for s in strikes
    ]
    return float(strikes[int(np.argmin(pain))]) if pain else 0.0


def pcr_signal(pcr: float) -> str:
    if pcr > 1.3:
        return '<span class="signal-bull">🟢 Bullish (High PCR)</span>'
    elif pcr < 0.7:
        return '<span class="signal-bear">🔴 Bearish (Low PCR)</span>'
    return '<span class="signal-neu">🟡 Neutral</span>'


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(spot, strike, t, r, sigma, is_call: bool) -> float:
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return max(0.0, (spot - strike) if is_call else (strike - spot))
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if is_call:
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)
    return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def _bs_greeks(spot, strike, t, r, sigma, is_call: bool) -> dict:
    """Standard Black-Scholes Delta/Gamma/Theta/Vega — used by the AI
    engine's option-Greeks factors (section 5B) and the scalping engine
    (section 5D). Theta is expressed per-calendar-day, Vega per 1% IV."""
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    pdf_d1 = math.exp(-0.5 * d1 ** 2) / math.sqrt(2 * math.pi)
    gamma = pdf_d1 / (spot * sigma * math.sqrt(t))
    vega = spot * pdf_d1 * math.sqrt(t) / 100.0
    if is_call:
        delta = _norm_cdf(d1)
        theta = (-(spot * pdf_d1 * sigma) / (2 * math.sqrt(t))
                 - r * strike * math.exp(-r * t) * _norm_cdf(d2)) / 365.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (-(spot * pdf_d1 * sigma) / (2 * math.sqrt(t))
                 + r * strike * math.exp(-r * t) * _norm_cdf(-d2)) / 365.0
    return {"delta": round(delta, 4), "gamma": round(gamma, 6),
            "theta": round(theta, 4), "vega": round(vega, 4)}


def implied_volatility(price, spot, strike, t, is_call: bool, r: float = 0.07) -> float:
    """Newton-Raphson IV solver — FYERS' optionchain endpoint commonly omits
    ce_iv/pe_iv, so IV is derived from premium via Black-Scholes instead."""
    if price <= 0 or spot <= 0 or strike <= 0 or t <= 0:
        return 0.0
    sigma = 0.3
    for _ in range(50):
        model_price = _bs_price(spot, strike, t, r, sigma, is_call)
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
        vega = spot * math.exp(-0.5 * d1 ** 2) / math.sqrt(2 * math.pi) * math.sqrt(t)
        diff = model_price - price
        if abs(diff) < 1e-4:
            break
        if vega < 1e-8:
            break
        sigma -= diff / vega
        sigma = max(0.001, min(sigma, 5.0))
    return round(sigma * 100, 2)


def parse_days_to_expiry(expiry_label: str) -> float:
    if not expiry_label:
        return 7.0
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            exp_date = datetime.strptime(expiry_label, fmt)
            delta = (exp_date - datetime.now()).total_seconds() / 86400
            return max(delta, 0.5)
        except ValueError:
            continue
    return 7.0


def add_iv_columns(df: pd.DataFrame, spot: float, expiry_label: str) -> pd.DataFrame:
    if "ce_iv" in df.columns and "pe_iv" in df.columns and df["ce_iv"].abs().sum() > 0:
        return df
    if not spot or df.empty:
        return df
    days_to_expiry = parse_days_to_expiry(expiry_label)
    t = max(days_to_expiry, 0.5) / 365.0
    df = df.copy()
    df["ce_iv"] = df.apply(
        lambda row: implied_volatility(row.get("ce_ltp", 0), spot, row["strike_price"], t, True), axis=1)
    df["pe_iv"] = df.apply(
        lambda row: implied_volatility(row.get("pe_ltp", 0), spot, row["strike_price"], t, False), axis=1)
    return df


def add_greeks_columns(df: pd.DataFrame, spot: float, expiry_label: str, r: float = 0.07) -> pd.DataFrame:
    """Adds ce_delta/pe_delta/ce_gamma_bs/pe_gamma_bs/ce_theta/pe_theta/
    ce_vega/pe_vega using the chain's own IV. Independent of the existing
    Gamma Build-up Analyzer's ce_gamma/pe_gamma (section 5C), which stays
    untouched — this feeds the new Master AI Confidence engine instead."""
    d = df.copy()
    if d.empty or not spot:
        for c in ("ce_delta", "pe_delta", "ce_gamma_bs", "pe_gamma_bs", "ce_theta", "pe_theta", "ce_vega", "pe_vega"):
            d[c] = 0.0
        return d
    days_to_expiry = parse_days_to_expiry(expiry_label)
    t = max(days_to_expiry, 0.5) / 365.0

    def _row(strike, iv_pct, is_call):
        sigma = max(float(iv_pct), 0.0) / 100.0
        if sigma <= 0:
            sigma = 0.30
        return _bs_greeks(spot, strike, t, r, sigma, is_call)

    ce_greeks = d.apply(lambda row: _row(row["strike_price"], row.get("ce_iv", 0), True), axis=1)
    pe_greeks = d.apply(lambda row: _row(row["strike_price"], row.get("pe_iv", 0), False), axis=1)
    d["ce_delta"] = ce_greeks.apply(lambda g: g["delta"])
    d["ce_gamma_bs"] = ce_greeks.apply(lambda g: g["gamma"])
    d["ce_theta"] = ce_greeks.apply(lambda g: g["theta"])
    d["ce_vega"] = ce_greeks.apply(lambda g: g["vega"])
    d["pe_delta"] = pe_greeks.apply(lambda g: g["delta"])
    d["pe_gamma_bs"] = pe_greeks.apply(lambda g: g["gamma"])
    d["pe_theta"] = pe_greeks.apply(lambda g: g["theta"])
    d["pe_vega"] = pe_greeks.apply(lambda g: g["vega"])
    return d


def compute_strike_bias(df: pd.DataFrame) -> pd.DataFrame:
    """Classic OI-buildup read per strike (rising Call OI ~ resistance /
    sell-side pressure, rising Put OI ~ support / buy-side pressure)."""
    out = df.copy()
    ce_chng = out["ce_chng_oi"] if "ce_chng_oi" in out.columns else pd.Series(0, index=out.index)
    pe_chng = out["pe_chng_oi"] if "pe_chng_oi" in out.columns else pd.Series(0, index=out.index)

    def ce_label(v):
        if v > 0:
            return "🔴 Sell Side (Call Writing)"
        elif v < 0:
            return "🟢 Unwinding"
        return "⚪ Flat"

    def pe_label(v):
        if v > 0:
            return "🟢 Buy Side (Put Writing)"
        elif v < 0:
            return "🔴 Unwinding"
        return "⚪ Flat"

    out["CE Bias"] = ce_chng.apply(ce_label)
    out["PE Bias"] = pe_chng.apply(pe_label)

    def combined(row):
        ce_v, pe_v = row["_ce_chng"], row["_pe_chng"]
        if pe_v > 0 and pe_v >= max(ce_v, 0):
            return "🟢 BUY"
        if ce_v > 0 and ce_v >= max(pe_v, 0):
            return "🔴 SELL"
        return "🟡 NEUTRAL"

    out["_ce_chng"] = ce_chng
    out["_pe_chng"] = pe_chng
    out["Strike Signal"] = out.apply(combined, axis=1)

    magnitudes = pd.concat([ce_chng.abs(), pe_chng.abs()])
    threshold = magnitudes.quantile(0.8) if len(magnitudes) > 0 and magnitudes.max() > 0 else float("inf")
    out["Big Move"] = ((ce_chng.abs() >= threshold) | (pe_chng.abs() >= threshold)).map(
        {True: "🚨 Big Move", False: ""}
    )
    out.drop(columns=["_ce_chng", "_pe_chng"], inplace=True)
    return out


def detect_big_moves(df: pd.DataFrame, top_n: int = 3) -> list:
    """Flags strikes with unusually large OI buildup (chng_oi), commonly
    read as smart-money positioning."""
    alerts = []
    if df.empty or "ce_chng_oi" not in df.columns or "pe_chng_oi" not in df.columns:
        return alerts

    ce_thresh = df["ce_chng_oi"].abs().quantile(0.85) if df["ce_chng_oi"].abs().max() > 0 else 0
    pe_thresh = df["pe_chng_oi"].abs().quantile(0.85) if df["pe_chng_oi"].abs().max() > 0 else 0

    top_ce = df.reindex(df["ce_chng_oi"].abs().sort_values(ascending=False).index).head(top_n)
    top_pe = df.reindex(df["pe_chng_oi"].abs().sort_values(ascending=False).index).head(top_n)

    for _, row in top_ce.iterrows():
        chg = row["ce_chng_oi"]
        if abs(chg) < ce_thresh or chg == 0:
            continue
        if chg > 0:
            alerts.append({
                "strike": row["strike_price"], "side": "CE", "direction": "SELL", "oi_change": chg,
                "note": f"Heavy CALL writing at {row['strike_price']:,.0f} — resistance building, "
                        f"bearish/range bias near this strike.",
            })
        else:
            alerts.append({
                "strike": row["strike_price"], "side": "CE", "direction": "BUY", "oi_change": chg,
                "note": f"CALL OI unwinding at {row['strike_price']:,.0f} — resistance weakening, "
                        f"possible breakout above.",
            })

    for _, row in top_pe.iterrows():
        chg = row["pe_chng_oi"]
        if abs(chg) < pe_thresh or chg == 0:
            continue
        if chg > 0:
            alerts.append({
                "strike": row["strike_price"], "side": "PE", "direction": "BUY", "oi_change": chg,
                "note": f"Heavy PUT writing at {row['strike_price']:,.0f} — support building, "
                        f"bullish bias near this strike.",
            })
        else:
            alerts.append({
                "strike": row["strike_price"], "side": "PE", "direction": "SELL", "oi_change": chg,
                "note": f"PUT OI unwinding at {row['strike_price']:,.0f} — support weakening, "
                        f"possible breakdown below.",
            })

    alerts.sort(key=lambda a: abs(a["oi_change"]), reverse=True)
    return alerts


# ══════════════════════════════════════════════════════════════════════════
# 5. BIG MOVE READY STRIKE ENGINE  (0-100 weighted score — kept as the
#    underlying "Breakout/Breakdown/Institutional/Smart-Money" data source
#    that the new AI Engine in section 5B builds on top of)
# ══════════════════════════════════════════════════════════════════════════

def _normalize(series: pd.Series) -> pd.Series:
    s = series.astype(float)
    if s.empty:
        return s
    if s.max() == s.min():
        return pd.Series(0.5, index=s.index)
    return (s - s.min()) / (s.max() - s.min())


BIG_MOVE_WEIGHTS = {
    "delta_oi": 0.15, "total_oi": 0.12, "volume": 0.10, "confirmation": 0.08,
    "put_writing": 0.10, "call_writing": 0.10, "unwinding": 0.05,
    "spot_distance": 0.08, "atm_distance": 0.07, "maxpain_distance": 0.05,
    "iv": 0.05, "pcr": 0.05,
}


def compute_big_move_scores(df: pd.DataFrame, spot_price: float, max_pain: float,
                             pcr: float, atm_strike: float) -> pd.DataFrame:
    """
    Weighted Big Move Score (0-100) per strike, combining ΔOI, total OI,
    volume, OI+volume confirmation, put/call writing, unwinding, distance
    from spot/ATM/max-pain, IV level, and overall PCR bias. Also derives
    Breakout/Breakdown Probability and Institutional/Smart-Money Score,
    which feed the AI Engine below.

    NOTE: since only a single point-in-time chain snapshot is available
    (no historical intraday series), factors are ranked RELATIVE to the
    current chain rather than against an absolute market-wide baseline.
    """
    if df.empty:
        return df

    d = df.copy()
    ce_oi = d.get("ce_oi", pd.Series(0, index=d.index))
    pe_oi = d.get("pe_oi", pd.Series(0, index=d.index))
    ce_chng = d.get("ce_chng_oi", pd.Series(0, index=d.index))
    pe_chng = d.get("pe_chng_oi", pd.Series(0, index=d.index))
    ce_vol = d.get("ce_volume", pd.Series(0, index=d.index))
    pe_vol = d.get("pe_volume", pd.Series(0, index=d.index))
    ce_iv = d.get("ce_iv", pd.Series(0, index=d.index))
    pe_iv = d.get("pe_iv", pd.Series(0, index=d.index))
    strikes = d["strike_price"]

    total_oi = ce_oi + pe_oi
    total_delta_oi = ce_chng.abs() + pe_chng.abs()
    total_volume = ce_vol + pe_vol
    avg_iv = (ce_iv + pe_iv) / 2

    oi_score = _normalize(total_oi)
    delta_oi_score = _normalize(total_delta_oi)
    volume_score = _normalize(total_volume)
    confirmation_score = (delta_oi_score.clip(lower=0) * volume_score.clip(lower=0)) ** 0.5

    put_writing_score = _normalize(pe_chng.clip(lower=0))
    call_writing_score = _normalize(ce_chng.clip(lower=0))
    unwinding_score = _normalize((-ce_chng.clip(upper=0)) + (-pe_chng.clip(upper=0)))

    ref = spot_price if spot_price else (atm_strike if atm_strike else float(strikes.median()))
    spot_distance_score = 1 - _normalize((strikes - ref).abs()) if ref else pd.Series(0.5, index=d.index)
    atm_distance_score = 1 - _normalize((strikes - atm_strike).abs()) if atm_strike else pd.Series(0.5, index=d.index)
    maxpain_distance_score = 1 - _normalize((strikes - max_pain).abs()) if max_pain else pd.Series(0.5, index=d.index)

    iv_score = _normalize(avg_iv)
    pcr_bias = min(abs((pcr or 1) - 1) / 1.0, 1.0)
    pcr_score = pd.Series(pcr_bias, index=d.index)

    w = BIG_MOVE_WEIGHTS
    score = (
        delta_oi_score * w["delta_oi"] + oi_score * w["total_oi"] + volume_score * w["volume"]
        + confirmation_score * w["confirmation"] + put_writing_score * w["put_writing"]
        + call_writing_score * w["call_writing"] + unwinding_score * w["unwinding"]
        + spot_distance_score * w["spot_distance"] + atm_distance_score * w["atm_distance"]
        + maxpain_distance_score * w["maxpain_distance"] + iv_score * w["iv"] + pcr_score * w["pcr"]
    ) * 100

    d["Big Move Score"] = score.clip(0, 100).round(1)

    def _label(v):
        if v >= 90:
            return "🔥 Explosive Move Ready"
        if v >= 80:
            return "🟢 Strong Move"
        if v >= 60:
            return "🟡 Watch"
        return "⚪ Ignore"

    d["Big Move Label"] = d["Big Move Score"].apply(_label)

    def _ce_buildup(v):
        if v > 0:
            return "Short Build-up (Call Writing)"
        if v < 0:
            return "Short Covering"
        return "Flat"

    def _pe_buildup(v):
        if v > 0:
            return "Long Build-up (Put Writing)"
        if v < 0:
            return "Long Unwinding"
        return "Flat"

    d["CE Build-up"] = ce_chng.apply(_ce_buildup)
    d["PE Build-up"] = pe_chng.apply(_pe_buildup)

    above_spot = strikes > ref
    below_spot = strikes < ref
    breakout_prob = pd.Series(0.0, index=d.index)
    breakdown_prob = pd.Series(0.0, index=d.index)
    breakout_prob.loc[above_spot] = (unwinding_score.loc[above_spot] * 0.6 + delta_oi_score.loc[above_spot] * 0.4) * 100
    breakdown_prob.loc[below_spot] = (unwinding_score.loc[below_spot] * 0.6 + delta_oi_score.loc[below_spot] * 0.4) * 100
    d["Breakout Probability"] = breakout_prob.round(1)
    d["Breakdown Probability"] = breakdown_prob.round(1)

    # Institutional / smart-money proxies: large OI base + outsized volume
    # relative to typical volume at that OI level suggests bigger players.
    d["Institutional Score"] = ((oi_score * 0.7 + delta_oi_score * 0.3) * 100).round(1)
    d["Smart Money Score"] = ((unwinding_score * 0.5 + volume_score * 0.5) * 100).round(1)

    return d


def _safe_top(sub_df: pd.DataFrame, col: str):
    if sub_df is None or sub_df.empty or col not in sub_df.columns:
        return None
    return sub_df.loc[sub_df[col].idxmax()]


# ══════════════════════════════════════════════════════════════════════════
# 5B. AI ENGINE  (per-strike CE/PE signal, AI Confidence, star ratings,
#     and the AI Trade Signal generator)
# ══════════════════════════════════════════════════════════════════════════

RATING_BANDS = [
    (90, "★★★★★ Strong Buy", "strongbuy"),
    (75, "★★★★ Buy", "buy"),
    (55, "★★★ Hold", "hold"),
    (35, "★★ Avoid", "avoid"),
    (0, "★ Ignore", "ignore"),
]
RATING_CSS_CLASS = {
    "strongbuy": "rating-strongbuy", "buy": "rating-buy", "hold": "rating-hold",
    "avoid": "rating-avoid", "ignore": "rating-ignore",
}


def rating_from_score(score: float) -> tuple:
    """Returns (label_with_stars, css_key) for a 0-100 score."""
    for threshold, label, key in RATING_BANDS:
        if score >= threshold:
            return label, key
    return "★ Ignore", "ignore"


# Side-aware label bands, e.g. "★★★★★ STRONG CE BUY" / "★★ PE AVOID".
SIDE_RATING_BANDS = [
    (90, "★★★★★", "STRONG {side} BUY", "strongbuy"),
    (75, "★★★★", "{side} BUY", "buy"),
    (55, "★★★", "{side} HOLD", "hold"),
    (35, "★★", "{side} AVOID", "avoid"),
    (0, "★", "{side} IGNORE", "ignore"),
]


def rating_label_for_side(score: float, side: str) -> tuple:
    """Returns (label, css_key) for a 0-100 score tagged with its side,
    e.g. (34, 'CE') -> ('★★ CE AVOID', 'avoid'); (94, 'PE') ->
    ('★★★★★ STRONG PE BUY', 'strongbuy')."""
    for threshold, stars, template, key in SIDE_RATING_BANDS:
        if score >= threshold:
            return f"{stars} {template.format(side=side)}", key
    stars, template, key = SIDE_RATING_BANDS[-1][1:]
    return f"{stars} {template.format(side=side)}", key


def compute_ai_engine(df: pd.DataFrame, spot_price: float, atm_strike: float,
                       max_pain: float, pcr: float) -> pd.DataFrame:
    """
    Institutional-style per-strike CE/PE engine. Builds two INDEPENDENT
    0-100 scores per strike:
      • CE Score — how favourable buying a CALL at this strike looks
      • PE Score — how favourable buying a PUT looks
    using OI, ΔOI, Volume, PCR, Max Pain, Spot/ATM distance, IV & IV
    deviation from the chain average, writing/unwinding build-ups,
    highest-OI/Volume/ΔOI proximity, and breakout/breakdown probability
    (from section 5). Must be called AFTER compute_big_move_scores.

    This is a heuristic scoring model derived entirely from the current
    option-chain snapshot — a positioning/read-through tool, not a
    guarantee. Always confirm with price action and your own risk rules.
    """
    if df.empty:
        return df
    d = df.copy()

    ce_oi, pe_oi = d.get("ce_oi", pd.Series(0, index=d.index)), d.get("pe_oi", pd.Series(0, index=d.index))
    ce_chng, pe_chng = d.get("ce_chng_oi", pd.Series(0, index=d.index)), d.get("pe_chng_oi", pd.Series(0, index=d.index))
    ce_vol, pe_vol = d.get("ce_volume", pd.Series(0, index=d.index)), d.get("pe_volume", pd.Series(0, index=d.index))
    ce_iv, pe_iv = d.get("ce_iv", pd.Series(0, index=d.index)), d.get("pe_iv", pd.Series(0, index=d.index))
    strikes = d["strike_price"]

    ce_oi_s = _normalize(ce_oi)
    pe_oi_s = _normalize(pe_oi)
    ce_dchng_s = _normalize(ce_chng)
    pe_dchng_s = _normalize(pe_chng)
    ce_unwind_s = _normalize(-ce_chng.clip(upper=0))
    pe_unwind_s = _normalize(-pe_chng.clip(upper=0))
    ce_vol_s = _normalize(ce_vol)
    pe_vol_s = _normalize(pe_vol)

    avg_ce_iv = ce_iv[ce_iv > 0].mean() if (ce_iv > 0).any() else 0
    avg_pe_iv = pe_iv[pe_iv > 0].mean() if (pe_iv > 0).any() else 0
    ce_iv_dev = _normalize(-(ce_iv - avg_ce_iv).abs())
    pe_iv_dev = _normalize(-(pe_iv - avg_pe_iv).abs())

    ref = spot_price if spot_price else (atm_strike if atm_strike else float(strikes.median()))
    proximity_s = 1 - _normalize((strikes - ref).abs()) if ref else pd.Series(0.5, index=d.index)
    maxpain_proximity_s = 1 - _normalize((strikes - max_pain).abs()) if max_pain else pd.Series(0.5, index=d.index)

    total_oi = ce_oi + pe_oi
    total_vol = ce_vol + pe_vol
    total_dchng = ce_chng.abs() + pe_chng.abs()
    highest_oi_s = _normalize(total_oi)
    highest_vol_s = _normalize(total_vol)
    highest_dchng_s = _normalize(total_dchng)

    pcr_bull = float(np.clip(((pcr or 1) - 1) / 1.0, -1, 1))
    pcr_bull_s = (pcr_bull + 1) / 2
    pcr_bear_s = 1 - pcr_bull_s

    breakout_s = _normalize(d["Breakout Probability"]) if "Breakout Probability" in d.columns else pd.Series(0, index=d.index)
    breakdown_s = _normalize(d["Breakdown Probability"]) if "Breakdown Probability" in d.columns else pd.Series(0, index=d.index)

    # ── CE (call-buy favourability) ─────────────────────────────────────
    cw = {"put_writing": 0.16, "call_unwind": 0.12, "ce_volume": 0.10, "pcr_bull": 0.10,
          "proximity": 0.10, "breakout": 0.10, "highest_oi": 0.06, "highest_vol": 0.06,
          "highest_dchng": 0.06, "maxpain": 0.06, "iv": 0.05, "pe_oi_support": 0.03}
    ce_score = (
        pe_dchng_s * cw["put_writing"] + ce_unwind_s * cw["call_unwind"] + ce_vol_s * cw["ce_volume"]
        + pcr_bull_s * cw["pcr_bull"] + proximity_s * cw["proximity"] + breakout_s * cw["breakout"]
        + highest_oi_s * cw["highest_oi"] + highest_vol_s * cw["highest_vol"]
        + highest_dchng_s * cw["highest_dchng"] + maxpain_proximity_s * cw["maxpain"]
        + ce_iv_dev * cw["iv"] + pe_oi_s * cw["pe_oi_support"]
    ) * 100

    # ── PE (put-buy favourability) ──────────────────────────────────────
    pw = {"call_writing": 0.16, "put_unwind": 0.12, "pe_volume": 0.10, "pcr_bear": 0.10,
          "proximity": 0.10, "breakdown": 0.10, "highest_oi": 0.06, "highest_vol": 0.06,
          "highest_dchng": 0.06, "maxpain": 0.06, "iv": 0.05, "ce_oi_resistance": 0.03}
    pe_score = (
        ce_dchng_s * pw["call_writing"] + pe_unwind_s * pw["put_unwind"] + pe_vol_s * pw["pe_volume"]
        + pcr_bear_s * pw["pcr_bear"] + proximity_s * pw["proximity"] + breakdown_s * pw["breakdown"]
        + highest_oi_s * pw["highest_oi"] + highest_vol_s * pw["highest_vol"]
        + highest_dchng_s * pw["highest_dchng"] + maxpain_proximity_s * pw["maxpain"]
        + pe_iv_dev * pw["iv"] + ce_oi_s * pw["ce_oi_resistance"]
    ) * 100

    d["CE Score"] = ce_score.clip(0, 100).round(1)
    d["PE Score"] = pe_score.clip(0, 100).round(1)

    ce_ratings = d["CE Score"].apply(rating_from_score)
    pe_ratings = d["PE Score"].apply(rating_from_score)
    d["CE Rating"] = ce_ratings.apply(lambda x: x[0])
    d["CE Rating Key"] = ce_ratings.apply(lambda x: x[1])
    d["PE Rating"] = pe_ratings.apply(lambda x: x[0])
    d["PE Rating Key"] = pe_ratings.apply(lambda x: x[1])

    d["Overall Score"] = ((d["CE Score"] + d["PE Score"]) / 2).round(1)
    d["AI Confidence"] = d[["CE Score", "PE Score"]].max(axis=1).round(1)
    d["BUY Probability"] = d["CE Score"]
    d["SELL Probability"] = d["PE Score"]

    def _final_signal(row):
        if row["CE Score"] >= row["PE Score"]:
            return f"CE · {row['CE Rating']}"
        return f"PE · {row['PE Rating']}"

    d["Final Signal"] = d.apply(_final_signal, axis=1)

    # ── Independent CE / PE BUY & SELL probability ──────────────────────
    d["CE BUY Probability"] = d["CE Score"]
    d["PE BUY Probability"] = d["PE Score"]
    d["CE SELL Probability"] = (100 - d["CE Score"]).clip(0, 100).round(1)
    d["PE SELL Probability"] = (100 - d["PE Score"]).clip(0, 100).round(1)

    # ── Independent CE / PE Entry / SL / Targets (premium-% based) ──────
    def _levels(ltp: pd.Series) -> dict:
        entry = ltp.round(2)
        sl = (ltp * 0.85).round(2)
        t1 = (ltp * 1.15).round(2)
        t2 = (ltp * 1.30).round(2)
        t3 = (ltp * 1.50).round(2)
        return {"Entry": entry, "SL": sl, "T1": t1, "T2": t2, "T3": t3}

    ce_levels = _levels(d.get("ce_ltp", pd.Series(0, index=d.index)))
    pe_levels = _levels(d.get("pe_ltp", pd.Series(0, index=d.index)))
    d["CE Entry"], d["CE SL"] = ce_levels["Entry"], ce_levels["SL"]
    d["CE Target 1"], d["CE Target 2"], d["CE Target 3"] = ce_levels["T1"], ce_levels["T2"], ce_levels["T3"]
    d["PE Entry"], d["PE SL"] = pe_levels["Entry"], pe_levels["SL"]
    d["PE Target 1"], d["PE Target 2"], d["PE Target 3"] = pe_levels["T1"], pe_levels["T2"], pe_levels["T3"]

    # ── Per-strike Institutional Buying / Selling / Smart Money ─────────
    d["Institutional Buying"] = ((pe_oi_s * 0.5 + pe_dchng_s * 0.5) * 100).round(1)
    d["Institutional Selling"] = ((ce_oi_s * 0.5 + ce_dchng_s * 0.5) * 100).round(1)
    d["Smart Money Activity"] = d.get("Smart Money Score", pd.Series(0, index=d.index))

    # ── Confidence % (alias of AI Confidence, kept for the spec'd name) ──
    d["Confidence %"] = d["AI Confidence"]

    # ── Final Recommendation — star + side label, strongest side wins ───
    def _final_recommendation(row):
        if row["CE Score"] >= row["PE Score"]:
            label, _ = rating_label_for_side(row["CE Score"], "CE")
        else:
            label, _ = rating_label_for_side(row["PE Score"], "PE")
        return label

    d["Final Recommendation"] = d.apply(_final_recommendation, axis=1)
    return d


def generate_trade_signals(df: pd.DataFrame, pcr: float, support, resistance,
                            min_confidence: float = 80, top_n: int = 15) -> list:
    """High-confidence-only Strike / CE / PE trade signals with Entry, SL,
    three targets, Risk-Reward and a plain-English Reason list."""
    if df.empty or "CE Score" not in df.columns:
        return []

    ce_vol_thresh = df["ce_volume"].quantile(0.75) if df["ce_volume"].max() > 0 else 0
    pe_vol_thresh = df["pe_volume"].quantile(0.75) if df["pe_volume"].max() > 0 else 0
    diffs = df["strike_price"].sort_values().diff().dropna()
    strike_gap = diffs.median() if len(diffs) else 1

    signals = []
    for _, row in df.iterrows():
        for side, score_col, rating_col, ltp_col in [
            ("CE", "CE Score", "CE Rating", "ce_ltp"),
            ("PE", "PE Score", "PE Rating", "pe_ltp"),
        ]:
            score = row[score_col]
            if score < min_confidence:
                continue
            ltp = row[ltp_col]
            if ltp <= 0:
                continue

            entry = round(float(ltp), 2)
            sl = round(entry * 0.85, 2)
            t1 = round(entry * 1.15, 2)
            t2 = round(entry * 1.30, 2)
            t3 = round(entry * 1.50, 2)
            risk = max(entry - sl, 0.01)
            reward = t2 - entry
            rr = round(reward / risk, 2) if risk > 0 else 0

            reasons = []
            if side == "CE":
                if row.get("pe_chng_oi", 0) > 0:
                    reasons.append("Heavy Put Writing")
                if row.get("ce_chng_oi", 0) < 0:
                    reasons.append("Call Short Covering")
                if ce_vol_thresh > 0 and row.get("ce_volume", 0) >= ce_vol_thresh:
                    reasons.append("Volume Spike")
                if pcr > 1.1:
                    reasons.append("Bullish PCR")
                if support is not None and abs(row["strike_price"] - support) <= strike_gap:
                    reasons.append("Support Confirmed")
                if row.get("Institutional Score", 0) >= 70:
                    reasons.append("Institution Buying")
                if row.get("Breakout Probability", 0) >= 60:
                    reasons.append("Breakout Setup")
            else:
                if row.get("ce_chng_oi", 0) > 0:
                    reasons.append("Heavy Call Writing")
                if row.get("pe_chng_oi", 0) < 0:
                    reasons.append("Put Long Unwinding")
                if pe_vol_thresh > 0 and row.get("pe_volume", 0) >= pe_vol_thresh:
                    reasons.append("Volume Spike")
                if pcr < 0.9:
                    reasons.append("Bearish PCR")
                if resistance is not None and abs(row["strike_price"] - resistance) <= strike_gap:
                    reasons.append("Resistance Confirmed")
                if row.get("Institutional Score", 0) >= 70:
                    reasons.append("Institution Selling")
                if row.get("Breakdown Probability", 0) >= 60:
                    reasons.append("Breakdown Setup")
            if not reasons:
                reasons.append("OI Build-up")

            side_label, side_css_key = rating_label_for_side(score, side)

            signals.append({
                "Strike": row["strike_price"], "Side": side,
                "Signal": side_label, "Signal Key": side_css_key,
                "Confidence": score, "Entry": entry, "SL": sl, "T1": t1, "T2": t2, "T3": t3,
                "Risk Reward": f"1 : {rr}" if rr > 0 else "—",
                "Reason": " · ".join(reasons), "Reasons": reasons,
            })

    signals.sort(key=lambda s: s["Confidence"], reverse=True)
    return signals[:top_n]


def compute_dashboard_summary(df: pd.DataFrame, signals: list, intel: dict) -> dict:
    if df.empty:
        return {}

    def _rr_value(sig):
        rr_str = sig.get("Risk Reward", "—")
        try:
            return float(rr_str.split(":")[-1].strip())
        except (ValueError, IndexError):
            return 0.0

    return {
        "Top CE Buy": _safe_top(df, "CE Score"),
        "Top PE Buy": _safe_top(df, "PE Score"),
        "Best Breakout Strike": _safe_top(df, "Breakout Probability"),
        "Best Breakdown Strike": _safe_top(df, "Breakdown Probability"),
        "Highest Institutional Buying": intel.get("institution_buying", 0),
        "Highest Institutional Selling": intel.get("institution_selling", 0),
        "Highest Smart Money": _safe_top(df, "Smart Money Score"),
        "Highest OI": df.loc[(df["ce_oi"] + df["pe_oi"]).idxmax()] if len(df) else None,
        "Highest Volume": df.loc[(df["ce_volume"] + df["pe_volume"]).idxmax()] if len(df) else None,
        "Highest Delta OI": df.loc[(df["ce_chng_oi"].abs() + df["pe_chng_oi"].abs()).idxmax()] if len(df) else None,
        "Best Risk Reward Trade": max(signals, key=_rr_value) if signals else None,
        "Today's Best Trade": signals[0] if signals else None,
    }


# ══════════════════════════════════════════════════════════════════════════
# 6. MARKET INTELLIGENCE  (trend, momentum, institutional flow, OI shifts)
# ══════════════════════════════════════════════════════════════════════════

def compute_market_intelligence(df: pd.DataFrame, spot_price: float, max_pain: float, pcr: float) -> dict:
    if df.empty:
        return {}

    mp_component = 0.0
    if max_pain:
        mp_component = ((spot_price - max_pain) / max_pain) * 100 if spot_price else 0.0
    momentum_score = float(np.clip(((pcr - 1) * 50) + (mp_component * 0.5), -100, 100))

    if momentum_score > 20:
        trend = "🟢 Bullish"
    elif momentum_score < -20:
        trend = "🔴 Bearish"
    else:
        trend = "🟡 Sideways"

    total_ce_oi, total_pe_oi = df["ce_oi"].sum(), df["pe_oi"].sum()
    high_oi_thresh_ce = df["ce_oi"].quantile(0.75) if len(df) else 0
    high_oi_thresh_pe = df["pe_oi"].quantile(0.75) if len(df) else 0

    institution_buying = df.loc[df["pe_oi"] >= high_oi_thresh_pe, "pe_chng_oi"].clip(lower=0).sum()
    institution_selling = df.loc[df["ce_oi"] >= high_oi_thresh_ce, "ce_chng_oi"].clip(lower=0).sum()

    call_writers_activity = df["ce_chng_oi"].clip(lower=0).sum()
    put_writers_activity = df["pe_chng_oi"].clip(lower=0).sum()
    call_buyers_activity = (-df["ce_chng_oi"].clip(upper=0)).sum()
    put_buyers_activity = (-df["pe_chng_oi"].clip(upper=0)).sum()

    highest_volume_strike = df.loc[(df["ce_volume"] + df["pe_volume"]).idxmax(), "strike_price"] if len(df) else None
    highest_oi_strike = df.loc[(df["ce_oi"] + df["pe_oi"]).idxmax(), "strike_price"] if len(df) else None
    highest_delta_oi_strike = df.loc[(df["ce_chng_oi"].abs() + df["pe_chng_oi"].abs()).idxmax(), "strike_price"] if len(df) else None

    support = df.loc[df["pe_oi"].idxmax(), "strike_price"] if len(df) else None
    resistance = df.loc[df["ce_oi"].idxmax(), "strike_price"] if len(df) else None

    breakout_prob_avg = df["Breakout Probability"].max() if "Breakout Probability" in df.columns else 0
    breakdown_prob_avg = df["Breakdown Probability"].max() if "Breakdown Probability" in df.columns else 0

    return {
        "momentum_score": momentum_score,
        "trend": trend,
        "institution_buying": institution_buying,
        "institution_selling": institution_selling,
        "call_writers_activity": call_writers_activity,
        "put_writers_activity": put_writers_activity,
        "call_buyers_activity": call_buyers_activity,
        "put_buyers_activity": put_buyers_activity,
        "highest_volume_strike": highest_volume_strike,
        "highest_oi_strike": highest_oi_strike,
        "highest_delta_oi_strike": highest_delta_oi_strike,
        "support": support,
        "resistance": resistance,
        "breakout_probability": breakout_prob_avg,
        "breakdown_probability": breakdown_prob_avg,
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
    }


def detect_oi_shift(symbol: str, support, resistance) -> list:
    """Compares this fetch's support/resistance to the previous fetch for
    the same symbol (stored in session_state) to flag a shift."""
    notes = []
    history = st.session_state.setdefault("oc_prev_levels", {})
    prev = history.get(symbol)
    if prev and support is not None and resistance is not None:
        if prev.get("support") is not None and support != prev["support"]:
            direction = "up" if support > prev["support"] else "down"
            notes.append(f"🛡️ Support shifted {direction}: {prev['support']:,.0f} → {support:,.0f}")
        if prev.get("resistance") is not None and resistance != prev["resistance"]:
            direction = "up" if resistance > prev["resistance"] else "down"
            notes.append(f"🧱 Resistance shifted {direction}: {prev['resistance']:,.0f} → {resistance:,.0f}")
    history[symbol] = {"support": support, "resistance": resistance}
    return notes


# ══════════════════════════════════════════════════════════════════════════
# 7. CHARTS
# ══════════════════════════════════════════════════════════════════════════

def oi_bar_chart(df: pd.DataFrame, max_pain: float) -> go.Figure:
    fig = make_subplots(
        rows=1, cols=2, subplot_titles=("Call OI  (CE)", "Put OI  (PE)"),
        shared_yaxes=True, horizontal_spacing=0.04,
    )
    max_oi = max(df["ce_oi"].max(), df["pe_oi"].max()) if len(df) else 1
    strikes_sorted = df["strike_price"].sort_values().unique()
    strike_gap = (strikes_sorted[1] - strikes_sorted[0]) if len(strikes_sorted) > 1 else 1

    fig.add_trace(go.Bar(
        x=-df["ce_oi"], y=df["strike_price"], orientation="h",
        marker_color=["#1a7f37" if abs(s - max_pain) < strike_gap / 2 else "#238636" for s in df["strike_price"]],
        name="CE OI", showlegend=False, customdata=df["ce_oi"],
        hovertemplate="Strike %{y}<br>CE OI: %{customdata:,}<extra></extra>",
    ), row=1, col=1)

    fig.add_trace(go.Bar(
        x=df["pe_oi"], y=df["strike_price"], orientation="h",
        marker_color=["#b91c1c" if abs(s - max_pain) < strike_gap / 2 else "#da3633" for s in df["strike_price"]],
        name="PE OI", showlegend=False,
        hovertemplate="Strike %{y}<br>PE OI: %{x:,}<extra></extra>",
    ), row=1, col=2)

    for col in [1, 2]:
        fig.add_hline(y=max_pain, line_dash="dot", line_color="#f0c814",
                      annotation_text=f"Max Pain {max_pain:,.0f}",
                      annotation_font_color="#f0c814", row=1, col=col)

    fig.update_layout(
        paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
        font=dict(color="#8b949e", family="Courier New"),
        height=500, margin=dict(l=10, r=10, t=40, b=10),
        xaxis=dict(showticklabels=False, zeroline=False, showgrid=False, range=[-max_oi * 1.1, 0]),
        xaxis2=dict(showticklabels=False, zeroline=False, showgrid=False, range=[0, max_oi * 1.1]),
        yaxis=dict(showgrid=True, gridcolor="#21262d", tickfont=dict(color="#e6edf3", size=11)),
    )
    fig.update_annotations(font_color="#8b949e")
    return fig


def pcr_gauge(pcr: float) -> go.Figure:
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=pcr,
        number={"font": {"color": "#e6edf3", "size": 36, "family": "Courier New"}},
        gauge={
            "axis": {"range": [0, 3], "tickcolor": "#8b949e", "tickfont": {"color": "#8b949e"}},
            "bar": {"color": "#58a6ff", "thickness": 0.25},
            "bgcolor": "#161b22", "borderwidth": 0,
            "steps": [
                {"range": [0, 0.7], "color": "#3b0d1a"},
                {"range": [0.7, 1.3], "color": "#1c2128"},
                {"range": [1.3, 3.0], "color": "#0d3b2e"},
            ],
            "threshold": {"line": {"color": "#f0c814", "width": 3}, "value": pcr},
        },
        title={"text": "PUT/CALL RATIO", "font": {"color": "#8b949e", "size": 12}},
        domain={"x": [0, 1], "y": [0, 1]},
    ))
    fig.update_layout(paper_bgcolor="#0d1117", font=dict(color="#8b949e"),
                       height=220, margin=dict(l=20, r=20, t=30, b=0))
    return fig


def momentum_gauge(momentum_score: float) -> go.Figure:
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=momentum_score,
        number={"font": {"color": "#e6edf3", "size": 32, "family": "Courier New"}, "suffix": ""},
        gauge={
            "axis": {"range": [-100, 100], "tickcolor": "#8b949e", "tickfont": {"color": "#8b949e"}},
            "bar": {"color": "#58a6ff", "thickness": 0.25},
            "bgcolor": "#161b22", "borderwidth": 0,
            "steps": [
                {"range": [-100, -20], "color": "#3b0d1a"},
                {"range": [-20, 20], "color": "#1c2128"},
                {"range": [20, 100], "color": "#0d3b2e"},
            ],
            "threshold": {"line": {"color": "#f0c814", "width": 3}, "value": momentum_score},
        },
        title={"text": "MOMENTUM METER", "font": {"color": "#8b949e", "size": 12}},
        domain={"x": [0, 1], "y": [0, 1]},
    ))
    fig.update_layout(paper_bgcolor="#0d1117", font=dict(color="#8b949e"),
                       height=220, margin=dict(l=20, r=20, t=30, b=0))
    return fig


def iv_chart(df: pd.DataFrame) -> go.Figure:
    has_ce_iv = "ce_iv" in df.columns
    has_pe_iv = "pe_iv" in df.columns
    fig = go.Figure()
    if has_ce_iv:
        fig.add_trace(go.Scatter(x=df["strike_price"], y=df["ce_iv"], mode="lines+markers",
                                  name="CE IV", line=dict(color="#238636", width=2), marker=dict(size=5)))
    if has_pe_iv:
        fig.add_trace(go.Scatter(x=df["strike_price"], y=df["pe_iv"], mode="lines+markers",
                                  name="PE IV", line=dict(color="#da3633", width=2), marker=dict(size=5)))
    if not has_ce_iv and not has_pe_iv:
        fig.add_annotation(text="IV data not available from this API response",
                            xref="paper", yref="paper", x=0.5, y=0.5,
                            font=dict(color="#8b949e"), showarrow=False)
    fig.update_layout(
        paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
        font=dict(color="#8b949e", family="Courier New"),
        height=280, margin=dict(l=10, r=10, t=10, b=30),
        xaxis=dict(showgrid=True, gridcolor="#21262d", title="Strike"),
        yaxis=dict(showgrid=True, gridcolor="#21262d", title="IV %"),
        legend=dict(bgcolor="#161b22", bordercolor="#30363d", borderwidth=1),
    )
    return fig


def style_chain_table(df: pd.DataFrame) -> pd.DataFrame:
    cols = ["ce_oi", "ce_chng_oi", "ce_volume", "ce_ltp", "CE Bias", "strike_price",
            "PE Bias", "pe_ltp", "pe_volume", "pe_chng_oi", "pe_oi", "Strike Signal", "Big Move"]
    available = [c for c in cols if c in df.columns]
    out = df[available].copy()
    rename = {
        "ce_oi": "CE OI", "ce_chng_oi": "CE ΔOI", "ce_volume": "CE Vol", "ce_ltp": "CE LTP",
        "strike_price": "Strike ⚡", "pe_ltp": "PE LTP", "pe_volume": "PE Vol",
        "pe_chng_oi": "PE ΔOI", "pe_oi": "PE OI",
    }
    out.rename(columns={k: v for k, v in rename.items() if k in out.columns}, inplace=True)
    return out


def style_big_move_table(df: pd.DataFrame) -> pd.DataFrame:
    """Legacy combined Big Move Table (kept for backward compatibility):
    Strike, CE Score, PE Score, Overall Score, BUY Probability,
    SELL Probability, Breakout %, Breakdown %, Institution Score,
    Smart Money Score, Final Signal."""
    cols = ["strike_price", "CE Score", "PE Score", "Overall Score", "BUY Probability",
            "SELL Probability", "Breakout Probability", "Breakdown Probability",
            "Institutional Score", "Smart Money Score", "Final Signal"]
    available = [c for c in cols if c in df.columns]
    out = df[available].copy()
    out.rename(columns={
        "strike_price": "Strike ⚡",
        "Breakout Probability": "Breakout %",
        "Breakdown Probability": "Breakdown %",
        "Institutional Score": "Institution Score",
    }, inplace=True)
    sort_col = "Overall Score" if "Overall Score" in out.columns else out.columns[0]
    return out.sort_values(sort_col, ascending=False).reset_index(drop=True)


def style_ce_pe_analysis_table(df: pd.DataFrame) -> pd.DataFrame:
    """Full separate CE/PE Big Move Ready table, one row per strike, with
    every field requested: CE/PE AI Score, CE/PE BUY/SELL Probability,
    CE/PE Entry/SL/T1/T2/T3, Confidence %, Institutional Buying/Selling,
    Smart Money Activity, Breakout/Breakdown Probability, and a single
    Final Recommendation (the stronger of the two sides)."""
    cols = [
        "strike_price",
        "CE Score", "PE Score",
        "CE BUY Probability", "PE BUY Probability",
        "CE SELL Probability", "PE SELL Probability",
        "CE Entry", "PE Entry",
        "CE SL", "PE SL",
        "CE Target 1", "PE Target 1",
        "CE Target 2", "PE Target 2",
        "CE Target 3", "PE Target 3",
        "Confidence %",
        "Institutional Buying", "Institutional Selling",
        "Smart Money Activity",
        "Breakout Probability", "Breakdown Probability",
        "Final Recommendation",
    ]
    available = [c for c in cols if c in df.columns]
    out = df[available].copy()
    out.rename(columns={
        "strike_price": "Strike ⚡",
        "CE Score": "CE AI Score", "PE Score": "PE AI Score",
        "Breakout Probability": "Breakout Probability %",
        "Breakdown Probability": "Breakdown Probability %",
    }, inplace=True)
    sort_col = "Confidence %" if "Confidence %" in out.columns else out.columns[0]
    return out.sort_values(sort_col, ascending=False).reset_index(drop=True)


def style_trade_signals_table(signals: list) -> pd.DataFrame:
    if not signals:
        return pd.DataFrame()
    df = pd.DataFrame(signals)
    df.drop(columns=[c for c in ("Signal Key", "Reasons") if c in df.columns], inplace=True)
    df.rename(columns={"Side": "CE/PE"}, inplace=True)
    return df


def _bigmove_row_style(row):
    signal = str(row.get("Final Signal", row.get("Final Recommendation", ""))).upper()
    if "STRONG" in signal and "BUY" in signal:
        color = "background-color:#0d3b2e;color:#3fb950;"
    elif "BUY" in signal:
        color = "background-color:#123524;color:#7ee787;"
    elif "HOLD" in signal:
        color = "background-color:#1c2128;color:#d29922;"
    elif "AVOID" in signal:
        color = "background-color:#2b1a05;color:#e8823a;"
    else:
        color = "background-color:#161b22;color:#8b949e;"
    return [color] * len(row)


# ══════════════════════════════════════════════════════════════════════════
# 8. EXCEL EXPORT  (openpyxl — full formatting)
# ══════════════════════════════════════════════════════════════════════════

FILL_HEADER = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
FILL_STRONGBUY = PatternFill(start_color="A9D18E", end_color="A9D18E", fill_type="solid")
FILL_BUY = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
FILL_HOLD = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
FILL_AVOID = PatternFill(start_color="FFD966", end_color="FFD966", fill_type="solid")
FILL_IGNORE = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
# Kept for backward-compat naming used elsewhere
FILL_SELL = FILL_IGNORE
FILL_WAIT = FILL_HOLD
FONT_HEADER = Font(color="FFFFFF", bold=True, size=11)
THIN_BORDER = Border(*(Side(style="thin", color="30363D"),) * 4)


def _style_header_row(ws, row_idx: int = 1):
    for cell in ws[row_idx]:
        cell.fill = FILL_HEADER
        cell.font = FONT_HEADER
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = THIN_BORDER


def _autosize_columns(ws):
    for col_cells in ws.columns:
        length = max((len(str(c.value)) if c.value is not None else 0) for c in col_cells)
        col_letter = get_column_letter(col_cells[0].column)
        ws.column_dimensions[col_letter].width = min(max(length + 3, 10), 40)


def _apply_borders(ws):
    for row in ws.iter_rows():
        for cell in row:
            cell.border = THIN_BORDER


def _color_signal_cells(ws, header_row_values: list, start_row: int = 2):
    """Applies Green/Yellow/Red (and Strong-Buy dark-green / Avoid-orange)
    conditional fills to any column whose header contains 'Signal',
    'Bias', 'Build-up', 'Label', or 'Rating' — covers every rating/signal
    column produced anywhere in this dashboard (Chain Table, Big Move
    Ready, AI Trade Signals)."""
    target_cols = [
        idx + 1 for idx, h in enumerate(header_row_values)
        if h and any(k in str(h) for k in ("Signal", "Bias", "Build-up", "Label", "Rating", "Recommendation"))
    ]
    for row in ws.iter_rows(min_row=start_row):
        for col_idx in target_cols:
            cell = row[col_idx - 1]
            val = str(cell.value or "").upper()
            fill = None
            if "STRONG BUY" in val or "EXPLOSIVE" in val or "LONG BUILD-UP" in val or "BULLISH" in val:
                fill = FILL_STRONGBUY
            elif "BUY" in val or "STRONG MOVE" in val:
                fill = FILL_BUY
            elif "HOLD" in val or "NEUTRAL" in val or "WATCH" in val or "FLAT" in val or "WAIT" in val:
                fill = FILL_HOLD
            elif "AVOID" in val or "SHORT BUILD-UP" in val or "BEARISH" in val:
                fill = FILL_AVOID
            elif "IGNORE" in val or "SELL" in val:
                fill = FILL_IGNORE
            if fill:
                cell.fill = fill


def _write_dataframe(ws, df: pd.DataFrame, start_row: int = 1):
    for j, col_name in enumerate(df.columns, start=1):
        ws.cell(row=start_row, column=j, value=str(col_name))
    for i, (_, row) in enumerate(df.iterrows(), start=start_row + 1):
        for j, val in enumerate(row, start=1):
            if isinstance(val, (np.integer,)):
                val = int(val)
            elif isinstance(val, (np.floating,)):
                val = float(val)
            ws.cell(row=i, column=j, value=val)
    header_values = list(df.columns)
    _style_header_row(ws, start_row)
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1).coordinate
    ws.auto_filter.ref = ws.dimensions
    _color_signal_cells(ws, header_values, start_row=start_row + 1)
    _apply_borders(ws)
    _autosize_columns(ws)


def build_excel_report(df: pd.DataFrame, spot_price: float, atm_strike: float, pcr: float,
                        max_pain: float, support, resistance, symbol: str, expiry_label: str,
                        signals: list) -> io.BytesIO:
    wb = Workbook()

    # ── Summary sheet ──
    ws_summary = wb.active
    ws_summary.title = "Summary"
    summary_rows = [
        ("Symbol", symbol),
        ("Expiry", expiry_label),
        ("Generated At", datetime.now().strftime("%d-%b-%Y %H:%M:%S")),
        ("Spot Price", round(spot_price, 2) if spot_price else "—"),
        ("ATM Strike", atm_strike),
        ("PCR", round(pcr, 3)),
        ("Max Pain", max_pain),
        ("Support (Max PE OI)", support),
        ("Resistance (Max CE OI)", resistance),
        ("Total CE OI", int(df["ce_oi"].sum()) if not df.empty else 0),
        ("Total PE OI", int(df["pe_oi"].sum()) if not df.empty else 0),
        ("AI Trade Signals Generated", len(signals)),
    ]
    ws_summary.cell(row=1, column=1, value="Metric")
    ws_summary.cell(row=1, column=2, value="Value")
    _style_header_row(ws_summary, 1)
    for i, (label, value) in enumerate(summary_rows, start=2):
        ws_summary.cell(row=i, column=1, value=label)
        ws_summary.cell(row=i, column=2, value=value)
    ws_summary.freeze_panes = "A2"
    _apply_borders(ws_summary)
    _autosize_columns(ws_summary)

    # ── Chain Table sheet ──
    ws_chain = wb.create_sheet("Chain Table")
    _write_dataframe(ws_chain, style_chain_table(df))

    # ── Big Move Ready sheet (full separate CE/PE analysis) ──
    ws_bigmove = wb.create_sheet("Big Move Ready")
    _write_dataframe(ws_bigmove, style_ce_pe_analysis_table(df))

    # ── Legacy combined Big Move sheet (kept for backward compatibility) ──
    ws_bigmove_legacy = wb.create_sheet("Big Move (Legacy)")
    _write_dataframe(ws_bigmove_legacy, style_big_move_table(df))

    # ── AI Trade Signals sheet ──
    ws_signals = wb.create_sheet("AI Trade Signals")
    sig_df = style_trade_signals_table(signals)
    if not sig_df.empty:
        _write_dataframe(ws_signals, sig_df)
    else:
        ws_signals.cell(row=1, column=1, value="No signals met the confidence threshold")
        _style_header_row(ws_signals, 1)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


# ══════════════════════════════════════════════════════════════════════════
# 5C. GAMMA BUILD-UP ANALYZER  (real-time, session-tracked per-strike Gamma
#     monitoring — Gamma Change / Change % / Trend / Signal / Strength /
#     Trade Action / AI Rating, blinking BUY/SELL rows, smart alerts,
#     optional audio ping, live summary panel, no-duplicate-signal guard)
# ══════════════════════════════════════════════════════════════════════════

GAMMA_HISTORY_KEY = "oc_gamma_history"
GAMMA_ALERTED_KEY = "oc_gamma_alerted_thresholds"
GAMMA_LAST_SIGNAL_KEY = "oc_gamma_last_signal"
GAMMA_AUDIO_FIRED_KEY = "oc_gamma_audio_fired"

# A tiny base64 WAV "ping" so an audio alert can be played without any
# external asset — used only when the user opts in via the sidebar.
_GAMMA_PING_WAV_B64 = (
    "UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA="
)


def _bs_gamma(spot: float, strike: float, t: float, r: float, sigma: float) -> float:
    """Standard Black-Scholes Gamma — identical for calls and puts at the
    same strike/vol/tenor. Returns 0 for degenerate inputs."""
    if t <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
    return math.exp(-0.5 * d1 ** 2) / (spot * sigma * math.sqrt(2 * math.pi * t))


def add_gamma_columns(df: pd.DataFrame, spot: float, expiry_label: str, r: float = 0.07) -> pd.DataFrame:
    """Adds ce_gamma / pe_gamma / gamma (chain-level, avg of CE & PE) using
    the CE/PE IV already present on the dataframe (from add_iv_columns).
    Gamma is expressed per 1-point move in the underlying, same convention
    used across the rest of this file's Black-Scholes helpers."""
    d = df.copy()
    if d.empty or not spot:
        d["ce_gamma"] = 0.0
        d["pe_gamma"] = 0.0
        d["gamma"] = 0.0
        return d

    days_to_expiry = parse_days_to_expiry(expiry_label)
    t = max(days_to_expiry, 0.5) / 365.0
    ce_iv = d.get("ce_iv", pd.Series(0, index=d.index))
    pe_iv = d.get("pe_iv", pd.Series(0, index=d.index))

    def _row_gamma(strike, iv_pct):
        sigma = max(float(iv_pct), 0.0) / 100.0
        if sigma <= 0:
            sigma = 0.30  # fallback vol so gamma still renders on thin payloads
        return _bs_gamma(spot, strike, t, r, sigma)

    d["ce_gamma"] = d.apply(lambda row: _row_gamma(row["strike_price"], row.get("ce_iv", 0)), axis=1)
    d["pe_gamma"] = d.apply(lambda row: _row_gamma(row["strike_price"], row.get("pe_iv", 0)), axis=1)
    d["gamma"] = ((d["ce_gamma"] + d["pe_gamma"]) / 2).round(6)
    return d


def _gamma_trend_label(change_pct: float) -> str:
    if change_pct >= 30:
        return "🚀 Explosive Increase"
    if change_pct >= 10:
        return "🟢 Increasing"
    if change_pct > -10:
        return "🟡 Stable"
    if change_pct > -30:
        return "🔴 Decreasing"
    return "⚫ Flat"


def _gamma_strength(change_pct: float) -> tuple:
    """Returns (label, css_key). css_key maps to a row color band."""
    a = abs(change_pct)
    if a >= 30:
        return "Very Strong", "strongbuy"
    if a >= 20:
        return "Strong", "buy"
    if a >= 10:
        return "Medium", "hold"
    if a >= 3:
        return "Weak", "sell"
    return "Very Weak", "strongsell"


def _gamma_signal(gamma_up: bool, oi_up: bool, volume_up: bool, ltp_up: bool, ltp_down: bool) -> str:
    if gamma_up and oi_up and volume_up and ltp_up:
        return "🟢 BUY"
    if (not gamma_up) and oi_up and ltp_down:
        return "🔴 SELL"
    return "🟡 WAIT"


def _gamma_trade_action(signal: str, strength_key: str, trend: str) -> str:
    if signal == "🟢 BUY":
        if "Explosive" in trend or strength_key == "strongbuy":
            return "BUY NOW"
        return "BUY ON DIP"
    if signal == "🔴 SELL":
        if strength_key in ("strongsell", "sell"):
            return "EXIT"
        return "SELL"
    if strength_key in ("strongbuy", "buy"):
        return "HOLD"
    return "BOOK PROFIT" if strength_key in ("sell", "strongsell") else "HOLD"


def _gamma_ai_rating(signal: str, strength_key: str) -> str:
    if signal == "🟢 BUY" and strength_key == "strongbuy":
        return "⭐⭐⭐⭐⭐"
    if signal == "🟢 BUY" and strength_key in ("buy", "hold"):
        return "⭐⭐⭐⭐"
    if signal == "🟡 WAIT" and strength_key == "hold":
        return "⭐⭐⭐"
    if signal == "🔴 SELL" and strength_key in ("sell", "hold"):
        return "⭐⭐"
    return "⭐"


def compute_gamma_analysis(df: pd.DataFrame, symbol: str, expiry_label: str) -> pd.DataFrame:
    """
    Compares this refresh's per-strike Gamma/OI/Volume/LTP against the
    previous refresh (stored in st.session_state, keyed by symbol+expiry+
    strike) and derives Gamma Change, Gamma Change %, Gamma Trend, Gamma
    Signal, Gamma Strength, Trade Action and AI Rating.

    On the very first fetch for a given symbol/expiry there is no prior
    snapshot yet, so every strike starts at a neutral 0.0% / Stable / WAIT
    baseline — this is expected and resolves itself from the second
    refresh onward.
    """
    if df.empty:
        return df

    d = df.copy()
    history = st.session_state.setdefault(GAMMA_HISTORY_KEY, {})
    last_signal_map = st.session_state.setdefault(GAMMA_LAST_SIGNAL_KEY, {})
    hist_key = f"{symbol}|{expiry_label}"
    prev_strikes = history.get(hist_key, {})
    new_strikes = {}

    total_oi = (d.get("ce_oi", 0) + d.get("pe_oi", 0))
    total_vol = (d.get("ce_volume", 0) + d.get("pe_volume", 0))
    total_ltp = (d.get("ce_ltp", 0) + d.get("pe_ltp", 0))

    gamma_change_list, gamma_pct_list, trend_list = [], [], []
    signal_list, signal_key_list, strength_list, strength_key_list = [], [], [], []
    action_list, rating_list = [], []
    is_new_signal_list = []

    for idx, row in d.iterrows():
        strike = row["strike_price"]
        cur_gamma = float(row.get("gamma", 0.0))
        cur_oi = float(total_oi.loc[idx])
        cur_vol = float(total_vol.loc[idx])
        cur_ltp = float(total_ltp.loc[idx])

        prev = prev_strikes.get(str(strike), {})
        prev_gamma = prev.get("gamma", cur_gamma)
        prev_oi = prev.get("oi", cur_oi)
        prev_vol = prev.get("volume", cur_vol)
        prev_ltp = prev.get("ltp", cur_ltp)

        gamma_diff = cur_gamma - prev_gamma
        gamma_pct = ((cur_gamma - prev_gamma) / prev_gamma * 100) if prev_gamma else 0.0
        gamma_pct = float(np.clip(gamma_pct, -999, 999))

        trend = _gamma_trend_label(gamma_pct)
        gamma_up = gamma_diff > 0
        oi_up = cur_oi > prev_oi
        vol_up = cur_vol > prev_vol
        ltp_up = cur_ltp > prev_ltp
        ltp_down = cur_ltp < prev_ltp

        signal = _gamma_signal(gamma_up, oi_up, vol_up, ltp_up, ltp_down)
        strength_label, strength_key = _gamma_strength(gamma_pct)
        action = _gamma_trade_action(signal, strength_key, trend)
        rating = _gamma_ai_rating(signal, strength_key)

        # ── no duplicate signal: only "fresh" once per direction change ──
        prior_signal = last_signal_map.get(f"{hist_key}|{strike}")
        is_new_signal = signal != "🟡 WAIT" and signal != prior_signal
        if signal != "🟡 WAIT":
            last_signal_map[f"{hist_key}|{strike}"] = signal
        elif prior_signal is not None and prior_signal != "🟡 WAIT":
            # gamma direction reverted to neutral — clear so next real
            # signal in either direction counts as fresh again
            last_signal_map[f"{hist_key}|{strike}"] = "🟡 WAIT"

        gamma_change_list.append(gamma_diff)
        gamma_pct_list.append(gamma_pct)
        trend_list.append(trend)
        signal_list.append(signal)
        signal_key_list.append("buy" if signal == "🟢 BUY" else ("sell" if signal == "🔴 SELL" else "wait"))
        strength_list.append(strength_label)
        strength_key_list.append(strength_key)
        action_list.append(action)
        rating_list.append(rating)
        is_new_signal_list.append(is_new_signal)

        new_strikes[str(strike)] = {"gamma": cur_gamma, "oi": cur_oi, "volume": cur_vol, "ltp": cur_ltp}

    d["Gamma Change"] = gamma_change_list
    d["Gamma Change %"] = gamma_pct_list
    d["Gamma Trend"] = trend_list
    d["Gamma Signal"] = signal_list
    d["Gamma Signal Key"] = signal_key_list
    d["Gamma Strength"] = strength_list
    d["Gamma Strength Key"] = strength_key_list
    d["Trade Action"] = action_list
    d["AI Rating"] = rating_list
    d["_gamma_is_new_signal"] = is_new_signal_list

    # ── Strong Buy / Strong Sell overrides (proxy conditions — no Greeks
    # beyond Gamma are available in this chain payload, so Delta/Theta/
    # VWAP conditions are approximated via ΔOI, Volume and LTP direction
    # already computed above) ────────────────────────────────────────────
    def _final_band(row):
        strong_buy = (
            row["Gamma Signal"] == "🟢 BUY"
            and row["Gamma Strength Key"] in ("strongbuy", "buy")
            and row.get("ce_chng_oi", 0) + row.get("pe_chng_oi", 0) > 0
        )
        strong_sell = (
            row["Gamma Signal"] == "🔴 SELL"
            and row["Gamma Strength Key"] in ("strongsell", "sell")
        )
        if strong_buy:
            return "strongbuy"
        if strong_sell:
            return "strongsell"
        if row["Gamma Signal"] == "🟢 BUY":
            return "buy"
        if row["Gamma Signal"] == "🔴 SELL":
            return "sell"
        return "hold"

    d["Gamma Row Band"] = d.apply(_final_band, axis=1)

    # persist this refresh as "previous" for the next one
    history[hist_key] = new_strikes
    st.session_state[GAMMA_HISTORY_KEY] = history
    st.session_state[GAMMA_LAST_SIGNAL_KEY] = last_signal_map

    # Sort so the strongest Gamma increase sits at the top, per spec.
    d = d.sort_values("Gamma Change %", ascending=False).reset_index(drop=True)
    return d


GAMMA_ROW_CSS = {
    "strongbuy": "gamma-row-strongbuy",
    "buy": "gamma-row-buy",
    "hold": "gamma-row-hold",
    "sell": "gamma-row-sell",
    "strongsell": "gamma-row-strongsell",
}


def render_gamma_html_table(df: pd.DataFrame, top_n: int = 40) -> str:
    """Builds a raw HTML table (not st.dataframe) so CSS keyframe blinking
    can actually animate — st.dataframe renders through a static grid
    component that does not support live CSS animation."""
    cols = [
        ("strike_price", "Strike", "{:,.0f}"),
        ("gamma", "Gamma", "{:.5f}"),
        ("Gamma Change", "Gamma Chg", "{:+.5f}"),
        ("Gamma Change %", "Gamma Chg %", "{:+.1f}%"),
        ("Gamma Trend", "Trend", None),
        ("Gamma Signal", "Signal", None),
        ("Gamma Strength", "Strength", None),
        ("Trade Action", "Action", None),
        ("AI Rating", "AI Rating", None),
    ]
    view = df.head(top_n)
    rows_html = []
    for _, row in view.iterrows():
        band = row.get("Gamma Row Band", "hold")
        css = GAMMA_ROW_CSS.get(band, "gamma-row-hold")
        blink = ""
        if band in ("strongbuy", "buy") and row.get("Gamma Change %", 0) > 0:
            blink = " gamma-row-blink-green"
        elif band in ("strongsell", "sell") and row.get("Gamma Change %", 0) < -15:
            blink = " gamma-row-blink-red"
        cells = []
        for key, _, fmt in cols:
            val = row.get(key, "")
            cells.append(f"<td>{fmt.format(val) if fmt else val}</td>")
        rows_html.append(f'<tr class="{css}{blink}">{"".join(cells)}</tr>')

    header_html = "".join(f"<th>{label}</th>" for _, label, _ in cols)
    return f"""
    <div style="max-height:560px; overflow-y:auto; border:1px solid #30363d; border-radius:8px;">
    <table class="gamma-table">
        <thead><tr>{header_html}</tr></thead>
        <tbody>{''.join(rows_html)}</tbody>
    </table>
    </div>
    """


def fire_gamma_smart_alerts(df: pd.DataFrame, symbol: str, expiry_label: str) -> list:
    """Popup-style st.toast alerts when the strongest Gamma Change % on the
    board crosses 10% / 20% / 30% thresholds — fired once per threshold
    per symbol/expiry until Gamma direction resets below it."""
    if df.empty or "Gamma Change %" not in df.columns:
        return []
    alerted = st.session_state.setdefault(GAMMA_ALERTED_KEY, {})
    key = f"{symbol}|{expiry_label}"
    state = alerted.setdefault(key, {"10": False, "20": False, "30": False})

    max_pct = float(df["Gamma Change %"].max())
    messages = []
    thresholds = [(30, "30", "🔥 Explosive Gamma Build-up"),
                  (20, "20", "🚀🚀 Strong Institutional Buying"),
                  (10, "10", "🚀 Gamma Build-up Detected")]
    for value, tkey, msg in thresholds:
        if max_pct >= value and not state[tkey]:
            messages.append(msg)
            state[tkey] = True
        elif max_pct < value:
            state[tkey] = False
    alerted[key] = state
    st.session_state[GAMMA_ALERTED_KEY] = alerted
    return messages


def render_gamma_tab(df: pd.DataFrame, symbol: str, expiry_label: str, spot_price: float,
                      live_mode: bool, audio_alert: bool):
    """Renders the full Gamma Build-up Analyzer tab: live status badge,
    summary panel, smart alerts, optional audio ping, and the blinking
    color-coded Gamma table. Does not touch or recompute anything from
    the rest of the dashboard."""
    st.markdown("##### ⚡ Advanced Gamma Build-up Analyzer")

    gdf = add_gamma_columns(df, spot_price, expiry_label)
    gdf = compute_gamma_analysis(gdf, symbol, expiry_label)

    # ── Live status badge ────────────────────────────────────────────────
    badge_col, time_col, prev_col = st.columns([1, 1, 1])
    with badge_col:
        if live_mode:
            st.markdown('<span class="gamma-live-badge">🔴 LIVE</span>', unsafe_allow_html=True)
        else:
            st.markdown('<span class="intel-label">⏸ Manual refresh</span>', unsafe_allow_html=True)
    with time_col:
        st.markdown(f"<span class='intel-label'>Updated</span><br>"
                    f"<span class='intel-value' style='font-size:14px;'>{datetime.now().strftime('%H:%M:%S')}</span>",
                    unsafe_allow_html=True)
    with prev_col:
        avg_gamma = gdf["gamma"].mean() if "gamma" in gdf.columns and len(gdf) else 0
        st.markdown(f"<span class='intel-label'>Avg Gamma (this refresh)</span><br>"
                    f"<span class='intel-value' style='font-size:14px;'>{avg_gamma:.5f}</span>",
                    unsafe_allow_html=True)

    # ── Smart alerts (popups) ────────────────────────────────────────────
    alert_msgs = fire_gamma_smart_alerts(gdf, symbol, expiry_label)
    for msg in alert_msgs:
        try:
            st.toast(msg, icon="⚡")
        except Exception:
            st.info(msg)

    # ── Audio alert (only once per fresh, non-duplicate signal) ─────────
    if audio_alert and gdf.get("_gamma_is_new_signal", pd.Series(dtype=bool)).any():
        fired = st.session_state.setdefault(GAMMA_AUDIO_FIRED_KEY, {})
        fire_key = f"{symbol}|{expiry_label}|{datetime.now().strftime('%H:%M:%S')}"
        if fired.get(f"{symbol}|{expiry_label}") != fire_key:
            fired[f"{symbol}|{expiry_label}"] = fire_key
            st.session_state[GAMMA_AUDIO_FIRED_KEY] = fired
            st.audio(io.BytesIO(__import__("base64").b64decode(_GAMMA_PING_WAV_B64)), format="audio/wav")

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Summary panel (above the Gamma table) ────────────────────────────
    st.markdown('<div class="block-title">📋 Gamma Summary Panel</div>', unsafe_allow_html=True)
    strong_buy_n = int((gdf["Gamma Row Band"] == "strongbuy").sum()) if "Gamma Row Band" in gdf.columns else 0
    buy_n = int((gdf["Gamma Row Band"] == "buy").sum()) if "Gamma Row Band" in gdf.columns else 0
    sell_n = int(gdf["Gamma Row Band"].isin(["sell", "strongsell"]).sum()) if "Gamma Row Band" in gdf.columns else 0

    top_gamma_row = gdf.loc[gdf["gamma"].idxmax()] if len(gdf) and "gamma" in gdf.columns else None
    top_gamma_pct_row = gdf.loc[gdf["Gamma Change %"].idxmax()] if len(gdf) and "Gamma Change %" in gdf.columns else None
    top_oi_row = gdf.loc[(gdf.get("ce_oi", 0) + gdf.get("pe_oi", 0)).idxmax()] if len(gdf) else None
    top_vol_row = gdf.loc[(gdf.get("ce_volume", 0) + gdf.get("pe_volume", 0)).idxmax()] if len(gdf) else None
    top_rating_row = gdf.iloc[gdf["AI Rating"].apply(len).values.argmax()] if len(gdf) and "AI Rating" in gdf.columns else None

    def _strike_of(row):
        try:
            return f"{row['strike_price']:,.0f}"
        except Exception:
            return "—"

    g1, g2, g3, g4 = st.columns(4)
    g1.metric("Total Strong Buy Strikes", strong_buy_n)
    g2.metric("Total Buy Strikes", buy_n)
    g3.metric("Total Sell Strikes", sell_n)
    g4.metric("Highest Gamma Strike", _strike_of(top_gamma_row) if top_gamma_row is not None else "—")

    g5, g6, g7, g8 = st.columns(4)
    g5.metric("Highest Gamma %", f"{top_gamma_pct_row['Gamma Change %']:+.1f}%" if top_gamma_pct_row is not None else "—")
    g6.metric("Highest OI Strike", _strike_of(top_oi_row) if top_oi_row is not None else "—")
    g7.metric("Highest Volume Strike", _strike_of(top_vol_row) if top_vol_row is not None else "—")
    g8.metric("Highest AI Rating", top_rating_row["AI Rating"] if top_rating_row is not None else "—")

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Gamma table (custom HTML — enables CSS blink animation) ─────────
    st.markdown(render_gamma_html_table(gdf), unsafe_allow_html=True)
    st.caption(
        "Gamma is derived per strike via Black-Scholes from the chain's own IV (or a 30% fallback vol "
        "on thin payloads) and compared against the previous refresh stored for this session. Rows "
        "blink green while Gamma is rising alongside a BUY signal, and blink red while Gamma is "
        "falling sharply (≤ −15%) alongside a SELL signal; blinking stops automatically once Gamma "
        "stabilizes, while the last BUY/SELL signal stays visible. Strongest Gamma increase is sorted "
        "to the top. This is a heuristic read from a single point-in-time + previous-refresh "
        "comparison, not real tick-by-tick market data — always confirm with live price action."
    )

    # ── Auto-refresh every 5 seconds when Live mode is enabled ──────────
    if live_mode:
        time.sleep(5)
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════
# 5D. FUTURES DATA  (OI + price trend — used by the Master AI Confidence
#     engine and the Scalping Engine; independent of the options chain)
# ══════════════════════════════════════════════════════════════════════════

FUTURES_NAME_MAP = {
    "NIFTY": ("NSE", "NIFTY"), "BANKNIFTY": ("NSE", "BANKNIFTY"),
    "FINNIFTY": ("NSE", "FINNIFTY"), "MIDCPNIFTY": ("NSE", "MIDCPNIFTY"),
    "NIFTYNEXT50": ("NSE", "NIFTYNEXT50"), "SENSEX": ("BSE", "SENSEX"),
    "BANKEX": ("BSE", "BANKEX"),
}

FUTURES_HISTORY_KEY = "oc_futures_history"


def get_futures_symbol_candidates(index_key: str, base_symbol: str = None) -> list:
    """Builds ordered NSE/BSE futures symbol candidates for the current and
    next-month contract (FYERS format, e.g. NSE:NIFTY25JULFUT). Since the
    exact FYERS futures naming convention can vary, both the current and
    next calendar month are tried, same fallback style used throughout
    this file for index/stock symbols."""
    now = datetime.now()
    candidates = []
    if index_key in FUTURES_NAME_MAP:
        exch, name = FUTURES_NAME_MAP[index_key]
    elif base_symbol:
        exch, name = "NSE", base_symbol.strip().upper()
    else:
        return []
    for month_offset in (0, 1):
        total_month = now.month - 1 + month_offset
        year = now.year + total_month // 12
        month = total_month % 12 + 1
        yy = str(year)[-2:]
        mon = datetime(year, month, 1).strftime("%b").upper()
        candidates.append(f"{exch}:{name}{yy}{mon}FUT")
    return candidates


def fetch_futures_snapshot(fyers, symbol_candidates: list) -> dict:
    """Best-effort futures LTP + Open Interest fetch via fyers.quotes(),
    trying each candidate symbol in turn. Returns {} if none resolve —
    the rest of the AI engine degrades gracefully (futures factors are
    simply skipped) rather than failing outright."""
    for sym in symbol_candidates:
        try:
            resp = fyers.quotes(data={"symbols": sym})
        except Exception:
            continue
        if not isinstance(resp, dict) or resp.get("s") != "ok":
            continue
        d_list = resp.get("d", [])
        if not d_list:
            continue
        v = d_list[0].get("v", {}) if isinstance(d_list[0], dict) else {}
        if not v:
            continue
        ltp = float(v.get("lp", 0) or 0)
        if ltp <= 0:
            continue
        return {
            "symbol": sym,
            "ltp": ltp,
            "oi": float(v.get("oi", 0) or v.get("open_interest", 0) or 0),
            "prev_close": float(v.get("prev_close_price", 0) or v.get("cp", 0) or 0),
            "change_pct": float(v.get("chp", 0) or 0),
            "volume": float(v.get("volume", 0) or v.get("vol_traded_today", 0) or 0),
        }
    return {}


def analyze_futures_buildup(snapshot: dict) -> dict:
    """Compares this refresh's Futures OI/LTP against the previous refresh
    (session-tracked) to classify Long Build-up / Short Build-up / Long
    Unwinding / Short Covering, and the raw price trend direction."""
    if not snapshot:
        return {}
    history = st.session_state.setdefault(FUTURES_HISTORY_KEY, {})
    key = snapshot.get("symbol", "futures")
    prev = history.get(key, {})
    cur_oi, cur_ltp = snapshot["oi"], snapshot["ltp"]
    prev_oi = prev.get("oi", cur_oi)
    prev_ltp = prev.get("ltp", cur_ltp)

    oi_up = cur_oi > prev_oi
    price_up = cur_ltp > prev_ltp
    price_down = cur_ltp < prev_ltp

    if oi_up and price_up:
        buildup = "Long Build-up"
    elif oi_up and price_down:
        buildup = "Short Build-up"
    elif not oi_up and price_down:
        buildup = "Long Unwinding"
    elif not oi_up and price_up:
        buildup = "Short Covering"
    else:
        buildup = "Flat"

    history[key] = {"oi": cur_oi, "ltp": cur_ltp}
    st.session_state[FUTURES_HISTORY_KEY] = history

    return {
        **snapshot,
        "buildup": buildup,
        "long_buildup": buildup == "Long Build-up",
        "short_buildup": buildup == "Short Build-up",
        "long_unwinding": buildup == "Long Unwinding",
        "short_covering": buildup == "Short Covering",
        "price_trend": "up" if price_up else ("down" if price_down else "flat"),
    }


# ══════════════════════════════════════════════════════════════════════════
# 5E. TECHNICAL INDICATORS  (candle-based: VWAP, EMA 9/20/50, RSI, MACD,
#     Supertrend, ADX — feed the Master AI Confidence engine and the
#     Scalping Engine. Computed from FYERS intraday history candles.)
# ══════════════════════════════════════════════════════════════════════════

def fetch_candle_data(fyers, symbol: str, resolution: str = "5", lookback_days: int = 5) -> pd.DataFrame:
    """Fetches recent OHLCV candles via the FYERS history API so intraday
    technical indicators (VWAP/EMA/RSI/MACD/Supertrend/ADX) can be derived
    — the option-chain endpoint itself carries no price-series data."""
    try:
        to_ts = int(time.time())
        from_ts = to_ts - int(lookback_days) * 86400
        resp = fyers.history(data={
            "symbol": symbol, "resolution": str(resolution), "date_format": "0",
            "range_from": str(from_ts), "range_to": str(to_ts), "cont_flag": "1",
        })
    except Exception:
        return pd.DataFrame()
    if not isinstance(resp, dict) or resp.get("s") != "ok":
        return pd.DataFrame()
    candles = resp.get("candles", [])
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="s")
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    if df.empty:
        return pd.Series(dtype=float)
    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_vol = df["volume"].cumsum().replace(0, np.nan)
    return ((typical * df["volume"]).cumsum() / cum_vol).fillna(typical)


def compute_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def compute_macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = compute_ema(series, fast)
    ema_slow = compute_ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = compute_ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    """Returns (supertrend_series, trend_series['up'|'down'])."""
    if df.empty or len(df) < period + 1:
        return pd.Series(dtype=float), pd.Series(dtype=object)
    atr = compute_atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2.0
    upperband = hl2 + multiplier * atr
    lowerband = hl2 - multiplier * atr
    close = df["close"]
    final_upper = upperband.copy()
    final_lower = lowerband.copy()
    trend = pd.Series(index=df.index, dtype=object)
    trend.iloc[0] = "up"
    supertrend = pd.Series(index=df.index, dtype=float)
    supertrend.iloc[0] = final_lower.iloc[0]
    for i in range(1, len(df)):
        if close.iloc[i - 1] > final_upper.iloc[i - 1]:
            final_upper.iloc[i] = upperband.iloc[i]
        else:
            final_upper.iloc[i] = min(upperband.iloc[i], final_upper.iloc[i - 1])
        if close.iloc[i - 1] < final_lower.iloc[i - 1]:
            final_lower.iloc[i] = lowerband.iloc[i]
        else:
            final_lower.iloc[i] = max(lowerband.iloc[i], final_lower.iloc[i - 1])
        if close.iloc[i] > final_upper.iloc[i - 1]:
            trend.iloc[i] = "up"
        elif close.iloc[i] < final_lower.iloc[i - 1]:
            trend.iloc[i] = "down"
        else:
            trend.iloc[i] = trend.iloc[i - 1]
        supertrend.iloc[i] = final_lower.iloc[i] if trend.iloc[i] == "up" else final_upper.iloc[i]
    return supertrend, trend


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    if df.empty or len(df) < period + 1:
        return pd.Series(dtype=float)
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr = compute_atr(df, period)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx.fillna(0)


def compute_technical_snapshot(candle_df: pd.DataFrame, atm_row: pd.Series = None) -> dict:
    """
    Runs every technical indicator on the fetched candle series and returns
    a flat dict of latest values + derived booleans consumed by both the
    Master AI Confidence engine (5H) and the Scalping Engine (5I):
      close, vwap, ema9/20/50, rsi, macd_line/signal/hist, adx, supertrend,
      atr_pct, volume_spike, breakout_up/down, price_above/below_vwap,
      ema_bullish/bearish, macd_bullish/bearish, ce_premium_up, pe_premium_up.
    Returns a dict of zeros/False if candles are unavailable so downstream
    code degrades gracefully instead of raising.
    """
    empty = {
        "available": False, "close": 0.0, "vwap": 0.0, "ema9": 0.0, "ema20": 0.0, "ema50": 0.0,
        "rsi": 50.0, "macd_line": 0.0, "macd_signal": 0.0, "macd_hist": 0.0, "adx": 0.0,
        "supertrend": "flat", "atr_pct": 0.0, "volume_spike": False,
        "breakout_up": False, "breakout_down": False,
        "price_above_vwap": False, "price_below_vwap": False,
        "ema_bullish": False, "ema_bearish": False,
        "macd_bullish": False, "macd_bearish": False,
        "ce_premium_up": False, "pe_premium_up": False,
    }
    if candle_df is None or candle_df.empty or len(candle_df) < 20:
        return empty

    df = candle_df.copy()
    vwap = compute_vwap(df)
    ema9 = compute_ema(df["close"], 9)
    ema20 = compute_ema(df["close"], 20)
    ema50 = compute_ema(df["close"], 50) if len(df) >= 50 else compute_ema(df["close"], max(len(df) - 1, 2))
    rsi = compute_rsi(df["close"], 14)
    macd_line, macd_signal, macd_hist = compute_macd(df["close"])
    adx = compute_adx(df, 14)
    supertrend, trend = compute_supertrend(df, 10, 3.0)
    atr = compute_atr(df, 14)

    close = float(df["close"].iloc[-1])
    vwap_v = float(vwap.iloc[-1]) if len(vwap) else close
    ema9_v = float(ema9.iloc[-1])
    ema20_v = float(ema20.iloc[-1])
    ema50_v = float(ema50.iloc[-1])
    rsi_v = float(rsi.iloc[-1])
    macd_v, macd_sig_v, macd_hist_v = float(macd_line.iloc[-1]), float(macd_signal.iloc[-1]), float(macd_hist.iloc[-1])
    adx_v = float(adx.iloc[-1]) if len(adx) else 0.0
    st_trend = str(trend.iloc[-1]) if len(trend) else "flat"
    atr_v = float(atr.iloc[-1]) if len(atr) else 0.0
    atr_pct = (atr_v / close * 100) if close else 0.0

    # Volume spike: last candle's volume vs rolling 20-candle average
    vol_avg = df["volume"].tail(20).mean()
    volume_spike = bool(vol_avg > 0 and df["volume"].iloc[-1] >= vol_avg * 1.5)

    # Breakout/breakdown: close breaches the prior 20-candle high/low
    lookback = df.iloc[-21:-1] if len(df) >= 21 else df.iloc[:-1]
    prior_high = lookback["high"].max() if len(lookback) else close
    prior_low = lookback["low"].min() if len(lookback) else close
    breakout_up = bool(close > prior_high)
    breakout_down = bool(close < prior_low)

    ema_bullish = ema9_v > ema20_v > ema50_v
    ema_bearish = ema9_v < ema20_v < ema50_v

    result = {
        "available": True, "close": close, "vwap": vwap_v,
        "ema9": ema9_v, "ema20": ema20_v, "ema50": ema50_v, "rsi": rsi_v,
        "macd_line": macd_v, "macd_signal": macd_sig_v, "macd_hist": macd_hist_v,
        "adx": adx_v, "supertrend": st_trend, "atr_pct": atr_pct,
        "volume_spike": volume_spike, "breakout_up": breakout_up, "breakout_down": breakout_down,
        "price_above_vwap": close > vwap_v, "price_below_vwap": close < vwap_v,
        "ema_bullish": ema_bullish, "ema_bearish": ema_bearish,
        "macd_bullish": macd_v > macd_sig_v and macd_hist_v > 0,
        "macd_bearish": macd_v < macd_sig_v and macd_hist_v < 0,
    }

    # ATM CE/PE premium direction vs the previous refresh (session-tracked)
    if atm_row is not None and len(atm_row):
        try:
            ce_ltp = float(atm_row.get("ce_ltp", 0))
            pe_ltp = float(atm_row.get("pe_ltp", 0))
        except (TypeError, ValueError):
            ce_ltp, pe_ltp = 0.0, 0.0
        prev_premiums = st.session_state.setdefault("oc_prev_atm_premium", {})
        prev_ce = prev_premiums.get("ce_ltp")
        prev_pe = prev_premiums.get("pe_ltp")
        result["ce_premium_up"] = prev_ce is not None and ce_ltp > prev_ce
        result["pe_premium_up"] = prev_pe is not None and pe_ltp > prev_pe
        prev_premiums["ce_ltp"] = ce_ltp
        prev_premiums["pe_ltp"] = pe_ltp
        st.session_state["oc_prev_atm_premium"] = prev_premiums

    return result


# ══════════════════════════════════════════════════════════════════════════
# 5F. MARKET-LEVEL OI SIGNAL DETECTION  (near-ATM aggregate — complements
#     the existing per-strike CE/PE Bias columns from section 4)
# ══════════════════════════════════════════════════════════════════════════

def detect_market_oi_signals(df: pd.DataFrame, atm_strike: float, band: int = 3) -> dict:
    """
    Aggregates strikes within `band` positions of ATM to detect market-wide
    Call Writing / Put Writing / Call Unwinding / Put Unwinding and an
    overall Bullish/Bearish OI Shift — a broader read than any single
    strike's bias, used by the Master AI Confidence engine and the
    Scalping Engine's strict OI conditions.
    """
    empty = {
        "ce_chng_near_atm": 0.0, "pe_chng_near_atm": 0.0,
        "call_writing": False, "put_writing": False,
        "call_unwinding": False, "put_unwinding": False,
        "bullish_oi_shift": False, "bearish_oi_shift": False,
        "long_buildup": False, "short_buildup": False,
    }
    if df.empty:
        return empty
    strikes_sorted = sorted(df["strike_price"].unique())
    if not strikes_sorted:
        return empty
    if atm_strike in strikes_sorted:
        idx = strikes_sorted.index(atm_strike)
    else:
        idx = min(range(len(strikes_sorted)), key=lambda i: abs(strikes_sorted[i] - atm_strike))
    near = strikes_sorted[max(0, idx - band): idx + band + 1]
    sub = df[df["strike_price"].isin(near)]

    ce_chng = float(sub["ce_chng_oi"].sum())
    pe_chng = float(sub["pe_chng_oi"].sum())

    return {
        "ce_chng_near_atm": ce_chng, "pe_chng_near_atm": pe_chng,
        "call_writing": ce_chng > 0, "put_writing": pe_chng > 0,
        "call_unwinding": ce_chng < 0, "put_unwinding": pe_chng < 0,
        "bullish_oi_shift": pe_chng > 0 and pe_chng > abs(ce_chng),
        "bearish_oi_shift": ce_chng > 0 and ce_chng > abs(pe_chng),
        # Long/Short Build-up read at the option-chain level: fresh Put
        "long_buildup": pe_chng > 0 and pe_chng >= abs(ce_chng),
        "short_buildup": ce_chng > 0 and ce_chng >= abs(pe_chng),
    }


# ══════════════════════════════════════════════════════════════════════════
# 5G. MARKET CONDITION CLASSIFIER  (Trending / Sideways / Volatile /
#     Breakout / Breakdown / Fake Breakout)
# ══════════════════════════════════════════════════════════════════════════

def classify_market_condition(tech: dict) -> str:
    """
    Classifies the current intraday condition from the technical snapshot:
      • Breakout   — closed above the recent range on a volume spike
      • Breakdown  — closed below the recent range on a volume spike
      • Fake Breakout — range was breached WITHOUT volume confirmation
        (a classic trap — price pokes through but doesn't hold)
      • Trending   — strong directional ADX with aligned EMA stack
      • Volatile   — ATR% is elevated without a clear directional stack
      • Sideways   — none of the above (default, low-ADX/low-ATR chop)
    """
    if not tech.get("available"):
        return "🟡 Sideways / Insufficient Data"

    adx = tech.get("adx", 0)
    atr_pct = tech.get("atr_pct", 0)
    breakout_up = tech.get("breakout_up", False)
    breakout_down = tech.get("breakout_down", False)
    vol_spike = tech.get("volume_spike", False)
    ema_bull = tech.get("ema_bullish", False)
    ema_bear = tech.get("ema_bearish", False)

    if (breakout_up or breakout_down) and not vol_spike:
        return "⚠️ Fake Breakout"
    if breakout_up and vol_spike:
        return "🚀 Breakout"
    if breakout_down and vol_spike:
        return "📉 Breakdown"
    if adx >= 25 and (ema_bull or ema_bear):
        return "📈 Trending"
    if atr_pct >= 0.35:
        return "🌪️ Volatile"
    return "🟡 Sideways"


# ══════════════════════════════════════════════════════════════════════════
# 5H. MASTER AI CONFIDENCE ENGINE  (0-100% confidence, BUY/SELL/SIDEWAYS —
#     combines Option-Chain data + Futures data + Technical indicators.
#     No single factor, including PCR, can dominate: every factor is a
#     capped, bounded weight out of a fixed total.)
# ══════════════════════════════════════════════════════════════════════════

# Each tuple: (label, weight). Weights sum to 100 so the final confidence
# is naturally expressed as a percentage of "how many of these factors
# agree, weighted by how much each one matters." PCR is deliberately just
# ONE line among ~25 — never a standalone driver of direction.
AI_FACTOR_WEIGHTS = {
    "total_oi_skew": 5, "delta_oi_near_atm": 7, "call_put_writing": 6,
    "call_put_unwinding": 4, "bullish_bearish_oi_shift": 5, "max_pain_pull": 4,
    "pcr": 4, "atm_premium_direction": 4, "iv_skew": 3,
    "price_vs_vwap": 6, "ema_stack": 7, "rsi": 4, "macd": 5,
    "supertrend": 6, "adx_trend": 4, "volume_spike": 4,
    "futures_buildup": 6, "futures_price_trend": 4, "support_resistance": 5,
    "delta_greek": 3, "gamma_greek": 3, "theta_decay": 2, "vega_iv_sensitivity": 2,
    "breakout_probability": 3, "smart_money": 4,
}


def compute_ai_master_confidence(df: pd.DataFrame, spot_price: float, pcr: float, max_pain: float,
                                  atm_strike: float, intel: dict, tech: dict, futures: dict,
                                  market_oi: dict) -> dict:
    """
    Combines every requested factor (Option-Chain OI/ΔOI/build-ups/Max
    Pain/PCR/ATM premium/IV/Greeks, Futures OI & price trend, and
    technicals VWAP/EMA9-20-50/RSI/MACD/Supertrend/ADX/Volume/Support-
    Resistance) into ONE weighted 0-100 confidence score and a
    BUY / SELL / SIDEWAYS direction. Returns a dict with confidence,
    direction, trend, strength, probability, and the individual factor
    votes (for the Live Reasons checklist).

    This is a heuristic, weighted-evidence model derived from the current
    option-chain + futures + technical snapshot together — never from any
    single indicator (PCR included) — it is a positioning read, not a
    guarantee, and should be confirmed with your own risk management.
    """
    votes = {}  # factor -> "bull" | "bear" | None
    w = AI_FACTOR_WEIGHTS

    total_ce_oi = float(df["ce_oi"].sum()) if not df.empty else 0.0
    total_pe_oi = float(df["pe_oi"].sum()) if not df.empty else 0.0
    votes["total_oi_skew"] = "bull" if total_pe_oi > total_ce_oi else ("bear" if total_ce_oi > total_pe_oi else None)

    ce_n, pe_n = market_oi.get("ce_chng_near_atm", 0), market_oi.get("pe_chng_near_atm", 0)
    votes["delta_oi_near_atm"] = "bull" if pe_n > ce_n else ("bear" if ce_n > pe_n else None)

    if market_oi.get("put_writing") and not market_oi.get("call_writing"):
        votes["call_put_writing"] = "bull"
    elif market_oi.get("call_writing") and not market_oi.get("put_writing"):
        votes["call_put_writing"] = "bear"
    else:
        votes["call_put_writing"] = None

    if market_oi.get("call_unwinding") and not market_oi.get("put_unwinding"):
        votes["call_put_unwinding"] = "bull"
    elif market_oi.get("put_unwinding") and not market_oi.get("call_unwinding"):
        votes["call_put_unwinding"] = "bear"
    else:
        votes["call_put_unwinding"] = None

    if market_oi.get("bullish_oi_shift"):
        votes["bullish_bearish_oi_shift"] = "bull"
    elif market_oi.get("bearish_oi_shift"):
        votes["bullish_bearish_oi_shift"] = "bear"
    else:
        votes["bullish_bearish_oi_shift"] = None

    if max_pain and spot_price:
        votes["max_pain_pull"] = "bull" if spot_price < max_pain else ("bear" if spot_price > max_pain else None)
    else:
        votes["max_pain_pull"] = None

    # PCR — capped weight, one vote among ~25, never a standalone driver
    if pcr > 1.1:
        votes["pcr"] = "bull"
    elif pcr < 0.9:
        votes["pcr"] = "bear"
    else:
        votes["pcr"] = None

    atm_row = None
    if not df.empty:
        atm_sub = df.iloc[(df["strike_price"] - atm_strike).abs().argsort().iloc[:1]]
        atm_row = atm_sub.iloc[0] if len(atm_sub) else None
    if atm_row is not None:
        ce_up, pe_up = tech.get("ce_premium_up", False), tech.get("pe_premium_up", False)
        if ce_up and not pe_up:
            votes["atm_premium_direction"] = "bull"
        elif pe_up and not ce_up:
            votes["atm_premium_direction"] = "bear"
        else:
            votes["atm_premium_direction"] = None
    else:
        votes["atm_premium_direction"] = None

    if not df.empty and "ce_iv" in df.columns and "pe_iv" in df.columns:
        avg_ce_iv, avg_pe_iv = df["ce_iv"].mean(), df["pe_iv"].mean()
        votes["iv_skew"] = "bear" if avg_pe_iv > avg_ce_iv * 1.05 else ("bull" if avg_ce_iv > avg_pe_iv * 1.05 else None)
    else:
        votes["iv_skew"] = None

    if tech.get("available"):
        votes["price_vs_vwap"] = "bull" if tech.get("price_above_vwap") else ("bear" if tech.get("price_below_vwap") else None)
        votes["ema_stack"] = "bull" if tech.get("ema_bullish") else ("bear" if tech.get("ema_bearish") else None)
        rsi = tech.get("rsi", 50)
        votes["rsi"] = "bull" if rsi >= 55 else ("bear" if rsi <= 45 else None)
        votes["macd"] = "bull" if tech.get("macd_bullish") else ("bear" if tech.get("macd_bearish") else None)
        votes["supertrend"] = "bull" if tech.get("supertrend") == "up" else ("bear" if tech.get("supertrend") == "down" else None)
        if tech.get("adx", 0) > 25:
            votes["adx_trend"] = "bull" if tech.get("ema_bullish") else ("bear" if tech.get("ema_bearish") else None)
        else:
            votes["adx_trend"] = None
        if tech.get("volume_spike"):
            votes["volume_spike"] = "bull" if tech.get("price_above_vwap") else ("bear" if tech.get("price_below_vwap") else None)
        else:
            votes["volume_spike"] = None
    else:
        for k in ("price_vs_vwap", "ema_stack", "rsi", "macd", "supertrend", "adx_trend", "volume_spike"):
            votes[k] = None

    if futures:
        votes["futures_buildup"] = "bull" if futures.get("long_buildup") else ("bear" if futures.get("short_buildup") else None)
        votes["futures_price_trend"] = "bull" if futures.get("price_trend") == "up" else ("bear" if futures.get("price_trend") == "down" else None)
    else:
        votes["futures_buildup"] = None
        votes["futures_price_trend"] = None

    support, resistance = intel.get("support"), intel.get("resistance")
    if spot_price and support and resistance:
        dist_support = abs(spot_price - support)
        dist_resistance = abs(spot_price - resistance)
        votes["support_resistance"] = "bull" if dist_support < dist_resistance else ("bear" if dist_resistance < dist_support else None)
    else:
        votes["support_resistance"] = None

    if atm_row is not None:
        ce_delta = float(atm_row.get("ce_delta", 0)) if "ce_delta" in df.columns else 0.0
        pe_delta = float(atm_row.get("pe_delta", 0)) if "pe_delta" in df.columns else 0.0
        votes["delta_greek"] = "bull" if abs(ce_delta) > abs(pe_delta) else ("bear" if abs(pe_delta) > abs(ce_delta) else None)
        ce_gamma_bs = float(atm_row.get("ce_gamma_bs", 0)) if "ce_gamma_bs" in df.columns else 0.0
        votes["gamma_greek"] = "bull" if ce_gamma_bs > 0 and market_oi.get("bullish_oi_shift") else (
            "bear" if ce_gamma_bs > 0 and market_oi.get("bearish_oi_shift") else None)
        ce_theta = float(atm_row.get("ce_theta", 0)) if "ce_theta" in df.columns else 0.0
        pe_theta = float(atm_row.get("pe_theta", 0)) if "pe_theta" in df.columns else 0.0
        votes["theta_decay"] = "bear" if abs(ce_theta) > abs(pe_theta) else ("bull" if abs(pe_theta) > abs(ce_theta) else None)
        ce_vega = float(atm_row.get("ce_vega", 0)) if "ce_vega" in df.columns else 0.0
        pe_vega = float(atm_row.get("pe_vega", 0)) if "pe_vega" in df.columns else 0.0
        votes["vega_iv_sensitivity"] = None if abs(ce_vega - pe_vega) < 1e-6 else (
            "bull" if ce_vega > pe_vega and votes.get("iv_skew") == "bull" else
            "bear" if pe_vega > ce_vega and votes.get("iv_skew") == "bear" else None)
    else:
        votes["delta_greek"] = votes["gamma_greek"] = votes["theta_decay"] = votes["vega_iv_sensitivity"] = None

    if "Breakout Probability" in df.columns and "Breakdown Probability" in df.columns and not df.empty:
        max_breakout = df["Breakout Probability"].max()
        max_breakdown = df["Breakdown Probability"].max()
        votes["breakout_probability"] = "bull" if max_breakout > max_breakdown else ("bear" if max_breakdown > max_breakout else None)
    else:
        votes["breakout_probability"] = None

    inst_buy = intel.get("institution_buying", 0)
    inst_sell = intel.get("institution_selling", 0)
    votes["smart_money"] = "bull" if inst_buy > inst_sell else ("bear" if inst_sell > inst_buy else None)

    bull_points, bear_points, max_points = 0.0, 0.0, 0.0
    for factor, weight in w.items():
        max_points += weight
        vote = votes.get(factor)
        if vote == "bull":
            bull_points += weight
        elif vote == "bear":
            bear_points += weight

    net = bull_points - bear_points
    confidence = round((max(bull_points, bear_points) / max_points) * 100, 1) if max_points else 0.0

    if max_points == 0 or abs(net) < max_points * 0.06:
        direction = "SIDEWAYS"
    else:
        direction = "BUY" if net > 0 else "SELL"

    if confidence >= 90:
        strength, probability = "Strong", "Very High"
    elif confidence >= 80:
        strength, probability = "Strong", "High"
    elif confidence >= 65:
        strength, probability = "Medium", "Medium"
    else:
        strength, probability = "Weak", "Low"

    trend_label = "Bullish" if net > 0 else ("Bearish" if net < 0 else "Neutral")

    return {
        "confidence": confidence, "direction": direction, "trend": trend_label,
        "strength": strength, "probability": probability,
        "bull_points": round(bull_points, 1), "bear_points": round(bear_points, 1),
        "max_points": max_points, "votes": votes, "atm_row": atm_row,
    }


AI_FACTOR_LABELS = {
    "total_oi_skew": ("Total OI favours Puts (support)", "Total OI favours Calls (resistance)"),
    "delta_oi_near_atm": ("Rising Put ΔOI near ATM", "Rising Call ΔOI near ATM"),
    "call_put_writing": ("Heavy Put Writing", "Heavy Call Writing"),
    "call_put_unwinding": ("Call Unwinding (resistance fading)", "Put Unwinding (support fading)"),
    "bullish_bearish_oi_shift": ("Bullish OI Shift", "Bearish OI Shift"),
    "max_pain_pull": ("Spot below Max Pain (pull-up bias)", "Spot above Max Pain (pull-down bias)"),
    "pcr": ("Bullish PCR", "Bearish PCR"),
    "atm_premium_direction": ("ATM CE Premium Increasing", "ATM PE Premium Increasing"),
    "iv_skew": ("Call IV richer (bullish skew)", "Put IV richer (bearish skew)"),
    "price_vs_vwap": ("Price Above VWAP", "Price Below VWAP"),
    "ema_stack": ("EMA Bullish Stack (9>20>50)", "EMA Bearish Stack (9<20<50)"),
    "rsi": ("RSI Bullish Zone", "RSI Bearish Zone"),
    "macd": ("MACD Positive/Bullish", "MACD Negative/Bearish"),
    "supertrend": ("Supertrend Buy", "Supertrend Sell"),
    "adx_trend": ("Strong Trending Move (ADX>25, Bullish)", "Strong Trending Move (ADX>25, Bearish)"),
    "volume_spike": ("Volume Breakout (Bullish)", "Volume Breakout (Bearish)"),
    "futures_buildup": ("Futures Long Build-up", "Futures Short Build-up"),
    "futures_price_trend": ("Futures Price Trending Up", "Futures Price Trending Down"),
    "support_resistance": ("Price Closer to Support", "Price Closer to Resistance"),
    "delta_greek": ("Call Delta Dominance", "Put Delta Dominance"),
    "gamma_greek": ("Gamma Build-up Bullish", "Gamma Build-up Bearish"),
    "theta_decay": ("Put Theta Decay Favours Calls", "Call Theta Decay Favours Puts"),
    "vega_iv_sensitivity": ("Call Vega + Rising IV", "Put Vega + Rising IV"),
    "breakout_probability": ("Breakout Probability > Breakdown", "Breakdown Probability > Breakout"),
    "smart_money": ("Strong Institutional Buying", "Strong Institutional Selling"),
}


def build_live_reasons(votes: dict, direction: str) -> list:
    """Turns the factor votes into a plain-English checklist of reasons
    supporting the current AI direction (used for the '✓ Heavy Put
    Writing / ✓ EMA Bullish / ...' Live Reasons display)."""
    if direction not in ("BUY", "SELL"):
        return []
    wanted = "bull" if direction == "BUY" else "bear"
    reasons = []
    for factor, vote in votes.items():
        if vote == wanted and factor in AI_FACTOR_LABELS:
            bull_label, bear_label = AI_FACTOR_LABELS[factor]
            reasons.append(bull_label if wanted == "bull" else bear_label)
    return reasons


def build_warning_messages(tech: dict, market_condition: str, expiry_label: str,
                            df: pd.DataFrame) -> list:
    """Detects and surfaces Possible Trap / Bull Trap / Bear Trap / Low
    Volume / Expiry / Volatility warnings from the data already computed
    elsewhere in this file — no new external data source required."""
    warnings = []

    if "Fake Breakout" in market_condition:
        if tech.get("breakout_up"):
            warnings.append("⚠️ Bull Trap Risk — breakout up without volume confirmation")
        elif tech.get("breakout_down"):
            warnings.append("⚠️ Bear Trap Risk — breakdown without volume confirmation")
        else:
            warnings.append("⚠️ Possible Trap — range breached without confirming volume")

    if tech.get("available") and not tech.get("volume_spike") and tech.get("adx", 0) < 20:
        warnings.append("🔈 Low Volume — conviction behind the current move is weak")

    if expiry_label:
        try:
            days_left = parse_days_to_expiry(expiry_label)
            if days_left <= 1:
                warnings.append("📅 Expiry Day — expect sharp Theta decay & whipsaw moves")
        except Exception:
            pass

    if "Volatile" in market_condition or tech.get("atr_pct", 0) >= 0.5:
        warnings.append("🌪️ Elevated Volatility — widen stop-loss expectations")

    if not df.empty and "ce_iv" in df.columns and "pe_iv" in df.columns:
        avg_iv = (df["ce_iv"].mean() + df["pe_iv"].mean()) / 2
        if avg_iv >= 25:
            warnings.append("📰 High IV — possible News/Event Impact priced in")

    return warnings


# ══════════════════════════════════════════════════════════════════════════
# 5I. AI SCALPING ENGINE  (1min / 3min / 5min — signals fire ONLY when
#     every strict condition matches; confidence must also be > 75%,
#     otherwise the engine explicitly outputs "WAIT - No High Probability
#     Trade". Refreshes every 5 seconds when Live mode is enabled.)
# ══════════════════════════════════════════════════════════════════════════

SCALP_MIN_CONFIDENCE = 75.0


def evaluate_scalping_conditions(tech: dict, market_oi: dict, futures: dict) -> dict:
    """
    Evaluates the exact BUY / SELL scalping condition sets from the spec.
    ALL conditions in a set must be true for that side to qualify — this
    is intentionally strict (a scalping engine should stay quiet more
    often than it fires).
    """
    rsi = tech.get("rsi", 50)
    buy_conditions = {
        "Price > VWAP": bool(tech.get("price_above_vwap")),
        "EMA9 > EMA20 > EMA50": bool(tech.get("ema_bullish")),
        "RSI 55–70": 55 <= rsi <= 70,
        "MACD Bullish": bool(tech.get("macd_bullish")),
        "Supertrend Buy": tech.get("supertrend") == "up",
        "ADX > 25": tech.get("adx", 0) > 25,
        "Put Writing Increasing": bool(market_oi.get("put_writing")),
        "Call Unwinding Increasing": bool(market_oi.get("call_unwinding")),
        "Futures Long Build-up": bool(futures.get("long_buildup")),
        "Volume Spike": bool(tech.get("volume_spike")),
        "ATM CE Premium Increasing": bool(tech.get("ce_premium_up")),
    }
    sell_conditions = {
        "Price < VWAP": bool(tech.get("price_below_vwap")),
        "EMA9 < EMA20 < EMA50": bool(tech.get("ema_bearish")),
        "RSI 30–45": 30 <= rsi <= 45,
        "MACD Bearish": bool(tech.get("macd_bearish")),
        "Supertrend Sell": tech.get("supertrend") == "down",
        "ADX > 25": tech.get("adx", 0) > 25,
        "Call Writing Increasing": bool(market_oi.get("call_writing")),
        "Put Unwinding Increasing": bool(market_oi.get("put_unwinding")),
        "Futures Short Build-up": bool(futures.get("short_buildup")),
        "Volume Spike": bool(tech.get("volume_spike")),
        "ATM PE Premium Increasing": bool(tech.get("pe_premium_up")),
    }
    return {
        "buy_conditions": buy_conditions, "sell_conditions": sell_conditions,
        "buy_all_met": all(buy_conditions.values()),
        "sell_all_met": all(sell_conditions.values()),
    }


def generate_scalping_levels(direction: str, atm_row, tech: dict) -> dict:
    """Entry / SL / T1 / T2 / T3 / Risk-Reward for the ATM CE (BUY) or ATM
    PE (SELL) premium, sized off the intraday ATR% so scalping stops are
    tighter than the swing-style AI Trade Signals in section 5B."""
    if atm_row is None or direction not in ("BUY", "SELL"):
        return {}
    side = "CE" if direction == "BUY" else "PE"
    ltp = float(atm_row.get("ce_ltp" if side == "CE" else "pe_ltp", 0) or 0)
    if ltp <= 0:
        return {}
    atr_pct = max(tech.get("atr_pct", 0.5), 0.3)
    sl_pct = min(max(atr_pct * 1.5, 8), 20) / 100.0
    entry = round(ltp, 2)
    sl = round(ltp * (1 - sl_pct), 2)
    t1 = round(ltp * (1 + sl_pct * 0.75), 2)
    t2 = round(ltp * (1 + sl_pct * 1.5), 2)
    t3 = round(ltp * (1 + sl_pct * 2.5), 2)
    risk = max(entry - sl, 0.01)
    reward = t2 - entry
    rr = round(reward / risk, 2) if risk > 0 else 0
    return {"side": side, "entry": entry, "sl": sl, "t1": t1, "t2": t2, "t3": t3,
            "rr": f"1 : {rr}" if rr > 0 else "—"}


def compute_scalping_signal(df: pd.DataFrame, spot_price: float, pcr: float, max_pain: float,
                             atm_strike: float, intel: dict, tech: dict, futures: dict,
                             market_oi: dict) -> dict:
    """
    Master entry point for the Scalping Engine. Combines the Master AI
    Confidence score (5H) with the strict per-timeframe BUY/SELL condition
    sets above. A BUY or SELL is only ever emitted when:
      (a) confidence > 75%, AND
      (b) every single condition in that side's strict set is met.
    Otherwise the engine outputs WAIT, per spec — never a low-confidence
    guess dressed up as a signal.
    """
    ai = compute_ai_master_confidence(df, spot_price, pcr, max_pain, atm_strike, intel, tech, futures, market_oi)
    cond = evaluate_scalping_conditions(tech, market_oi, futures)
    market_condition = classify_market_condition(tech)
    warnings = build_warning_messages(tech, market_condition, intel.get("_expiry_label", ""), df)

    confidence = ai["confidence"]
    final_direction = "WAIT"
    if confidence > SCALP_MIN_CONFIDENCE:
        if ai["direction"] == "BUY" and cond["buy_all_met"]:
            final_direction = "BUY"
        elif ai["direction"] == "SELL" and cond["sell_all_met"]:
            final_direction = "SELL"

    reasons = build_live_reasons(ai["votes"], final_direction if final_direction != "WAIT" else ai["direction"])
    atm_row = ai.get("atm_row")
    levels = generate_scalping_levels(final_direction, atm_row, tech) if final_direction != "WAIT" else {}

    return {
        "direction": final_direction, "raw_ai_direction": ai["direction"],
        "confidence": confidence, "trend": ai["trend"], "strength": ai["strength"],
        "probability": ai["probability"], "market_condition": market_condition,
        "reasons": reasons, "warnings": warnings, "levels": levels,
        "buy_conditions": cond["buy_conditions"], "sell_conditions": cond["sell_conditions"],
        "buy_all_met": cond["buy_all_met"], "sell_all_met": cond["sell_all_met"],
        "atm_row": atm_row,
    }


def render_scalping_tab(fyers, df: pd.DataFrame, symbol: str, symbol_key: str, spot_price: float,
                         pcr: float, max_pain: float, atm_strike: float, intel: dict,
                         expiry_label: str, timeframe: str, live_mode: bool):
    """Renders the full AI Scalping Engine tab: timeframe-based candle
    fetch, futures snapshot, technical + OI + AI confidence computation,
    the AI Output panel, Trading Levels, Live Reasons, Warnings, and the
    strict condition checklists for both sides."""
    st.markdown(f"##### 🎯 AI Scalping Engine — {timeframe} Chart")

    resolution_map = {"1 min": "1", "3 min": "3", "5 min": "5"}
    resolution = resolution_map.get(timeframe, "5")

    with st.spinner("Fetching intraday candles, futures data & computing AI confidence..."):
        candle_df = fetch_candle_data(fyers, symbol, resolution=resolution, lookback_days=5)
        futures_candidates = get_futures_symbol_candidates(symbol_key)
        futures_snapshot = fetch_futures_snapshot(fyers, futures_candidates) if futures_candidates else {}
        futures = analyze_futures_buildup(futures_snapshot) i
Preview truncated for large file
