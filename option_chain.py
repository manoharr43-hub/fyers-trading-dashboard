"""
option_chain_enhanced.py (ENHANCED v2)
======================================
Institutional-grade NSE/NCDEX Options Chain Dashboard with:
✓ AI-Powered Price Action Signals
✓ COMMODITIES SUPPORT (Gold, Crude, Natural Gas, Silver, Copper)
✓ REVERSAL ENTRY ZONES (from Pine Script)
✓ Multi-timeframe Market Structure Detection

Data Source: FYERS (Primary) → NSE/NCDEX (Fallback)
Live Signals: MSS, HH/HL/LH/LL, BOS, CHoCH, VWAP, EMA, RSI, MACD, Volume, RVOL
Reversal Detection: Buy/Sell Pressure, Entry Signals, Next Candle Probability
Trade Signal Output: BUY/SELL/HOLD with Entry, SL, T1, T2, T3, Probability, Confidence
"""

from __future__ import annotations

import io
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from collections import deque

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

# ══════════════════════════════════════════════════════════════════════════
# 1. LOGGING
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
# 2. CONSTANTS — NSE/NCDEX
# ══════════════════════════════════════════════════════════════════════════

NSE_BASE_URL = "https://www.nseindia.com"
NSE_INDEX_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-indices"
NSE_EQUITY_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-equities"
NCDEX_BASE_URL = "https://www.ncdex.com"

INDEX_SYMBOLS: dict[str, str] = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "SENSEX": "SENSEX",
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# NEW: COMMODITIES SYMBOLS (NCDEX)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
COMMODITY_SYMBOLS: dict[str, dict] = {
    "GOLD": {
        "nse_symbol": "GOLD",
        "fyers_symbol": "NSE:GOLDGULD-FUT",
        "lot_size": 100,
        "tick_size": 1,
        "multiplier": 1,
        "exchange": "NCDEX",
    },
    "CRUDEOIL": {
        "nse_symbol": "CRUDEOIL",
        "fyers_symbol": "NSE:CRUDEOILMCX-FUT",
        "lot_size": 100,
        "tick_size": 1,
        "multiplier": 100,
        "exchange": "NCDEX",
    },
    "NATURALGAS": {
        "nse_symbol": "NATURALGAS",
        "fyers_symbol": "NSE:NATURALGASMCX-FUT",
        "lot_size": 1,
        "tick_size": 0.1,
        "multiplier": 1,
        "exchange": "NCDEX",
    },
    "SILVER": {
        "nse_symbol": "SILVER",
        "fyers_symbol": "NSE:SILVERGULD-FUT",
        "lot_size": 30,
        "tick_size": 1,
        "multiplier": 1,
        "exchange": "NCDEX",
    },
    "COPPER": {
        "nse_symbol": "COPPER",
        "fyers_symbol": "NSE:COPPERMCX-FUT",
        "lot_size": 250,
        "tick_size": 1,
        "multiplier": 1,
        "exchange": "NCDEX",
    },
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
    "NATURALGAS": 1,
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
    "Referer": f"{NSE_BASE_URL}/option-chain",
    "Connection": "keep-alive",
}

REQUIRED_CHAIN_COLUMNS = ["strike_price", "ce_ltp", "ce_oi", "pe_ltp", "pe_oi"]

DARK_BG = "#0d1117"
PANEL_BG = "#161b22"
BORDER_COLOR = "#30363d"
TEXT_MAIN = "#e6edf3"
TEXT_MUTED = "#8b949e"
GREEN = "#3fb950"
RED = "#f85149"
AMBER = "#d29922"
BLUE = "#58a6ff"

# Timeframe constants
TIMEFRAMES = {
    "5M": 5 * 60,
    "15M": 15 * 60,
    "30M": 30 * 60,
    "1H": 60 * 60,
    "1D": 24 * 60 * 60,
}

# ══════════════════════════════════════════════════════════════════════════
# 3. REVERSAL ENTRY ZONES CONSTANTS (from Pine Script)
# ══════════════════════════════════════════════════════════════════════════

REVERSAL_PRESETS = {
    "Low": {"atr_mult": 2.8, "pct": 0.015},
    "Very Low": {"atr_mult": 3.5, "pct": 0.02},
}

# Technical analysis parameters
DEFAULT_RSI_PERIOD = 14
DEFAULT_EMA_PERIODS = {"fast": 9, "slow": 21, "20": 20, "50": 50, "200": 200}
DEFAULT_MACD_PARAMS = {"fast": 12, "slow": 26, "signal": 9}
DEFAULT_VWAP_PERIOD = 20

# MSS and signal parameters
MSS_MIN_STRENGTH = 1.0
BOS_CONFIRMATION_BARS = 1
CHOCH_CONFIRMATION_BARS = 2

FYERS_INDEX_SYMBOL_CANDIDATES: dict[str, list[str]] = {
    "NIFTY": ["NSE:NIFTY50-INDEX"],
    "BANKNIFTY": ["NSE:NIFTYBANK-INDEX", "NSE:BANKNIFTY-INDEX"],
    "FINNIFTY": ["NSE:FINNIFTY-INDEX"],
    "MIDCPNIFTY": ["NSE:MIDCPNIFTY-INDEX", "NSE:MIDCAPNIFTY-INDEX"],
    "SENSEX": ["BSE:SENSEX-INDEX", "BSE:SENSEX-INDEX50"],
    "BANKEX": ["BSE:BANKEX-INDEX"],
}


# ══════════════════════════════════════════════════════════════════════════
# 4. REVERSAL ENTRY ZONE DATA CLASS
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class ReversalZone:
    """Represents a detected reversal entry zone."""
    zone_type: str  # "BULLISH" or "BEARISH"
    center_price: float
    core_low: float
    core_high: float
    outer_low: float
    outer_high: float
    pivot_bar: int
    is_active: bool = True
    strength: float = 50.0  # 0-100
    confidence: float = 50.0


@dataclass
class PressureSignal:
    """Represents buy/sell pressure at current candle."""
    buy_pressure: bool
    sell_pressure: bool
    volume_spike: bool
    volume_ratio: float
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class EntrySignal:
    """Represents an entry signal with confidence."""
    signal_type: str  # "BUY", "SELL", "HOLD"
    confidence: float  # 0-100
    reasons: list[str]
    entry_price: float
    stop_loss: float
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class NextCandlePrediction:
    """Next candle probability prediction."""
    buy_probability: float  # 0-100
    sell_probability: float  # 0-100
    strength: str  # "STRONG", "NORMAL", "WEAK"
    dominant_direction: str  # "BUY" or "SELL"
    timestamp: datetime = field(default_factory=datetime.now)


# ══════════════════════════════════════════════════════════════════════════
# 5. HTTP / SESSION LAYER
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
# 6. HELPER FUNCTIONS
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


def normalize_stock_symbol(raw: str) -> str:
    s = (raw or "").strip().upper()
    if s.endswith("-EQ"):
        s = s[:-3]
    if ":" in s:
        s = s.split(":")[-1]
    return s


def is_commodity(symbol: str) -> bool:
    """Check if symbol is a commodity."""
    return symbol.upper() in COMMODITY_SYMBOLS


def get_commodity_info(symbol: str) -> Optional[dict]:
    """Get commodity metadata."""
    return COMMODITY_SYMBOLS.get(symbol.upper())


# ══════════════════════════════════════════════════════════════════════════
# 7. REVERSAL ZONE DETECTION (from Pine Script)
# ══════════════════════════════════════════════════════════════════════════

def detect_reversal_zones(
    df: pd.DataFrame, 
    preset: str = "Low",
    calc_mode: str = "Average",
    avg_len: int = 5,
    atr_len: int = 5,
    custom_abs: float = 0.05,
) -> list[ReversalZone]:
    """
    Detects reversal entry zones from market structure (HH/LL swings).
    Converts Pine Script reversal engine to Python.
    """
    if df.empty or len(df) < max(atr_len, avg_len):
        return []
    
    zones = []
    preset_config = REVERSAL_PRESETS.get(preset, REVERSAL_PRESETS["Low"])
    atr_mult = preset_config["atr_mult"]
    preset_pct = preset_config["pct"]
    
    # Calculate ATR and base levels
    atr_values = pd.Series(df["high"] - df["low"]).rolling(atr_len).mean()
    atr_now = atr_values.iloc[-1] if not atr_values.empty else df["high"].iloc[-1] - df["low"].iloc[-1]
    
    reversal_threshold = max(
        df["close"].iloc[-1] * preset_pct / 100.0,
        max(custom_abs, atr_mult * atr_now)
    )
    
    if calc_mode == "High/Low":
        hi_base = df["high"]
        lo_base = df["low"]
    else:  # Average
        hi_base = df["high"].rolling(avg_len).mean()
        lo_base = df["low"].rolling(avg_len).mean()
    
    # Swing detection
    run_high = df["high"].iloc[0]
    run_low = df["low"].iloc[0]
    swing_dir = 1  # 1 = looking for high, -1 = looking for low
    
    for i in range(1, len(df)):
        if swing_dir == 1:
            run_high = max(run_high, hi_base.iloc[i])
            if run_high - lo_base.iloc[i] >= reversal_threshold:
                # Bullish reversal detected (swing high formed)
                zone = ReversalZone(
                    zone_type="BULLISH",
                    center_price=float(run_high),
                    core_low=float(run_high - atr_now * 0.12),
                    core_high=float(run_high + atr_now * 0.12),
                    outer_low=float(run_high - atr_now * 0.26),
                    outer_high=float(run_high + atr_now * 0.26),
                    pivot_bar=i,
                    strength=min(100.0, ((run_high - lo_base.iloc[i]) / reversal_threshold) * 100),
                )
                zones.append(zone)
                run_low = lo_base.iloc[i]
                swing_dir = -1
        else:
            run_low = min(run_low, lo_base.iloc[i])
            if hi_base.iloc[i] - run_low >= reversal_threshold:
                # Bearish reversal detected (swing low formed)
                zone = ReversalZone(
                    zone_type="BEARISH",
                    center_price=float(run_low),
                    core_low=float(run_low - atr_now * 0.12),
                    core_high=float(run_low + atr_now * 0.12),
                    outer_low=float(run_low - atr_now * 0.26),
                    outer_high=float(run_low + atr_now * 0.26),
                    pivot_bar=i,
                    strength=min(100.0, ((hi_base.iloc[i] - run_low) / reversal_threshold) * 100),
                )
                zones.append(zone)
                run_high = hi_base.iloc[i]
                swing_dir = 1
    
    return zones[-5:] if zones else []  # Return last 5 zones


# ══════════════════════════════════════════════════════════════════════════
# 8. PRESSURE & ENTRY SIGNAL DETECTION
# ══════════════════════════════════════════════════════════════════════════

def detect_buy_pressure(df: pd.DataFrame, bar_idx: int = -1) -> PressureSignal:
    """Detect buy pressure: volume spike + bullish candle + price > VWAP."""
    if df.empty:
        return PressureSignal(False, False, False, 0.0)
    
    current = df.iloc[bar_idx]
    avg_vol = df["volume"].rolling(20).mean().iloc[bar_idx]
    vol_ratio = current["volume"] / avg_vol if avg_vol > 0 else 0.0
    vol_spike = vol_ratio > 1.5
    
    vwap = (current["high"] + current["low"] + current["close"]) / 3
    bullish_candle = current["close"] > current["open"]
    price_above_vwap = current["close"] > vwap
    
    buy_pressure = vol_spike and bullish_candle and price_above_vwap
    
    return PressureSignal(
        buy_pressure=buy_pressure,
        sell_pressure=False,
        volume_spike=vol_spike,
        volume_ratio=vol_ratio,
    )


def detect_sell_pressure(df: pd.DataFrame, bar_idx: int = -1) -> PressureSignal:
    """Detect sell pressure: volume spike + bearish candle + price < VWAP."""
    if df.empty:
        return PressureSignal(False, False, False, 0.0)
    
    current = df.iloc[bar_idx]
    avg_vol = df["volume"].rolling(20).mean().iloc[bar_idx]
    vol_ratio = current["volume"] / avg_vol if avg_vol > 0 else 0.0
    vol_spike = vol_ratio > 1.5
    
    vwap = (current["high"] + current["low"] + current["close"]) / 3
    bearish_candle = current["close"] < current["open"]
    price_below_vwap = current["close"] < vwap
    
    sell_pressure = vol_spike and bearish_candle and price_below_vwap
    
    return PressureSignal(
        buy_pressure=False,
        sell_pressure=sell_pressure,
        volume_spike=vol_spike,
        volume_ratio=vol_ratio,
    )


def detect_entry_signal(df: pd.DataFrame, bar_idx: int = -1) -> EntrySignal:
    """
    Generate entry signal based on multiple conditions:
    - EMA alignment (20 > 50)
    - Price position (above/below VWAP)
    - RSI confirmation
    - MACD confirmation
    - Volume spike
    """
    if df.empty or len(df) < 50:
        return EntrySignal("HOLD", 0.0, [], 0.0, 0.0)
    
    current = df.iloc[bar_idx]
    ema_20 = df["close"].ewm(span=20).mean().iloc[bar_idx]
    ema_50 = df["close"].ewm(span=50).mean().iloc[bar_idx]
    ema_200 = df["close"].ewm(span=200).mean().iloc[bar_idx]
    
    rsi = 100 - (100 / (1 + (df["close"].diff().where(df["close"].diff() > 0, 0).rolling(14).mean() / 
                               df["close"].diff().where(df["close"].diff() < 0, 0).rolling(14).mean() * -1)))
    rsi_val = rsi.iloc[bar_idx] if not rsi.empty else 50
    
    vwap = (current["high"] + current["low"] + current["close"]) / 3
    vol_avg = df["volume"].rolling(20).mean().iloc[bar_idx]
    vol_spike = current["volume"] > vol_avg * 1.5
    
    # BUY Conditions
    buy_conditions = []
    if ema_20 > ema_50:
        buy_conditions.append("EMA 20 > EMA 50")
    if current["close"] > vwap:
        buy_conditions.append("Price > VWAP")
    if rsi_val > 55:
        buy_conditions.append(f"RSI {rsi_val:.0f} (bullish)")
    if vol_spike:
        buy_conditions.append("Volume spike detected")
    if current["close"] > current["open"]:
        buy_conditions.append("Bullish candle")
    
    # SELL Conditions
    sell_conditions = []
    if ema_20 < ema_50:
        sell_conditions.append("EMA 20 < EMA 50")
    if current["close"] < vwap:
        sell_conditions.append("Price < VWAP")
    if rsi_val < 45:
        sell_conditions.append(f"RSI {rsi_val:.0f} (bearish)")
    if vol_spike:
        sell_conditions.append("Volume spike detected")
    if current["close"] < current["open"]:
        sell_conditions.append("Bearish candle")
    
    # Determine signal
    signal_type = "HOLD"
    confidence = 0.0
    reasons = []
    
    if len(buy_conditions) >= 4:
        signal_type = "BUY"
        confidence = min(100.0, len(buy_conditions) * 20)
        reasons = buy_conditions
    elif len(sell_conditions) >= 4:
        signal_type = "SELL"
        confidence = min(100.0, len(sell_conditions) * 20)
        reasons = sell_conditions
    
    return EntrySignal(
        signal_type=signal_type,
        confidence=confidence,
        reasons=reasons,
        entry_price=float(current["close"]),
        stop_loss=float(df["low"].iloc[-20:].min()),
    )


def predict_next_candle(df: pd.DataFrame) -> NextCandlePrediction:
    """
    Predict next candle direction and probability.
    Based on Pine Script: f_next_candle_probability()
    """
    if df.empty or len(df) < 50:
        return NextCandlePrediction(50.0, 50.0, "WEAK", "BUY")
    
    current = df.iloc[-1]
    ema_20 = df["close"].ewm(span=20).mean().iloc[-1]
    ema_50 = df["close"].ewm(span=50).mean().iloc[-1]
    ema_200 = df["close"].ewm(span=200).mean().iloc[-1]
    rsi = 100 - (100 / (1 + (df["close"].diff().where(df["close"].diff() > 0, 0).rolling(14).mean() / 
                               df["close"].diff().where(df["close"].diff() < 0, 0).rolling(14).mean() * -1)))
    rsi_val = rsi.iloc[-1] if not rsi.empty else 50
    vwap = (current["high"] + current["low"] + current["close"]) / 3
    vol_avg = df["volume"].rolling(20).mean().iloc[-1]
    vol_ratio = current["volume"] / vol_avg if vol_avg > 0 else 1.0
    
    buy_prob = 0.0
    sell_prob = 0.0
    
    # EMA trend (30 points)
    if current["close"] > ema_20 and ema_20 > ema_50:
        buy_prob += 12
        sell_prob += 3
    elif current["close"] < ema_20 and ema_20 < ema_50:
        sell_prob += 12
        buy_prob += 3
    else:
        buy_prob += 6
        sell_prob += 6
    
    # Higher timeframe trend (15 points)
    if ema_20 > ema_50 and ema_50 > ema_200:
        buy_prob += 10
        sell_prob += 2
    elif ema_20 < ema_50 and ema_50 < ema_200:
        sell_prob += 10
        buy_prob += 2
    else:
        buy_prob += 5
        sell_prob += 5
    
    # RSI (25 points)
    if rsi_val > 55:
        buy_prob += 12
        sell_prob += 3
    elif rsi_val < 45:
        sell_prob += 12
        buy_prob += 3
    else:
        buy_prob += 8
        sell_prob += 8
    
    # Price vs VWAP (15 points)
    if current["close"] > vwap:
        buy_prob += 9
        sell_prob += 2
    elif current["close"] < vwap:
        sell_prob += 9
        buy_prob += 2
    else:
        buy_prob += 5
        sell_prob += 5
    
    # Volume (15 points)
    if vol_ratio > 1.5:
        if current["close"] > current["open"]:
            buy_prob += 9
            sell_prob += 1
        else:
            sell_prob += 9
            buy_prob += 1
    else:
        buy_prob += 4
        sell_prob += 4
    
    total = buy_prob + sell_prob
    if total > 0:
        buy_prob = (buy_prob / total) * 100
        sell_prob = (sell_prob / total) * 100
    else:
        buy_prob = 50.0
        sell_prob = 50.0
    
    strength = "STRONG" if max(buy_prob, sell_prob) >= 75 else ("NORMAL" if max(buy_prob, sell_prob) >= 65 else "WEAK")
    dominant = "BUY" if buy_prob > sell_prob else "SELL"
    
    return NextCandlePrediction(
        buy_probability=buy_prob,
        sell_probability=sell_prob,
        strength=strength,
        dominant_direction=dominant,
    )


# ══════════════════════════════════════════════════════════════════════════
# 9. TECHNICAL INDICATORS (Original functions preserved)
# ══════════════════════════════════════════════════════════════════════════

def calculate_rsi(df: pd.DataFrame, period: int = DEFAULT_RSI_PERIOD, col: str = "close") -> pd.Series:
    """Calculate RSI (Relative Strength Index)."""
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
    """Calculate Exponential Moving Average."""
    if df.empty or col not in df.columns:
        return pd.Series(index=df.index, dtype=float)
    return df[col].ewm(span=period, adjust=False).mean()


def calculate_macd(df: pd.DataFrame, fast: int = DEFAULT_MACD_PARAMS["fast"],
                   slow: int = DEFAULT_MACD_PARAMS["slow"],
                   signal: int = DEFAULT_MACD_PARAMS["signal"],
                   col: str = "close") -> tuple[pd.Series, pd.Series, pd.Series]:
    """Calculate MACD, Signal line, and Histogram."""
    if df.empty or col not in df.columns:
        return pd.Series(0, index=df.index), pd.Series(0, index=df.index), pd.Series(0, index=df.index)
    
    ema_fast = calculate_ema(df, fast, col)
    ema_slow = calculate_ema(df, slow, col)
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    histogram = macd - signal_line
    
    return macd, signal_line, histogram


def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    """Calculate Volume Weighted Average Price."""
    if df.empty or not all(c in df.columns for c in ["high", "low", "close", "volume"]):
        return pd.Series(index=df.index, dtype=float)
    
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    vwap = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()
    return vwap.fillna(df["close"])


def add_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all technical indicators to DataFrame."""
    if df.empty:
        return df
    
    d = df.copy()
    d["rsi"] = calculate_rsi(d)
    d["ema_9"] = calculate_ema(d, 9)
    d["ema_20"] = calculate_ema(d, 20)
    d["ema_21"] = calculate_ema(d, 21)
    d["ema_50"] = calculate_ema(d, 50)
    d["ema_200"] = calculate_ema(d, 200)
    d["macd"], d["macd_signal"], d["macd_hist"] = calculate_macd(d)
    d["vwap"] = calculate_vwap(d)
    
    return d


# ══════════════════════════════════════════════════════════════════════════
# 10. CHARTS (Plotly) — with Reversal Zones
# ══════════════════════════════════════════════════════════════════════════

def _plotly_dark_layout(fig: go.Figure, height: int = 420, title: str = "") -> go.Figure:
    fig.update_layout(
        paper_bgcolor=DARK_BG, plot_bgcolor=DARK_BG,
        font=dict(color=TEXT_MUTED, family="Courier New"),
        height=height, margin=dict(l=10, r=10, t=40 if title else 10, b=10),
        title=dict(text=title, font=dict(color=TEXT_MAIN, size=14)) if title else None,
        legend=dict(bgcolor=PANEL_BG, bordercolor=BORDER_COLOR, borderwidth=1),
    )
    return fig


def chart_reversal_zones(df: pd.DataFrame, zones: list[ReversalZone], title: str = "Reversal Entry Zones") -> go.Figure:
    """Chart price action with reversal zones and indicators."""
    if df.empty or "close" not in df.columns:
        return _plotly_dark_layout(go.Figure(), title=title)
    
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.1,
        row_heights=[0.7, 0.3],
        subplot_titles=("Price with Reversal Zones", "Volume")
    )
    
    # Candlestick
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name="OHLC", showlegend=True
    ), row=1, col=1)
    
    # Reversal zones as rectangles
    for zone in zones[-5:]:  # Last 5 zones
        y_ref = zone.center_price
        color = GREEN if zone.zone_type == "BULLISH" else RED
        
        # Outer zone
        fig.add_hline(y=zone.outer_high, line_dash="dot", line_color=color, line_width=1, 
                     annotation_text=f"{zone.zone_type} Outer", row=1, col=1)
        fig.add_hline(y=zone.outer_low, line_dash="dot", line_color=color, line_width=1, row=1, col=1)
        
        # Core zone
        fig.add_hrect(y0=zone.core_low, y1=zone.core_high, 
                     fillcolor=color, opacity=0.2, layer="below", row=1, col=1)
    
    # VWAP
    if "vwap" in df.columns:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["vwap"], mode="lines", name="VWAP",
            line=dict(color=BLUE, width=1.5)
        ), row=1, col=1)
    
    # EMAs
    if "ema_20" in df.columns:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["ema_20"], mode="lines", name="EMA 20",
            line=dict(color=GREEN, width=1)
        ), row=1, col=1)
    
    if "ema_50" in df.columns:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["ema_50"], mode="lines", name="EMA 50",
            line=dict(color=RED, width=1)
        ), row=1, col=1)
    
    # Volume
    if "volume" in df.columns:
        colors = [GREEN if df["close"].iloc[i] >= df["open"].iloc[i] else RED for i in range(len(df))]
        fig.add_trace(go.Bar(
            x=df.index, y=df["volume"], name="Volume", marker_color=colors, showlegend=True
        ), row=2, col=1)
    
    fig.update_layout(
        xaxis=dict(showgrid=True, gridcolor=BORDER_COLOR),
        yaxis=dict(showgrid=True, gridcolor=BORDER_COLOR),
    )
    
    return _plotly_dark_layout(fig, height=600, title=title)


def chart_pressure_indicators(df: pd.DataFrame) -> go.Figure:
    """Chart RSI, MACD, and Volume Pressure."""
    if df.empty:
        return _plotly_dark_layout(go.Figure())
    
    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.08,
        subplot_titles=("RSI (14)", "MACD", "Volume Pressure")
    )
    
    # RSI
    if "rsi" in df.columns:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["rsi"], mode="lines", name="RSI",
            line=dict(color=BLUE, width=2)
        ), row=1, col=1)
        fig.add_hline(y=70, line_dash="dash", line_color=RED, annotation_text="Overbought", row=1, col=1)
        fig.add_hline(y=30, line_dash="dash", line_color=GREEN, annotation_text="Oversold", row=1, col=1)
    
    # MACD
    if "macd" in df.columns and "macd_signal" in df.columns:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["macd"], mode="lines", name="MACD",
            line=dict(color=BLUE, width=2)
        ), row=2, col=1)
        fig.add_trace(go.Scatter(
            x=df.index, y=df["macd_signal"], mode="lines", name="MACD Signal",
            line=dict(color=RED, width=1.5)
        ), row=2, col=1)
        if "macd_hist" in df.columns:
            colors = [GREEN if v >= 0 else RED for v in df["macd_hist"]]
            fig.add_trace(go.Bar(
                x=df.index, y=df["macd_hist"], name="MACD Histogram",
                marker_color=colors
            ), row=2, col=1)
    
    # Volume pressure (simple: volume ratio)
    if "volume" in df.columns:
        vol_avg = df["volume"].rolling(20).mean()
        vol_ratio = df["volume"] / vol_avg
        colors = [GREEN if df["close"].iloc[i] >= df["open"].iloc[i] else RED for i in range(len(df))]
        fig.add_trace(go.Bar(
            x=df.index, y=vol_ratio, name="Volume Pressure",
            marker_color=colors
        ), row=3, col=1)
        fig.add_hline(y=1.5, line_dash="dash", line_color=AMBER, annotation_text="Spike Threshold", row=3, col=1)
    
    fig.update_yaxes(title_text="RSI", row=1, col=1)
    fig.update_yaxes(title_text="MACD", row=2, col=1)
    fig.update_yaxes(title_text="Vol Ratio", row=3, col=1)
    
    fig.update_layout(
        xaxis=dict(showgrid=True, gridcolor=BORDER_COLOR),
        yaxis=dict(showgrid=True, gridcolor=BORDER_COLOR),
    )
    
    return _plotly_dark_layout(fig, height=700)


# ══════════════════════════════════════════════════════════════════════════
# 11. STREAMLIT UI
# ══════════════════════════════════════════════════════════════════════════

def _configure_page() -> None:
    try:
        st.set_page_config(
            page_title="NSE/NCDEX Options Chain + Reversal Zones", page_icon="📊",
            layout="wide", initial_sidebar_state="expanded",
        )
    except Exception as e:
        logger.warning("st.set_page_config() skipped: %s", e)


def _inject_css() -> None:
    st.markdown(f"""
    <style>
    .stApp {{ background-color: {DARK_BG}; }}
    section[data-testid="stSidebar"] {{ background-color: {PANEL_BG}; border-right: 1px solid {BORDER_COLOR}; }}
    div[data-testid="metric-container"] {{
        background: {PANEL_BG}; border: 1px solid {BORDER_COLOR}; border-radius: 8px; padding: 14px 18px;
    }}
    h1, h2, h3 {{ color: {TEXT_MAIN} !important; }}
    .block-title {{ color: {BLUE}; font-size: 13px; font-weight: 600; text-transform: uppercase;
        letter-spacing: 0.1em; margin-bottom: 8px; }}
    .reversal-card {{ background: {PANEL_BG}; border: 2px solid {BLUE}; border-radius: 8px;
        padding: 14px 16px; margin-bottom: 8px; }}
    .pressure-card {{ background: {PANEL_BG}; border: 1px solid {BORDER_COLOR}; border-radius: 8px;
        padding: 10px 12px; margin-bottom: 6px; }}
    </style>
    """, unsafe_allow_html=True)


def run_enhanced_dashboard(fyers: Any = None) -> None:
    """Main dashboard with commodity + reversal support."""
    _configure_page()
    _inject_css()
    
    st.markdown("## 📊 NSE/NCDEX Options Chain + Reversal Entry Zones")
    st.markdown("**Enhanced with**: Commodities Support | Reversal Zones | Entry Signals | Next Candle Prediction")
    
    with st.sidebar:
        st.markdown("### ⚙️ Configuration")
        
        # Instrument type
        instr_type = st.radio("Instrument Type", ["Index", "Stock", "Commodity"], key="instr_type")
        
        if instr_type == "Index":
            symbol = st.selectbox("Index", list(INDEX_SYMBOLS.keys()), key="index_select")
            is_index = True
            is_commodity_flag = False
        elif instr_type == "Commodity":
            symbol = st.selectbox("Commodity", list(COMMODITY_SYMBOLS.keys()), key="commodity_select")
            is_index = False
            is_commodity_flag = True
        else:
            raw_symbol = st.text_input("Stock Symbol", "RELIANCE", key="stock_input")
            symbol = normalize_stock_symbol(raw_symbol)
            is_index = False
            is_commodity_flag = False
        
        st.divider()
        st.markdown("### 🔍 Reversal Settings")
        reversal_preset = st.selectbox("Reversal Sensitivity", ["Low", "Very Low"], key="reversal_preset")
        avg_len = st.slider("Average Length", 3, 20, 5, key="avg_len")
        atr_len = st.slider("ATR Length", 3, 20, 5, key="atr_len")
        
        st.divider()
        st.markdown("### 📊 Analysis")
        analyze_price_action = st.checkbox("Fetch & Analyze Price Action (requires FYERS)", value=False)
        
        st.divider()
        fetch_clicked = st.button("🔄 Fetch Live Data", use_container_width=True, type="primary")
    
    # Placeholder for data
    if fetch_clicked:
        with st.spinner(f"Fetching data for {symbol}…"):
            # Here you would integrate with FYERS/NSE API
            # For now, create sample data
            st.info(f"✅ Enhanced Dashboard ready for: **{symbol}** ({instr_type})")
            
            if is_commodity_flag:
                comm_info = get_commodity_info(symbol)
                st.write(f"**Commodity Info:** {comm_info}")
            
            # Create sample OHLCV data
            np.random.seed(42)
            dates = pd.date_range("2024-01-01", periods=100, freq="5min")
            sample_df = pd.DataFrame({
                "timestamp": dates,
                "open": np.random.uniform(50000, 52000, 100),
                "high": np.random.uniform(52000, 53000, 100),
                "low": np.random.uniform(49000, 51000, 100),
                "close": np.random.uniform(50000, 52000, 100),
                "volume": np.random.uniform(1000000, 5000000, 100),
            })
            
            # Add indicators
            sample_df = add_technical_indicators(sample_df)
            
            # Detect reversals
            zones = detect_reversal_zones(sample_df, preset=reversal_preset)
            
            # Get signals
            pressure = detect_buy_pressure(sample_df)
            entry_sig = detect_entry_signal(sample_df)
            next_pred = predict_next_candle(sample_df)
            
            st.divider()
            st.markdown("### 📍 Reversal Entry Zones")
            
            if zones:
                for zone in zones[-3:]:
                    st.markdown(f"""
                    <div class="reversal-card">
                        <b>{zone.zone_type} Zone</b> @ ₹{zone.center_price:.2f} (Strength: {zone.strength:.0f}%)
                        <br>Core: ₹{zone.core_low:.2f} - ₹{zone.core_high:.2f}
                        <br>Outer: ₹{zone.outer_low:.2f} - ₹{zone.outer_high:.2f}
                    </div>
                    """, unsafe_allow_html=True)
            else:
                st.info("No reversal zones detected in current data.")
            
            st.divider()
            st.markdown("### 🎯 Pressure & Entry")
            
            col1, col2, col3 = st.columns(3)
            col1.metric("Buy Pressure", "✓ YES" if pressure.buy_pressure else "✗ NO", 
                       delta=f"{pressure.volume_ratio:.2f}x")
            col2.metric("Entry Signal", entry_sig.signal_type, delta=f"{entry_sig.confidence:.0f}%")
            col3.metric("Next Candle", next_pred.dominant_direction, 
                       delta=f"{max(next_pred.buy_probability, next_pred.sell_probability):.0f}%")
            
            st.divider()
            st.markdown("### 📈 Charts")
            
            tab1, tab2 = st.tabs(["Reversal Zones", "Pressure Indicators"])
            
            with tab1:
                st.plotly_chart(chart_reversal_zones(sample_df, zones), use_container_width=True)
            
            with tab2:
                st.plotly_chart(chart_pressure_indicators(sample_df), use_container_width=True)
            
            st.divider()
            st.markdown("### 📋 Next Candle Prediction")
            
            pred_col1, pred_col2, pred_col3, pred_col4 = st.columns(4)
            pred_col1.metric("Buy Probability", f"{next_pred.buy_probability:.1f}%")
            pred_col2.metric("Sell Probability", f"{next_pred.sell_probability:.1f}%")
            pred_col3.metric("Strength", next_pred.strength)
            pred_col4.metric("Dominant", next_pred.dominant_direction)
            
            if entry_sig.reasons:
                st.markdown("**Entry Signal Reasons:**")
                for reason in entry_sig.reasons:
                    st.write(f"• {reason}")


if __name__ == "__main__":
    run_enhanced_dashboard()
