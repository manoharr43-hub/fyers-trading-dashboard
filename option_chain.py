"""
FYERS Options Chain Dashboard — Pro Edition
=============================================
Streamlit + FYERS API v3 dashboard with:
  • Big Move Ready Strike engine (weighted 0-100 scoring)
  • Robust multi-format expiry loading for indices + F&O stocks
  • Automatic symbol fallback (NIFTYNEXT50 / SENSEX / BANKEX / stocks)
  • Excel export (openpyxl) with full formatting
  • Market intelligence: trend, momentum, institutional & smart-money reads
  • All original features preserved (PCR, Max Pain, IV chart, OI chart,
    Chain table, Big Move Alerts, Support/Resistance, Strike Signal)
"""

import io
import math
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
div[data-testid="metric-container"] div[data-testid="stMetricValue"] { color: #e6edf3 !important; font-size: 24px; font-weight: 700; font-family: 'Courier New', monospace; }

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
</style>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════
# 1. SYMBOL HANDLING  (fixes NIFTYNEXT50 / SENSEX / BANKEX / F&O stocks)
# ══════════════════════════════════════════════════════════════════════════

# Each index maps to an ORDERED list of symbol formats to try. FYERS has
# changed / disagreed on exact index symbol spellings across API/SDK
# versions, so every plausible variant is tried until one returns data.
INDEX_SYMBOL_CANDIDATES = {
    "NIFTY 50":     ["NSE:NIFTY50-INDEX"],
    "NIFTY BANK":   ["NSE:NIFTYBANK-INDEX"],
    "FINNIFTY":     ["NSE:FINNIFTY-INDEX"],
    "MIDCAP NIFTY": ["NSE:MIDCPNIFTY-INDEX"],
    "NIFTY NXT 50": ["NSE:NIFTYNEXT50-INDEX", "NSE:NIFTYNXT50-INDEX", "NSE:NIFTY_NEXT_50-INDEX"],
    "SENSEX":       ["BSE:SENSEX-INDEX", "BSE:SENSEX-INDEX50"],
    "BANKEX":       ["BSE:BANKEX-INDEX"],
}


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
# 2. EXPIRY HANDLING  (weekly + monthly, auto refresh, DD-MMM-YYYY, no
#    manual timestamp editing, nearest expiry selected by default)
# ══════════════════════════════════════════════════════════════════════════

def format_expiry_label(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%d-%b-%Y")
    except (TypeError, ValueError, OSError):
        return str(ts)


def extract_expiry_list(response: dict) -> list:
    """
    Returns a list of (label, timestamp) tuples, sorted chronologically
    (nearest expiry first), with labels always normalised to DD-MMM-YYYY
    regardless of what FYERS sent, since the raw 'date' field format has
    been inconsistent across index/stock responses.
    """
    data = response.get("data", {}) if isinstance(response, dict) else {}
    raw = data.get("expiryData") or data.get("expirydata") or []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ts = item.get("expiry") or item.get("timestamp")
        if ts is None:
            continue
        out.append((format_expiry_label(ts), str(ts)))
    # de-duplicate while preserving order, then sort by the numeric timestamp
    seen = set()
    deduped = []
    for label, ts in out:
        if ts not in seen:
            seen.add(ts)
            deduped.append((label, ts))
    deduped.sort(key=lambda x: int(x[1]) if x[1].isdigit() else 0)
    return deduped


def fetch_expiry_list(fyers, symbol_candidates: list) -> tuple:
    """
    Fetches ONLY the expiry list (cheap call, strikecount=2, no timestamp)
    so the sidebar dropdown can be populated automatically the moment an
    instrument is chosen — the user never has to hand-edit a timestamp.
    Returns (expiry_list, symbol_used) or ([], "") on failure.
    """
    response, used_symbol, _ = fetch_optionchain_with_fallback(
        fyers, symbol_candidates, strikecount=2, expiry_timestamp=""
    )
    if not response or response.get("s") != "ok":
        return [], ""
    return extract_expiry_list(response), used_symbol


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
# 5. BIG MOVE READY STRIKE ENGINE  (0-100 weighted score)
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
    from spot/ATM/max-pain, IV level, and overall PCR bias.

    NOTE: since only a single point-in-time chain snapshot is available
    (no historical intraday series), factors are ranked RELATIVE to the
    current chain rather than against an absolute market-wide baseline.
    IV Expansion is approximated using current IV level (percentile rank)
    as FYERS' optionchain endpoint does not expose historical IV deltas.
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


def get_big_move_summary(d: pd.DataFrame) -> dict:
    if d.empty:
        return {}
    best_buy = _safe_top(d[d["PE Build-up"] == "Long Build-up (Put Writing)"], "Big Move Score")
    best_sell = _safe_top(d[d["CE Build-up"] == "Short Build-up (Call Writing)"], "Big Move Score")
    breakout = _safe_top(d, "Breakout Probability")
    breakdown = _safe_top(d, "Breakdown Probability")
    highest_prob = _safe_top(d, "Big Move Score")
    institutional = _safe_top(d, "Institutional Score")
    smart_money = _safe_top(d, "Smart Money Score")
    return {
        "Best BUY Strike": best_buy if best_buy is not None else highest_prob,
        "Best SELL Strike": best_sell if best_sell is not None else highest_prob,
        "Breakout Strike": breakout,
        "Breakdown Strike": breakdown,
        "Highest Probability Strike": highest_prob,
        "Institutional Activity": institutional,
        "Smart Money Activity": smart_money,
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
    cols = ["strike_price", "Big Move Score", "Big Move Label", "CE Build-up", "PE Build-up",
            "Breakout Probability", "Breakdown Probability", "Institutional Score", "Smart Money Score"]
    available = [c for c in cols if c in df.columns]
    out = df[available].copy()
    out.rename(columns={"strike_price": "Strike ⚡"}, inplace=True)
    return out.sort_values("Big Move Score", ascending=False).reset_index(drop=True)


def _bigmove_row_style(row):
    label = row.get("Big Move Label", "")
    if "Explosive" in str(label):
        color = "background-color:#0d3b2e;color:#3fb950;"
    elif "Strong" in str(label):
        color = "background-color:#123524;color:#7ee787;"
    elif "Watch" in str(label):
        color = "background-color:#1c2128;color:#d29922;"
    else:
        color = "background-color:#161b22;color:#8b949e;"
    return [color] * len(row)


# ══════════════════════════════════════════════════════════════════════════
# 8. EXCEL EXPORT  (openpyxl — full formatting)
# ══════════════════════════════════════════════════════════════════════════

FILL_HEADER = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
FILL_BUY = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
FILL_SELL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
FILL_WAIT = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
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
    """Applies green/red/yellow fill to any column whose header contains
    'Signal' or 'Bias' or 'Build-up' or 'Label', based on BUY/SELL/WAIT text."""
    target_cols = [
        idx + 1 for idx, h in enumerate(header_row_values)
        if h and any(k in str(h) for k in ("Signal", "Bias", "Build-up", "Label"))
    ]
    for row in ws.iter_rows(min_row=start_row):
        for col_idx in target_cols:
            cell = row[col_idx - 1]
            val = str(cell.value or "")
            if "BUY" in val or "Long Build-up" in val or "Bullish" in val or "Explosive" in val or "Strong" in val:
                cell.fill = FILL_BUY
            elif "SELL" in val or "Short Build-up" in val or "Bearish" in val:
                cell.fill = FILL_SELL
            elif "NEUTRAL" in val or "WAIT" in val or "Watch" in val or "Flat" in val:
                cell.fill = FILL_WAIT


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


def build_excel_report(df: pd.DataFrame, big_move_df: pd.DataFrame, spot_price: float,
                        atm_strike: float, pcr: float, max_pain: float, support, resistance,
                        symbol: str, expiry_label: str) -> io.BytesIO:
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
    chain_export = style_chain_table(df)
    _write_dataframe(ws_chain, chain_export)

    # ── Big Move Ready sheet ──
    ws_bigmove = wb.create_sheet("Big Move Ready")
    bigmove_export = style_big_move_table(big_move_df)
    _write_dataframe(ws_bigmove, bigmove_export)

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


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
        # no manual timestamp editing required.
        if st.session_state.get("oc_current_symbol_key") != symbol_key:
            st.session_state["oc_current_symbol_key"] = symbol_key
            st.session_state["oc_expiry_list"] = []
            st.session_state.pop("oc_df", None)
            with st.spinner("Loading expiry dates …"):
                expiry_list, _used = fetch_expiry_list(fyers, symbol_candidates)
            st.session_state["oc_expiry_list"] = expiry_list

        max_strikes = 20 if is_stock else 30
        strike_count = st.slider("Strikes Around ATM", 5, max_strikes, min(20, max_strikes), step=5)

        expiry_options = st.session_state.get("oc_expiry_list", [])
        if expiry_options:
            expiry_labels = [label for label, _ in expiry_options]
            selected_expiry_label = st.selectbox(
                "Expiry (Weekly / Monthly — nearest selected by default)", expiry_labels, index=0
            )
            expiry_timestamp = dict(expiry_options).get(selected_expiry_label, "")
        else:
            st.caption("⏳ Fetching available expiry dates for this instrument …")
            expiry_timestamp = ""
            selected_expiry_label = ""
            if st.button("🔁 Retry Loading Expiry List"):
                with st.spinner("Loading expiry dates …"):
                    expiry_list, _used = fetch_expiry_list(fyers, symbol_candidates)
                st.session_state["oc_expiry_list"] = expiry_list
                st.rerun()

        debug_mode = st.checkbox("Show raw API response (debug)", value=False)
        st.divider()
        fetch_btn = st.button("🔄 Fetch Live Data", use_container_width=True, type="primary")

    # ── Fetch & Process ─────────────────────────────────────────────────
    if fetch_btn:
        with st.spinner("Connecting to Fyers API …"):
            response, used_symbol, attempts = fetch_optionchain_with_fallback(
                fyers, symbol_candidates, strike_count, expiry_timestamp
            )

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

        new_expiry_list = extract_expiry_list(response)
        if new_expiry_list:
            st.session_state["oc_expiry_list"] = new_expiry_list

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

    # ── Render from session_state (persists across reruns/tab switches) ─
    if "oc_df" not in st.session_state:
        st.info("👈 Choose an instrument in the sidebar and click **Fetch Live Data**.")
        return

    df = st.session_state["oc_df"]
    spot_price = st.session_state["oc_spot"]
    symbol = st.session_state.get("oc_symbol", "")
    expiry_label = st.session_state.get("oc_expiry_label", "")

    if df.empty:
        st.warning("No strikes available in the current chain snapshot.")
        return

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

    tab1, tab2, tab3, tab4 = st.tabs(
        ["📋 Chain Table", "📊 OI Analysis", "📈 IV Skew", "🔥 Big Move Ready"]
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
        summary = get_big_move_summary(df)

        def _fmt_summary(row):
            if row is None:
                return "—"
            return f"{row['strike_price']:,.0f}  (Score {row['Big Move Score']:.0f})"

        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Best BUY Strike", _fmt_summary(summary.get("Best BUY Strike")))
        s2.metric("Best SELL Strike", _fmt_summary(summary.get("Best SELL Strike")))
        s3.metric("Breakout Strike", _fmt_summary(summary.get("Breakout Strike")))
        s4.metric("Breakdown Strike", _fmt_summary(summary.get("Breakdown Strike")))

        s5, s6, s7 = st.columns(3)
        s5.metric("Highest Probability Strike", _fmt_summary(summary.get("Highest Probability Strike")))
        s6.metric("Institutional Activity", _fmt_summary(summary.get("Institutional Activity")))
        s7.metric("Smart Money Activity", _fmt_summary(summary.get("Smart Money Activity")))

        st.markdown("<br>", unsafe_allow_html=True)
        st.markdown("**Big Move Ready Table** (color-coded by score band)")
        bm_table = style_big_move_table(df)
        st.dataframe(
            bm_table.style.apply(_bigmove_row_style, axis=1)
                .format({c: "{:,.1f}" for c in bm_table.select_dtypes("number").columns if c != "Strike ⚡"})
                .format({"Strike ⚡": "{:,.0f}"}),
            use_container_width=True, height=520,
        )
        st.caption(
            "🔥 90–100 Explosive Move Ready · 🟢 80–89 Strong Move · 🟡 60–79 Watch · ⚪ Below 60 Ignore. "
            "Score blends ΔOI, total OI, volume, OI+volume confirmation, Put/Call writing, unwinding, "
            "distance from Spot/ATM/Max Pain, IV level, and overall PCR bias."
        )

    # ── Excel Download ───────────────────────────────────────────────────
    st.divider()
    st.markdown('<div class="block-title">📥 Export</div>', unsafe_allow_html=True)
    try:
        excel_buffer = build_excel_report(
            df, df, spot_price, atm_strike, pcr, max_pain,
            intel.get("support"), intel.get("resistance"), symbol, expiry_label,
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
