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

FIX NOTE (this file):
  render_scalping_html_table() previously looked up the column key
  "Gamma" (capital G) while compute_scalping_table() actually creates
  the column as lowercase "gamma". Because the lookup key didn't match
  any column, row.get("Gamma", "") silently fell back to an empty
  string, and then the numeric format spec "{:.5f}".format("") raised:
      ValueError: Unknown format code 'f' for object of type 'str'
  Fixed by changing the column key from "Gamma" to "gamma" in the
  `cols` list inside render_scalping_html_table (see section 5D below).

FIX NOTE 2 (this file):
  compute_ai_engine()'s _final_signal() and _final_recommendation()
  previously compared CE Score >= PE Score, which handed every tie (and
  therefore every near-tie) to CE. Because roughly 37% of the weight in
  both scores comes from identical shared inputs (proximity, highest
  OI/volume/ΔOI, max-pain distance), CE Score and PE Score land very
  close together whenever the market is balanced — and the old `>=`
  silently resolved that closeness in CE's favor every single time, so
  PE/SELL signals almost never surfaced even on bearish-leaning chains.
  Fixed by using strict `>` comparisons on both sides and only breaking
  a genuine tie using the strike's own Breakout vs Breakdown Probability
  instead of hardcoding a CE win (see section 5B below).
"""

import io
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np
from ai_analysis_engine import analyze_market
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

    # ── FIX: Final Signal — previously used `CE Score >= PE Score`, which
    # handed every tie (and near-tie, since ~37% of both formulas' weight
    # comes from identical shared inputs — proximity, highest OI/Volume/
    # ΔOI, max-pain distance) to CE every single time. That silently
    # suppressed PE/SELL signals on balanced or bearish-leaning chains.
    # Now both sides use strict `>` comparisons, and a genuine tie is
    # broken using the strike's own Breakout vs Breakdown Probability
    # instead of defaulting to CE.
    def _final_signal(row):
        ce, pe = row["CE Score"], row["PE Score"]
        if ce > pe:
            return f"CE · {row['CE Rating']}"
        if pe > ce:
            return f"PE · {row['PE Rating']}"
        if row.get("Breakdown Probability", 0) > row.get("Breakout Probability", 0):
            return f"PE · {row['PE Rating']}"
        return f"CE · {row['CE Rating']}"

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

    # ── FIX: Final Recommendation — same tie-break fix as Final Signal
    # above, applied to the star + side label used in the primary Big
    # Move Ready table (this is the "Final Recommendation" column).
    def _final_recommendation(row):
        ce, pe = row["CE Score"], row["PE Score"]
        if ce > pe:
            label, _ = rating_label_for_side(ce, "CE")
            return label
        if pe > ce:
            label, _ = rating_label_for_side(pe, "PE")
            return label
        if row.get("Breakdown Probability", 0) > row.get("Breakout Probability", 0):
            label, _ = rating_label_for_side(pe, "PE")
            return label
        label, _ = rating_label_for_side(ce, "CE")
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


def render_gamma_tab(gdf: pd.DataFrame, symbol: str, expiry_label: str,
                      live_mode: bool, audio_alert: bool):
    """Renders the full Gamma Build-up Analyzer tab: live status badge,
    summary panel, smart alerts, optional audio ping, and the blinking
    color-coded Gamma table. Does not touch or recompute anything from
    the rest of the dashboard.

    NOTE: `gdf` must already have add_gamma_columns() + compute_gamma_
    analysis() applied to it (this is done once per refresh in
    show_option_chain, then shared with the AI Scalping Engine tab too)
    — compute_gamma_analysis() updates the session-tracked "previous
    refresh" history as a side effect, so it must only run once per
    Streamlit script run or the Gamma Change %/Trend comparisons would
    be silently corrupted."""
    st.markdown("##### ⚡ Advanced Gamma Build-up Analyzer")

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
# 5D. AI SCALPING ENGINE  (multi-factor confirmation — EMA/VWAP/RSI/MACD/
#     ADX/ATR/Supertrend/India VIX from FYERS historical candles, combined
#     with the chain's own OI/Gamma/Build-up factors. Requires >= 8 of 13
#     confirmations in the same direction before showing a BUY/SELL signal
#     — otherwise stays at WAIT. This intentionally does NOT reuse or
#     alter the existing CE/PE AI Engine, Big Move Engine, or Gamma
#     Analyzer signal logic; it is a separate, additive engine.)
# ══════════════════════════════════════════════════════════════════════════

SCALP_BOS_HISTORY_KEY = "oc_scalp_last_bos_dir"
SCALP_ALERT_KEY_PREFIX = "oc_scalp_last_fire"
SCALP_MIN_CONFIRMATIONS = 8
SCALP_TOTAL_CHECKS = 13


def fetch_underlying_candles(fyers, symbol_candidates: list, resolution: str = "5",
                              lookback_days: int = 5) -> pd.DataFrame:
    """Fetches recent OHLCV candles for the underlying via the FYERS
    history endpoint, trying each candidate symbol format until one
    returns data (mirrors the same fallback pattern used for the option
    chain itself). Returns a DataFrame [time, open, high, low, close,
    volume] sorted chronologically, or an empty DataFrame if unavailable
    — the Scalping Engine degrades to WAIT-only rather than raising when
    historical candles can't be fetched (market closed, plan without
    history access, symbol not resolvable, etc.)."""
    end = datetime.now()
    start = end - timedelta(days=lookback_days)
    for sym in symbol_candidates:
        req = {
            "symbol": sym, "resolution": str(resolution), "date_format": "1",
            "range_from": start.strftime("%Y-%m-%d"), "range_to": end.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        }
        try:
            resp = fyers.history(data=req)
        except Exception:  # noqa: BLE001 - external SDK, keep resilient
            continue
        if not isinstance(resp, dict) or resp.get("s") != "ok":
            continue
        candles = resp.get("candles", [])
        if not candles:
            continue
        try:
            cdf = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume"])
            cdf["time"] = pd.to_datetime(cdf["time"], unit="s")
            cdf.sort_values("time", inplace=True)
            cdf.reset_index(drop=True, inplace=True)
            return cdf
        except Exception:  # noqa: BLE001
            continue
    return pd.DataFrame()


def fetch_india_vix(fyers):
    """Returns the latest India VIX value, or None if unavailable — VIX
    is informational context for the Scalping Engine, not a hard gate."""
    for sym in ("NSE:INDIAVIX-INDEX", "NSE:INDIA_VIX-INDEX"):
        try:
            resp = fyers.quotes(data={"symbols": sym})
            v = resp.get("d", [{}])[0].get("v", {}) if isinstance(resp, dict) else {}
            val = float(v.get("lp", 0) or 0)
            if val > 0:
                return val
        except Exception:  # noqa: BLE001
            continue
    return None


# ── Technical indicators (manual implementations — no external TA-lib
#    dependency, so this keeps working wherever the base dashboard runs) ──

def _ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def _rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast, ema_slow = _ema(series, fast), _ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = _ema(macd_line, signal)
    return macd_line, signal_line, macd_line - signal_line


def _atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def _adx(df: pd.DataFrame, n: int = 14):
    high, low = df["high"], df["low"]
    up_move, down_move = high.diff(), -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr = _atr(df, n)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr.replace(0, np.nan)
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    adx = dx.ewm(alpha=1 / n, adjust=False).mean()
    return adx.fillna(0.0), plus_di.fillna(0.0), minus_di.fillna(0.0)


def _supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    atr = _atr(df, period)
    hl2 = (df["high"] + df["low"]) / 2
    upperband, lowerband = hl2 + multiplier * atr, hl2 - multiplier * atr
    close = df["close"]
    n = len(df)
    final_upper, final_lower = upperband.copy(), lowerband.copy()
    direction = pd.Series(1, index=df.index, dtype="int64")
    trend = pd.Series(0.0, index=df.index, dtype="float64")
    for i in range(n):
        if i == 0:
            trend.iloc[i] = final_lower.iloc[i]
            continue
        final_upper.iloc[i] = min(upperband.iloc[i], final_upper.iloc[i - 1]) \
            if close.iloc[i - 1] <= final_upper.iloc[i - 1] else upperband.iloc[i]
        final_lower.iloc[i] = max(lowerband.iloc[i], final_lower.iloc[i - 1]) \
            if close.iloc[i - 1] >= final_lower.iloc[i - 1] else lowerband.iloc[i]
        if close.iloc[i] > final_upper.iloc[i - 1]:
            direction.iloc[i] = 1
        elif close.iloc[i] < final_lower.iloc[i - 1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i - 1]
        trend.iloc[i] = final_lower.iloc[i] if direction.iloc[i] == 1 else final_upper.iloc[i]
    return trend, direction


def _vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum().replace(0, np.nan)
    return (typical * df["volume"]).cumsum() / cum_vol


# ── Simplified price-action / SMC-style pattern detectors ────────────────
# These are heuristic proxies for their named concepts, built only from
# FYERS OHLCV candles (no order-book/depth data is available), and are
# meant to add directional context — not to be a certified SMC engine.

def _swing_points(df: pd.DataFrame, lookback: int = 3):
    highs, lows = df["high"], df["low"]
    window = lookback * 2 + 1
    swing_high = highs == highs.rolling(window, center=True).max()
    swing_low = lows == lows.rolling(window, center=True).min()
    return swing_high.fillna(False), swing_low.fillna(False)


def _detect_bos_choch(df: pd.DataFrame) -> dict:
    """Break of Structure: latest close beyond the most recent confirmed
    swing high/low. Change of Character: the break direction flips from
    the last confirmed break (tracked in session_state across refreshes)."""
    if len(df) < 10:
        return {"bos_bull": False, "bos_bear": False, "bos_label": "None", "choch_label": "None"}
    swing_high, swing_low = _swing_points(df)
    last_swing_high_val = df.loc[swing_high, "high"].iloc[-1] if swing_high.any() else None
    last_swing_low_val = df.loc[swing_low, "low"].iloc[-1] if swing_low.any() else None
    last_close = df["close"].iloc[-1]

    bos_bull = last_swing_high_val is not None and last_close > last_swing_high_val
    bos_bear = last_swing_low_val is not None and last_close < last_swing_low_val

    prev_dir = st.session_state.get(SCALP_BOS_HISTORY_KEY)
    cur_dir = "bull" if bos_bull else ("bear" if bos_bear else prev_dir)
    choch = prev_dir is not None and cur_dir is not None and cur_dir != prev_dir and (bos_bull or bos_bear)
    if bos_bull or bos_bear:
        st.session_state[SCALP_BOS_HISTORY_KEY] = cur_dir

    bos_label = "Bullish BOS" if bos_bull else ("Bearish BOS" if bos_bear else "None")
    choch_label = "None"
    if choch:
        choch_label = "Bullish CHOCH" if cur_dir == "bull" else "Bearish CHOCH"
    return {"bos_bull": bos_bull, "bos_bear": bos_bear, "bos_label": bos_label, "choch_label": choch_label}


def _detect_order_block(df: pd.DataFrame):
    """Simplified Order Block: the last opposite-colour candle immediately
    preceding a stronger same-direction impulse candle."""
    if len(df) < 3:
        return False, False, "None"
    c = df.iloc[-3:]
    bodies = c["close"] - c["open"]
    bullish_ob = bool(bodies.iloc[0] < 0 and bodies.iloc[1] > 0 and bodies.iloc[2] > 0
                      and abs(bodies.iloc[1]) > abs(bodies.iloc[0]))
    bearish_ob = bool(bodies.iloc[0] > 0 and bodies.iloc[1] < 0 and bodies.iloc[2] < 0
                      and abs(bodies.iloc[1]) > abs(bodies.iloc[0]))
    label = "Bullish OB" if bullish_ob else ("Bearish OB" if bearish_ob else "None")
    return bullish_ob, bearish_ob, label


def _detect_fvg(df: pd.DataFrame) -> str:
    """3-candle Fair Value Gap on the most recent completed window:
    candle[-3].high < candle[-1].low (bullish gap) or
    candle[-3].low > candle[-1].high (bearish gap)."""
    if len(df) < 3:
        return "None"
    a, c = df.iloc[-3], df.iloc[-1]
    if a["high"] < c["low"]:
        return "Bullish FVG"
    if a["low"] > c["high"]:
        return "Bearish FVG"
    return "None"


def _detect_liquidity_sweep(df: pd.DataFrame, lookback: int = 10) -> str:
    """Flags a liquidity sweep when the latest candle wicks beyond the
    recent swing high/low but closes back inside the prior range."""
    if len(df) < lookback + 1:
        return "None"
    window = df.iloc[-(lookback + 1):-1]
    last = df.iloc[-1]
    if last["high"] > window["high"].max() and last["close"] < window["high"].max():
        return "Sell-side Sweep (Bearish)"
    if last["low"] < window["low"].min() and last["close"] > window["low"].min():
        return "Buy-side Sweep (Bullish)"
    return "None"


def _prev_day_high_low(df: pd.DataFrame):
    if df.empty:
        return None, None
    d = df.copy()
    d["date"] = d["time"].dt.date
    days = sorted(d["date"].unique())
    if len(days) < 2:
        return None, None
    prev_df = d[d["date"] == days[-2]]
    return float(prev_df["high"].max()), float(prev_df["low"].min())


def _support_resistance_from_candles(df: pd.DataFrame, lookback: int = 50):
    if df.empty:
        return None, None
    recent = df.tail(lookback)
    return float(recent["low"].min()), float(recent["high"].max())


def compute_scalping_trend_engine(fyers, symbol_candidates: list, df: pd.DataFrame, gdf: pd.DataFrame) -> dict:
    """
    Builds the underlying-level multi-factor confirmation engine: EMA9/
    20/50, VWAP, RSI (5m & 15m), MACD, ADX, ATR, Supertrend, India VIX,
    previous-day high/low, support/resistance, Order Block, Liquidity
    Sweep, FVG and BOS/CHOCH from FYERS historical candles — combined
    with the option chain's own aggregate OI/Gamma/Build-up direction
    (from `df`/`gdf`, already computed elsewhere in this file) into
    exactly the 13 BUY and 13 SELL confirmation checks specified for
    this engine.

    Every confirmation fails safe toward "not counted" on missing data —
    this can only push the result toward WAIT, never force a BUY/SELL.
    Returns a dict; `available` is False if there isn't enough candle
    history to compute the indicators at all.
    """
    result = {"available": False, "buy_confirmations": [], "sell_confirmations": [],
              "buy_count": 0, "sell_count": 0}

    c5 = fetch_underlying_candles(fyers, symbol_candidates, resolution="5", lookback_days=5)
    if c5.empty or len(c5) < 30:
        return result
    c15 = fetch_underlying_candles(fyers, symbol_candidates, resolution="15", lookback_days=10)

    close5 = c5["close"]
    ema9, ema20, ema50 = _ema(close5, 9), _ema(close5, 20), _ema(close5, 50)
    rsi5 = _rsi(close5, 14)
    rsi15 = _rsi(c15["close"], 14) if not c15.empty and len(c15) >= 20 else pd.Series([50.0])
    macd_line, macd_signal, _ = _macd(close5)
    adx, _, _ = _adx(c5, 14)
    atr = _atr(c5, 14)
    supertrend, st_dir = _supertrend(c5, 10, 3.0)
    vwap = _vwap(c5)
    vix = fetch_india_vix(fyers)
    prev_high, prev_low = _prev_day_high_low(c5)
    support, resistance = _support_resistance_from_candles(c5)
    bos_info = _detect_bos_choch(c5)
    ob_bull, ob_bear, ob_label = _detect_order_block(c5)
    fvg_label = _detect_fvg(c5)
    sweep_label = _detect_liquidity_sweep(c5)

    last_close = float(close5.iloc[-1])
    last_vwap = float(vwap.iloc[-1]) if not vwap.empty and pd.notna(vwap.iloc[-1]) else last_close
    last_ema9, last_ema20, last_ema50 = float(ema9.iloc[-1]), float(ema20.iloc[-1]), float(ema50.iloc[-1])
    last_rsi5, last_rsi15 = float(rsi5.iloc[-1]), float(rsi15.iloc[-1])
    last_macd, last_macd_sig = float(macd_line.iloc[-1]), float(macd_signal.iloc[-1])
    last_adx, last_atr = float(adx.iloc[-1]), float(atr.iloc[-1])
    last_st_dir = int(st_dir.iloc[-1]) if len(st_dir) else 0
    volume_spike = bool(len(c5) >= 20 and c5["volume"].iloc[-1] > c5["volume"].tail(20).mean() * 1.5)

    # ── Aggregate chain-side factors (from df / gdf, computed elsewhere) ─
    total_ce_chng = float(df.get("ce_chng_oi", pd.Series(dtype=float)).sum())
    total_pe_chng = float(df.get("pe_chng_oi", pd.Series(dtype=float)).sum())
    avg_gamma_change = float(gdf["Gamma Change"].mean()) if "Gamma Change" in gdf.columns and len(gdf) else 0.0
    ce_build = df.get("CE Build-up", pd.Series(dtype=object))
    pe_build = df.get("PE Build-up", pd.Series(dtype=object))
    pe_long_buildup_dominant = (pe_build == "Long Build-up (Put Writing)").sum() > (pe_build == "Long Unwinding").sum()
    pe_long_unwinding_dominant = (pe_build == "Long Unwinding").sum() > (pe_build == "Long Build-up (Put Writing)").sum()
    ce_short_covering_dominant = (ce_build == "Short Covering").sum() > (ce_build == "Short Build-up (Call Writing)").sum()
    ce_short_buildup_dominant = (ce_build == "Short Build-up (Call Writing)").sum() > (ce_build == "Short Covering").sum()

    # ── Exactly the 13 BUY / 13 SELL confirmations specified ─────────────
    buy_checks = {
        "Price above VWAP": last_close > last_vwap,
        "EMA9 > EMA20 > EMA50": last_ema9 > last_ema20 > last_ema50,
        "RSI(5m) > 60": last_rsi5 > 60,
        "MACD Bullish": last_macd > last_macd_sig,
        "ADX > 25": last_adx > 25,
        "PE OI increasing": total_pe_chng > 0,
        "CE OI decreasing": total_ce_chng < 0,
        "Positive Gamma": avg_gamma_change > 0,
        "Long Build-up": bool(pe_long_buildup_dominant),
        "Short Covering": bool(ce_short_covering_dominant),
        "Bullish Order Block": ob_bull,
        "Bullish BOS": bos_info["bos_bull"],
        "Volume Spike": volume_spike,
    }
    sell_checks = {
        "Price below VWAP": last_close < last_vwap,
        "EMA9 < EMA20 < EMA50": last_ema9 < last_ema20 < last_ema50,
        "RSI(5m) < 40": last_rsi5 < 40,
        "MACD Bearish": last_macd < last_macd_sig,
        "ADX > 25": last_adx > 25,
        "CE OI increasing": total_ce_chng > 0,
        "PE OI decreasing": total_pe_chng < 0,
        "Negative Gamma": avg_gamma_change < 0,
        "Short Build-up": bool(ce_short_buildup_dominant),
        "Long Unwinding": bool(pe_long_unwinding_dominant),
        "Bearish Order Block": ob_bear,
        "Bearish BOS": bos_info["bos_bear"],
        "Volume Spike": volume_spike,
    }

    result.update({
        "available": True,
        "last_close": last_close, "vwap": last_vwap,
        "ema9": last_ema9, "ema20": last_ema20, "ema50": last_ema50,
        "rsi5": last_rsi5, "rsi15": last_rsi15,
        "macd": last_macd, "macd_signal": last_macd_sig,
        "adx": last_adx, "atr": last_atr, "supertrend_dir": last_st_dir,
        "vix": vix, "prev_high": prev_high, "prev_low": prev_low,
        "support": support, "resistance": resistance,
        "bos_label": bos_info["bos_label"], "choch_label": bos_info["choch_label"],
        "order_block_label": ob_label, "fvg_label": fvg_label, "sweep_label": sweep_label,
        "buy_checks": buy_checks, "sell_checks": sell_checks,
        "buy_confirmations": [k for k, v in buy_checks.items() if v],
        "sell_confirmations": [k for k, v in sell_checks.items() if v],
        "buy_count": int(sum(buy_checks.values())),
        "sell_count": int(sum(sell_checks.values())),
        "candle_time": c5["time"].iloc[-1],
    })
    return result


def determine_scalping_signal(buy_count: int, sell_count: int,
                               min_confirmations: int = SCALP_MIN_CONFIRMATIONS) -> str:
    """Only returns a BUY/SELL signal once one side reaches the minimum
    confirmation count AND leads the other side; otherwise WAIT."""
    if buy_count >= min_confirmations and buy_count > sell_count:
        return "🟢 STRONG SCALP BUY" if buy_count >= min_confirmations + 3 else "🟢 SCALP BUY"
    if sell_count >= min_confirmations and sell_count > buy_count:
        return "🔴 STRONG SCALP SELL" if sell_count >= min_confirmations + 3 else "🔴 SCALP SELL"
    return "🟡 WAIT"


def scalping_confidence_pct(buy_count: int, sell_count: int, total_checks: int = SCALP_TOTAL_CHECKS) -> float:
    return round(min(max(buy_count, sell_count) / total_checks, 1.0) * 100, 1)


def confidence_stars(pct: float) -> str:
    if pct >= 90:
        return "★★★★★"
    if pct >= 80:
        return "★★★★☆"
    if pct >= 70:
        return "★★★☆☆"
    return "NO TRADE"


def compute_scalping_table(df: pd.DataFrame, gdf: pd.DataFrame, trend_engine: dict,
                            min_confirmations: int = SCALP_MIN_CONFIRMATIONS) -> pd.DataFrame:
    """Builds the per-strike AI Scalping table: the overall multi-factor
    Signal/Confidence apply chain-wide (the underlying trend doesn't
    differ strike to strike), while Entry/SL/Targets/Gamma/Gamma Change/
    Delta OI/Volume Strength/Build-up columns are per-strike, pulled from
    `df` (chain snapshot) and `gdf` (gamma-analyzed chain, computed once
    per refresh in show_option_chain).

    NOTE: this function always creates the column named lowercase
    "gamma" (matching gdf's own column name), never "Gamma" — the
    corresponding renderer (render_scalping_html_table, section 5D
    below) must look up "gamma" too, or the format-string call on a
    missing/blank value raises ValueError: Unknown format code 'f' for
    object of type 'str'."""
    d = df.copy()

    if not trend_engine.get("available") or d.empty:
        d["AI Scalping Signal"] = "🟡 WAIT"
        d["Confidence %"] = 0.0
        d["Confidence Stars"] = "NO TRADE"
        for col in ("Entry Price", "Stop Loss", "Target 1", "Target 2"):
            d[col] = 0.0
        d["Risk Reward"] = "—"
        d["gamma"] = gdf["gamma"] if "gamma" in gdf.columns else 0.0
        d["Gamma Change"] = gdf["Gamma Change"] if "Gamma Change" in gdf.columns else 0.0
        d["Delta OI"] = d.get("ce_chng_oi", 0) - d.get("pe_chng_oi", 0)
        d["Volume Strength"] = "—"
        d["Long Build-up"] = d.get("PE Build-up", "—")
        d["Short Build-up"] = d.get("CE Build-up", "—")
        d["Order Block"] = trend_engine.get("order_block_label", "None")
        d["BOS"] = trend_engine.get("bos_label", "None")
        d["CHOCH"] = trend_engine.get("choch_label", "None")
        d["Trend Strength"] = "—"
        return d

    buy_count, sell_count = trend_engine["buy_count"], trend_engine["sell_count"]
    signal = determine_scalping_signal(buy_count, sell_count, min_confirmations)
    confidence_pct = scalping_confidence_pct(buy_count, sell_count)
    stars = confidence_stars(confidence_pct)

    gamma_cols = gdf.set_index("strike_price")[["gamma", "Gamma Change"]] if "gamma" in gdf.columns else None
    if gamma_cols is not None and not gamma_cols.index.duplicated().any():
        d = d.merge(gamma_cols, on="strike_price", how="left")
    else:
        d["gamma"], d["Gamma Change"] = 0.0, 0.0
    d["gamma"] = d["gamma"].fillna(0.0)
    d["Gamma Change"] = d["Gamma Change"].fillna(0.0)

    d["AI Scalping Signal"] = signal
    d["Confidence %"] = confidence_pct
    d["Confidence Stars"] = stars
    d["Delta OI"] = d.get("ce_chng_oi", 0) - d.get("pe_chng_oi", 0)

    total_vol = d.get("ce_volume", pd.Series(0, index=d.index)) + d.get("pe_volume", pd.Series(0, index=d.index))
    vol_thresh_strong = total_vol.quantile(0.85) if len(total_vol) and total_vol.max() > 0 else 0
    vol_thresh_med = total_vol.quantile(0.6) if len(total_vol) and total_vol.max() > 0 else 0

    def _vol_strength(v):
        if vol_thresh_strong > 0 and v >= vol_thresh_strong:
            return "Strong"
        if vol_thresh_med > 0 and v >= vol_thresh_med:
            return "Medium"
        return "Weak"

    d["Volume Strength"] = total_vol.apply(_vol_strength)
    d["Long Build-up"] = d.get("PE Build-up", "—")
    d["Short Build-up"] = d.get("CE Build-up", "—")
    d["Order Block"] = trend_engine.get("order_block_label", "None")
    d["BOS"] = trend_engine.get("bos_label", "None")
    d["CHOCH"] = trend_engine.get("choch_label", "None")
    d["Trend Strength"] = "Strong" if trend_engine.get("adx", 0) > 25 else "Weak"

    is_buy_type, is_sell_type = "BUY" in signal, "SELL" in signal

    def _entry_row(row):
        if is_buy_type:
            return row.get("ce_ltp", 0)
        if is_sell_type:
            return row.get("pe_ltp", 0)
        return 0.0

    d["Entry Price"] = d.apply(_entry_row, axis=1).round(2)
    d["Stop Loss"] = (d["Entry Price"] * 0.85).round(2)
    d["Target 1"] = (d["Entry Price"] * 1.15).round(2)
    d["Target 2"] = (d["Entry Price"] * 1.30).round(2)

    def _rr(row):
        entry, sl, t1 = row["Entry Price"], row["Stop Loss"], row["Target 1"]
        if entry <= 0:
            return "—"
        risk = max(entry - sl, 0.01)
        reward = t1 - entry
        return f"1 : {round(reward / risk, 2)}" if risk > 0 else "—"

    d["Risk Reward"] = d.apply(_rr, axis=1)
    zero_entry_mask = d["Entry Price"] <= 0
    d.loc[zero_entry_mask, ["Entry Price", "Stop Loss", "Target 1", "Target 2"]] = 0.0

    # Sort so the most actionable strikes (closest to spot / highest
    # gamma) surface first, without touching any existing sort elsewhere.
    if "gamma" in d.columns:
        d = d.sort_values("gamma", ascending=False).reset_index(drop=True)
    return d


SCALP_ROW_CSS = {
    "strongbuy": "gamma-row-strongbuy", "buy": "gamma-row-buy", "hold": "gamma-row-hold",
    "sell": "gamma-row-sell", "strongsell": "gamma-row-strongsell",
}


def _scalp_band(signal: str) -> str:
    return {
        "🟢 STRONG SCALP BUY": "strongbuy", "🟢 SCALP BUY": "buy",
        "🔴 STRONG SCALP SELL": "strongsell", "🔴 SCALP SELL": "sell",
    }.get(signal, "hold")


def render_scalping_html_table(df: pd.DataFrame, top_n: int = 40) -> str:
    """Custom HTML table (not st.dataframe) so the same CSS blink
    keyframes used by the Gamma tab can animate Strong Buy/Sell rows.

    FIX: the "Gamma" column key below is now lowercase "gamma" to match
    the actual column name created in compute_scalping_table(). The
    previous mismatched capital-G key caused row.get("Gamma", "") to
    silently fall back to an empty string, and the subsequent
    "{:.5f}".format("") call raised:
        ValueError: Unknown format code 'f' for object of type 'str'
    """
    cols = [
        ("strike_price", "Strike", "{:,.0f}"),
        ("AI Scalping Signal", "Signal", None),
        ("Confidence %", "Conf %", "{:.1f}%"),
        ("Confidence Stars", "Rating", None),
        ("Entry Price", "Entry", "{:.2f}"),
        ("Stop Loss", "SL", "{:.2f}"),
        ("Target 1", "T1", "{:.2f}"),
        ("Target 2", "T2", "{:.2f}"),
        ("Risk Reward", "RR", None),
        ("gamma", "Gamma", "{:.5f}"),               # <-- FIXED: was ("Gamma", "Gamma", "{:.5f}")
        ("Gamma Change", "Gamma Chg", "{:+.5f}"),
        ("Delta OI", "Delta OI", "{:+,.0f}"),
        ("Volume Strength", "Vol Strength", None),
        ("Long Build-up", "Long BU", None),
        ("Short Build-up", "Short BU", None),
        ("Order Block", "Order Block", None),
        ("BOS", "BOS", None),
        ("CHOCH", "CHOCH", None),
        ("Trend Strength", "Trend", None),
    ]
    view = df.head(top_n)
    rows_html = []
    for _, row in view.iterrows():
        band = _scalp_band(row.get("AI Scalping Signal", "🟡 WAIT"))
        css = SCALP_ROW_CSS.get(band, "gamma-row-hold")
        blink = ""
        if band == "strongbuy":
            blink = " gamma-row-blink-green"
        elif band == "strongsell":
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


def fire_scalping_alert(trend_engine: dict, symbol: str, signal: str, audio_alert: bool):
    """Blinking banner is handled in render_scalping_tab; this fires the
    popup + optional audio ping only when the Signal changes AND it's a
    new candle — never repeats for the same candle even across reruns."""
    candle_time = trend_engine.get("candle_time")
    key = f"{SCALP_ALERT_KEY_PREFIX}|{symbol}"
    last = st.session_state.get(key)
    cur = (str(candle_time), signal) if candle_time is not None else None
    if cur is None:
        return
    if last != cur:
        st.session_state[key] = cur
        if signal != "🟡 WAIT" and (last is None or last[0] == cur[0] or last[1] != signal):
            try:
                st.toast(f"AI Scalping: {signal}", icon="🎯")
            except Exception:  # noqa: BLE001
                st.info(f"AI Scalping: {signal}")
            if audio_alert:
                st.audio(io.BytesIO(__import__("base64").b64decode(_GAMMA_PING_WAV_B64)), format="audio/wav")


def render_scalping_tab(scalp_df: pd.DataFrame, trend_engine: dict, symbol: str, audio_alert: bool):
    """Renders the AI Scalping Engine tab: overall blinking signal banner,
    the full indicator readout, the BUY/SELL confirmation checklist, and
    the per-strike Scalping table."""
    st.markdown("##### 🎯 AI Scalping Engine — Multi-Factor Confirmation")

    if not trend_engine.get("available"):
        st.warning(
            "Historical candle data isn't available right now (market closed, a plan without "
            "history access, or the FYERS history call failed for every symbol variant tried). "
            "The Scalping Engine needs 5m/15m candles for EMA/RSI/MACD/ADX/Supertrend and stays "
            "at 🟡 WAIT until that data is available — it will never force a BUY/SELL without it."
        )
        return

    signal = scalp_df["AI Scalping Signal"].iloc[0] if len(scalp_df) else "🟡 WAIT"
    band = _scalp_band(signal)
    blink_class = "gamma-row-blink-green" if band == "strongbuy" else (
        "gamma-row-blink-red" if band == "strongsell" else "")

    fire_scalping_alert(trend_engine, symbol, signal, audio_alert)

    st.markdown(
        f"""<div class="intel-card {blink_class}" style="text-align:center;">
        <div class="intel-label">Overall Scalping Signal</div>
        <div class="intel-value" style="font-size:26px;">{signal}</div>
        <div style="color:#8b949e;font-size:12px;margin-top:4px;">
            Confidence {scalp_df['Confidence %'].iloc[0]:.1f}%  ·  {scalp_df['Confidence Stars'].iloc[0]}
            &nbsp;|&nbsp; BUY confirmations {trend_engine['buy_count']}/{SCALP_TOTAL_CHECKS}
            &nbsp;|&nbsp; SELL confirmations {trend_engine['sell_count']}/{SCALP_TOTAL_CHECKS}
        </div>
        </div>""",
        unsafe_allow_html=True,
    )

    st.markdown("<br>", unsafe_allow_html=True)
    i1, i2, i3, i4 = st.columns(4)
    i1.metric("EMA 9 / 20 / 50", f"{trend_engine['ema9']:.1f} / {trend_engine['ema20']:.1f} / {trend_engine['ema50']:.1f}")
    i2.metric("VWAP", f"{trend_engine['vwap']:.1f}")
    i3.metric("RSI (5m / 15m)", f"{trend_engine['rsi5']:.1f} / {trend_engine['rsi15']:.1f}")
    i4.metric("ADX", f"{trend_engine['adx']:.1f}")

    i5, i6, i7, i8 = st.columns(4)
    i5.metric("MACD / Signal", f"{trend_engine['macd']:.2f} / {trend_engine['macd_signal']:.2f}")
    i6.metric("ATR", f"{trend_engine['atr']:.2f}")
    i7.metric("Supertrend", "🟢 Bullish" if trend_engine["supertrend_dir"] == 1 else "🔴 Bearish")
    i8.metric("India VIX", f"{trend_engine['vix']:.2f}" if trend_engine.get("vix") else "—")

    i9, i10, i11, i12 = st.columns(4)
    i9.metric("Prev Day High / Low",
              f"{trend_engine['prev_high']:,.0f} / {trend_engine['prev_low']:,.0f}" if trend_engine.get("prev_high") else "—")
    i10.metric("Support / Resistance",
               f"{trend_engine['support']:,.0f} / {trend_engine['resistance']:,.0f}" if trend_engine.get("support") else "—")
    i11.metric("Order Block / FVG", f"{trend_engine['order_block_label']} / {trend_engine['fvg_label']}")
    i12.metric("BOS / CHOCH", f"{trend_engine['bos_label']} / {trend_engine['choch_label']}")

    st.markdown("<br>", unsafe_allow_html=True)
    conf_col1, conf_col2 = st.columns(2)
    with conf_col1:
        st.markdown("**🟢 BUY confirmations met**")
        if trend_engine["buy_confirmations"]:
            st.markdown("\n".join(f"- {c}" for c in trend_engine["buy_confirmations"]))
        else:
            st.caption("None currently met.")
    with conf_col2:
        st.markdown("**🔴 SELL confirmations met**")
        if trend_engine["sell_confirmations"]:
            st.markdown("\n".join(f"- {c}" for c in trend_engine["sell_confirmations"]))
        else:
            st.caption("None currently met.")

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="block-title">📋 AI Scalping Table (per strike)</div>', unsafe_allow_html=True)
    st.markdown(render_scalping_html_table(scalp_df), unsafe_allow_html=True)
    st.caption(
        f"A BUY/SELL Scalping Signal requires at least {SCALP_MIN_CONFIRMATIONS} of {SCALP_TOTAL_CHECKS} "
        "confirmations in the same direction (VWAP, EMA alignment, RSI, MACD, ADX, CE/PE OI direction, "
        "Gamma direction, Build-up/Covering/Unwinding, Order Block, BOS, Volume Spike); otherwise it "
        "stays at 🟡 WAIT. Confidence % and the ★ rating are scored independently against the "
        f"{SCALP_TOTAL_CHECKS}-point checklist and can sit below the signal's own confirmation bar — "
        "treat a 'NO TRADE' star rating as a caution flag even when a Scalp signal is showing. Order "
        "Block / FVG / Liquidity Sweep / BOS / CHOCH are simplified heuristic pattern proxies from "
        "FYERS candle data, not a certified SMC engine. This is a positioning read, not financial "
        "advice — always confirm with live price action and manage your own risk."
    )


# ══════════════════════════════════════════════════════════════════════════
# 9. MAIN DASHBOARD
# ══════════════════════════════════════════════════════════════════════════

def show_option_chain(fyers):
    st.markdown("## 📊 Master Options Chain Dashboard")

    # ── Sidebar ──────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("### ⚙️ Configuration")
        option_type = st.radio("Instrument Type", ["Indices", "F&O Stocks"], key="instr_type_radio")
        is_stock = option_type == "F&O Stocks"

        if not is_stock:
            selected_key = st.selectbox("Index", list(INDEX_SYMBOL_CANDIDATES.keys()))
            symbol_candidates = INDEX_SYMBOL_CANDIDATES[selected_key]
            symbol_key = selected_key
        else:
            stock = st.text_input("Stock Symbol (e.g. RELIANCE, TCS, INFY, SBIN, ICICIBANK, HDFCBANK)", "RELIANCE")
            symbol_candidates = get_stock_symbol_candidates(stock)
            symbol_key = symbol_candidates[0]

        # Auto-reload expiry list whenever the chosen instrument changes —
        # no manual timestamp editing required. We fetch the RAW expiry
        # payload here too, and store it untouched for on-screen inspection
        # — no assumption is made about which instruments are weekly vs
        # monthly-only; whatever the API returns is what gets shown.
        if st.session_state.get("oc_current_symbol_key") != symbol_key:
            st.session_state["oc_current_symbol_key"] = symbol_key
            st.session_state["oc_expiry_list"] = []
            st.session_state["oc_raw_expiry_payload"] = []
            st.session_state.pop("oc_df", None)
            with st.spinner("Loading expiry dates …"):
                expiry_list, _used, raw_payload = fetch_expiry_list(fyers, symbol_candidates)
            st.session_state["oc_expiry_list"] = expiry_list
            st.session_state["oc_raw_expiry_payload"] = raw_payload

        max_strikes = 20 if is_stock else 30
        strike_count = st.slider("Strikes Around ATM", 5, max_strikes, min(20, max_strikes), step=5)

        expiry_options = st.session_state.get("oc_expiry_list", [])
        raw_expiry_payload = st.session_state.get("oc_raw_expiry_payload", [])

        # ── Raw API expiry inspection (always shown, never filtered) ────
        with st.expander("🔍 Raw FYERS Expiry Response (debug/inspection)", expanded=False):
            st.markdown(f"**Number of expiries returned by API:** {len(raw_expiry_payload)}")
            if len(raw_expiry_payload) == 1:
                st.warning("Only one expiry is available from FYERS API.")
            elif len(raw_expiry_payload) == 0:
                st.caption("No expiry data returned yet — select an instrument or click Retry below.")
            st.markdown("**Raw `expiryData` payload (exactly as received):**")
            st.json(raw_expiry_payload if raw_expiry_payload else {})
            st.markdown("**Parsed expiry list (label, timestamp) — every expiry above, unfiltered:**")
            st.write(expiry_options if expiry_options else "—")

        if expiry_options:
            tagged = classify_expiries(expiry_options)  # (label, ts, tag) — every expiry preserved, nearest-first
            display_labels = [f"{label}  ·  {tag}" for label, ts, tag in tagged]
            label_to_ts = {f"{label}  ·  {tag}": ts for label, ts, tag in tagged}
            label_to_plain = {f"{label}  ·  {tag}": label for label, ts, tag in tagged}
            selected_display = st.selectbox(
                "Expiry (Weekly / Monthly — nearest selected by default)", display_labels, index=0
            )
            expiry_timestamp = label_to_ts.get(selected_display, "")
            selected_expiry_label = label_to_plain.get(selected_display, "")

            if len(expiry_options) == 1:
                st.info("Only one expiry is available from FYERS API.")

            # Data-driven check against what THIS fetch actually returned —
            # not a hard-coded per-index assumption. If none of the
            # expiries the API returned classify as "Weekly" (classify_
            # expiries logic is unmodified), the FYERS API is not currently
            # listing a weekly contract for this instrument.
            has_weekly = any(tag == "Weekly" for _, _, tag in tagged)
            if not has_weekly:
                st.warning("Weekly expiries are not available from the FYERS API for this instrument.")

            st.caption(
                f"Selected expiry: **{selected_expiry_label}**  |  "
                f"API request expiry (timestamp sent to FYERS): **{expiry_timestamp or '—'}**"
            )
        else:
            st.caption("⏳ Fetching available expiry dates for this instrument …")
            expiry_timestamp = ""
            selected_expiry_label = ""
            if st.button("🔁 Retry Loading Expiry List"):
                with st.spinner("Loading expiry dates …"):
                    expiry_list, _used, raw_payload = fetch_expiry_list(fyers, symbol_candidates)
                st.session_state["oc_expiry_list"] = expiry_list
                st.session_state["oc_raw_expiry_payload"] = raw_payload
                st.rerun()

        ai_min_conf = st.slider("AI Min Confidence % (Trade Signals)", 50, 95, 80, step=5)
        debug_mode = st.checkbox("Show raw API response (debug)", value=False)
        st.divider()
        fetch_btn = st.button("🔄 Fetch Live Data", use_container_width=True, type="primary")

        st.divider()
        st.markdown("### ⚡ Gamma Build-up Analyzer")
        gamma_live_mode = st.checkbox(
            "Enable Live Gamma Auto-Refresh (5s)", value=False, key="oc_gamma_live_mode",
            help="When enabled, the Gamma Build-up tab re-runs every 5 seconds so Gamma Change / "
                 "Trend / Signal are compared against the previous refresh.",
        )
        gamma_audio_alert = st.checkbox("🔔 Audio ping on new Gamma signal", value=False, key="oc_gamma_audio")

        st.divider()
        st.markdown("### 🎯 AI Scalping Engine")
        scalping_enabled = st.checkbox(
            "Enable AI Scalping Engine (fetches 5m/15m candles)", value=False, key="oc_scalping_enabled",
            help="Multi-factor confirmation engine (EMA/VWAP/RSI/MACD/ADX/Supertrend + chain OI/Gamma). "
                 "Off by default since it makes extra FYERS history/quote API calls.",
        )
        scalping_audio_alert = st.checkbox("🔔 Audio ping on new Scalping signal", value=False, key="oc_scalping_audio")

    # ── Fetch & Process ─────────────────────────────────────────────────
    if fetch_btn:
        with st.spinner("Connecting to Fyers API …"):
            response, used_symbol, attempts = fetch_optionchain_with_fallback(
                fyers, symbol_candidates, strike_count, expiry_timestamp
            )
            # If the selected expiry itself returns no usable data, walk
            # forward through the remaining expiries automatically instead
            # of failing outright. This never drops an expiry from the
            # dropdown — it only changes which one is actively displayed
            # if the chosen one comes back empty.
            if (not response or response.get("s") != "ok") and expiry_options:
                for _, ts in expiry_options:
                    if ts == expiry_timestamp:
                        continue
                    response, used_symbol, attempts = fetch_optionchain_with_fallback(
                        fyers, symbol_candidates, strike_count, ts
                    )
                    if response and response.get("s") == "ok":
                        expiry_timestamp = ts
                        selected_expiry_label = format_expiry_label(ts)
                        st.info(f"Selected expiry had no data — automatically switched to {selected_expiry_label}.")
                        break

        if debug_mode:
            st.write("**Symbols tried:**", attempts)
            st.json(response if response else {})

        if not response:
            st.error("API call failed for all symbol variants tried. Check your Fyers connection/token.")
            return

        if response.get("s") != "ok":
            err_code = response.get("code", "—")
            err_msg = response.get("message", "No data returned")
            st.error(
                f"API Error (code {err_code}): {err_msg}\n\n"
                f"Tried: {', '.join(s for s, _ in attempts)}. "
                "If this is a stock, confirm it actually has active F&O contracts on NSE — "
                "not every stock has listed options."
            )
            return

        symbol = used_symbol

        # Re-derive both the raw and parsed expiry lists straight from this
        # fetch's response too, so the sidebar inspection panel always
        # reflects the most recent API payload for the chosen instrument —
        # every expiry present is kept, nothing is filtered out.
        new_raw_payload = extract_raw_expiry_payload(response)
        new_expiry_list = extract_expiry_list(response)
        if new_expiry_list:
            st.session_state["oc_expiry_list"] = new_expiry_list
        if new_raw_payload:
            st.session_state["oc_raw_expiry_payload"] = new_raw_payload

        options_data, data = extract_options_data(response)
        spot_price = extract_spot_price(response, data)

        if not spot_price:
            try:
                quote_resp = fyers.quotes(data={"symbols": symbol})
                q = quote_resp.get("d", [{}])[0].get("v", {}) if isinstance(quote_resp, dict) else {}
                spot_price = float(q.get("lp", 0) or 0)
            except Exception:
                pass

        if not options_data:
            st.warning(
                "⚠️ No options data returned for this symbol. This can mean: the market is closed, "
                "the symbol/strike count combination is invalid, or the API response uses a different "
                "field name than expected. Enable **'Show raw API response'** in the sidebar and "
                "re-fetch to inspect the actual payload."
            )
            return

        df = normalize_chain_shape(options_data)
        df = ensure_numeric_columns(df)
        df.sort_values("strike_price", inplace=True)
        df.reset_index(drop=True, inplace=True)
        df = compute_strike_bias(df)
        df = add_iv_columns(df, spot_price, selected_expiry_label)

        st.session_state["oc_df"] = df
        st.session_state["oc_spot"] = spot_price
        st.session_state["oc_symbol"] = symbol
        st.session_state["oc_expiry_label"] = selected_expiry_label
        st.session_state["oc_expiry_timestamp"] = expiry_timestamp
        st.session_state["oc_ai_min_conf"] = ai_min_conf

    # ── Render from session_state (persists across reruns/tab switches) ─
    if "oc_df" not in st.session_state:
        st.info("👈 Choose an instrument in the sidebar and click **Fetch Live Data**.")
        return

    df = st.session_state["oc_df"]
    spot_price = st.session_state["oc_spot"]
    symbol = st.session_state.get("oc_symbol", "")
    expiry_label = st.session_state.get("oc_expiry_label", "")
    request_expiry_ts = st.session_state.get("oc_expiry_timestamp", "")
    ai_min_conf = st.session_state.get("oc_ai_min_conf", ai_min_conf)

    if df.empty:
        st.warning("No strikes available in the current chain snapshot.")
        return

    # ── Expiry inspection banner (always visible above the chain) ───────
    exp_i1, exp_i2, exp_i3, exp_i4 = st.columns(4)
    exp_i1.metric("Expiries Returned by API", len(st.session_state.get("oc_raw_expiry_payload", [])))
    exp_i2.metric("Selected Expiry", expiry_label or "—")
    exp_i3.metric("API Request Expiry (ts)", request_expiry_ts or "—")
    exp_i4.metric("Symbol Used", symbol or "—")
    if len(st.session_state.get("oc_raw_expiry_payload", [])) == 1:
        st.info("Only one expiry is available from FYERS API.")
    else:
        _tagged_now = classify_expiries(st.session_state.get("oc_expiry_list", []))
        if _tagged_now and not any(tag == "Weekly" for _, _, tag in _tagged_now):
            st.warning("Weekly expiries are not available from the FYERS API for this instrument.")

    total_ce = df["ce_oi"].sum()
    total_pe = df["pe_oi"].sum()
    pcr = total_pe / total_ce if total_ce > 0 else 0
    max_pain = calculate_max_pain(df)

    if spot_price:
        atm_strike = df.iloc[(df["strike_price"] - spot_price).abs().argsort().iloc[:1]]["strike_price"].values[0]
    else:
        atm_strike = df["strike_price"].median()

    df = compute_big_move_scores(df, spot_price, max_pain, pcr, atm_strike)
    intel = compute_market_intelligence(df, spot_price, max_pain, pcr)
    df = compute_ai_engine(df, spot_price, atm_strike, max_pain, pcr)
    signals = generate_trade_signals(df, pcr, intel.get("support"), intel.get("resistance"),
                                      min_confidence=ai_min_conf, top_n=15)

    # Gamma analysis is computed exactly once per refresh here (it updates
    # session-tracked "previous refresh" history as a side effect) and is
    # shared by both the Gamma tab and the AI Scalping Engine tab below.
    gdf = add_gamma_columns(df, spot_price, expiry_label)
    gdf = compute_gamma_analysis(gdf, symbol, expiry_label)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Spot Price", f"₹{spot_price:,.2f}" if spot_price else "—")
    c2.metric("ATM Strike", f"₹{atm_strike:,.0f}")
    c3.metric("Total CE OI", f"{total_ce/1e5:.1f}L")
    c4.metric("Total PE OI", f"{total_pe/1e5:.1f}L")
    c5.metric("Max Pain", f"₹{max_pain:,.0f}")

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Big Move Alerts (existing feature, preserved) ───────────────────
    big_moves = detect_big_moves(df)
    if big_moves:
        st.markdown("**⚡ Big Move Alerts — Unusual OI Activity**")
        for alert in big_moves[:5]:
            badge = "🟢 BUY" if alert["direction"] == "BUY" else "🔴 SELL"
            box = st.success if alert["direction"] == "BUY" else st.error
            box(f"{badge} · Strike **{alert['strike']:,.0f}** ({alert['side']}) · "
                f"ΔOI {alert['oi_change']:+,.0f} — {alert['note']}")
        st.caption(
            "Based on unusual open-interest change (top percentile of ΔOI across strikes). "
            "This is a positioning signal, not financial advice — confirm with price action before acting."
        )
    st.markdown("<br>", unsafe_allow_html=True)

    # ── OI Shift Detection ───────────────────────────────────────────────
    shift_notes = detect_oi_shift(symbol, intel.get("support"), intel.get("resistance"))
    if shift_notes:
        for note in shift_notes:
            st.info(note)

    sig_col, gauge_col = st.columns([1, 1])
    with sig_col:
        st.markdown("**Market Sentiment**")
        st.markdown(pcr_signal(pcr), unsafe_allow_html=True)
        st.markdown(f"<br>PCR = **{pcr:.3f}**  |  Max Pain = **{max_pain:,.0f}**", unsafe_allow_html=True)
        st.markdown(f"🛡️ Support (max PE OI): **{intel.get('support', 0):,.0f}**")
        st.markdown(f"🧱 Resistance (max CE OI): **{intel.get('resistance', 0):,.0f}**")
    with gauge_col:
        st.plotly_chart(pcr_gauge(pcr), use_container_width=True, config={"displayModeBar": False})

    st.divider()

    # ── Market Intelligence ──────────────────────────────────────────────
    st.markdown('<div class="block-title">📡 Market Intelligence</div>', unsafe_allow_html=True)
    mi1, mi2 = st.columns([1, 1])
    with mi1:
        st.markdown(
            f"""<div class="intel-card"><div class="intel-label">Market Trend</div>
            <div class="intel-value">{intel.get('trend', '—')}</div></div>""",
            unsafe_allow_html=True,
        )
        st.markdown(
            f"""<div class="intel-card"><div class="intel-label">Institution Buying vs Selling</div>
            <div class="intel-value">🟢 {intel.get('institution_buying', 0):,.0f}
            &nbsp;/&nbsp; 🔴 {intel.get('institution_selling', 0):,.0f}</div></div>""",
            unsafe_allow_html=True,
        )
        st.markdown(
            f"""<div class="intel-card"><div class="intel-label">Option Writers vs Buyers Activity</div>
            <div class="intel-value">Writers: {intel.get('call_writers_activity',0)+intel.get('put_writers_activity',0):,.0f}
            &nbsp;|&nbsp; Buyers: {intel.get('call_buyers_activity',0)+intel.get('put_buyers_activity',0):,.0f}</div></div>""",
            unsafe_allow_html=True,
        )
        st.markdown(
            f"""<div class="intel-card"><div class="intel-label">Breakout / Breakdown Probability</div>
            <div class="intel-value">🚀 {intel.get('breakout_probability',0):.0f}%
            &nbsp;/&nbsp; 📉 {intel.get('breakdown_probability',0):.0f}%</div></div>""",
            unsafe_allow_html=True,
        )
    with mi2:
        st.plotly_chart(momentum_gauge(intel.get("momentum_score", 0)), use_container_width=True,
                         config={"displayModeBar": False})
        st.markdown(
            f"""<div class="intel-card"><div class="intel-label">Highest Volume / OI / ΔOI Strike</div>
            <div class="intel-value">Vol: {intel.get('highest_volume_strike', 0):,.0f} &nbsp;|&nbsp;
            OI: {intel.get('highest_oi_strike', 0):,.0f} &nbsp;|&nbsp;
            ΔOI: {intel.get('highest_delta_oi_strike', 0):,.0f}</div></div>""",
            unsafe_allow_html=True,
        )
    st.caption(
        "Momentum, institutional flow, and writers/buyers activity are derived from the current chain "
        "snapshot (OI, ΔOI, volume, PCR, spot-vs-max-pain) — not from a historical time series, since "
        "the option chain endpoint returns only a point-in-time view."
    )

    st.divider()

    # ── Dashboard Summary ─────────────────────────────────────────────────
    st.markdown('<div class="block-title">📈 Dashboard Summary</div>', unsafe_allow_html=True)
    summary2 = compute_dashboard_summary(df, signals, intel)

    def _fmt_strike_row(row):
        if row is None:
            return "—"
        try:
            return f"{row['strike_price']:,.0f}"
        except Exception:
            return "—"

    def _fmt_num(v):
        try:
            return f"{v:,.0f}"
        except (TypeError, ValueError):
            return "—"

    def _fmt_trade(sig):
        if not sig:
            return "—"
        return f"{sig['Strike']:,.0f} {sig['Side']} ({sig['Confidence']:.0f}%)"

    d1, d2, d3, d4 = st.columns(4)
    d1.metric("Top CE Buy", _fmt_strike_row(summary2.get("Top CE Buy")))
    d2.metric("Top PE Buy", _fmt_strike_row(summary2.get("Top PE Buy")))
    d3.metric("Best Breakout Strike", _fmt_strike_row(summary2.get("Best Breakout Strike")))
    d4.metric("Best Breakdown Strike", _fmt_strike_row(summary2.get("Best Breakdown Strike")))

    d5, d6, d7, d8 = st.columns(4)
    d5.metric("Highest Institutional Buying", _fmt_num(summary2.get("Highest Institutional Buying")))
    d6.metric("Highest Institutional Selling", _fmt_num(summary2.get("Highest Institutional Selling")))
    d7.metric("Highest Smart Money", _fmt_strike_row(summary2.get("Highest Smart Money")))
    d8.metric("Highest OI", _fmt_strike_row(summary2.get("Highest OI")))

    d9, d10, d11, d12 = st.columns(4)
    d9.metric("Highest Volume", _fmt_strike_row(summary2.get("Highest Volume")))
    d10.metric("Highest Delta OI", _fmt_strike_row(summary2.get("Highest Delta OI")))
    d11.metric("Best Risk/Reward Trade", _fmt_trade(summary2.get("Best Risk Reward Trade")))
    d12.metric("Today's Best Trade", _fmt_trade(summary2.get("Today's Best Trade")))

    st.divider()

    tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs(
        ["📋 Chain Table", "📊 OI Analysis", "📈 IV Skew", "🔥 Big Move Ready",
         "🤖 AI Trade Signals", "⚡ Gamma Build-up", "🎯 AI Scalping Engine"]
    )

    with tab1:
        flagged = df[df["Big Move"] == "🚨 Big Move"] if "Big Move" in df.columns else pd.DataFrame()
        if not flagged.empty:
            buy_strikes = flagged[flagged["Strike Signal"] == "🟢 BUY"]["strike_price"].tolist()
            sell_strikes = flagged[flagged["Strike Signal"] == "🔴 SELL"]["strike_price"].tolist()
            parts = []
            if buy_strikes:
                parts.append(f"🟢 **Buy-side build-up:** {', '.join(f'{s:,.0f}' for s in buy_strikes)}")
            if sell_strikes:
                parts.append(f"🔴 **Sell-side build-up:** {', '.join(f'{s:,.0f}' for s in sell_strikes)}")
            if parts:
                st.markdown("🚨 **Big OI moves detected** — " + "  |  ".join(parts))

        display_df = style_chain_table(df)
        numeric_cols = display_df.select_dtypes("number").columns
        st.dataframe(
            display_df.style
                .background_gradient(subset=[c for c in ["CE OI", "PE OI"] if c in display_df.columns],
                                      cmap="RdYlGn", vmin=0)
                .format({c: "{:,.0f}" for c in numeric_cols}),
            use_container_width=True, height=520,
        )

    with tab2:
        st.markdown("##### Open Interest — Calls vs Puts")
        st.plotly_chart(oi_bar_chart(df, max_pain), use_container_width=True, config={"displayModeBar": False})

        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("**Top 5 CE OI Strikes**")
            top5_ce = df.nlargest(5, "ce_oi")[["strike_price", "ce_oi", "ce_ltp"]].reset_index(drop=True)
            st.dataframe(top5_ce.style.format({"ce_oi": "{:,.0f}", "ce_ltp": "{:.2f}"}),
                         use_container_width=True, height=215)
        with col_b:
            st.markdown("**Top 5 PE OI Strikes**")
            top5_pe = df.nlargest(5, "pe_oi")[["strike_price", "pe_oi", "pe_ltp"]].reset_index(drop=True)
            st.dataframe(top5_pe.style.format({"pe_oi": "{:,.0f}", "pe_ltp": "{:.2f}"}),
                         use_container_width=True, height=215)

        st.markdown("**OI Build-up Classification**")
        buildup_view = df[["strike_price", "CE Build-up", "PE Build-up"]].rename(
            columns={"strike_price": "Strike"})
        st.dataframe(buildup_view, use_container_width=True, height=260)
        st.caption(
            "Long Build-up = fresh Put writing (bullish) · Short Build-up = fresh Call writing (bearish) · "
            "Short Covering = Call OI unwinding · Long Unwinding = Put OI unwinding."
        )

    with tab3:
        st.markdown("##### Implied Volatility Skew")
        st.plotly_chart(iv_chart(df), use_container_width=True, config={"displayModeBar": False})

    with tab4:
        st.markdown("##### 🔥 Big Move Ready Strike Engine")
        s1, s2, s3, s4 = st.columns(4)
        top_ce_row = _safe_top(df, "CE Score")
        top_pe_row = _safe_top(df, "PE Score")
        s1.metric("Top CE Score Strike", _fmt_strike_row(top_ce_row))
        s2.metric("Top PE Score Strike", _fmt_strike_row(top_pe_row))
        s3.metric("Best Breakout Strike", _fmt_strike_row(summary2.get("Best Breakout Strike")))
        s4.metric("Best Breakdown Strike", _fmt_strike_row(summary2.get("Best Breakdown Strike")))

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("**Full CE / PE Analysis** — independent AI Score, BUY/SELL Probability, "
                     "Entry/SL/Targets, Institutional flow and Smart Money Activity for both sides "
                     "of every strike (color-coded by Final Recommendation)")
        ce_pe_table = style_ce_pe_analysis_table(df)
        numeric_ce_pe_cols = [c for c in ce_pe_table.select_dtypes("number").columns if c != "Strike ⚡"]
        st.dataframe(
            ce_pe_table.style.apply(_bigmove_row_style, axis=1)
                .format({c: "{:,.1f}" for c in numeric_ce_pe_cols})
                .format({"Strike ⚡": "{:,.0f}"}),
            use_container_width=True, height=560,
        )
        st.caption(
            "CE AI Score / PE AI Score = independent 0-100 favourability for buying a CALL / PUT at "
            "that strike, built from OI, ΔOI, Volume, PCR, Max Pain, Spot/ATM distance, IV, "
            "Long/Short Build-up, Long/Short Unwinding, and Heavy Call/Put Writing. BUY Probability = "
            "the side's own score; SELL Probability = 100 − that score. Entry/SL/Targets are "
            "premium-percentage based (SL −15%, T1 +15%, T2 +30%, T3 +50%) for each side "
            "independently. Institutional Buying/Selling and Smart Money Activity are OI-base + "
            "ΔOI proxies for large-player positioning. Final Recommendation shows the stronger side "
            "with its ★ rating: ★★★★★ 90-100 Strong Buy · ★★★★ 75-89 Buy · ★★★ 55-74 Hold · "
            "★★ 35-54 Avoid · ★ below 35 Ignore. When CE Score and PE Score are genuinely tied, the "
            "strike's own Breakout vs Breakdown Probability breaks the tie instead of always "
            "defaulting to CE."
        )

        with st.expander("Legacy combined Big Move Table (Overall Score view)"):
            bm_table = style_big_move_table(df)
            numeric_bm_cols = [c for c in bm_table.select_dtypes("number").columns if c != "Strike ⚡"]
            st.dataframe(
                bm_table.style.apply(_bigmove_row_style, axis=1)
                    .format({c: "{:,.1f}" for c in numeric_bm_cols})
                    .format({"Strike ⚡": "{:,.0f}"}),
                use_container_width=True, height=420,
            )

    with tab5:
        st.markdown("##### 🤖 AI Trade Signal Engine — High Confidence Only")
        st.caption(f"Showing strikes with AI Confidence ≥ {ai_min_conf}% (adjust in the sidebar).")

        if not signals:
            st.info("No strikes currently meet the selected confidence threshold. Try lowering it in the sidebar.")
        else:
            for sig in signals:
                css_class = RATING_CSS_CLASS.get(sig.get("Signal Key", "ignore"), "rating-ignore")
                reasons_list = sig.get("Reasons") or [r.strip() for r in sig.get("Reason", "").split("·") if r.strip()]
                reasons_html = "".join(f"<li>{r}</li>" for r in reasons_list)

                st.markdown(f"""
                <div class="intel-card">
                  <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;">
                    <div><b style="color:#e6edf3;">{sig['Strike']:,.0f} {sig['Side']}</b>
                      &nbsp; <span class="{css_class}">{sig['Signal']}</span></div>
                    <div class="intel-label">Confidence
                      <span style="color:#e6edf3;font-weight:700;font-size:15px;">{sig['Confidence']:.0f}%</span></div>
                  </div>
                  <div style="margin-top:10px;font-family:'Courier New',monospace;color:#e6edf3;font-size:14px;">
                    Entry <b>{sig['Entry']}</b> &nbsp;|&nbsp; Stop Loss <b>{sig['SL']}</b> &nbsp;|&nbsp;
                    Target 1 {sig['T1']} &nbsp; Target 2 {sig['T2']} &nbsp; Target 3 {sig['T3']}
                    &nbsp;|&nbsp; RR {sig['Risk Reward']}
                  </div>
                  <div style="margin-top:8px;color:#8b949e;font-size:12px;">
                    Reason:
                    <ul style="margin:4px 0 0 18px;padding:0;">{reasons_html}</ul>
                  </div>
                </div>
                """, unsafe_allow_html=True)

            st.markdown("<br>", unsafe_allow_html=True)
            st.markdown("**All Qualifying Signals**")
            sig_table = style_trade_signals_table(signals)
            st.dataframe(sig_table, use_container_width=True, height=360)

        st.caption(
            "Heuristic engine built entirely from the current chain snapshot (OI, ΔOI, Volume, PCR, "
            "Max Pain, IV, spot/ATM/max-pain distance, breakout/breakdown probability, institutional & "
            "smart-money proxies). Entry/SL/Targets are premium-percentage based, not option-Greeks "
            "based. This is a positioning read, not financial advice — always confirm with price action "
            "and manage your own risk."
        )

    with tab6:
        render_gamma_tab(gdf, symbol, expiry_label, gamma_live_mode, gamma_audio_alert)

    with tab7:
        if scalping_enabled:
            with st.spinner("Fetching 5m/15m candles & India VIX for the AI Scalping Engine …"):
                trend_engine = compute_scalping_trend_engine(fyers, symbol_candidates, df, gdf)
            scalp_df = compute_scalping_table(df, gdf, trend_engine)
            render_scalping_tab(scalp_df, trend_engine, symbol, scalping_audio_alert)
        else:
            st.info(
                "🎯 The AI Scalping Engine is off. Enable **'Enable AI Scalping Engine'** in the "
                "sidebar to fetch 5m/15m candles and India VIX and compute the 13-point BUY/SELL "
                "confirmation checklist (EMA/VWAP/RSI/MACD/ADX/Supertrend, CE/PE OI direction, "
                "Gamma direction, Build-up/Covering/Unwinding, Order Block, BOS, Volume Spike)."
            )

    # ── Excel Download ───────────────────────────────────────────────────
    st.divider()
    st.markdown('<div class="block-title">📥 Export</div>', unsafe_allow_html=True)
    try:
        excel_buffer = build_excel_report(
            df, spot_price, atm_strike, pcr, max_pain,
            intel.get("support"), intel.get("resistance"), symbol, expiry_label, signals,
        )
        st.download_button(
            "⬇️ Download Excel Report",
            data=excel_buffer,
            file_name=f"options_chain_{symbol.replace(':', '_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    except Exception as e:  # noqa: BLE001
        st.error(f"Could not build Excel report: {e}")


# ══════════════════════════════════════════════════════════════════════════
# 10. ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════
#
# This module exposes show_option_chain(fyers) as its main entry point.
# `fyers` must be an already-authenticated FYERS API v3 client instance
# (from the `fyers-apiv3` package), e.g.:
#
#     from fyers_apiv3 import fyersModel
#     fyers = fyersModel.FyersModel(
#         client_id="YOUR-APP-ID",
#         token="YOUR-ACCESS-TOKEN",
#         is_async=False,
#         log_path="",
#     )
#     show_option_chain(fyers)
#
# Run this file with:  streamlit run fyers_options_chain_dashboard.py
