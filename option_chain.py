"""
option_chain_with_fyers.py - NSE Options Dashboard + FYERS Fallback
====================================
✅ Primary: NSE API (indices, equities)
✅ Fallback: FYERS API (BSE indices like SENSEX, BANKEX)
✅ FYERS authentication built-in
✅ Seamless multi-source data fetching
"""

from __future__ import annotations

import io
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# Import FYERS integration
from fyers_integration import (
    render_fyers_login_ui,
    is_fyers_available,
    fetch_option_chain_fyers,
    get_fyers_auth,
)

# ══════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════

logger = logging.getLogger("option_chain_dashboard")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ══════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════

NSE_BASE_URL = "https://www.nseindia.com"

NSE_API_ENDPOINTS = {
    "option_chain_v1": f"{NSE_BASE_URL}/api/option-chain-indices",
    "option_chain_v2": f"{NSE_BASE_URL}/api/option-chain",
    "live_data": f"{NSE_BASE_URL}/api/equity-stockIndices",
    "quote": f"{NSE_BASE_URL}/api/quote-equity",
}

INDEX_SYMBOLS: dict[str, str] = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "SENSEX": "SENSEX",
}

COMMODITY_SYMBOLS: dict[str, str] = {
    "GOLD": "GOLD",
    "CRUDEOIL": "CRUDEOIL",
    "NATURALGAS": "NATURALGAS",
    "SILVER": "SILVER",
    "COPPER": "COPPER",
}

INSTRUMENT_TYPES = {
    "indices": "Index Options",
    "equities": "Equity Options",
    "commodities": "Commodity Futures",
}

NSE_UNSUPPORTED_INDICES: set[str] = {"SENSEX", "BANKEX"}

DEFAULT_LOT_SIZES: dict[str, int] = {
    "NIFTY": 25,
    "BANKNIFTY": 15,
    "FINNIFTY": 25,
    "MIDCPNIFTY": 50,
    "SENSEX": 10,
    "BANKEX": 15,
    "GOLD": 100,
    "CRUDEOIL": 100,
    "NATURALGAS": 1000,
    "SILVER": 30,
    "COPPER": 250,
    "_STOCK_DEFAULT": 1,
}

RISK_FREE_RATE = 0.07
MIN_SIGMA = 0.01
MAX_SIGMA = 5.0
TRADING_DAYS_MIN_T = 0.25

REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5

_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": f"{NSE_BASE_URL}/",
    "Connection": "keep-alive",
    "DNT": "1",
}

REQUIRED_CHAIN_COLUMNS = ["strike_price", "ce_ltp", "ce_oi", "pe_ltp", "pe_oi"]

# UI COLORS
DARK_BG = "#0d1117"
PANEL_BG = "#161b22"
BORDER_COLOR = "#30363d"
TEXT_MAIN = "#e6edf3"
TEXT_MUTED = "#8b949e"
GREEN = "#3fb950"
RED = "#f85149"
AMBER = "#d29922"
BLUE = "#58a6ff"


# ══════════════════════════════════════════════════════════════════════════
# HTTP SESSION
# ══════════════════════════════════════════════════════════════════════════

def _build_retrying_session() -> requests.Session:
    """Build session with exponential backoff."""
    session = requests.Session()
    session.headers.update(_NSE_HEADERS)
    
    retry_cfg = Retry(
        total=MAX_RETRIES,
        backoff_factor=RETRY_BACKOFF_SECONDS,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    
    adapter = HTTPAdapter(max_retries=retry_cfg)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


@st.cache_resource(show_spinner=False)
def get_nse_session() -> requests.Session:
    """Get cached NSE session."""
    session = _build_retrying_session()
    _warm_up_session(session)
    return session


def _warm_up_session(session: requests.Session) -> bool:
    """Warm up session with initial requests."""
    try:
        logger.info("Warming up NSE session...")
        session.get(NSE_BASE_URL, timeout=REQUEST_TIMEOUT)
        session.get(f"{NSE_BASE_URL}/option-chain", timeout=REQUEST_TIMEOUT)
        logger.info("✅ Session warm-up successful")
        return True
    except requests.exceptions.RequestException as e:
        logger.warning("⚠️ NSE session warm-up failed: %s", e)
        return False


def fetch_json_with_retry(
    session: requests.Session, 
    url: str, 
    params: Optional[dict] = None,
    max_retries: int = MAX_RETRIES,
    endpoint_name: str = "unknown",
) -> tuple[Optional[dict], Optional[str]]:
    """Fetch JSON with intelligent retry and error handling."""
    
    last_error = "Unknown error"
    logger.info(f"Fetching {endpoint_name}: {url}")
    
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            
            if resp.status_code == 429:
                wait_time = int(resp.headers.get("Retry-After", RETRY_BACKOFF_SECONDS * attempt))
                last_error = f"Rate limited (429) - wait {wait_time}s"
                logger.warning(last_error)
                time.sleep(wait_time)
                continue
            
            if resp.status_code in (401, 403):
                last_error = f"HTTP {resp.status_code} (auth/forbidden)"
                logger.warning(f"{last_error} — re-warming NSE session")
                _warm_up_session(session)
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            
            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code} (server error)"
                logger.warning(last_error)
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            
            if resp.status_code == 404:
                last_error = f"HTTP 404 (endpoint not found)"
                logger.error(last_error)
                return None, last_error
            
            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}"
                logger.warning(last_error)
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            
            try:
                payload = resp.json()
                if payload:
                    logger.info(f"✅ {endpoint_name} fetched successfully")
                    return payload, None
                else:
                    last_error = f"Empty JSON payload"
                    logger.warning(last_error)
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue
                    
            except ValueError as e:
                last_error = f"Invalid JSON: {e}"
                logger.warning(last_error)
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
        
        except requests.exceptions.Timeout:
            last_error = f"Timeout on attempt {attempt}/{max_retries}"
            logger.warning(f"{last_error} for {url}")
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue
            
        except requests.exceptions.ConnectionError as e:
            last_error = f"Connection error: {e}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue
            
        except requests.exceptions.RequestException as e:
            last_error = f"Request exception: {e}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

    logger.error(f"❌ fetch_json_with_retry exhausted retries: {last_error}")
    return None, last_error


# ══════════════════════════════════════════════════════════════════════════
# DATA FETCH WITH MULTI-SOURCE FALLBACK
# ══════════════════════════════════════════════════════════════════════════

def normalize_stock_symbol(raw: str) -> str:
    """Normalize stock symbol."""
    s = (raw or "").strip().upper()
    if s.endswith("-EQ"):
        s = s[:-3]
    if ":" in s:
        s = s.split(":")[-1]
    return s


@st.cache_data(ttl=15, show_spinner=False)
def fetch_option_chain_raw(symbol: str, is_index: bool, is_commodity: bool = False) -> dict:
    """Fetch option chain with multi-source fallback: NSE → FYERS."""
    
    session = get_nse_session()
    
    # Step 1: Try NSE for indices/commodities
    endpoints_to_try = []
    
    if is_index or is_commodity:
        endpoints_to_try = [
            (NSE_API_ENDPOINTS["option_chain_v1"], "NSE Option Chain v1"),
            (NSE_API_ENDPOINTS["option_chain_v2"], "NSE Option Chain v2"),
        ]
    else:
        endpoints_to_try = [
            (NSE_API_ENDPOINTS["option_chain_v1"], "NSE Option Chain v1"),
        ]
    
    last_error = None
    
    for url, endpoint_name in endpoints_to_try:
        logger.info(f"Trying endpoint: {endpoint_name}")
        payload, error = fetch_json_with_retry(
            session, url, params={"symbol": symbol}, endpoint_name=endpoint_name
        )
        
        if payload is None:
            last_error = error
            logger.warning(f"⚠️ {endpoint_name} failed: {error}")
            continue
        
        records = payload.get("records") if isinstance(payload, dict) else None
        if isinstance(records, dict) and records.get("data"):
            logger.info(f"✅ Successfully fetched from {endpoint_name}")
            return {"ok": True, "payload": payload, "error": None, "source": endpoint_name}
        else:
            last_error = "Response structure invalid"
            logger.warning(f"⚠️ Invalid response from {endpoint_name}")
            continue
    
    # Step 2: Try FYERS as fallback (useful for BSE indices like SENSEX)
    if symbol in NSE_UNSUPPORTED_INDICES and is_fyers_available():
        logger.info(f"NSE failed for {symbol}, trying FYERS as fallback...")
        fyers_result = fetch_option_chain_fyers(symbol, is_index, is_commodity)
        if fyers_result.get("ok"):
            logger.info("✅ Successfully fetched from FYERS")
            return fyers_result
        else:
            last_error = fyers_result.get("error", "FYERS fetch failed")
    elif symbol in NSE_UNSUPPORTED_INDICES:
        last_error = (
            f"NSE doesn't support {symbol} (BSE index). "
            f"Connect FYERS account in sidebar for access."
        )
    
    error_msg = last_error or "All endpoints failed"
    logger.error(f"❌ Could not fetch option chain for {symbol}: {error_msg}")
    return {"ok": False, "payload": None, "error": error_msg, "source": "NONE"}


# ══════════════════════════════════════════════════════════════════════════
# DATA PARSING & VALIDATION
# ══════════════════════════════════════════════════════════════════════════

def _safe_num(val: Any, default: float = 0.0) -> float:
    """Safely convert value to float."""
    try:
        if val is None:
            return default
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def parse_option_chain(payload: dict, preferred_expiry: str = "") -> tuple[pd.DataFrame, dict]:
    """Parse option chain payload into DataFrame."""
    
    meta = {
        "spot_price": 0.0, 
        "expiry_dates": [], 
        "selected_expiry": "",
        "fetched_at": datetime.now(), 
        "total_rows_seen": 0, 
        "rows_parsed": 0,
    }
    
    records = payload.get("records", {}) if isinstance(payload, dict) else {}
    chain = records.get("data", []) if isinstance(records, dict) else []
    
    meta["spot_price"] = _safe_num(records.get("underlyingValue"))
    expiry_dates = records.get("expiryDates", []) or []
    meta["expiry_dates"] = expiry_dates

    if not chain:
        return pd.DataFrame(), meta

    selected_expiry = preferred_expiry if preferred_expiry in expiry_dates else (
        expiry_dates[0] if expiry_dates else ""
    )
    meta["selected_expiry"] = selected_expiry

    rows = []
    meta["total_rows_seen"] = len(chain)
    
    for item in chain:
        if not isinstance(item, dict):
            continue
        if selected_expiry and item.get("expiryDate") != selected_expiry:
            continue
        
        strike = item.get("strikePrice")
        if strike is None:
            continue
        
        ce, pe = item.get("CE") or {}, item.get("PE") or {}
        rows.append({
            "strike_price": _safe_num(strike),
            "ce_ltp": _safe_num(ce.get("lastPrice")),
            "ce_change": _safe_num(ce.get("change")),
            "ce_bid": _safe_num(ce.get("bidprice")),
            "ce_bid_qty": _safe_num(ce.get("bidQty")),
            "ce_ask": _safe_num(ce.get("askPrice")),
            "ce_ask_qty": _safe_num(ce.get("askQty")),
            "ce_volume": _safe_num(ce.get("totalTradedVolume")),
            "ce_oi": _safe_num(ce.get("openInterest")),
            "ce_chng_oi": _safe_num(ce.get("changeinOpenInterest")),
            "ce_oi_change_pct": _safe_num(ce.get("pchangeinOpenInterest")),
            "ce_iv": _safe_num(ce.get("impliedVolatility")),
            "pe_ltp": _safe_num(pe.get("lastPrice")),
            "pe_change": _safe_num(pe.get("change")),
            "pe_bid": _safe_num(pe.get("bidprice")),
            "pe_bid_qty": _safe_num(pe.get("bidQty")),
            "pe_ask": _safe_num(pe.get("askPrice")),
            "pe_ask_qty": _safe_num(pe.get("askQty")),
            "pe_volume": _safe_num(pe.get("totalTradedVolume")),
            "pe_oi": _safe_num(pe.get("openInterest")),
            "pe_chng_oi": _safe_num(pe.get("changeinOpenInterest")),
            "pe_oi_change_pct": _safe_num(pe.get("pchangeinOpenInterest")),
            "pe_iv": _safe_num(pe.get("impliedVolatility")),
        })

    meta["rows_parsed"] = len(rows)
    if not rows:
        return pd.DataFrame(), meta

    df = pd.DataFrame(rows)
    df = df.groupby("strike_price", as_index=False).first()
    df.sort_values("strike_price", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df, meta


def validate_chain_df(df: pd.DataFrame) -> bool:
    """Validate option chain DataFrame."""
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return False
        if not all(c in df.columns for c in REQUIRED_CHAIN_COLUMNS):
            return False
        strikes = pd.to_numeric(df["strike_price"], errors="coerce").dropna()
        return bool((strikes > 0).sum() > 0)
    except Exception as e:
        logger.error("validate_chain_df raised exception: %s", e)
        return False


def filter_strikes_around_atm(df: pd.DataFrame, spot: float, n_each_side: int) -> pd.DataFrame:
    """Filter strikes around ATM."""
    if df is None or df.empty or n_each_side <= 0:
        return df
    d = df.sort_values("strike_price").reset_index(drop=True)
    ref = spot if spot else float(d["strike_price"].median())
    atm_idx = int((d["strike_price"] - ref).abs().idxmin())
    lo = max(0, atm_idx - n_each_side)
    hi = min(len(d), atm_idx + n_each_side + 1)
    return d.iloc[lo:hi].reset_index(drop=True)


def parse_days_to_expiry(expiry_label: str) -> float:
    """Parse days to expiry from label."""
    if not expiry_label:
        return 7.0
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            exp_dt = datetime.strptime(expiry_label, fmt)
            delta_days = (exp_dt.replace(hour=15, minute=30) - datetime.now()).total_seconds() / 86400
            return max(delta_days, TRADING_DAYS_MIN_T)
        except ValueError:
            continue
    return 7.0


# ══════════════════════════════════════════════════════════════════════════
# GREEKS ENGINE
# ══════════════════════════════════════════════════════════════════════════

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_greeks(spot: float, strike: float, t_years: float, r: float, sigma: float,
              is_call: bool) -> dict[str, float]:
    """Calculate Black-Scholes Greeks."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    
    sigma = min(max(sigma, MIN_SIGMA), MAX_SIGMA)
    sqrt_t = math.sqrt(t_years)
    
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t_years) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t
    except (ValueError, ZeroDivisionError):
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    pdf_d1 = _norm_pdf(d1)
    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100.0

    if is_call:
        delta = _norm_cdf(d1)
        theta = (
            -(spot * pdf_d1 * sigma) / (2 * sqrt_t)
            - r * strike * math.exp(-r * t_years) * _norm_cdf(d2)
        ) / 365.0
    else:
        delta = _norm_cdf(d1) - 1.0
        theta = (
            -(spot * pdf_d1 * sigma) / (2 * sqrt_t)
            + r * strike * math.exp(-r * t_years) * _norm_cdf(-d2)
        ) / 365.0

    return {
        "delta": round(delta, 4), 
        "gamma": round(gamma, 6),
        "theta": round(theta, 4), 
        "vega": round(vega, 4),
    }


def add_greeks_columns(df: pd.DataFrame, spot: float, expiry_label: str,
                        r: float = RISK_FREE_RATE) -> pd.DataFrame:
    """Add Greeks columns to DataFrame."""
    d = df.copy()
    
    if d.empty:
        for col in ("ce_delta", "ce_gamma", "ce_theta", "ce_vega",
                    "pe_delta", "pe_gamma", "pe_theta", "pe_vega"):
            d[col] = 0.0
        return d

    t_years = parse_days_to_expiry(expiry_label) / 365.0

    ce_g = d.apply(
        lambda row: bs_greeks(spot, row["strike_price"], t_years, r, row["ce_iv"] / 100.0, True),
        axis=1,
    )
    pe_g = d.apply(
        lambda row: bs_greeks(spot, row["strike_price"], t_years, r, row["pe_iv"] / 100.0, False),
        axis=1,
    )
    
    for key in ("delta", "gamma", "theta", "vega"):
        d[f"ce_{key}"] = ce_g.apply(lambda x: x[key])
        d[f"pe_{key}"] = pe_g.apply(lambda x: x[key])
    
    return d


# ══════════════════════════════════════════════════════════════════════════
# IV RANK / IV PERCENTILE
# ══════════════════════════════════════════════════════════════════════════

def _atm_iv(df: pd.DataFrame, spot: float) -> float:
    """Calculate ATM Implied Volatility."""
    if df.empty or not spot:
        return 0.0
    idx = (df["strike_price"] - spot).abs().idxmin()
    row = df.loc[idx]
    ivs = [v for v in (row.get("ce_iv", 0), row.get("pe_iv", 0)) if v and v > 0]
    return float(np.mean(ivs)) if ivs else 0.0


def update_iv_history(symbol: str, expiry_label: str, atm_iv: float) -> None:
    """Update IV history for rank/percentile calculation."""
    if atm_iv <= 0:
        return
    history = st.session_state.setdefault("iv_history", {})
    key = f"{symbol}|{expiry_label}"
    series = history.get(key, [])
    series.append(atm_iv)
    if len(series) > 500:
        series = series[-500:]
    history[key] = series
    st.session_state["iv_history"] = history


def compute_iv_rank_percentile(symbol: str, expiry_label: str, current_iv: float) -> tuple[float, float]:
    """Compute IV Rank and IV Percentile."""
    history = st.session_state.get("iv_history", {})
    series = history.get(f"{symbol}|{expiry_label}", [])
    if len(series) < 2 or current_iv <= 0:
        return 0.0, 0.0
    lo, hi = min(series), max(series)
    iv_rank = ((current_iv - lo) / (hi - lo)) * 100 if hi > lo else 50.0
    iv_percentile = (sum(1 for v in series if v <= current_iv) / len(series)) * 100
    return round(float(np.clip(iv_rank, 0, 100)), 1), round(iv_percentile, 1)


# ══════════════════════════════════════════════════════════════════════════
# GAMMA EXPOSURE / DELTA EXPOSURE
# ══════════════════════════════════════════════════════════════════════════

def compute_gex_dex(df: pd.DataFrame, spot: float, lot_size: int) -> dict[str, Any]:
    """Compute Gamma and Delta Exposure."""
    if df.empty or not spot:
        return {
            "total_gex": 0.0, "total_dex": 0.0, "by_strike": pd.DataFrame(),
            "max_gex_strike": None, "min_gex_strike": None, "gamma_flip": None
        }

    d = df.copy()
    d["gex"] = (
        (d.get("ce_gamma", 0) * d.get("ce_oi", 0)) - (d.get("pe_gamma", 0) * d.get("pe_oi", 0))
    ) * (spot ** 2) * 0.01 * lot_size
    
    d["dex"] = (
        (d.get("ce_delta", 0) * d.get("ce_oi", 0)) + (d.get("pe_delta", 0) * d.get("pe_oi", 0))
    ) * spot * lot_size

    total_gex = float(d["gex"].sum())
    total_dex = float(d["dex"].sum())
    
    max_gex_row = d.loc[d["gex"].idxmax()] if len(d) else None
    min_gex_row = d.loc[d["gex"].idxmin()] if len(d) else None

    d_sorted = d.sort_values("strike_price").reset_index(drop=True)
    cum_gex = d_sorted["gex"].cumsum()
    gamma_flip = None
    sign_changes = np.where(np.diff(np.sign(cum_gex.replace(0, np.nan).ffill().fillna(0))) != 0)[0]
    
    if len(sign_changes) > 0:
        idx = int(sign_changes[0])
        gamma_flip = float(d_sorted.loc[idx, "strike_price"])

    return {
        "total_gex": total_gex, 
        "total_dex": total_dex,
        "by_strike": d[["strike_price", "gex", "dex"]],
        "max_gex_strike": float(max_gex_row["strike_price"]) if max_gex_row is not None else None,
        "min_gex_strike": float(min_gex_row["strike_price"]) if min_gex_row is not None else None,
        "gamma_flip": gamma_flip,
    }


# ══════════════════════════════════════════════════════════════════════════
# CORE ANALYTICS
# ══════════════════════════════════════════════════════════════════════════

def calc_pcr(df: pd.DataFrame) -> float:
    """Calculate Put-Call Ratio."""
    if df.empty:
        return 0.0
    total_ce = df["ce_oi"].sum()
    total_pe = df["pe_oi"].sum()
    return round(float(total_pe / total_ce), 3) if total_ce > 0 else 0.0


def calc_max_pain(df: pd.DataFrame) -> float:
    """Calculate Max Pain level."""
    if df.empty:
        return 0.0
    strikes = df["strike_price"].values
    ce_oi = df["ce_oi"].values
    pe_oi = df["pe_oi"].values
    pain = [
        float(np.sum(np.maximum(s - strikes, 0) * ce_oi) + np.sum(np.maximum(strikes - s, 0) * pe_oi))
        for s in strikes
    ]
    return float(strikes[int(np.argmin(pain))]) if pain else 0.0


def calc_max_oi(df: pd.DataFrame) -> dict[str, Optional[float]]:
    """Calculate Max OI strikes."""
    if df.empty:
        return {"max_ce_oi_strike": None, "max_pe_oi_strike": None}
    return {
        "max_ce_oi_strike": float(df.loc[df["ce_oi"].idxmax(), "strike_price"]),
        "max_pe_oi_strike": float(df.loc[df["pe_oi"].idxmax(), "strike_price"]),
    }


def calc_support_resistance(df: pd.DataFrame) -> tuple[Optional[float], Optional[float]]:
    """Calculate support and resistance levels."""
    if df.empty:
        return None, None
    support = float(df.loc[df["pe_oi"].idxmax(), "strike_price"])
    resistance = float(df.loc[df["ce_oi"].idxmax(), "strike_price"])
    return support, resistance


# ══════════════════════════════════════════════════════════════════════════
# UI COMPONENTS - CHARTS
# ══════════════════════════════════════════════════════════════════════════

def _plotly_dark_layout(fig: go.Figure, height: int = 420, title: str = "") -> go.Figure:
    """Apply dark theme to Plotly figure."""
    fig.update_layout(
        paper_bgcolor=DARK_BG, 
        plot_bgcolor=DARK_BG,
        font=dict(color=TEXT_MUTED, family="Courier New"),
        height=height, 
        margin=dict(l=10, r=10, t=40 if title else 10, b=10),
        title=dict(text=title, font=dict(color=TEXT_MAIN, size=14)) if title else None,
        legend=dict(bgcolor=PANEL_BG, bordercolor=BORDER_COLOR, borderwidth=1),
    )
    return fig


def chart_oi_bars(df: pd.DataFrame, max_pain: float) -> go.Figure:
    """Create OI bar chart."""
    fig = make_subplots(
        rows=1, cols=2, 
        subplot_titles=("Call OI (CE)", "Put OI (PE)"),
        shared_yaxes=True, 
        horizontal_spacing=0.04
    )
    
    if df.empty:
        return _plotly_dark_layout(fig)
    
    max_oi = max(df["ce_oi"].max(), df["pe_oi"].max(), 1)
    strikes_sorted = df["strike_price"].sort_values().unique()
    gap = (strikes_sorted[1] - strikes_sorted[0]) if len(strikes_sorted) > 1 else 1

    fig.add_trace(go.Bar(
        x=-df["ce_oi"], y=df["strike_price"], orientation="h",
        marker_color=[GREEN if abs(s - max_pain) < gap / 2 else "#238636" for s in df["strike_price"]],
        name="CE OI", 
        showlegend=False,
        hovertemplate="Strike %{y}<br>CE OI: %{customdata:,}<extra></extra>", 
        customdata=df["ce_oi"],
    ), row=1, col=1)
    
    fig.add_trace(go.Bar(
        x=df["pe_oi"], y=df["strike_price"], orientation="h",
        marker_color=[RED if abs(s - max_pain) < gap / 2 else "#da3633" for s in df["strike_price"]],
        name="PE OI", 
        showlegend=False,
        hovertemplate="Strike %{y}<br>PE OI: %{x:,}<extra></extra>",
    ), row=1, col=2)
    
    for col in (1, 2):
        fig.add_hline(
            y=max_pain, line_dash="dot", line_color=AMBER,
            annotation_text=f"Max Pain {max_pain:,.0f}", 
            annotation_font_color=AMBER, 
            row=1, col=col
        )
    
    fig.update_layout(
        xaxis=dict(showticklabels=False, showgrid=False, range=[-max_oi * 1.1, 0]),
        xaxis2=dict(showticklabels=False, showgrid=False, range=[0, max_oi * 1.1]),
        yaxis=dict(showgrid=True, gridcolor=BORDER_COLOR, tickfont=dict(color=TEXT_MAIN, size=11)),
    )
    fig.update_annotations(font_color=TEXT_MUTED)
    return _plotly_dark_layout(fig, height=480)


def chart_iv_skew(df: pd.DataFrame) -> go.Figure:
    """Create IV Skew chart."""
    fig = go.Figure()
    if not df.empty:
        fig.add_trace(go.Scatter(
            x=df["strike_price"], y=df["ce_iv"], 
            mode="lines+markers",
            name="CE IV", 
            line=dict(color=GREEN, width=2)
        ))
        fig.add_trace(go.Scatter(
            x=df["strike_price"], y=df["pe_iv"], 
            mode="lines+markers",
            name="PE IV", 
            line=dict(color=RED, width=2)
        ))
    fig.update_layout(
        xaxis=dict(title="Strike", showgrid=True, gridcolor=BORDER_COLOR),
        yaxis=dict(title="IV %", showgrid=True, gridcolor=BORDER_COLOR)
    )
    return _plotly_dark_layout(fig, height=320, title="Implied Volatility Skew")


def gauge_pcr(pcr: float) -> go.Figure:
    """Create PCR gauge."""
    fig = go.Figure(go.Indicator(
        mode="gauge+number", 
        value=pcr,
        number={"font": {"color": TEXT_MAIN, "size": 32, "family": "Courier New"}},
        gauge={
            "axis": {"range": [0, 3], "tickcolor": TEXT_MUTED, "tickfont": {"color": TEXT_MUTED}},
            "bar": {"color": BLUE, "thickness": 0.25}, 
            "bgcolor": PANEL_BG, 
            "borderwidth": 0,
            "steps": [
                {"range": [0, 0.7], "color": "#3b0d1a"}, 
                {"range": [0.7, 1.3], "color": "#1c2128"},
                {"range": [1.3, 3.0], "color": "#0d3b2e"}
            ],
            "threshold": {"line": {"color": AMBER, "width": 3}, "value": pcr},
        },
        title={"text": "PUT / CALL RATIO", "font": {"color": TEXT_MUTED, "size": 12}},
    ))
    return _plotly_dark_layout(fig, height=220)


# ══════════════════════════════════════════════════════════════════════════
# TABLE RENDERING
# ══════════════════════════════════════════════════════════════════════════

_TABLE_CSS = f"""
<style>
.oc-table-wrap {{ max-height: 620px; overflow-y: auto; border: 1px solid {BORDER_COLOR}; border-radius: 8px; }}
.oc-table {{ width: 100%; border-collapse: collapse; font-family: 'Courier New', monospace; font-size: 12.5px; }}
.oc-table th {{ background: #1F4E78; color: #ffffff; padding: 8px 10px; text-align: center;
                position: sticky; top: 0; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }}
.oc-table td {{ padding: 6px 9px; text-align: center; border-bottom: 1px solid #21262d; color: {TEXT_MAIN}; white-space: nowrap; }}
.oc-atm-row td {{ background-color: #1c2128 !important; font-weight: 700; }}
</style>
"""


def _safe_cell(val: Any) -> str:
    if val is None:
        return ""
    try:
        if isinstance(val, float) and math.isnan(val):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(val)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _oi_cell_style(val: float, heavy_thresh: float, max_val: float) -> str:
    if max_val <= 0:
        return f"color:{TEXT_MUTED};"
    pct = max(0.0, min(100.0, (val / max_val) * 100))
    intensity = 0.10 + pct / 250
    is_heavy = heavy_thresh > 0 and val >= heavy_thresh
    bg = f"background:linear-gradient(90deg, rgba(63,185,80,{intensity:.2f}) {pct:.0f}%, transparent {pct:.0f}%);"
    weight = "font-weight:700;" if is_heavy else ""
    return bg + weight


def _oi_change_cell_style(val: float, heavy_thresh: float) -> str:
    if val == 0:
        return f"color:{TEXT_MUTED};"
    is_large = heavy_thresh > 0 and abs(val) >= heavy_thresh
    if val > 0:
        return f"color:#0d3b2e;font-weight:700;background-color:{GREEN};" if is_large else f"color:{GREEN};"
    return f"color:#3b0d1a;font-weight:700;background-color:{RED};" if is_large else f"color:{RED};"


def render_chain_table_html(df: pd.DataFrame, show_greeks: bool, top_n: int = 400) -> str:
    """Render option chain table as HTML."""
    if df.empty:
        return _TABLE_CSS + "<div style='color:#8b949e;padding:12px;'>No rows to display.</div>"

    base_cols = [
        ("ce_oi", "CE OI"), ("ce_chng_oi", "CE ΔOI"), ("ce_oi_change_pct", "CE ΔOI%"),
        ("ce_volume", "CE Vol"), ("ce_iv", "CE IV"), ("ce_ltp", "CE LTP"),
        ("ce_bid", "CE Bid"), ("ce_ask", "CE Ask"),
    ]
    greek_ce_cols = [("ce_delta", "CE Δ"), ("ce_gamma", "CE Γ"), ("ce_theta", "CE Θ"), ("ce_vega", "CE V")]
    mid_cols = [("strike_price", "STRIKE")]
    greek_pe_cols = [("pe_delta", "PE Δ"), ("pe_gamma", "PE Γ"), ("pe_theta", "PE Θ"), ("pe_vega", "PE V")]
    pe_cols = [
        ("pe_bid", "PE Bid"), ("pe_ask", "PE Ask"), ("pe_ltp", "PE LTP"), ("pe_iv", "PE IV"),
        ("pe_volume", "PE Vol"), ("pe_oi_change_pct", "PE ΔOI%"), ("pe_chng_oi", "PE ΔOI"), ("pe_oi", "PE OI"),
    ]

    cols = base_cols + (greek_ce_cols if show_greeks else []) + mid_cols + \
        (greek_pe_cols if show_greeks else []) + pe_cols
    cols = [(k, label) for k, label in cols if k in df.columns]

    fmt = {
        "ce_oi": "{:,.0f}", "ce_chng_oi": "{:+,.0f}", "ce_oi_change_pct": "{:+.1f}%",
        "ce_volume": "{:,.0f}", "ce_iv": "{:.1f}", "ce_ltp": "{:.2f}", "ce_bid": "{:.2f}", "ce_ask": "{:.2f}",
        "ce_delta": "{:.3f}", "ce_gamma": "{:.5f}", "ce_theta": "{:.3f}", "ce_vega": "{:.3f}",
        "strike_price": "{:,.0f}",
        "pe_delta": "{:.3f}", "pe_gamma": "{:.5f}", "pe_theta": "{:.3f}", "pe_vega": "{:.3f}",
        "pe_bid": "{:.2f}", "pe_ask": "{:.2f}", "pe_ltp": "{:.2f}", "pe_iv": "{:.1f}",
        "pe_volume": "{:,.0f}", "pe_oi_change_pct": "{:+.1f}%", "pe_chng_oi": "{:+,.0f}", "pe_oi": "{:,.0f}",
    }

    heavy_ce_oi = df["ce_oi"].quantile(0.80) if df["ce_oi"].max() > 0 else 0
    heavy_pe_oi = df["pe_oi"].quantile(0.80) if df["pe_oi"].max() > 0 else 0
    heavy_ce_chng = df["ce_chng_oi"].abs().quantile(0.80) if (df["ce_chng_oi"] != 0).any() else 0
    heavy_pe_chng = df["pe_chng_oi"].abs().quantile(0.80) if (df["pe_chng_oi"] != 0).any() else 0
    max_ce_oi, max_pe_oi = df["ce_oi"].max(), df["pe_oi"].max()

    view = df.head(top_n)
    header_html = "".join(f"<th>{label}</th>" for _, label in cols)
    rows_html = []
    
    for _, row in view.iterrows():
        is_atm = bool(row.get("ATM", False))
        cells = []
        for key, _ in cols:
            val = row.get(key, "")
            spec = fmt.get(key)
            display_val = spec.format(val) if spec and pd.notna(val) else ("" if pd.isna(val) else val)
            style = ""
            
            if key == "ce_oi":
                style = _oi_cell_style(val, heavy_ce_oi, max_ce_oi)
            elif key == "pe_oi":
                style = _oi_cell_style(val, heavy_pe_oi, max_pe_oi)
            elif key == "ce_chng_oi":
                style = _oi_change_cell_style(val, heavy_ce_chng)
            elif key == "pe_chng_oi":
                style = _oi_change_cell_style(val, heavy_pe_chng)
            
            cells.append(f'<td style="{style}">{_safe_cell(display_val)}</td>')
        
        row_class = "oc-atm-row" if is_atm else ""
        rows_html.append(f'<tr class="{row_class}">{"".join(cells)}</tr>')

    return (
        _TABLE_CSS
        + f'<div class="oc-table-wrap"><table class="oc-table"><thead><tr>{header_html}</tr></thead>'
        + f'<tbody>{"".join(rows_html)}</tbody></table></div>'
    )


# ══════════════════════════════════════════════════════════════════════════
# EXPORT
# ══════════════════════════════════════════════════════════════════════════

def export_csv_bytes(df: pd.DataFrame) -> bytes:
    """Export DataFrame to CSV bytes."""
    return df.to_csv(index=False).encode("utf-8")


# ══════════════════════════════════════════════════════════════════════════
# STREAMLIT UI
# ══════════════════════════════════════════════════════════════════════════

def _configure_page() -> None:
    """Configure Streamlit page."""
    try:
        st.set_page_config(
            page_title="NSE Options Chain Dashboard", 
            page_icon="📊",
            layout="wide", 
            initial_sidebar_state="expanded",
        )
    except Exception as e:
        logger.warning("st.set_page_config() skipped: %s", e)


def _inject_css() -> None:
    """Inject custom CSS."""
    st.markdown(f"""
    <style>
    .stApp {{ background-color: {DARK_BG}; }}
    section[data-testid="stSidebar"] {{ background-color: {PANEL_BG}; border-right: 1px solid {BORDER_COLOR}; }}
    div[data-testid="metric-container"] {{
        background: {PANEL_BG}; border: 1px solid {BORDER_COLOR}; border-radius: 8px; padding: 14px 18px;
    }}
    div[data-testid="metric-container"] label {{ color: {TEXT_MUTED} !important; font-size: 12px;
        text-transform: uppercase; letter-spacing: 0.08em; }}
    div[data-testid="metric-container"] div[data-testid="stMetricValue"] {{
        color: {TEXT_MAIN} !important; font-size: 21px; font-weight: 700; font-family: 'Courier New', monospace; }}
    h1, h2, h3 {{ color: {TEXT_MAIN} !important; }}
    .block-title {{ color: {BLUE}; font-size: 13px; font-weight: 600; text-transform: uppercase;
        letter-spacing: 0.1em; margin-bottom: 8px; }}
    button[data-baseweb="tab"] {{ color: {TEXT_MUTED} !important; }}
    button[data-baseweb="tab"][aria-selected="true"] {{ color: {BLUE} !important; border-bottom: 2px solid {BLUE}; }}
    hr {{ border-color: {BORDER_COLOR}; }}
    </style>
    """, unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════
# MAIN DASHBOARD
# ══════════════════════════════════════════════════════════════════════════

def _sidebar_config() -> dict:
    """Render sidebar configuration."""
    with st.sidebar:
        st.markdown("### 🔐 Data Source")
        
        # FYERS authentication UI
        render_fyers_login_ui()
        
        st.divider()
        st.markdown("### ⚙️ Configuration")
        
        instrument_type = st.radio(
            "Instrument Type",
            ["Index Options", "Equity Options", "Commodity Futures"],
            key="oc_instr_type"
        )
        
        is_index = instrument_type == "Index Options"
        is_commodity = instrument_type == "Commodity Futures"

        if is_index:
            symbol = st.selectbox("Index", list(INDEX_SYMBOLS.keys()), key="oc_index_select")
            if symbol in NSE_UNSUPPORTED_INDICES:
                if is_fyers_available():
                    st.caption(f"✅ {symbol} available via FYERS")
                else:
                    st.caption(
                        f"ℹ️ {symbol} (BSE index) — connect FYERS account above for access"
                    )
        elif is_commodity:
            symbol = st.selectbox("Commodity", list(COMMODITY_SYMBOLS.keys()), key="oc_commodity_select")
            st.caption(f"💰 Trading {symbol} futures via MCX")
        else:
            raw_symbol = st.text_input(
                "Stock Symbol (e.g. RELIANCE, TCS, INFY, SBIN, HDFCBANK)", 
                "RELIANCE", 
                key="oc_stock_input"
            )
            symbol = normalize_stock_symbol(raw_symbol)

        strike_count = st.slider("Strikes Around ATM", 5, 40, 15, step=5, key="oc_strike_count")
        show_greeks = st.checkbox("Show Greeks columns", value=True, key="oc_show_greeks")
        
        default_lot = DEFAULT_LOT_SIZES.get(symbol, DEFAULT_LOT_SIZES["_STOCK_DEFAULT"])
        lot_size = st.number_input(
            "Lot Size (for GEX/DEX)", 
            min_value=1, 
            value=default_lot, 
            step=1, 
            key="oc_lot_size",
        )

        st.divider()
        auto_refresh = st.checkbox("Enable auto-refresh", value=False, key="oc_auto_refresh")
        refresh_secs = st.slider(
            "Refresh interval (seconds)", 
            10, 120, 20, step=5,
            key="oc_refresh_secs", 
            disabled=not auto_refresh
        )

        st.divider()
        fetch_clicked = st.button("🔄 Fetch Live Data", use_container_width=True, type="primary")

    return {
        "is_index": is_index,
        "is_commodity": is_commodity,
        "symbol": symbol,
        "strike_count": strike_count,
        "show_greeks": show_greeks,
        "lot_size": lot_size,
        "auto_refresh": auto_refresh,
        "refresh_secs": refresh_secs,
        "fetch_clicked": fetch_clicked,
    }


def _do_fetch_and_process(cfg: dict) -> Optional[dict]:
    """Fetch → parse → validate → analytics pipeline."""
    
    preferred_expiry = st.session_state.get("oc_selected_expiry", "")
    
    fetch_result = fetch_option_chain_raw(
        cfg["symbol"], cfg["is_index"], cfg["is_commodity"]
    )

    if not fetch_result.get("ok"):
        st.error(
            f"⚠️ Could not fetch option chain for **{cfg['symbol']}**: "
            f"{fetch_result.get('error') or 'Unknown error.'}\n\n"
            f"**Troubleshooting:**\n"
            f"• NSE API may be unavailable\n"
            f"• For BSE indices (SENSEX, BANKEX), connect FYERS account in sidebar\n"
            f"• Try a different index (NIFTY, BANKNIFTY, FINNIFTY)\n"
            f"• NSE may rate-limit — wait and retry"
        )
        return None

    payload: dict = fetch_result["payload"]
    data_source: str = fetch_result.get("source", "NSE")

    df_all, meta = parse_option_chain(payload, preferred_expiry)

    if not validate_chain_df(df_all):
        st.error(f"⚠️ Received response but no usable option chain for **{cfg['symbol']}**.")
        return None

    spot = meta["spot_price"]
    df = filter_strikes_around_atm(df_all, spot, cfg["strike_count"])
    if df.empty:
        df = df_all

    expiry_label = meta["selected_expiry"]
    atm_strike = float(df.iloc[(df["strike_price"] - spot).abs().argsort().iloc[0]]["strike_price"]) if spot else \
        float(df["strike_price"].median())

    df = add_greeks_columns(df, spot, expiry_label)
    
    pcr = calc_pcr(df)
    max_pain = calc_max_pain(df)
    support, resistance = calc_support_resistance(df)
    max_oi = calc_max_oi(df)

    atm_iv = _atm_iv(df, spot)
    update_iv_history(cfg["symbol"], expiry_label, atm_iv)
    iv_rank, iv_percentile = compute_iv_rank_percentile(cfg["symbol"], expiry_label, atm_iv)

    gex_dex = compute_gex_dex(df, spot, cfg["lot_size"])

    return {
        "df": df, 
        "meta": meta, 
        "spot": spot, 
        "atm_strike": atm_strike, 
        "expiry_label": expiry_label,
        "pcr": pcr, 
        "max_pain": max_pain, 
        "support": support, 
        "resistance": resistance, 
        "max_oi": max_oi,
        "atm_iv": atm_iv, 
        "iv_rank": iv_rank, 
        "iv_percentile": iv_percentile, 
        "gex_dex": gex_dex,
        "data_source": data_source,
    }


def _render_summary_cards(state: dict) -> None:
    """Render summary metric cards."""
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Spot Price", f"₹{state['spot']:,.2f}" if state["spot"] else "—")
    c2.metric("ATM Strike", f"₹{state['atm_strike']:,.0f}")
    c3.metric("PCR", f"{state['pcr']:.3f}")
    c4.metric("Max Pain", f"₹{state['max_pain']:,.0f}")
    c5.metric("IV Rank / %ile", f"{state['iv_rank']:.0f} / {state['iv_percentile']:.0f}")

    c6, c7, c8, c9, c10 = st.columns(5)
    c6.metric("Support", f"₹{state['support']:,.0f}" if state["support"] else "—")
    c7.metric("Resistance", f"₹{state['resistance']:,.0f}" if state["resistance"] else "—")
    c8.metric("Total GEX", f"{state['gex_dex'].get('total_gex', 0):,.0f}")
    c9.metric("Total DEX", f"{state['gex_dex'].get('total_dex', 0):,.0f}")
    c10.metric("Data Source", state.get("data_source", "NSE"))


def run_dashboard() -> None:
    """Main dashboard entry point."""
    _configure_page()
    _inject_css()
    
    st.markdown("## 📊 NSE Options Chain Dashboard + FYERS")
    st.markdown("**✅ NSE INDEX OPTIONS • ✅ EQUITY OPTIONS • ✅ BSE via FYERS • ✅ COMMODITY FUTURES**")
    st.markdown("Greeks Engine + Gamma Exposure + IV Rank + Multi-Source Fallback")

    cfg = _sidebar_config()

    if cfg["symbol"] != st.session_state.get("oc_last_symbol"):
        st.session_state["oc_last_symbol"] = cfg["symbol"]
        st.session_state.pop("oc_state", None)
        st.session_state.pop("oc_selected_expiry", None)

    if cfg["fetch_clicked"] or cfg["auto_refresh"]:
        with st.spinner(f"Fetching {cfg['symbol']} …"):
            result = _do_fetch_and_process(cfg)
        if result is not None:
            st.session_state["oc_state"] = result
            st.session_state["oc_selected_expiry"] = result["expiry_label"]

    state = st.session_state.get("oc_state")
    
    # Main tabs
    tab_chain, tab_charts, tab_greeks, tab_export = st.tabs([
        "📋 Option Chain",
        "📈 Charts",
        "🧮 Greeks",
        "📥 Export",
    ])

    with tab_chain:
        if state is None:
            st.info("👈 Select instrument in sidebar and click **Fetch Live Data**")
        else:
            df = state["df"]
            _render_summary_cards(state)
            st.markdown(render_chain_table_html(df, cfg["show_greeks"]), unsafe_allow_html=True)

    with tab_charts:
        if state is None:
            st.info("👈 Fetch data first")
        else:
            df = state["df"]
            st.plotly_chart(
                chart_oi_bars(df, state["max_pain"]), 
                use_container_width=True,
                config={"displayModeBar": False}
            )
            col_a, col_b = st.columns(2)
            with col_a:
                st.plotly_chart(
                    gauge_pcr(state["pcr"]), 
                    use_container_width=True, 
                    config={"displayModeBar": False}
                )
            st.plotly_chart(
                chart_iv_skew(df), 
                use_container_width=True, 
                config={"displayModeBar": False}
            )

    with tab_greeks:
        if state is None:
            st.info("👈 Fetch data first")
        else:
            st.info("Greeks visualization & analysis coming soon...")

    with tab_export:
        if state is None:
            st.info("👈 Fetch data first")
        else:
            st.markdown('<div class="block-title">📥 Export</div>', unsafe_allow_html=True)
            df = state["df"]
            try:
                csv_bytes = export_csv_bytes(df)
                st.download_button(
                    "⬇️ Download CSV",
                    data=csv_bytes,
                    file_name=f"option_chain_{cfg['symbol']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv",
                    use_container_width=True,
                )
            except Exception as e:
                st.error(f"Export failed: {e}")

    if state is not None and cfg["auto_refresh"]:
        time.sleep(cfg["refresh_secs"])
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    run_dashboard()
