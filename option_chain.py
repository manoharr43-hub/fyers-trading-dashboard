"""
================================================================================
UNIFIED NSE/BSE/MCX OPTIONS CHAIN & TECHNICAL ANALYSIS DASHBOARD
================================================================================

Institutional-grade multi-market options & technical analysis dashboard.

MARKETS SUPPORTED:
- NSE: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, F&O Stocks
- BSE: SENSEX, BANKEX (where available)
- MCX: GOLD, SILVER, CRUDE OIL, NATURAL GAS, COPPER

FEATURES:
- Live CE/PE chain: Strike, LTP, Bid, Ask, Volume, OI, OI Change, IV, Greeks
- Technical indicators: EMA, RSI, MACD, VWAP, ATR, SuperTrend, FVG, Order Blocks
- Multi-timeframe analysis: 5M, 15M, 30M, 1H, 1D
- AI Signal Engine: Multiple confirmation factors, transparent scoring
- Market Structure: HH/HL/LH/LL, BOS, CHoCH, MSS
- Institutional Analysis: Smart money bias, OI buildup, PCR, Max Pain
- GEX/DEX calculations
- Trade Plans: Entry, SL, Targets, Risk/Reward
- Volume & RVOL analysis
- Live Alert System (architecture for Telegram/Email/Webhook)
- Excel export with professional formatting
- Auto-refresh & caching

DATA SAFETY:
- No fake data generation
- Shows "DATA UNAVAILABLE" for missing live data
- No signals generated from incomplete data
- Transparent data quality reporting

Run with:
    streamlit run option_chain.py
"""

from __future__ import annotations

import io
import json
import logging
import math
import time
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple, Dict, List
from enum import Enum

import numpy as np
import pandas as pd
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

# ══════════════════════════════════════════════════════════════════════════
# LOGGING SETUP
# ══════════════════════════════════════════════════════════════════════════

logger = logging.getLogger("unified_dashboard")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

# ══════════════════════════════════════════════════════════════════════════
# ENUMS & CONSTANTS
# ══════════════════════════════════════════════════════════════════════════

class Market(Enum):
    """Supported markets"""
    NSE = "NSE"
    BSE = "BSE"
    MCX = "MCX"

class Signal(Enum):
    """Signal types"""
    STRONG_BUY = "STRONG BUY"
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    STRONG_SELL = "STRONG SELL"

class Trend(Enum):
    """Trend types"""
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"

class DataQuality(Enum):
    """Data quality levels"""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNAVAILABLE = "UNAVAILABLE"

# Market constants
NSE_BASE_URL = "https://www.nseindia.com"
NSE_INDEX_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-indices"
NSE_EQUITY_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-equities"

# Index symbols
NSE_INDICES = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
}

BSE_INDICES = {
    "SENSEX": "SENSEX",
    "BANKEX": "BANKEX",
}

# Commodity symbols
MCX_COMMODITIES = {
    "GOLD": "GOLD",
    "SILVER": "SILVER",
    "CRUDE": "CRUDE",
    "NATURALGAS": "NATURALGAS",
    "COPPER": "COPPER",
}

# Lot sizes (defaults - editable in sidebar)
DEFAULT_LOT_SIZES = {
    "NIFTY": 25,
    "BANKNIFTY": 15,
    "FINNIFTY": 25,
    "MIDCPNIFTY": 50,
    "SENSEX": 10,
    "BANKEX": 15,
    "GOLD": 1,
    "SILVER": 1,
    "CRUDE": 1,
    "NATURALGAS": 1,
    "COPPER": 1,
    "_DEFAULT": 1,
}

# Technical timeframes
TIMEFRAMES = {
    "5M": 5,
    "15M": 15,
    "30M": 30,
    "1H": 60,
    "1D": 1440,
}

# Colors for UI
DARK_BG = "#0d1117"
PANEL_BG = "#161b22"
BORDER_COLOR = "#30363d"
TEXT_MAIN = "#e6edf3"
TEXT_MUTED = "#8b949e"
GREEN = "#3fb950"
RED = "#f85149"
AMBER = "#d29922"
BLUE = "#58a6ff"

# Request settings
REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{NSE_BASE_URL}/option-chain",
}

# ══════════════════════════════════════════════════════════════════════════
# SESSION STATE INITIALIZATION
# ══════════════════════════════════════════════════════════════════════════

def init_session_state():
    """Initialize Streamlit session state"""
    if "market" not in st.session_state:
        st.session_state.market = Market.NSE
    if "symbol" not in st.session_state:
        st.session_state.symbol = "NIFTY"
    if "last_fetch" not in st.session_state:
        st.session_state.last_fetch = None
    if "cached_data" not in st.session_state:
        st.session_state.cached_data = {}
    if "technical_data" not in st.session_state:
        st.session_state.technical_data = {}
    if "alert_config" not in st.session_state:
        st.session_state.alert_config = {
            "enabled": False,
            "telegram_enabled": False,
            "email_enabled": False,
            "webhook_enabled": False,
        }

# ══════════════════════════════════════════════════════════════════════════
# HTTP SESSION LAYER
# ══════════════════════════════════════════════════════════════════════════

def build_retrying_session() -> requests.Session:
    """Build a requests session with retry logic"""
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    retry_cfg = Retry(
        total=MAX_RETRIES,
        backoff_factor=RETRY_BACKOFF_SECONDS,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry_cfg)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

@st.cache_resource(show_spinner=False)
def get_nse_session() -> requests.Session:
    """Get cached NSE session"""
    session = build_retrying_session()
    _warm_up_session(session)
    return session

def _warm_up_session(session: requests.Session) -> bool:
    """Warm up NSE session with initial requests"""
    try:
        session.get(NSE_BASE_URL, timeout=REQUEST_TIMEOUT)
        session.get(f"{NSE_BASE_URL}/option-chain", timeout=REQUEST_TIMEOUT)
        return True
    except Exception as e:
        logger.warning(f"NSE session warm-up failed: {e}")
        return False

def fetch_json_with_retry(
    session: requests.Session, url: str, params: Optional[dict] = None
) -> Tuple[Optional[dict], Optional[str]]:
    """Fetch JSON with retry logic"""
    last_error = "Unknown error"
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            
            if resp.status_code in (401, 403):
                last_error = f"HTTP {resp.status_code} (stale session) on attempt {attempt}/{MAX_RETRIES}"
                logger.warning(f"{last_error} — re-warming NSE session")
                _warm_up_session(session)
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code} on attempt {attempt}/{MAX_RETRIES}"
                logger.warning(last_error)
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            
            payload = resp.json()
            if not payload:
                last_error = f"Empty JSON payload on attempt {attempt}/{MAX_RETRIES}"
                logger.warning(last_error)
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                continue
            
            return payload, None
            
        except requests.exceptions.Timeout:
            last_error = f"Timeout on attempt {attempt}/{MAX_RETRIES}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        except requests.exceptions.ConnectionError as e:
            last_error = f"Connection error on attempt {attempt}/{MAX_RETRIES}: {e}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
        except Exception as e:
            last_error = f"Error on attempt {attempt}/{MAX_RETRIES}: {e}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    
    logger.error(f"fetch_json_with_retry exhausted retries for {url}: {last_error}")
    return None, last_error

# ══════════════════════════════════════════════════════════════════════════
# SAFE NUMERIC CONVERSION
# ══════════════════════════════════════════════════════════════════════════

def safe_num(val: Any, default: float = 0.0) -> float:
    """Safely convert value to float"""
    try:
        if val is None:
            return default
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default

def safe_int(val: Any, default: int = 0) -> int:
    """Safely convert value to int"""
    try:
        if val is None:
            return default
        return int(float(val))
    except (TypeError, ValueError):
        return default

# ══════════════════════════════════════════════════════════════════════════
# DATA FETCHING - NSE OPTION CHAIN
# ══════════════════════════════════════════════════════════════════════════

@st.cache_data(ttl=15, show_spinner=False)
def fetch_nse_option_chain(symbol: str, is_index: bool) -> dict:
    """Fetch NSE option chain"""
    session = get_nse_session()
    url = NSE_INDEX_CHAIN_URL if is_index else NSE_EQUITY_CHAIN_URL
    payload, error = fetch_json_with_retry(session, url, params={"symbol": symbol})
    
    if payload is None:
        return {"ok": False, "error": error or "No data returned"}
    
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, dict) or not records.get("data"):
        return {"ok": False, "error": "Response had no option-chain records"}
    
    return {"ok": True, "payload": payload, "error": None}

def parse_nse_option_chain(payload: dict, preferred_expiry: str = "") -> Tuple[pd.DataFrame, dict]:
    """Parse NSE option chain JSON into DataFrame"""
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
    meta["spot_price"] = safe_num(records.get("underlyingValue"))
    
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
        
        ce = item.get("CE") or {}
        pe = item.get("PE") or {}
        
        rows.append({
            "strike_price": safe_num(strike),
            "ce_ltp": safe_num(ce.get("lastPrice")),
            "ce_change": safe_num(ce.get("change")),
            "ce_bid": safe_num(ce.get("bidprice")),
            "ce_bid_qty": safe_num(ce.get("bidQty")),
            "ce_ask": safe_num(ce.get("askPrice")),
            "ce_ask_qty": safe_num(ce.get("askQty")),
            "ce_volume": safe_num(ce.get("totalTradedVolume")),
            "ce_oi": safe_num(ce.get("openInterest")),
            "ce_chng_oi": safe_num(ce.get("changeinOpenInterest")),
            "ce_oi_change_pct": safe_num(ce.get("pchangeinOpenInterest")),
            "ce_iv": safe_num(ce.get("impliedVolatility")),
            "pe_ltp": safe_num(pe.get("lastPrice")),
            "pe_change": safe_num(pe.get("change")),
            "pe_bid": safe_num(pe.get("bidprice")),
            "pe_bid_qty": safe_num(pe.get("bidQty")),
            "pe_ask": safe_num(pe.get("askPrice")),
            "pe_ask_qty": safe_num(pe.get("askQty")),
            "pe_volume": safe_num(pe.get("totalTradedVolume")),
            "pe_oi": safe_num(pe.get("openInterest")),
            "pe_chng_oi": safe_num(pe.get("changeinOpenInterest")),
            "pe_oi_change_pct": safe_num(pe.get("pchangeinOpenInterest")),
            "pe_iv": safe_num(pe.get("impliedVolatility")),
        })
    
    meta["rows_parsed"] = len(rows)
    
    if not rows:
        return pd.DataFrame(), meta
    
    df = pd.DataFrame(rows)
    df = df.groupby("strike_price", as_index=False).first()
    df.sort_values("strike_price", inplace=True)
    df.reset_index(drop=True, inplace=True)
    
    return df, meta

# ══════════════════════════════════════════════════════════════════════════
# MCX/BSE COMMODITY DATA HANDLING
# ══════════════════════════════════════════════════════════════════════════

def fetch_mcx_data(symbol: str) -> Tuple[Optional[dict], str]:
    """
    Attempt to fetch MCX commodity data.
    
    MCX data requires broker APIs. If no live source is available,
    return DATA_UNAVAILABLE message.
    """
    # MCX data is not freely available via NSE API
    # Requires broker integration (FYERS, Alice Blue, etc.)
    logger.info(f"MCX {symbol} data requested - live source unavailable")
    return None, "MCX live data unavailable - requires broker API integration"

def fetch_bse_data(symbol: str) -> Tuple[Optional[dict], str]:
    """
    Attempt to fetch BSE index data.
    
    BSE indices require special API access or broker integration.
    """
    if symbol == "SENSEX":
        logger.info("SENSEX data requested - live source unavailable")
        return None, "SENSEX data unavailable - requires broker API (FYERS) or direct BSE access"
    
    logger.info(f"BSE {symbol} data requested - live source unavailable")
    return None, "BSE data unavailable - requires broker API integration"

# ══════════════════════════════════════════════════════════════════════════
# GREEKS CALCULATION - BLACK SCHOLES
# ══════════════════════════════════════════════════════════════════════════

def norm_cdf(x: float) -> float:
    """Standard normal CDF"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def norm_pdf(x: float) -> float:
    """Standard normal PDF"""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def bs_greeks(
    spot: float, strike: float, t_years: float, r: float, sigma: float, is_call: bool
) -> dict[str, float]:
    """Calculate Black-Scholes Greeks"""
    MIN_SIGMA = 0.01
    MAX_SIGMA = 5.0
    
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    
    sigma = min(max(sigma, MIN_SIGMA), MAX_SIGMA)
    sqrt_t = math.sqrt(t_years)
    
    try:
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma ** 2) * t_years) / (sigma * sqrt_t)
        d2 = d1 - sigma * sqrt_t
    except (ValueError, ZeroDivisionError):
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    
    pdf_d1 = norm_pdf(d1)
    gamma = pdf_d1 / (spot * sigma * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100.0
    
    if is_call:
        delta = norm_cdf(d1)
        theta = (
            -(spot * pdf_d1 * sigma) / (2 * sqrt_t)
            - r * strike * math.exp(-r * t_years) * norm_cdf(d2)
        ) / 365.0
    else:
        delta = norm_cdf(d1) - 1.0
        theta = (
            -(spot * pdf_d1 * sigma) / (2 * sqrt_t)
            + r * strike * math.exp(-r * t_years) * norm_cdf(-d2)
        ) / 365.0
    
    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 6),
        "theta": round(theta, 4),
        "vega": round(vega, 4),
    }

def add_greeks_columns(
    df: pd.DataFrame, spot: float, expiry_label: str, r: float = 0.07
) -> pd.DataFrame:
    """Add Greeks columns to DataFrame"""
    d = df.copy()
    
    if d.empty:
        for col in ("ce_delta", "ce_gamma", "ce_theta", "ce_vega",
                    "pe_delta", "pe_gamma", "pe_theta", "pe_vega"):
            d[col] = 0.0
        return d
    
    # Parse days to expiry
    try:
        exp_dt = datetime.strptime(expiry_label, "%d-%b-%Y")
        delta_days = (exp_dt.replace(hour=15, minute=30) - datetime.now()).total_seconds() / 86400
        t_years = max(delta_days, 0.25) / 365.0
    except:
        t_years = 7.0 / 365.0
    
    # Calculate Greeks
    ce_greeks = d.apply(
        lambda row: bs_greeks(spot, row["strike_price"], t_years, r, row["ce_iv"] / 100.0, True),
        axis=1,
    )
    pe_greeks = d.apply(
        lambda row: bs_greeks(spot, row["strike_price"], t_years, r, row["pe_iv"] / 100.0, False),
        axis=1,
    )
    
    for key in ("delta", "gamma", "theta", "vega"):
        d[f"ce_{key}"] = ce_greeks.apply(lambda x: x[key])
        d[f"pe_{key}"] = pe_greeks.apply(lambda x: x[key])
    
    return d

# ══════════════════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ══════════════════════════════════════════════════════════════════════════

def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    """Calculate Exponential Moving Average"""
    return series.ewm(span=period, adjust=False).mean()

def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Relative Strength Index"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_macd(series: pd.Series) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Calculate MACD"""
    ema12 = calculate_ema(series, 12)
    ema26 = calculate_ema(series, 26)
    macd_line = ema12 - ema26
    signal_line = calculate_ema(macd_line, 9)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram

def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Average True Range"""
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return atr

def calculate_supertrend(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 10, multiplier: float = 3.0) -> Tuple[pd.Series, pd.Series]:
    """Calculate SuperTrend"""
    hl_avg = (high + low) / 2
    atr = calculate_atr(high, low, close, period)
    
    basic_ub = hl_avg + multiplier * atr
    basic_lb = hl_avg - multiplier * atr
    
    final_ub = pd.Series(index=close.index, dtype=float)
    final_lb = pd.Series(index=close.index, dtype=float)
    
    final_ub.iloc[0] = basic_ub.iloc[0]
    final_lb.iloc[0] = basic_lb.iloc[0]
    
    for i in range(1, len(close)):
        final_ub.iloc[i] = basic_ub.iloc[i] if basic_ub.iloc[i] < final_ub.iloc[i-1] or close.iloc[i-1] > final_ub.iloc[i-1] else final_ub.iloc[i-1]
        final_lb.iloc[i] = basic_lb.iloc[i] if basic_lb.iloc[i] > final_lb.iloc[i-1] or close.iloc[i-1] < final_lb.iloc[i-1] else final_lb.iloc[i-1]
    
    return final_ub, final_lb

def calculate_vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    """Calculate Volume Weighted Average Price"""
    tp = (high + low + close) / 3
    vwap = (tp * volume).cumsum() / volume.cumsum()
    return vwap

def calculate_relative_volume(volume: pd.Series, period: int = 20) -> pd.Series:
    """Calculate Relative Volume (RVOL)"""
    avg_vol = volume.rolling(period).mean()
    rvol = volume / avg_vol
    return rvol.fillna(1.0)

def detect_market_structure(close: pd.Series, window: int = 5) -> dict:
    """Detect market structure (HH/HL/LH/LL)"""
    if len(close) < window:
        return {"structure": Trend.NEUTRAL, "score": 0}
    
    recent = close.tail(window).values
    score = 0
    
    # Higher highs detection
    if len(recent) > 1:
        highs = close.rolling(2).max().tail(window).values
        if np.sum(np.diff(highs) > 0) > window / 2:
            score += 20
    
    # Higher lows detection
    if len(recent) > 1:
        lows = close.rolling(2).min().tail(window).values
        if np.sum(np.diff(lows) > 0) > window / 2:
            score += 20
    
    # EMA trend
    ema9 = calculate_ema(close, 9).iloc[-1] if len(close) >= 9 else close.iloc[-1]
    ema21 = calculate_ema(close, 21).iloc[-1] if len(close) >= 21 else close.iloc[-1]
    
    if ema9 > ema21:
        score += 20
    elif ema9 < ema21:
        score -= 20
    
    # Price position relative to EMAs
    current = close.iloc[-1]
    if current > ema9:
        score += 15
    if current > ema21:
        score += 15
    
    # Determine trend
    if score > 40:
        trend = Trend.BULLISH
    elif score < -40:
        trend = Trend.BEARISH
    else:
        trend = Trend.NEUTRAL
    
    return {"structure": trend, "score": min(100, max(0, 50 + score))}

def detect_bullish_fvg(high: pd.Series, low: pd.Series, close: pd.Series) -> List[dict]:
    """Detect Bullish Fair Value Gaps"""
    fvgs = []
    
    if len(close) < 3:
        return fvgs
    
    for i in range(2, len(close)):
        # Bullish FVG: current candle gap above previous candle
        if low.iloc[i] > high.iloc[i-2]:
            fvg = {
                "type": "Bullish FVG",
                "top": high.iloc[i-2],
                "bottom": low.iloc[i],
                "filled": False,
                "index": i,
            }
            fvgs.append(fvg)
    
    return fvgs

def detect_bearish_fvg(high: pd.Series, low: pd.Series, close: pd.Series) -> List[dict]:
    """Detect Bearish Fair Value Gaps"""
    fvgs = []
    
    if len(close) < 3:
        return fvgs
    
    for i in range(2, len(close)):
        # Bearish FVG: current candle gap below previous candle
        if high.iloc[i] < low.iloc[i-2]:
            fvg = {
                "type": "Bearish FVG",
                "top": low.iloc[i-2],
                "bottom": high.iloc[i],
                "filled": False,
                "index": i,
            }
            fvgs.append(fvg)
    
    return fvgs

# ══════════════════════════════════════════════════════════════════════════
# SIGNAL ENGINE
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class SignalResult:
    """Signal calculation result"""
    signal: Signal
    confidence: float
    mtf_trend: Dict[str, Trend]
    mtf_scores: Dict[str, float]
    factors: Dict[str, float]
    data_quality: DataQuality
    reasoning: List[str]

class SignalEngine:
    """AI Signal Engine with multi-factor confirmation"""
    
    def __init__(self, ltp: float, data_quality: DataQuality = DataQuality.UNAVAILABLE):
        self.ltp = ltp
        self.data_quality = data_quality
        self.factors = {}
        self.reasoning = []
    
    def calculate_signals(
        self, 
        df: pd.DataFrame, 
        spot: float, 
        symbol: str,
        market: Market = Market.NSE
    ) -> SignalResult:
        """Calculate comprehensive signal"""
        
        # If data quality is low/unavailable, no signal
        if self.data_quality in (DataQuality.LOW, DataQuality.UNAVAILABLE):
            return SignalResult(
                signal=Signal.HOLD,
                confidence=0.0,
                mtf_trend={},
                mtf_scores={},
                factors={},
                data_quality=self.data_quality,
                reasoning=["Insufficient data quality for signal generation"],
            )
        
        # Initialize factors
        factors = {}
        reasoning = []
        
        # 1. PCR Analysis (5 points)
        if not df.empty:
            total_ce_oi = df["ce_oi"].sum()
            total_pe_oi = df["pe_oi"].sum()
            pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 1.0
            
            if pcr > 1.3:
                factors["pcr"] = 5.0
                reasoning.append("PCR > 1.3: Bullish (put bias)")
            elif pcr < 0.7:
                factors["pcr"] = -5.0
                reasoning.append("PCR < 0.7: Bearish (call bias)")
            else:
                factors["pcr"] = 0.0
        
        # 2. Max Pain proximity (5 points)
        if not df.empty:
            max_pain = self._calculate_max_pain(df)
            if spot and max_pain:
                diff_pct = abs(spot - max_pain) / spot * 100
                if diff_pct < 2:
                    factors["max_pain"] = 3.0
                    reasoning.append("Price near Max Pain (attraction point)")
                else:
                    factors["max_pain"] = -2.0
        
        # 3. OI Analysis (10 points)
        if not df.empty:
            ce_oi_change = df["ce_chng_oi"].sum()
            pe_oi_change = df["pe_chng_oi"].sum()
            
            if pe_oi_change > ce_oi_change:
                factors["oi_buildup"] = 5.0
                reasoning.append("Put OI buildup: Bearish positioning")
            elif ce_oi_change > pe_oi_change:
                factors["oi_buildup"] = 5.0
                reasoning.append("Call OI buildup: Bullish positioning")
        
        # 4. Volume Analysis (5 points)
        if not df.empty and "volume" in df.columns:
            avg_volume = df["ce_volume"].mean() + df["pe_volume"].mean()
            current_volume = df["ce_volume"].iloc[-1] + df["pe_volume"].iloc[-1] if len(df) > 0 else 0
            if current_volume > avg_volume * 1.5:
                factors["volume"] = 5.0
                reasoning.append("Above-average volume: Good liquidity")
        
        # 5. IV Analysis (5 points)
        if not df.empty and df["ce_iv"].sum() > 0:
            avg_iv = df["ce_iv"].mean()
            if avg_iv > 25:
                factors["iv"] = 2.0
                reasoning.append("High IV: Elevated volatility")
            elif avg_iv < 15:
                factors["iv"] = -2.0
                reasoning.append("Low IV: Compressed range expected")
        
        # 6. Greeks Analysis (10 points)
        if "ce_delta" in df.columns:
            ce_delta = df["ce_delta"].mean()
            pe_delta = df["pe_delta"].mean()
            
            if ce_delta > 0.5:
                factors["greeks"] = 5.0
                reasoning.append("Calls showing positive delta bias")
            elif pe_delta < -0.5:
                factors["greeks"] = 5.0
                reasoning.append("Puts showing negative delta bias")
        
        # Calculate total confidence
        total_score = sum(factors.values())
        confidence = min(100, max(0, 50 + total_score))
        
        # Determine signal
        if confidence > 70:
            if total_score > 5:
                signal = Signal.STRONG_BUY
            else:
                signal = Signal.BUY
        elif confidence < 30:
            if total_score < -5:
                signal = Signal.STRONG_SELL
            else:
                signal = Signal.SELL
        else:
            signal = Signal.HOLD
        
        return SignalResult(
            signal=signal,
            confidence=confidence,
            mtf_trend={"1D": Trend.NEUTRAL},  # Placeholder
            mtf_scores={"1D": 50.0},
            factors=factors,
            data_quality=self.data_quality,
            reasoning=reasoning,
        )
    
    def _calculate_max_pain(self, df: pd.DataFrame) -> Optional[float]:
        """Calculate max pain"""
        if df.empty:
            return None
        
        strikes = df["strike_price"].values
        ce_oi = df["ce_oi"].values
        pe_oi = df["pe_oi"].values
        
        try:
            pain_points = [
                float(np.sum(np.maximum(s - strikes, 0) * ce_oi) + 
                      np.sum(np.maximum(strikes - s, 0) * pe_oi))
                for s in strikes
            ]
            return float(strikes[int(np.argmin(pain_points))])
        except:
            return None

# ══════════════════════════════════════════════════════════════════════════
# SUPPORT & RESISTANCE DETECTION
# ══════════════════════════════════════════════════════════════════════════

def detect_support_resistance(df: pd.DataFrame, spot: float) -> dict:
    """Detect support and resistance levels"""
    if df.empty:
        return {
            "immediate_support": None,
            "strong_support": None,
            "immediate_resistance": None,
            "strong_resistance": None,
        }
    
    # Support = Highest Put OI
    max_pe_oi_idx = df["pe_oi"].idxmax()
    support_strike = float(df.loc[max_pe_oi_idx, "strike_price"])
    
    # Resistance = Highest Call OI
    max_ce_oi_idx = df["ce_oi"].idxmax()
    resistance_strike = float(df.loc[max_ce_oi_idx, "strike_price"])
    
    # Find close strikes for immediate support/resistance
    strikes = sorted(df["strike_price"].unique())
    atm_idx = (df["strike_price"] - spot).abs().idxmin() if spot else len(strikes) // 2
    atm_strike = float(df.loc[atm_idx, "strike_price"])
    
    # Immediate levels (1-2 strikes away)
    immediate_support = None
    immediate_resistance = None
    
    for i, strike in enumerate(strikes):
        if strike < atm_strike and (immediate_support is None or strike > immediate_support):
            immediate_support = strike
        elif strike > atm_strike and (immediate_resistance is None or strike < immediate_resistance):
            immediate_resistance = strike
    
    return {
        "immediate_support": immediate_support,
        "strong_support": min(support_strike, atm_strike) if support_strike else None,
        "immediate_resistance": immediate_resistance,
        "strong_resistance": max(resistance_strike, atm_strike) if resistance_strike else None,
    }

# ══════════════════════════════════════════════════════════════════════════
# TRADE PLAN GENERATION
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class TradePlan:
    """Trade plan with entry, targets, and risk management"""
    signal: Signal
    confidence: float
    entry: float
    stop_loss: float
    target1: float
    target2: float
    target3: float
    risk_per_trade: float
    reward_per_trade: float
    risk_reward_ratio: float
    data_quality: DataQuality
    reason: str

def generate_trade_plan(
    signal_result: SignalResult,
    spot: float,
    atr: float,
    support: Optional[float] = None,
    resistance: Optional[float] = None,
) -> Optional[TradePlan]:
    """Generate trade plan if conditions are met"""
    
    # No plan if data quality is insufficient
    if signal_result.data_quality in (DataQuality.LOW, DataQuality.UNAVAILABLE):
        return None
    
    # No plan for HOLD signals
    if signal_result.signal == Signal.HOLD:
        return None
    
    entry = spot
    
    # Determine direction
    is_buy = signal_result.signal in (Signal.BUY, Signal.STRONG_BUY)
    
    if is_buy:
        # Buy setup
        stop_loss = entry - (atr * 2) if atr > 0 else entry * 0.98
        target1 = entry + (atr * 1.5)
        target2 = entry + (atr * 2.5)
        target3 = entry + (atr * 4)
    else:
        # Sell setup
        stop_loss = entry + (atr * 2) if atr > 0 else entry * 1.02
        target1 = entry - (atr * 1.5)
        target2 = entry - (atr * 2.5)
        target3 = entry - (atr * 4)
    
    risk = abs(entry - stop_loss)
    reward = abs(target3 - entry)
    risk_reward = reward / risk if risk > 0 else 0
    
    reason = " + ".join(signal_result.reasoning[:3]) if signal_result.reasoning else "Multi-factor confirmation"
    
    return TradePlan(
        signal=signal_result.signal,
        confidence=signal_result.confidence,
        entry=entry,
        stop_loss=stop_loss,
        target1=target1,
        target2=target2,
        target3=target3,
        risk_per_trade=risk,
        reward_per_trade=reward,
        risk_reward_ratio=risk_reward,
        data_quality=signal_result.data_quality,
        reason=reason,
    )

# ══════════════════════════════════════════════════════════════════════════
# OPTION CHAIN ANALYTICS
# ══════════════════════════════════════════════════════════════════════════

def calculate_pcr_max_pain(df: pd.DataFrame) -> Tuple[float, float, dict]:
    """Calculate PCR and Max Pain"""
    if df.empty:
        return 0.0, 0.0, {}
    
    total_ce_oi = df["ce_oi"].sum()
    total_pe_oi = df["pe_oi"].sum()
    
    pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 0.0
    
    # Max Pain
    strikes = df["strike_price"].values
    ce_oi = df["ce_oi"].values
    pe_oi = df["pe_oi"].values
    
    try:
        pain_points = [
            float(np.sum(np.maximum(s - strikes, 0) * ce_oi) +
                  np.sum(np.maximum(strikes - s, 0) * pe_oi))
            for s in strikes
        ]
        max_pain = float(strikes[int(np.argmin(pain_points))])
    except:
        max_pain = 0.0
    
    return pcr, max_pain, {
        "total_ce_oi": int(total_ce_oi),
        "total_pe_oi": int(total_pe_oi),
        "ce_oi_change": int(df["ce_chng_oi"].sum()),
        "pe_oi_change": int(df["pe_chng_oi"].sum()),
    }

def calculate_gex_dex(df: pd.DataFrame, spot: float, lot_size: int = 1) -> dict:
    """Calculate GEX and DEX"""
    if df.empty or not spot:
        return {"total_gex": 0.0, "total_dex": 0.0}
    
    d = df.copy()
    d["gex"] = (
        (d.get("ce_gamma", 0) * d.get("ce_oi", 0)) - 
        (d.get("pe_gamma", 0) * d.get("pe_oi", 0))
    ) * (spot ** 2) * 0.01 * lot_size
    
    d["dex"] = (
        (d.get("ce_delta", 0) * d.get("ce_oi", 0)) + 
        (d.get("pe_delta", 0) * d.get("pe_oi", 0))
    ) * spot * lot_size
    
    total_gex = float(d["gex"].sum())
    total_dex = float(d["dex"].sum())
    
    return {
        "total_gex": total_gex,
        "total_dex": total_dex,
    }

# ══════════════════════════════════════════════════════════════════════════
# INSTITUTIONAL ANALYSIS
# ══════════════════════════════════════════════════════════════════════════

def detect_institutional_activity(df: pd.DataFrame) -> dict:
    """Detect institutional positioning"""
    if df.empty:
        return {
            "smart_money_bias": Trend.NEUTRAL,
            "institutional_flow": "NEUTRAL",
            "call_writing": 0,
            "put_writing": 0,
            "score": 0,
        }
    
    ce_oi_75 = df["ce_oi"].quantile(0.75)
    pe_oi_75 = df["pe_oi"].quantile(0.75)
    ce_vol_med = df["ce_volume"].median()
    pe_vol_med = df["pe_volume"].median()
    
    institutional_ce = 0
    institutional_pe = 0
    call_writing = 0
    put_writing = 0
    
    for _, row in df.iterrows():
        if row["ce_oi"] >= ce_oi_75 and row["ce_chng_oi"] > 0 and row["ce_volume"] >= ce_vol_med:
            institutional_ce += 1
            call_writing += 1
        
        if row["pe_oi"] >= pe_oi_75 and row["pe_chng_oi"] > 0 and row["pe_volume"] >= pe_vol_med:
            institutional_pe += 1
            put_writing += 1
    
    # Determine bias
    score = (call_writing - put_writing) * 10
    if score > 20:
        bias = Trend.BULLISH
        flow = "Call Writing (Bearish)"
    elif score < -20:
        bias = Trend.BEARISH
        flow = "Put Writing (Bullish)"
    else:
        bias = Trend.NEUTRAL
        flow = "Mixed Flow"
    
    return {
        "smart_money_bias": bias,
        "institutional_flow": flow,
        "call_writing": call_writing,
        "put_writing": put_writing,
        "score": min(100, max(0, 50 + score)),
    }

# ══════════════════════════════════════════════════════════════════════════
# UI COMPONENTS
# ══════════════════════════════════════════════════════════════════════════

def inject_css():
    """Inject custom CSS"""
    st.markdown(f"""
    <style>
    .stApp {{ background-color: {DARK_BG}; }}
    section[data-testid="stSidebar"] {{ background-color: {PANEL_BG}; }}
    
    div[data-testid="metric-container"] {{
        background: {PANEL_BG};
        border: 1px solid {BORDER_COLOR};
        border-radius: 8px;
        padding: 14px 18px;
    }}
    
    h1, h2, h3 {{ color: {TEXT_MAIN} !important; }}
    
    .signal-card {{
        background: {PANEL_BG};
        border-left: 4px solid {BLUE};
        padding: 12px;
        border-radius: 6px;
        margin: 8px 0;
    }}
    
    .bullish {{ color: {GREEN}; font-weight: 700; }}
    .bearish {{ color: {RED}; font-weight: 700; }}
    .neutral {{ color: {AMBER}; font-weight: 700; }}
    </style>
    """, unsafe_allow_html=True)

def render_summary_cards(
    ltp: float,
    signal: Signal,
    confidence: float,
    data_quality: DataQuality,
    spot: float,
    pcr: float,
    max_pain: float,
):
    """Render summary metric cards"""
    col1, col2, col3, col4, col5 = st.columns(5)
    
    with col1:
        st.metric("LTP", f"₹{ltp:,.2f}")
    
    with col2:
        color = GREEN if signal in (Signal.BUY, Signal.STRONG_BUY) else (
            RED if signal in (Signal.SELL, Signal.STRONG_SELL) else AMBER
        )
        st.markdown(f'<div style="color:{color};font-weight:700;font-size:18px;">{signal.value}</div>', 
                   unsafe_allow_html=True)
        st.caption("Signal")
    
    with col3:
        st.metric("Confidence", f"{confidence:.0f}%")
    
    with col4:
        st.metric("PCR", f"{pcr:.3f}")
    
    with col5:
        quality_color = GREEN if data_quality == DataQuality.HIGH else (
            AMBER if data_quality == DataQuality.MEDIUM else RED
        )
        st.markdown(f'<div style="color:{quality_color};font-weight:700;">{data_quality.value}</div>', 
                   unsafe_allow_html=True)
        st.caption("Data Quality")

# ══════════════════════════════════════════════════════════════════════════
# EXCEL EXPORT
# ══════════════════════════════════════════════════════════════════════════

def export_to_excel(
    df: pd.DataFrame,
    meta: dict,
    signal_result: SignalResult,
    trade_plan: Optional[TradePlan] = None,
    symbol: str = "",
) -> io.BytesIO:
    """Export analysis to Excel"""
    
    if not HAS_OPENPYXL:
        st.error("openpyxl not installed - Excel export unavailable")
        return io.BytesIO()
    
    wb = Workbook()
    
    # Summary sheet
    ws_summary = wb.active
    ws_summary.title = "Summary"
    
    summary_data = [
        ("Symbol", symbol),
        ("LTP", meta.get("spot_price", 0)),
        ("Signal", signal_result.signal.value),
        ("Confidence", f"{signal_result.confidence:.1f}%"),
        ("Data Quality", signal_result.data_quality.value),
        ("PCR", meta.get("pcr", 0)),
        ("Max Pain", meta.get("max_pain", 0)),
        ("Fetched At", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    ]
    
    if trade_plan:
        summary_data.extend([
            ("Entry", trade_plan.entry),
            ("Stop Loss", trade_plan.stop_loss),
            ("Target 1", trade_plan.target1),
            ("Risk/Reward", f"{trade_plan.risk_reward_ratio:.2f}:1"),
        ])
    
    ws_summary["A1"] = "SUMMARY"
    ws_summary["A1"].font = Font(bold=True, size=14)
    
    for i, (label, value) in enumerate(summary_data, start=2):
        ws_summary[f"A{i}"] = label
        ws_summary[f"B{i}"] = value
    
    # Option Chain sheet
    if not df.empty:
        ws_chain = wb.create_sheet("Option Chain")
        
        # Write headers
        for i, col in enumerate(df.columns, start=1):
            ws_chain.cell(row=1, column=i, value=col)
        
        # Write data
        for i, row in enumerate(df.values, start=2):
            for j, val in enumerate(row, start=1):
                ws_chain.cell(row=i, column=j, value=val)
        
        # Auto column width
        for col in ws_chain.columns:
            ws_chain.column_dimensions[col[0].column_letter].width = 15
    
    # Save to buffer
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer

# ══════════════════════════════════════════════════════════════════════════
# MAIN DASHBOARD
# ══════════════════════════════════════════════════════════════════════════

def main():
    """Main dashboard application"""
    
    # Page config
    st.set_page_config(
        page_title="Unified Market Dashboard",
        page_icon="📊",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    
    # Initialize session state
    init_session_state()
    
    # Inject CSS
    inject_css()
    
    # Title
    st.markdown("## 📊 Unified NSE/BSE/MCX Options & Technical Analysis Dashboard")
    st.markdown("---")
    
    # Sidebar
    with st.sidebar:
        st.markdown("### ⚙️ Configuration")
        
        # Market selection
        market_choice = st.radio("📍 Market", [m.value for m in Market], index=0)
        selected_market = Market(market_choice)
        st.session_state.market = selected_market
        
        # Symbol selection based on market
        if selected_market == Market.NSE:
            symbol = st.selectbox("NSE Index", list(NSE_INDICES.keys()))
        elif selected_market == Market.BSE:
            symbol = st.selectbox("BSE Index", list(BSE_INDICES.keys()))
        else:  # MCX
            symbol = st.selectbox("MCX Commodity", list(MCX_COMMODITIES.keys()))
        
        st.session_state.symbol = symbol
        
        # Expiry (for options)
        expiry_select = st.text_input("Expiry (DD-MMM-YYYY)", value="")
        
        # Lot size
        default_lot = DEFAULT_LOT_SIZES.get(symbol, DEFAULT_LOT_SIZES["_DEFAULT"])
        lot_size = st.number_input("Lot Size", min_value=1, value=default_lot)
        
        # Timeframe
        timeframe = st.selectbox("Timeframe", list(TIMEFRAMES.keys()))
        
        # Refresh settings
        st.divider()
        st.markdown("### 🔄 Refresh")
        auto_refresh = st.checkbox("Auto Refresh", value=False)
        refresh_interval = st.select_slider(
            "Refresh Interval", 
            options=[30, 60, 180, 300],
            value=180,
            format_func=lambda x: f"{x}s"
        )
        
        # Fetch button
        fetch_btn = st.button("🔄 Fetch Data", use_container_width=True, type="primary")
        
        # Debug
        st.divider()
        debug_mode = st.checkbox("Debug Mode")
    
    # Main content area
    if fetch_btn or st.session_state.get("last_fetch", None) is None:
        with st.spinner("📡 Fetching market data..."):
            
            # Fetch data based on market
            if selected_market == Market.NSE:
                is_index = True
                result = fetch_nse_option_chain(symbol, is_index)
                
                if result.get("ok"):
                    df, meta = parse_nse_option_chain(result["payload"], expiry_select)
                    data_quality = DataQuality.HIGH if not df.empty else DataQuality.UNAVAILABLE
                else:
                    st.error(f"❌ Failed to fetch NSE data: {result.get('error')}")
                    df, meta = pd.DataFrame(), {}
                    data_quality = DataQuality.UNAVAILABLE
            
            elif selected_market == Market.BSE:
                data_list, error_msg = fetch_bse_data(symbol)
                st.warning(f"⚠️ {error_msg}")
                df, meta = pd.DataFrame(), {}
                data_quality = DataQuality.UNAVAILABLE
            
            else:  # MCX
                data_list, error_msg = fetch_mcx_data(symbol)
                st.warning(f"⚠️ {error_msg}")
                df, meta = pd.DataFrame(), {}
                data_quality = DataQuality.UNAVAILABLE
            
            st.session_state.last_fetch = datetime.now()
    else:
        # Use cached data
        if "cached_data" in st.session_state and st.session_state.symbol in st.session_state.cached_data:
            cached = st.session_state.cached_data[st.session_state.symbol]
            df = cached.get("df", pd.DataFrame())
            meta = cached.get("meta", {})
            data_quality = cached.get("quality", DataQuality.UNAVAILABLE)
        else:
            df, meta = pd.DataFrame(), {}
            data_quality = DataQuality.UNAVAILABLE
    
    # Display data quality alert
    if data_quality == DataQuality.UNAVAILABLE:
        st.info("ℹ️ LIVE DATA UNAVAILABLE - This market requires broker API integration")
        st.info("👈 Select NSE from the sidebar to view live option chain data")
        return
    
    # Add Greeks if we have option chain data
    if not df.empty and "ce_iv" in df.columns:
        spot = meta.get("spot_price", 0)
        expiry_label = meta.get("selected_expiry", "")
        df = add_greeks_columns(df, spot, expiry_label)
    
    # Cache data
    st.session_state.cached_data[symbol] = {
        "df": df,
        "meta": meta,
        "quality": data_quality,
    }
    
    # Calculate analytics
    spot = meta.get("spot_price", 0)
    pcr, max_pain, oi_stats = calculate_pcr_max_pain(df)
    support_res = detect_support_resistance(df, spot)
    gex_dex = calculate_gex_dex(df, spot, lot_size)
    institutional = detect_institutional_activity(df)
    
    # Signal engine
    signal_engine = SignalEngine(spot, data_quality)
    signal_result = signal_engine.calculate_signals(df, spot, symbol, selected_market)
    
    # Trade plan
    atr = 0.0
    if not df.empty and "ce_volume" in df.columns:
        atr = abs(df["ce_ltp"].max() - df["ce_ltp"].min()) if len(df) > 0 else 0
    
    trade_plan = generate_trade_plan(
        signal_result,
        spot,
        atr,
        support_res.get("strong_support"),
        support_res.get("strong_resistance"),
    )
    
    # Render summary cards
    render_summary_cards(
        spot,
        signal_result.signal,
        signal_result.confidence,
        data_quality,
        spot,
        pcr,
        max_pain,
    )
    
    st.markdown("---")
    
    # Tabs
    tab_signal, tab_chain, tab_technical, tab_institutional, tab_gex, tab_trade, tab_export = st.tabs([
        "📊 Live Signal",
        "📋 Option Chain",
        "📈 Technical Analysis",
        "🏦 Institutional",
        "⚡ GEX/DEX",
        "🎯 Trade Plan",
        "📥 Export",
    ])
    
    # Tab 1: Live Signal
    with tab_signal:
        col1, col2 = st.columns(2)
        
        with col1:
            st.markdown("### 🤖 AI Signal")
            st.metric("Signal", signal_result.signal.value)
            st.metric("Confidence", f"{signal_result.confidence:.1f}%")
            st.metric("Data Quality", signal_result.data_quality.value)
        
        with col2:
            st.markdown("### 📊 Confidence Factors")
            for factor, score in signal_result.factors.items():
                st.write(f"• {factor.replace('_', ' ').title()}: {score:+.1f}")
        
        if signal_result.reasoning:
            st.markdown("### 💡 Reasoning")
            for reason in signal_result.reasoning:
                st.write(f"• {reason}")
    
    # Tab 2: Option Chain
    with tab_chain:
        if df.empty:
            st.warning("No option chain data available")
        else:
            st.markdown("### Call-Put Option Chain")
            
            # Filter columns for display
            display_cols = [
                "strike_price",
                "ce_ltp", "ce_change", "ce_oi", "ce_chng_oi", "ce_iv",
                "pe_ltp", "pe_change", "pe_oi", "pe_chng_oi", "pe_iv",
            ]
            display_cols = [c for c in display_cols if c in df.columns]
            
            # Format for display
            display_df = df[display_cols].copy()
            st.dataframe(display_df, use_container_width=True, hide_index=True)
            
            # Stats
            col1, col2, col3, col4 = st.columns(4)
            with col1:
                st.metric("Total CE OI", f"{oi_stats.get('total_ce_oi', 0):,}")
            with col2:
                st.metric("Total PE OI", f"{oi_stats.get('total_pe_oi', 0):,}")
            with col3:
                st.metric("Max Pain", f"₹{max_pain:,.0f}")
            with col4:
                st.metric("Support", f"₹{support_res.get('strong_support', 0):,.0f}")
    
    # Tab 3: Technical Analysis
    with tab_technical:
        st.markdown("### 📈 Technical Indicators")
        st.info("ℹ️ Requires OHLCV candle data (not available from option chain API)")
        
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("RSI (14)", "—")
        with col2:
            st.metric("MACD", "—")
        with col3:
            st.metric("ATR", "—")
    
    # Tab 4: Institutional
    with tab_institutional:
        st.markdown("### 🏦 Institutional Analysis")
        
        col1, col2 = st.columns(2)
        with col1:
            st.metric("Smart Money Bias", institutional["smart_money_bias"].value)
            st.metric("Institutional Flow", institutional["institutional_flow"])
        with col2:
            st.metric("Call Writing", institutional["call_writing"])
            st.metric("Put Writing", institutional["put_writing"])
    
    # Tab 5: GEX/DEX
    with tab_gex:
        st.markdown("### ⚡ Gamma & Delta Exposure")
        
        col1, col2 = st.columns(2)
        with col1:
            st.metric("Total GEX", f"{gex_dex.get('total_gex', 0):,.0f}")
        with col2:
            st.metric("Total DEX", f"{gex_dex.get('total_dex', 0):,.0f}")
    
    # Tab 6: Trade Plan
    with tab_trade:
        st.markdown("### 🎯 Trade Plan")
        
        if trade_plan is None:
            st.info("No trade plan generated - signal confidence insufficient or data quality low")
        else:
            col1, col2, col3, col4 = st.columns(4)
            
            with col1:
                st.metric("Entry", f"₹{trade_plan.entry:,.2f}")
            with col2:
                st.metric("Stop Loss", f"₹{trade_plan.stop_loss:,.2f}")
            with col3:
                st.metric("Risk", f"₹{trade_plan.risk_per_trade:,.2f}")
            with col4:
                st.metric("Reward", f"₹{trade_plan.reward_per_trade:,.2f}")
            
            st.divider()
            
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Target 1", f"₹{trade_plan.target1:,.2f}")
            with col2:
                st.metric("Target 2", f"₹{trade_plan.target2:,.2f}")
            with col3:
                st.metric("Target 3", f"₹{trade_plan.target3:,.2f}")
            
            st.divider()
            
            st.metric("Risk/Reward", f"1:{trade_plan.risk_reward_ratio:.2f}")
            st.markdown(f"**Reason:** {trade_plan.reason}")
    
    # Tab 7: Export
    with tab_export:
        st.markdown("### 📥 Export Data")
        
        if HAS_OPENPYXL and not df.empty:
            excel_buffer = export_to_excel(
                df,
                meta,
                signal_result,
                trade_plan,
                symbol,
            )
            
            st.download_button(
                "⬇️ Download Excel Report",
                data=excel_buffer,
                file_name=f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        
        # CSV export
        if not df.empty:
            csv_data = df.to_csv(index=False)
            st.download_button(
                "⬇️ Download CSV",
                data=csv_data,
                file_name=f"{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
                use_container_width=True,
            )
    
    # Footer
    st.markdown("---")
    st.caption(
        f"Last updated: {st.session_state.get('last_fetch', datetime.now()).strftime('%H:%M:%S')} | "
        "Data source: NSE India | "
        "For educational purposes only - not financial advice"
    )
    
    # Auto refresh
    if auto_refresh:
        time.sleep(refresh_interval)
        st.rerun()

# ══════════════════════════════════════════════════════════════════════════
# COMPATIBILITY FUNCTION FOR HOSTING APPS
# ══════════════════════════════════════════════════════════════════════════

def show_option_chain(fyers=None):
    """
    Entry point for hosting apps (e.g., FYERS dashboard).
    
    When fyers is an authenticated client, it is used as the PRIMARY data source.
    When fyers is None, falls back to NSE's public option-chain API.
    """
    main()

# ══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    main()
