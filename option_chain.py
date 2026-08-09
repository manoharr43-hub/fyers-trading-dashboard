"""
option_chain_enhanced.py (MASTER PROMPT IMPLEMENTATION)
========================================================
Institutional-grade NSE India Options Chain Dashboard with Category-wise Live Signal System.

FEATURES:
- LHS Category Navigation (INDEX, COMMODITIES, F&O STOCKS)
- Single Instrument Selection (NO MIXING)
- Multi-Timeframe Analysis (5M, 15M, 30M, 1H, 1D)
- Market Structure Detection per Timeframe
- Master Signal Engine with Weighted Confirmations
- Signal Reasons Display & Confidence Metrics
- Timeframe Confirmation Panel
- Dynamic Instrument Context Management
- Auto-Refresh with Symbol Lock
- Full Backward Compatibility

Data Source: FYERS (Primary) → NSE (Fallback for option chains only)
Live Signals: MSS, HH/HL/LH/LL, BOS, CHoCH, VWAP, EMA, RSI, MACD, Volume, RVOL
Confirmation: Multi-Timeframe Analysis with Weighted Logic
Trade Output: BUY/SELL/HOLD/NO TRADE with Entry, SL, T1, T2, T3, Probability, Confidence
"""

from __future__ import annotations

import io
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from enum import Enum

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False
    logger_temp = logging.getLogger("option_chain_enhanced")
    logger_temp.warning("openpyxl not available - Excel export disabled")

# ══════════════════════════════════════════════════════════════════════════
# 1. LOGGING & CONFIG
# ══════════════════════════════════════════════════════════════════════════

logger = logging.getLogger("option_chain_enhanced")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
logger.setLevel(logging.INFO)


# ══════════════════════════════════════════════════════════════════════════
# 2. ENUMS & DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════

class InstrumentCategory(str, Enum):
    INDEX = "INDEX"
    COMMODITY = "COMMODITY"
    STOCK = "F&O STOCK"


class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    NO_TRADE = "NO_TRADE"


class Trend(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    SIDEWAYS = "SIDEWAYS"


class StructureType(str, Enum):
    HH = "HH"  # Higher High
    HL = "HL"  # Higher Low
    LH = "LH"  # Lower High
    LL = "LL"  # Lower Low


# ══════════════════════════════════════════════════════════════════════════
# 3. INSTRUMENT CATALOG
# ══════════════════════════════════════════════════════════════════════════

INSTRUMENT_CATALOG = {
    InstrumentCategory.INDEX: {
        "NIFTY": {"symbol": "NIFTY", "display": "NIFTY 50", "exchange": "NSE"},
        "BANKNIFTY": {"symbol": "BANKNIFTY", "display": "BANK NIFTY", "exchange": "NSE"},
        "FINNIFTY": {"symbol": "FINNIFTY", "display": "FIN NIFTY", "exchange": "NSE"},
        "MIDCPNIFTY": {"symbol": "MIDCPNIFTY", "display": "MID CAP NIFTY", "exchange": "NSE"},
        "SENSEX": {"symbol": "SENSEX", "display": "SENSEX", "exchange": "BSE"},
        "BANKEX": {"symbol": "BANKEX", "display": "BANK EX", "exchange": "BSE"},
    },
    InstrumentCategory.COMMODITY: {
        "GOLD": {"symbol": "GOLD", "display": "Gold", "exchange": "MCX"},
        "SILVER": {"symbol": "SILVER", "display": "Silver", "exchange": "MCX"},
        "CRUDEOIL": {"symbol": "CRUDEOIL", "display": "Crude Oil", "exchange": "MCX"},
        "NATURALGAS": {"symbol": "NATURALGAS", "display": "Natural Gas", "exchange": "NCDEX"},
        "COPPER": {"symbol": "COPPER", "display": "Copper", "exchange": "MCX"},
    },
    InstrumentCategory.STOCK: {
        "RELIANCE": {"symbol": "RELIANCE", "display": "Reliance", "exchange": "NSE"},
        "TCS": {"symbol": "TCS", "display": "TCS", "exchange": "NSE"},
        "INFY": {"symbol": "INFY", "display": "Infosys", "exchange": "NSE"},
        "WIPRO": {"symbol": "WIPRO", "display": "Wipro", "exchange": "NSE"},
        "MARUTI": {"symbol": "MARUTI", "display": "Maruti", "exchange": "NSE"},
        "BAJAJFINSV": {"symbol": "BAJAJFINSV", "display": "Bajaj Fin Serv", "exchange": "NSE"},
        "HDFCBANK": {"symbol": "HDFCBANK", "display": "HDFC Bank", "exchange": "NSE"},
        "ICICIBANK": {"symbol": "ICICIBANK", "display": "ICICI Bank", "exchange": "NSE"},
    },
}

TIMEFRAMES = {
    "5M": 5 * 60,
    "15M": 15 * 60,
    "30M": 30 * 60,
    "1H": 60 * 60,
    "1D": 24 * 60 * 60,
}

TIMEFRAME_ORDER = ["5M", "15M", "30M", "1H", "1D"]

# ══════════════════════════════════════════════════════════════════════════
# 4. COLOR SCHEME
# ══════════════════════════════════════════════════════════════════════════

DARK_BG = "#0d1117"
PANEL_BG = "#161b22"
BORDER_COLOR = "#30363d"
TEXT_MAIN = "#e6edf3"
TEXT_MUTED = "#8b949e"
GREEN = "#3fb950"
RED = "#f85149"
AMBER = "#d29922"
BLUE = "#58a6ff"
PURPLE = "#d2a8ff"

# ══════════════════════════════════════════════════════════════════════════
# 5. CONSTANTS (from original)
# ══════════════════════════════════════════════════════════════════════════

NSE_BASE_URL = "https://www.nseindia.com"
NSE_INDEX_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-indices"
NSE_EQUITY_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-equities"

DEFAULT_LOT_SIZES = {
    "NIFTY": 25, "BANKNIFTY": 15, "FINNIFTY": 25, "MIDCPNIFTY": 50,
    "SENSEX": 10, "BANKEX": 15, "_STOCK_DEFAULT": 1,
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
    "Referer": f"{NSE_BASE_URL}/option-chain",
    "Connection": "keep-alive",
}

REQUIRED_CHAIN_COLUMNS = ["strike_price", "ce_ltp", "ce_oi", "pe_ltp", "pe_oi"]

DEFAULT_RSI_PERIOD = 14
DEFAULT_EMA_PERIODS = {"fast": 9, "slow": 21}
DEFAULT_MACD_PARAMS = {"fast": 12, "slow": 26, "signal": 9}
DEFAULT_VWAP_PERIOD = 20

MSS_MIN_STRENGTH = 1.0
BOS_CONFIRMATION_BARS = 1
CHOCH_CONFIRMATION_BARS = 2

FYERS_INDEX_SYMBOL_CANDIDATES = {
    "NIFTY": ["NSE:NIFTY50-INDEX"],
    "BANKNIFTY": ["NSE:NIFTYBANK-INDEX", "NSE:BANKNIFTY-INDEX"],
    "FINNIFTY": ["NSE:FINNIFTY-INDEX"],
    "MIDCPNIFTY": ["NSE:MIDCPNIFTY-INDEX", "NSE:MIDCAPNIFTY-INDEX"],
    "SENSEX": ["BSE:SENSEX-INDEX", "BSE:SENSEX-INDEX50"],
    "BANKEX": ["BSE:BANKEX-INDEX"],
}

NSE_UNSUPPORTED_INDICES = {"SENSEX", "BANKEX"}


# ══════════════════════════════════════════════════════════════════════════
# 6. SIGNAL REASON TRACKING
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class ConfirmationSignal:
    """Single confirmation signal with weight and status."""
    name: str
    weight: float  # 0.0 to 1.0
    confirmed: bool
    value: Optional[str] = None
    timeframe: str = "5M"


@dataclass
class TimeframeAnalysis:
    """Analysis results for a single timeframe."""
    timeframe: str
    trend: Trend
    structure: str  # "HH/HL" or "LH/LL" or "—"
    mss_direction: str  # "UP", "DOWN", "NONE"
    bos_direction: str  # "UP", "DOWN", "NONE"
    ema_position: str  # "ABOVE", "BELOW", "MIXED"
    vwap_position: str  # "ABOVE", "BELOW"
    rsi_zone: str  # "OVERBOUGHT", "OVERSOLD", "NEUTRAL"
    macd_status: str  # "BULLISH", "BEARISH", "NEUTRAL"
    volume_status: str  # "HIGH", "NORMAL", "LOW"
    signal: SignalType


@dataclass
class MasterSignal:
    """Master signal with all confirmations and reasoning."""
    signal: SignalType
    confidence: float  # 0-100
    probability: float  # 0-100
    entry: float
    stop_loss: float
    target_1: float
    target_2: float
    target_3: float
    risk_reward: float
    confirmations: list[ConfirmationSignal]
    timeframe_analyses: dict[str, TimeframeAnalysis]
    reasons: list[str]
    conflicts: list[str]
    updated_at: datetime = field(default_factory=datetime.now)


# ══════════════════════════════════════════════════════════════════════════
# 7. SESSION STATE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════

def init_session_state() -> None:
    """Initialize or maintain session state."""
    if "selected_category" not in st.session_state:
        st.session_state.selected_category = InstrumentCategory.INDEX
    if "selected_instrument" not in st.session_state:
        st.session_state.selected_instrument = "NIFTY"
    if "instrument_data_cache" not in st.session_state:
        st.session_state.instrument_data_cache = {}
    if "instrument_state" not in st.session_state:
        st.session_state.instrument_state = {}
    if "last_update_time" not in st.session_state:
        st.session_state.last_update_time = {}


def clear_instrument_state(symbol: str) -> None:
    """Completely clear state for an instrument."""
    cache_key = f"{symbol}_data"
    if cache_key in st.session_state.instrument_data_cache:
        del st.session_state.instrument_data_cache[cache_key]
    if symbol in st.session_state.instrument_state:
        del st.session_state.instrument_state[symbol]
    logger.info("Cleared state for instrument: %s", symbol)


def get_instrument_state(symbol: str) -> dict:
    """Get or create state dict for instrument."""
    if symbol not in st.session_state.instrument_state:
        st.session_state.instrument_state[symbol] = {
            "candles": {},  # timeframe -> DataFrame
            "indicators": {},  # timeframe -> DataFrame with indicators
            "structure": {},  # timeframe -> structure data
            "master_signal": None,
            "chain_data": None,
        }
    return st.session_state.instrument_state[symbol]


# ══════════════════════════════════════════════════════════════════════════
# 8. HTTP SESSION (from original)
# ══════════════════════════════════════════════════════════════════════════

def _build_retrying_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(_NSE_HEADERS)
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
    session = _build_retrying_session()
    _warm_up_session(session)
    return session


def _warm_up_session(session: requests.Session) -> bool:
    try:
        session.get(NSE_BASE_URL, timeout=REQUEST_TIMEOUT)
        session.get(f"{NSE_BASE_URL}/option-chain", timeout=REQUEST_TIMEOUT)
        return True
    except requests.exceptions.RequestException as e:
        logger.warning("NSE session warm-up failed: %s", e)
        return False


def fetch_json_with_retry(
    session: requests.Session, url: str, params: Optional[dict] = None,
    max_retries: int = MAX_RETRIES,
) -> tuple[Optional[dict], Optional[str]]:
    last_error = "Unknown error"
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except requests.exceptions.Timeout:
            last_error = f"Timeout on attempt {attempt}/{max_retries}"
            logger.warning("%s for %s", last_error, url)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue
        except requests.exceptions.ConnectionError as e:
            last_error = f"Connection error on attempt {attempt}/{max_retries}: {e}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue
        except requests.exceptions.RequestException as e:
            last_error = f"Request exception on attempt {attempt}/{max_retries}: {e}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        if resp.status_code in (401, 403):
            last_error = f"HTTP {resp.status_code} (stale session) on attempt {attempt}/{max_retries}"
            logger.warning("%s — re-warming NSE session and retrying", last_error)
            _warm_up_session(session)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        if resp.status_code != 200:
            last_error = f"HTTP {resp.status_code} on attempt {attempt}/{max_retries}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        try:
            payload = resp.json()
        except ValueError as e:
            last_error = f"Invalid JSON on attempt {attempt}/{max_retries}: {e}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        if not payload:
            last_error = f"Empty JSON payload on attempt {attempt}/{max_retries}"
            logger.warning(last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        return payload, None

    logger.error("fetch_json_with_retry exhausted all retries for %s: %s", url, last_error)
    return None, last_error


# ══════════════════════════════════════════════════════════════════════════
# 9. TECHNICAL INDICATOR FUNCTIONS (from original, with additions)
# ══════════════════════════════════════════════════════════════════════════

def _safe_num(val: Any, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def calculate_rsi(df: pd.DataFrame, period: int = DEFAULT_RSI_PERIOD, col: str = "close") -> pd.Series:
    """Calculate RSI."""
    if df.empty or col not in df.columns:
        return pd.Series(index=df.index, dtype=float)
    
    delta = df[col].diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    
    avg_gain = gain.rolling(window=period, min_periods=1).mean()
    avg_loss = loss.rolling(window=period, min_periods=1).mean()
    
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def calculate_ema(df: pd.DataFrame, period: int, col: str = "close") -> pd.Series:
    """Calculate EMA."""
    if df.empty or col not in df.columns:
        return pd.Series(index=df.index, dtype=float)
    return df[col].ewm(span=period, adjust=False).mean()


def calculate_macd(df: pd.DataFrame, fast: int = DEFAULT_MACD_PARAMS["fast"],
                   slow: int = DEFAULT_MACD_PARAMS["slow"],
                   signal: int = DEFAULT_MACD_PARAMS["signal"],
                   col: str = "close") -> tuple[pd.Series, pd.Series, pd.Series]:
    """Calculate MACD."""
    if df.empty or col not in df.columns:
        return pd.Series(0, index=df.index), pd.Series(0, index=df.index), pd.Series(0, index=df.index)
    
    ema_fast = calculate_ema(df, fast, col)
    ema_slow = calculate_ema(df, slow, col)
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    histogram = macd - signal_line
    
    return macd, signal_line, histogram


def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    """Calculate VWAP."""
    if df.empty or not all(c in df.columns for c in ["high", "low", "close", "volume"]):
        return pd.Series(index=df.index, dtype=float)
    
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    vwap = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()
    return vwap.fillna(df["close"])


def calculate_rvol(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Calculate Relative Volume."""
    if df.empty or "volume" not in df.columns:
        return pd.Series(1.0, index=df.index)
    
    avg_vol = df["volume"].rolling(window=period, min_periods=1).mean()
    rvol = df["volume"] / avg_vol.replace(0, 1.0)
    return rvol.fillna(1.0)


def add_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all technical indicators to DataFrame."""
    if df.empty:
        return df
    
    d = df.copy()
    d["rsi"] = calculate_rsi(d)
    d["ema_9"] = calculate_ema(d, 9)
    d["ema_21"] = calculate_ema(d, 21)
    d["macd"], d["macd_signal"], d["macd_hist"] = calculate_macd(d)
    d["vwap"] = calculate_vwap(d)
    d["rvol"] = calculate_rvol(d)
    
    return d


# ══════════════════════════════════════════════════════════════════════════
# 10. MARKET STRUCTURE DETECTION (Enhanced)
# ══════════════════════════════════════════════════════════════════════════

def detect_current_structure(df: pd.DataFrame) -> str:
    """Detect current market structure: HH/HL or LH/LL."""
    if df.empty or len(df) < 4:
        return "—"
    
    # Get last 4 bars
    recent = df.tail(4)
    highs = recent["high"].values
    lows = recent["low"].values
    
    # HH/HL = bullish structure
    if highs[-2] > highs[-4] and highs[-1] > highs[-3]:  # HH
        if lows[-2] > lows[-4]:  # HL
            return "HH/HL"
        return "HH"
    
    # LH/LL = bearish structure
    if highs[-2] < highs[-4] and highs[-1] < highs[-3]:  # LH
        if lows[-2] < lows[-4]:  # LL
            return "LH/LL"
        return "LH"
    
    return "—"


def detect_trend(df: pd.DataFrame) -> Trend:
    """Detect trend using EMA."""
    if df.empty or len(df) < 21:
        return Trend.SIDEWAYS
    
    ema_9 = calculate_ema(df, 9)
    ema_21 = calculate_ema(df, 21)
    close = df["close"].iloc[-1]
    
    current_ema_9 = ema_9.iloc[-1]
    current_ema_21 = ema_21.iloc[-1]
    
    if current_ema_9 > current_ema_21 and close > current_ema_9:
        return Trend.BULLISH
    elif current_ema_9 < current_ema_21 and close < current_ema_9:
        return Trend.BEARISH
    else:
        return Trend.SIDEWAYS


def detect_bos(df: pd.DataFrame) -> str:
    """Detect Break of Structure direction."""
    if df.empty or len(df) < 10:
        return "NONE"
    
    recent = df.tail(10)
    resistance = recent["high"].max()
    support = recent["low"].min()
    
    current_high = df["high"].iloc[-1]
    current_low = df["low"].iloc[-1]
    
    if current_high > resistance:
        return "UP"
    elif current_low < support:
        return "DOWN"
    
    return "NONE"


def detect_choch(df: pd.DataFrame, lookback: int = 10) -> str:
    """Detect Change of Character."""
    if df.empty or len(df) < lookback:
        return "NONE"
    
    recent = df.tail(lookback)
    lows = recent["low"].values
    highs = recent["high"].values
    
    lower_lows = sum(1 for i in range(1, len(lows)) if lows[i] < lows[i-1])
    lower_highs = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i-1])
    bearish_shift = (lower_lows >= lookback - 2) and (lower_highs >= lookback - 2)
    
    higher_lows = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i-1])
    higher_highs = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i-1])
    bullish_shift = (higher_lows >= lookback - 2) and (higher_highs >= lookback - 2)
    
    if bearish_shift:
        return "DOWN"
    elif bullish_shift:
        return "UP"
    
    return "NONE"


def detect_mss(df: pd.DataFrame) -> str:
    """Detect Market Structure Shift."""
    if df.empty or len(df) < 5:
        return "NONE"
    
    bos = detect_bos(df)
    choch = detect_choch(df, 5)
    
    if bos != "NONE":
        return bos
    if choch != "NONE":
        return choch
    
    return "NONE"


# ══════════════════════════════════════════════════════════════════════════
# 11. MULTI-TIMEFRAME ANALYSIS ENGINE
# ══════════════════════════════════════════════════════════════════════════

def analyze_timeframe(df: pd.DataFrame, timeframe: str) -> TimeframeAnalysis:
    """Comprehensive analysis for single timeframe."""
    if df is None or df.empty or len(df) < 10:
        return TimeframeAnalysis(
            timeframe=timeframe, trend=Trend.SIDEWAYS, structure="—",
            mss_direction="NONE", bos_direction="NONE", ema_position="MIXED",
            vwap_position="BELOW", rsi_zone="NEUTRAL", macd_status="NEUTRAL",
            volume_status="NORMAL", signal=SignalType.NO_TRADE,
        )
    
    df = add_technical_indicators(df)
    
    # Basic metrics
    trend = detect_trend(df)
    structure = detect_current_structure(df)
    mss_direction = detect_mss(df)
    bos_direction = detect_bos(df)
    
    close = float(df["close"].iloc[-1])
    ema_9 = float(df["ema_9"].iloc[-1]) if "ema_9" in df.columns else close
    ema_21 = float(df["ema_21"].iloc[-1]) if "ema_21" in df.columns else close
    vwap = float(df["vwap"].iloc[-1]) if "vwap" in df.columns else close
    rsi = float(df["rsi"].iloc[-1]) if "rsi" in df.columns else 50.0
    macd_val = float(df["macd"].iloc[-1]) if "macd" in df.columns else 0.0
    macd_hist = float(df["macd_hist"].iloc[-1]) if "macd_hist" in df.columns else 0.0
    rvol = float(df["rvol"].iloc[-1]) if "rvol" in df.columns else 1.0
    
    # EMA Position
    if close > ema_9 and ema_9 > ema_21:
        ema_position = "ABOVE"
    elif close < ema_9 and ema_9 < ema_21:
        ema_position = "BELOW"
    else:
        ema_position = "MIXED"
    
    # VWAP Position
    vwap_position = "ABOVE" if close > vwap else "BELOW"
    
    # RSI Zone
    if rsi >= 70:
        rsi_zone = "OVERBOUGHT"
    elif rsi <= 30:
        rsi_zone = "OVERSOLD"
    else:
        rsi_zone = "NEUTRAL"
    
    # MACD Status
    if macd_val > 0 and macd_hist > 0:
        macd_status = "BULLISH"
    elif macd_val < 0 and macd_hist < 0:
        macd_status = "BEARISH"
    else:
        macd_status = "NEUTRAL"
    
    # Volume Status
    volume_status = "HIGH" if rvol > 1.3 else ("LOW" if rvol < 0.7 else "NORMAL")
    
    return TimeframeAnalysis(
        timeframe=timeframe, trend=trend, structure=structure,
        mss_direction=mss_direction, bos_direction=bos_direction,
        ema_position=ema_position, vwap_position=vwap_position,
        rsi_zone=rsi_zone, macd_status=macd_status,
        volume_status=volume_status, signal=SignalType.HOLD,
    )


# ══════════════════════════════════════════════════════════════════════════
# 12. MASTER SIGNAL ENGINE (WEIGHTED CONFIRMATION LOGIC)
# ══════════════════════════════════════════════════════════════════════════

def generate_master_signal(
    timeframe_analyses: dict[str, TimeframeAnalysis],
    spot: float,
    chain_data: Optional[dict] = None,
) -> MasterSignal:
    """
    Master signal engine with weighted multi-timeframe confirmation logic.
    
    Weight Distribution:
    - Higher Timeframe Trend: 25%
    - Market Structure (1D): 20%
    - MSS Alignment: 20%
    - BOS/CHoCH: 15%
    - Technical Confirmation (5M-1H): 20%
    """
    
    if not timeframe_analyses or not any(timeframe_analyses.values()):
        return MasterSignal(
            signal=SignalType.NO_TRADE, confidence=0.0, probability=50.0,
            entry=spot, stop_loss=spot * 0.98, target_1=spot * 1.02,
            target_2=spot * 1.03, target_3=spot * 1.04, risk_reward=1.0,
            confirmations=[], timeframe_analyses={}, reasons=["No data available"],
            conflicts=["No timeframe data to analyze"],
        )
    
    confirmations: list[ConfirmationSignal] = []
    reasons: list[str] = []
    conflicts: list[str] = []
    
    bullish_score = 0.0
    bearish_score = 0.0
    total_weight = 0.0
    
    # ─── 1H TREND (25% weight) ───
    if "1H" in timeframe_analyses:
        tf_1h = timeframe_analyses["1H"]
        if tf_1h.trend == Trend.BULLISH:
            bullish_score += 0.25
            confirmations.append(ConfirmationSignal("1H Trend Bullish", 0.25, True))
            reasons.append("✅ 1H Trend Bullish")
        elif tf_1h.trend == Trend.BEARISH:
            bearish_score += 0.25
            confirmations.append(ConfirmationSignal("1H Trend Bearish", 0.25, True))
            reasons.append("❌ 1H Trend Bearish")
        else:
            confirmations.append(ConfirmationSignal("1H Trend Sideways", 0.25, False))
    
    total_weight += 0.25
    
    # ─── MARKET STRUCTURE (20% weight) ───
    if "1D" in timeframe_analyses:
        tf_1d = timeframe_analyses["1D"]
        if "HH/HL" in tf_1d.structure or tf_1d.structure == "HH":
            bullish_score += 0.20
            confirmations.append(ConfirmationSignal("1D Structure Bullish", 0.20, True))
            reasons.append(f"✅ 1D Structure {tf_1d.structure}")
        elif "LH/LL" in tf_1d.structure or tf_1d.structure == "LL":
            bearish_score += 0.20
            confirmations.append(ConfirmationSignal("1D Structure Bearish", 0.20, True))
            reasons.append(f"❌ 1D Structure {tf_1d.structure}")
    
    total_weight += 0.20
    
    # ─── MSS ALIGNMENT (20% weight) ───
    bullish_mss_count = 0
    bearish_mss_count = 0
    
    for tf_name, tf_analysis in timeframe_analyses.items():
        if tf_analysis.mss_direction == "UP":
            bullish_mss_count += 1
        elif tf_analysis.mss_direction == "DOWN":
            bearish_mss_count += 1
    
    if bullish_mss_count >= 2:
        bullish_score += 0.20
        confirmations.append(ConfirmationSignal(f"MSS UP ({bullish_mss_count} TFs)", 0.20, True))
        reasons.append(f"✅ MSS UP confirmed ({bullish_mss_count} timeframes)")
    elif bearish_mss_count >= 2:
        bearish_score += 0.20
        confirmations.append(ConfirmationSignal(f"MSS DOWN ({bearish_mss_count} TFs)", 0.20, True))
        reasons.append(f"❌ MSS DOWN confirmed ({bearish_mss_count} timeframes)")
    elif bullish_mss_count > bearish_mss_count:
        bullish_score += 0.10
        confirmations.append(ConfirmationSignal("Slight MSS Bullish Bias", 0.10, True))
    elif bearish_mss_count > bullish_mss_count:
        bearish_score += 0.10
        confirmations.append(ConfirmationSignal("Slight MSS Bearish Bias", 0.10, True))
    else:
        confirmations.append(ConfirmationSignal("MSS Neutral", 0.20, False))
    
    total_weight += 0.20
    
    # ─── BOS/CHoCH (15% weight) ───
    bos_up_count = sum(1 for tf in timeframe_analyses.values() if tf.bos_direction == "UP")
    bos_down_count = sum(1 for tf in timeframe_analyses.values() if tf.bos_direction == "DOWN")
    
    if bos_up_count >= 1:
        bullish_score += 0.15
        confirmations.append(ConfirmationSignal("BOS UP", 0.15, True))
        reasons.append(f"✅ BOS UP ({bos_up_count} TF)")
    elif bos_down_count >= 1:
        bearish_score += 0.15
        confirmations.append(ConfirmationSignal("BOS DOWN", 0.15, True))
        reasons.append(f"❌ BOS DOWN ({bos_down_count} TF)")
    else:
        confirmations.append(ConfirmationSignal("No BOS", 0.15, False))
    
    total_weight += 0.15
    
    # ─── TECHNICAL CONFIRMATION 5M-1H (20% weight) ───
    tech_bullish = 0.0
    tech_bearish = 0.0
    
    for tf_name in ["5M", "15M", "30M", "1H"]:
        if tf_name not in timeframe_analyses:
            continue
        
        tf = timeframe_analyses[tf_name]
        
        # EMA bullish
        if tf.ema_position == "ABOVE":
            tech_bullish += 0.025
        
        # VWAP bullish
        if tf.vwap_position == "ABOVE":
            tech_bullish += 0.025
        
        # MACD bullish
        if tf.macd_status == "BULLISH":
            tech_bullish += 0.025
        
        # RSI not overbought in bullish
        if tf.rsi_zone != "OVERBOUGHT":
            tech_bullish += 0.012
        
        # Volume confirmation
        if tf.volume_status == "HIGH":
            tech_bullish += 0.015
    
    for tf_name in ["5M", "15M", "30M", "1H"]:
        if tf_name not in timeframe_analyses:
            continue
        
        tf = timeframe_analyses[tf_name]
        
        # EMA bearish
        if tf.ema_position == "BELOW":
            tech_bearish += 0.025
        
        # VWAP bearish
        if tf.vwap_position == "BELOW":
            tech_bearish += 0.025
        
        # MACD bearish
        if tf.macd_status == "BEARISH":
            tech_bearish += 0.025
        
        # RSI not oversold in bearish
        if tf.rsi_zone != "OVERSOLD":
            tech_bearish += 0.012
        
        # Volume confirmation
        if tf.volume_status == "HIGH":
            tech_bearish += 0.015
    
    tech_score = min(tech_bullish, 0.20)
    if tech_bullish > 0:
        bullish_score += tech_score
        confirmations.append(ConfirmationSignal(f"Technical Setup Bullish", tech_score, True))
    elif tech_bearish > 0:
        tech_score = min(tech_bearish, 0.20)
        bearish_score += tech_score
        confirmations.append(ConfirmationSignal(f"Technical Setup Bearish", tech_score, True))
    
    total_weight += 0.20
    
    # ─── CHAIN DATA CONFIRMATION (if available) ───
    if chain_data:
        pcr = chain_data.get("pcr", 1.0)
        if pcr > 1.3:
            bullish_score += 0.05
            reasons.append(f"✅ High PCR ({pcr:.2f}) - Bullish bias")
        elif pcr < 0.7:
            bearish_score += 0.05
            reasons.append(f"❌ Low PCR ({pcr:.2f}) - Bearish bias")
    
    # ─── CONFLICT DETECTION ───
    if abs(bullish_score - bearish_score) < 0.10:
        conflicts.append("CONFIRMATIONS CONFLICT - No clear direction")
    
    # ─── FINAL DECISION ───
    normalized_bullish = (bullish_score / total_weight) * 100 if total_weight > 0 else 0
    normalized_bearish = (bearish_score / total_weight) * 100 if total_weight > 0 else 0
    
    if normalized_bullish > normalized_bearish + 20:
        signal = SignalType.BUY
        confidence = min(normalized_bullish, 100.0)
        probability = min(60 + normalized_bullish / 3, 100.0)
    elif normalized_bearish > normalized_bullish + 20:
        signal = SignalType.SELL
        confidence = min(normalized_bearish, 100.0)
        probability = min(60 + normalized_bearish / 3, 100.0)
    elif abs(normalized_bullish - normalized_bearish) < 5:
        signal = SignalType.HOLD
        confidence = 50.0
        probability = 50.0
    else:
        signal = SignalType.HOLD
        confidence = max(normalized_bullish, normalized_bearish) * 0.6
        probability = 50.0
    
    # ─── ENTRY, SL, TARGETS ───
    if signal == SignalType.BUY:
        entry = spot
        stop_loss = spot * 0.985
        range_val = entry - stop_loss
        target_1 = entry + range_val
        target_2 = entry + (range_val * 1.5)
        target_3 = entry + (range_val * 2.0)
    elif signal == SignalType.SELL:
        entry = spot
        stop_loss = spot * 1.015
        range_val = stop_loss - entry
        target_1 = entry - range_val
        target_2 = entry - (range_val * 1.5)
        target_3 = entry - (range_val * 2.0)
    else:
        entry = spot
        stop_loss = spot * 0.98
        target_1 = spot * 1.01
        target_2 = spot * 1.02
        target_3 = spot * 1.03
    
    risk_reward = abs(target_1 - entry) / abs(entry - stop_loss) if entry != stop_loss else 1.0
    
    if conflicts:
        reasons.append(f"⚠️ {conflicts[0]}")
    
    if not reasons:
        reasons.append("Neutral - insufficient confirmation")
    
    return MasterSignal(
        signal=signal,
        confidence=confidence,
        probability=probability,
        entry=entry,
        stop_loss=stop_loss,
        target_1=target_1,
        target_2=target_2,
        target_3=target_3,
        risk_reward=risk_reward,
        confirmations=confirmations,
        timeframe_analyses=timeframe_analyses,
        reasons=reasons,
        conflicts=conflicts,
    )


# ══════════════════════════════════════════════════════════════════════════
# 13. FYERS DATA FETCHING (from original)
# ══════════════════════════════════════════════════════════════════════════

def _fyers_field(d: dict, *aliases: str, default: Any = None) -> Any:
    for alias in aliases:
        if alias in d and d[alias] is not None:
            return d[alias]
    return default


def _fyers_call_history(fyers: Any, symbol: str, resolution: str, count: int = 100) -> Optional[dict]:
    """Fetch OHLCV from FYERS."""
    try:
        req = {
            "symbol": symbol,
            "resolution": str(resolution),
            "date_format": "1",
            "range_from": "0",
            "range_to": "0",
            "cont_flag": "1"
        }
        return fyers.history(data=req)
    except Exception as e:
        logger.warning("FYERS history() call raised for %s (res %s): %s", symbol, resolution, e)
        return None


def fetch_fyers_candles(fyers: Any, symbol: str, timeframe_minutes: int, count: int = 100) -> Optional[pd.DataFrame]:
    """Fetch OHLCV candles from FYERS for a timeframe."""
    if fyers is None:
        return None

    resolution_map = {
        5: "5",
        15: "15",
        30: "30",
        60: "60",
        1440: "1D",
    }
    resolution = resolution_map.get(timeframe_minutes, str(timeframe_minutes))

    resp = _fyers_call_history(fyers, symbol, resolution, count)
    if not isinstance(resp, dict) or resp.get("s") != "ok":
        logger.warning("FYERS history returned non-ok status for %s: %s", symbol, resp.get("s") if resp else None)
        return None

    data = resp.get("candles", []) if isinstance(resp.get("candles"), list) else []
    if not data:
        logger.warning("FYERS history returned empty candles for %s", symbol)
        return None

    rows = []
    for candle in data:
        if not isinstance(candle, list) or len(candle) < 5:
            continue
        try:
            rows.append({
                "timestamp": int(candle[0]) if len(candle) > 0 else 0,
                "open": float(candle[1]) if len(candle) > 1 else 0.0,
                "high": float(candle[2]) if len(candle) > 2 else 0.0,
                "low": float(candle[3]) if len(candle) > 3 else 0.0,
                "close": float(candle[4]) if len(candle) > 4 else 0.0,
                "volume": float(candle[5]) if len(candle) > 5 else 0.0,
            })
        except (TypeError, ValueError, IndexError):
            continue

    if not rows:
        return None

    df = pd.DataFrame(rows)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def normalize_stock_symbol(raw: str) -> str:
    s = (raw or "").strip().upper()
    if s.endswith("-EQ"):
        s = s[:-3]
    if ":" in s:
        s = s.split(":")[-1]
    return s


def fyers_stock_symbol_candidates(stock: str) -> list[str]:
    base = normalize_stock_symbol(stock)
    return [f"NSE:{base}-EQ", f"NSE:{base}"]


def _fyers_index_candidates(symbol_key: str) -> list[str]:
    return FYERS_INDEX_SYMBOL_CANDIDATES.get(symbol_key, [f"NSE:{symbol_key}-INDEX"])


# ══════════════════════════════════════════════════════════════════════════
# 14. STREAMLIT UI — CONFIGURATION & STYLING
# ══════════════════════════════════════════════════════════════════════════

def _configure_page() -> None:
    try:
        st.set_page_config(
            page_title="Options Chain Dashboard | Category-wise Live Signals",
            page_icon="📊", layout="wide", initial_sidebar_state="expanded",
        )
    except Exception as e:
        logger.warning("st.set_page_config() skipped: %s", e)


def _inject_css() -> None:
    st.markdown(f"""
    <style>
    .stApp {{ background-color: {DARK_BG}; }}
    section[data-testid="stSidebar"] {{ background-color: {PANEL_BG}; border-right: 1px solid {BORDER_COLOR}; }}
    .main {{ color: {TEXT_MAIN}; }}
    
    .category-nav {{
        display: flex;
        flex-direction: column;
        gap: 8px;
        margin-bottom: 20px;
    }}
    
    .category-btn {{
        padding: 12px 16px;
        border: 1px solid {BORDER_COLOR};
        border-radius: 6px;
        background: {PANEL_BG};
        color: {TEXT_MUTED};
        cursor: pointer;
        font-weight: 500;
        transition: all 0.2s;
    }}
    
    .category-btn:hover {{
        border-color: {BLUE};
        background: #1c2128;
    }}
    
    .category-btn.active {{
        background: {BLUE};
        color: {DARK_BG};
        border-color: {BLUE};
    }}
    
    .instrument-item {{
        padding: 8px 12px;
        margin: 4px 0;
        border: 1px solid {BORDER_COLOR};
        border-radius: 4px;
        background: {DARK_BG};
        color: {TEXT_MUTED};
        cursor: pointer;
        font-size: 13px;
        transition: all 0.2s;
    }}
    
    .instrument-item:hover {{
        border-color: {BLUE};
        background: {PANEL_BG};
    }}
    
    .instrument-item.selected {{
        background: {BLUE};
        color: {DARK_BG};
        border-color: {BLUE};
        font-weight: 600;
    }}
    
    .signal-card {{
        background: linear-gradient(135deg, {PANEL_BG} 0%, #1c2128 100%);
        border: 2px solid {BLUE};
        border-radius: 12px;
        padding: 20px;
        margin: 12px 0;
    }}
    
    .signal-buy {{ border-left: 6px solid {GREEN}; }}
    .signal-sell {{ border-left: 6px solid {RED}; }}
    .signal-hold {{ border-left: 6px solid {AMBER}; }}
    .signal-notrade {{ border-left: 6px solid {TEXT_MUTED}; }}
    
    .signal-title {{
        font-size: 24px;
        font-weight: 700;
        margin-bottom: 12px;
    }}
    
    .signal-buy .signal-title {{ color: {GREEN}; }}
    .signal-sell .signal-title {{ color: {RED}; }}
    .signal-hold .signal-title {{ color: {AMBER}; }}
    .signal-notrade .signal-title {{ color: {TEXT_MUTED}; }}
    
    .metric-row {{
        display: grid;
        grid-template-columns: repeat(5, 1fr);
        gap: 12px;
        margin: 12px 0;
    }}
    
    .metric-box {{
        background: {DARK_BG};
        padding: 12px;
        border-radius: 6px;
        border: 1px solid {BORDER_COLOR};
        text-align: center;
    }}
    
    .metric-label {{
        color: {TEXT_MUTED};
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        margin-bottom: 6px;
    }}
    
    .metric-value {{
        color: {TEXT_MAIN};
        font-size: 16px;
        font-weight: 700;
        font-family: 'Courier New', monospace;
    }}
    
    .reason-item {{
        padding: 8px;
        margin: 4px 0;
        background: {DARK_BG};
        border-left: 3px solid {BORDER_COLOR};
        border-radius: 4px;
        color: {TEXT_MAIN};
        font-size: 13px;
    }}
    
    .reason-bullish {{ border-left-color: {GREEN}; }}
    .reason-bearish {{ border-left-color: {RED}; }}
    .reason-neutral {{ border-left-color: {AMBER}; }}
    
    .tf-table {{
        width: 100%;
        border-collapse: collapse;
        background: {PANEL_BG};
        border: 1px solid {BORDER_COLOR};
        border-radius: 6px;
        overflow: hidden;
    }}
    
    .tf-table th {{
        background: #1F4E78;
        color: #fff;
        padding: 10px;
        text-align: center;
        font-size: 12px;
        font-weight: 600;
        text-transform: uppercase;
    }}
    
    .tf-table td {{
        padding: 10px;
        border-bottom: 1px solid {BORDER_COLOR};
        text-align: center;
        font-size: 12px;
        color: {TEXT_MAIN};
    }}
    
    .tf-table tr:last-child td {{ border-bottom: none; }}
    
    h1, h2, h3 {{ color: {TEXT_MAIN} !important; }}
    </style>
    """, unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════
# 15. LHS CATEGORY NAVIGATION
# ══════════════════════════════════════════════════════════════════════════

def render_lhs_navigation() -> tuple[InstrumentCategory, str]:
    """Render LHS category navigation and instrument selection."""
    st.sidebar.markdown("## 📊 INSTRUMENT SELECT")
    st.sidebar.divider()
    
    # Category selection
    st.sidebar.markdown("### Category")
    
    category_cols = st.sidebar.columns(3)
    selected_category = st.session_state.selected_category
    
    categories = [
        (InstrumentCategory.INDEX, "📈 INDEX"),
        (InstrumentCategory.COMMODITY, "🛢️ COMMODITY"),
        (InstrumentCategory.STOCK, "📉 F&O STOCK"),
    ]
    
    for idx, (cat, label) in enumerate(categories):
        with category_cols[idx]:
            if st.button(label, key=f"cat_{cat}", use_container_width=True):
                st.session_state.selected_category = cat
                selected_category = cat
    
    st.sidebar.divider()
    st.sidebar.markdown("### Instruments")
    
    # Instrument selection
    instruments_for_category = INSTRUMENT_CATALOG.get(selected_category, {})
    
    for symbol, details in instruments_for_category.items():
        is_selected = symbol == st.session_state.selected_instrument
        btn_label = f"{'✓ ' if is_selected else ''}{details['display']}"
        
        if st.sidebar.button(btn_label, key=f"inst_{symbol}", use_container_width=True):
            # Clear previous instrument state
            if st.session_state.selected_instrument != symbol:
                clear_instrument_state(st.session_state.selected_instrument)
            st.session_state.selected_instrument = symbol
            st.rerun()
    
    st.sidebar.divider()
    
    return selected_category, st.session_state.selected_instrument


# ══════════════════════════════════════════════════════════════════════════
# 16. SIGNAL DISPLAY
# ══════════════════════════════════════════════════════════════════════════

def render_master_signal(signal: MasterSignal, spot: float, symbol: str) -> None:
    """Render master signal card with all details."""
    
    # Determine styling
    signal_color_map = {
        SignalType.BUY: (GREEN, "signal-buy", "🟢"),
        SignalType.SELL: (RED, "signal-sell", "🔴"),
        SignalType.HOLD: (AMBER, "signal-hold", "🟡"),
        SignalType.NO_TRADE: (TEXT_MUTED, "signal-notrade", "⚪"),
    }
    
    color, css_class, emoji = signal_color_map.get(signal.signal, (TEXT_MUTED, "signal-notrade", "⚪"))
    
    st.markdown(f"""
    <div class="signal-card {css_class}">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
            <div>
                <div style="font-size: 14px; color: {TEXT_MUTED}; text-transform: uppercase; letter-spacing: 0.08em;">
                    {symbol}
                </div>
                <div class="signal-title">{emoji} {signal.signal.value}</div>
            </div>
            <div style="text-align: right;">
                <div style="color: {TEXT_MUTED}; font-size: 11px; text-transform: uppercase;">Live Price</div>
                <div style="color: {TEXT_MAIN}; font-size: 20px; font-weight: 700;">₹{spot:,.2f}</div>
            </div>
        </div>
        
        <div class="metric-row">
            <div class="metric-box">
                <div class="metric-label">Entry</div>
                <div class="metric-value">₹{signal.entry:,.2f}</div>
            </div>
            <div class="metric-box">
                <div class="metric-label">Stop Loss</div>
                <div class="metric-value" style="color: {RED};">₹{signal.stop_loss:,.2f}</div>
            </div>
            <div class="metric-box">
                <div class="metric-label">Target 1</div>
                <div class="metric-value" style="color: {AMBER};">₹{signal.target_1:,.2f}</div>
            </div>
            <div class="metric-box">
                <div class="metric-label">Target 2</div>
                <div class="metric-value" style="color: {AMBER};">₹{signal.target_2:,.2f}</div>
            </div>
            <div class="metric-box">
                <div class="metric-label">Target 3</div>
                <div class="metric-value" style="color: {GREEN};">₹{signal.target_3:,.2f}</div>
            </div>
        </div>
        
        <div class="metric-row">
            <div class="metric-box">
                <div class="metric-label">Confidence</div>
                <div class="metric-value" style="color: {color};">{signal.confidence:.0f}%</div>
            </div>
            <div class="metric-box">
                <div class="metric-label">Probability</div>
                <div class="metric-value" style="color: {color};">{signal.probability:.0f}%</div>
            </div>
            <div class="metric-box">
                <div class="metric-label">Risk : Reward</div>
                <div class="metric-value">1:{signal.risk_reward:.2f}</div>
            </div>
            <div class="metric-box">
                <div class="metric-label">Updated</div>
                <div class="metric-value" style="font-size: 12px;">{signal.updated_at.strftime('%H:%M:%S')}</div>
            </div>
            <div class="metric-box">
                <div class="metric-label">Confirmations</div>
                <div class="metric-value">{len([c for c in signal.confirmations if c.confirmed])}/{len(signal.confirmations)}</div>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)


def render_signal_reasons(signal: MasterSignal) -> None:
    """Render signal reasons and conflicts."""
    
    st.markdown("### 📋 Signal Reasoning")
    
    col_reasons, col_conflicts = st.columns([2, 1])
    
    with col_reasons:
        st.markdown("**Bullish Confirmations:**")
        for reason in [r for r in signal.reasons if "✅" in r or "BUY" in r.upper()]:
            st.markdown(f"<div class='reason-item reason-bullish'>{reason}</div>", unsafe_allow_html=True)
        
        st.markdown("**Bearish Confirmations:**")
        for reason in [r for r in signal.reasons if "❌" in r or "SELL" in r.upper()]:
            st.markdown(f"<div class='reason-item reason-bearish'>{reason}</div>", unsafe_allow_html=True)
        
        st.markdown("**Other:**")
        for reason in [r for r in signal.reasons if "✅" not in r and "❌" not in r]:
            st.markdown(f"<div class='reason-item reason-neutral'>{reason}</div>", unsafe_allow_html=True)
    
    with col_conflicts:
        if signal.conflicts:
            st.error("⚠️ **Conflicts Detected**")
            for conflict in signal.conflicts:
                st.markdown(f"• {conflict}")
        else:
            st.success("✅ **No Conflicts**")


def render_timeframe_confirmation_table(analyses: dict[str, TimeframeAnalysis]) -> None:
    """Render multi-timeframe confirmation table."""
    
    st.markdown("### 🔄 Multi-Timeframe Confirmation")
    
    # Build table HTML
    table_html = f"""
    <table class="tf-table">
        <thead>
            <tr>
                <th>Timeframe</th>
                <th>Trend</th>
                <th>Structure</th>
                <th>MSS</th>
                <th>BOS</th>
                <th>EMA</th>
                <th>VWAP</th>
                <th>MACD</th>
                <th>RSI</th>
                <th>Volume</th>
            </tr>
        </thead>
        <tbody>
    """
    
    for tf_name in TIMEFRAME_ORDER:
        if tf_name not in analyses:
            continue
        
        tf = analyses[tf_name]
        
        # Trend color
        trend_color = GREEN if tf.trend == Trend.BULLISH else (RED if tf.trend == Trend.BEARISH else AMBER)
        
        # MSS color
        mss_color = GREEN if tf.mss_direction == "UP" else (RED if tf.mss_direction == "DOWN" else TEXT_MUTED)
        
        # BOS color
        bos_color = GREEN if tf.bos_direction == "UP" else (RED if tf.bos_direction == "DOWN" else TEXT_MUTED)
        
        # EMA color
        ema_color = GREEN if tf.ema_position == "ABOVE" else (RED if tf.ema_position == "BELOW" else AMBER)
        
        # VWAP color
        vwap_color = GREEN if tf.vwap_position == "ABOVE" else RED
        
        # MACD color
        macd_color = GREEN if tf.macd_status == "BULLISH" else (RED if tf.macd_status == "BEARISH" else AMBER)
        
        # RSI color
        rsi_color = RED if tf.rsi_zone == "OVERBOUGHT" else (RED if tf.rsi_zone == "OVERSOLD" else GREEN)
        
        # Volume color
        vol_color = GREEN if tf.volume_status == "HIGH" else AMBER
        
        table_html += f"""
            <tr>
                <td><strong>{tf_name}</strong></td>
                <td style="color: {trend_color};">{tf.trend.value}</td>
                <td>{tf.structure}</td>
                <td style="color: {mss_color}; font-weight: 600;">{tf.mss_direction}</td>
                <td style="color: {bos_color}; font-weight: 600;">{tf.bos_direction}</td>
                <td style="color: {ema_color};">{tf.ema_position}</td>
                <td style="color: {vwap_color};">{tf.vwap_position}</td>
                <td style="color: {macd_color};">{tf.macd_status}</td>
                <td style="color: {rsi_color};">{tf.rsi_zone}</td>
                <td style="color: {vol_color};">{tf.volume_status}</td>
            </tr>
        """
    
    table_html += """
        </tbody>
    </table>
    """
    
    st.markdown(table_html, unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════
# 17. MAIN DASHBOARD
# ══════════════════════════════════════════════════════════════════════════

def run_dashboard(fyers: Any = None) -> None:
    """Main dashboard runner."""
    
    _configure_page()
    _inject_css()
    init_session_state()
    
    st.markdown("# 📊 NSE Options Dashboard | Category-wise Live Signals")
    st.caption("Multi-Timeframe Analysis with Master Signal Engine | No Instrument Mixing")
    
    # LHS Navigation
    selected_category, selected_symbol = render_lhs_navigation()
    
    # Main content
    inst_details = INSTRUMENT_CATALOG.get(selected_category, {}).get(selected_symbol, {})
    display_name = inst_details.get("display", selected_symbol)
    exchange = inst_details.get("exchange", "NSE")
    
    # Fetch data button
    col_fetch, col_settings = st.columns([3, 1])
    
    with col_fetch:
        fetch_clicked = st.button("🔄 Fetch Live Data", use_container_width=True, type="primary")
    
    with col_settings:
        show_chart = st.checkbox("📈 Chart", value=True)
    
    st.divider()
    
    # Data fetching and processing
    if fetch_clicked or "fetched_symbol" not in st.session_state or st.session_state.get("fetched_symbol") != selected_symbol:
        with st.spinner(f"Analyzing {display_name}…"):
            state = get_instrument_state(selected_symbol)
            
            # Fetch FYERS data for multiple timeframes
            if fyers is not None:
                symbol_candidates = (
                    _fyers_index_candidates(selected_symbol) if selected_category == InstrumentCategory.INDEX
                    else fyers_stock_symbol_candidates(selected_symbol)
                )
                
                fyers_symbol = symbol_candidates[0] if symbol_candidates else selected_symbol
                
                spot = 0.0
                timeframe_dfs = {}
                
                for tf_name, tf_mins in TIMEFRAMES.items():
                    df = fetch_fyers_candles(fyers, fyers_symbol, tf_mins, count=100)
                    if df is not None and not df.empty:
                        timeframe_dfs[tf_name] = df
                        if tf_name == "5M" and len(df) > 0:
                            spot = df["close"].iloc[-1]
                
                state["candles"] = timeframe_dfs
                
                # Analyze each timeframe
                analyses = {}
                for tf_name, df in timeframe_dfs.items():
                    analyses[tf_name] = analyze_timeframe(df, tf_name)
                
                # Generate master signal
                if spot > 0:
                    master_signal = generate_master_signal(analyses, spot, None)
                    state["master_signal"] = master_signal
                    state["spot"] = spot
                    st.session_state.fetched_symbol = selected_symbol
                else:
                    st.error("❌ Could not fetch live price data from FYERS.")
                    st.info("Ensure FYERS client is connected and the symbol is available.")
                    return
            else:
                st.warning("⚠️ FYERS client not connected. Live price action signals unavailable.")
                st.info("To enable price action analysis, connect a FYERS client.")
                return
    
    # Display results
    state = get_instrument_state(selected_symbol)
    
    if state.get("master_signal"):
        signal = state["master_signal"]
        spot = state.get("spot", 0.0)
        
        # Main signal card
        render_master_signal(signal, spot, display_name)
        
        st.divider()
        
        # Reasoning
        render_signal_reasons(signal)
        
        st.divider()
        
        # Timeframe confirmation
        if signal.timeframe_analyses:
            render_timeframe_confirmation_table(signal.timeframe_analyses)
        
        st.divider()
        
        # Chart (if available)
        if show_chart and "5M" in state.get("candles", {}):
            with st.expander("📈 Price Action Chart (5M)", expanded=False):
                df_5m = state["candles"]["5M"]
                if not df_5m.empty:
                    # Simple candlestick chart
                    fig = go.Figure(data=[go.Candlestick(
                        x=df_5m.index,
                        open=df_5m["open"],
                        high=df_5m["high"],
                        low=df_5m["low"],
                        close=df_5m["close"],
                    )])
                    fig.update_layout(
                        paper_bgcolor=DARK_BG, plot_bgcolor=DARK_BG,
                        font=dict(color=TEXT_MUTED),
                        height=400,
                        title=f"{display_name} - 5M Price Action",
                    )
                    st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning("⚠️ No signal data available. Click 'Fetch Live Data' to analyze.")
    
    # Footer
    st.divider()
    st.caption(
        "**NSE Options Dashboard** | Category-wise Navigation | Multi-TF Confirmed Signals | "
        "No Instrument Mixing | Educational Tool — Not Financial Advice"
    )


def show_option_chain(fyers: Any = None) -> None:
    """Entry point for hosting apps."""
    logger.info("Enhanced dashboard initialized with FYERS=%s", fyers is not None)
    run_dashboard(fyers)


if __name__ == "__main__":
    run_dashboard()
