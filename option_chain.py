from __future__ import annotations

import io
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
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

INDIA_TZ = ZoneInfo("Asia/Kolkata")
def _india_now() -> datetime:
    return datetime.now(INDIA_TZ)

# ══════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════════════════
NSE_BASE_URL = "https://www.nseindia.com"
NSE_INDEX_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-indices"
NSE_EQUITY_CHAIN_URL = f"{NSE_BASE_URL}/api/option-chain-equities"

INDEX_SYMBOLS: dict[str, str] = {
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "SENSEX": "SENSEX",
}

DEFAULT_LOT_SIZES: dict[str, int] = {
    "NIFTY": 25,
    "BANKNIFTY": 15,
    "FINNIFTY": 25,
    "MIDCPNIFTY": 50,
    "SENSEX": 10,
    "BANKEX": 15,
    "_STOCK_DEFAULT": 1,
}

# ══════════════════════════════════════════════════════════════════════════
# MARKET PRESSURE DATACLASS
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class MarketPressure:
    total_call_pressure: float = 50.0
    total_put_pressure: float = 50.0
    net_market_bias: float = 0.0
    market_sentiment: str = "NEUTRAL"
    pcr_vs_pressure: str = "N/A"
    volume_surge_detected: bool = False
    oi_accumulation_detected: bool = False
    itm_pressure: float = 50.0
    atm_pressure: float = 50.0
    otm_pressure: float = 50.0

# ══════════════════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS
# ══════════════════════════════════════════════════════════════════════════
def calculate_rsi(df: pd.DataFrame, period: int = 14, col: str = "close") -> pd.Series:
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
    if df.empty or col not in df.columns:
        return pd.Series(index=df.index, dtype=float)
    return df[col].ewm(span=period, adjust=False).mean()

def calculate_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9, col: str = "close"):
    if df.empty or col not in df.columns:
        return pd.Series(0, index=df.index), pd.Series(0, index=df.index), pd.Series(0, index=df.index)
    ema_fast = calculate_ema(df, fast, col)
    ema_slow = calculate_ema(df, slow, col)
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    histogram = macd - signal_line
    return macd, signal_line, histogram

def calculate_vwap(df: pd.DataFrame) -> pd.Series:
    if df.empty or not all(c in df.columns for c in ["high", "low", "close", "volume"]):
        return pd.Series(index=df.index, dtype=float)
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    vwap = (typical_price * df["volume"]).cumsum() / df["volume"].cumsum()
    return vwap.fillna(df["close"])

def calculate_rvol(df: pd.DataFrame, period: int = 20) -> pd.Series:
    if df.empty or "volume" not in df.columns:
        return pd.Series(1.0, index=df.index)
    avg_vol = df["volume"].rolling(window=period, min_periods=1).mean()
    rvol = df["volume"] / avg_vol.replace(0, 1.0)
    return rvol.fillna(1.0)

# ══════════════════════════════════════════════════════════════════════════
# MOVEMENT SCANNER (UPDATED)
# ══════════════════════════════════════════════════════════════════════════
def compute_scalping_early_warning(
    df_dict: dict[str, pd.DataFrame],
    spot: float,
    market_pressure: MarketPressure
) -> dict[str, Any]:
    result = {
        "enabled": True, "status": "WAIT", "direction": "NEUTRAL",
        "score": 0.0, "confidence": 0.0, "trigger": "No setup",
        "reasons": [], "timeframe": "1M/3M/5M",
        "entry": 0.0, "stop_loss": 0.0, "target_1": 0.0,
        "target_2": 0.0, "target_3": 0.0,
    }

    # --- existing scanner logic (ATR, RVOL, EMA, VWAP checks) ---

    # ✅ Extra institutional pressure filter
    if result["direction"] in ("UP", "DOWN"):
        if market_pressure.market_sentiment == "BEARISH" and result["direction"] == "UP":
            result.update(status="HOLD", trigger="Sentiment mismatch")
        elif market_pressure.market_sentiment == "BULLISH" and result["direction"] == "DOWN":
            result.update(status="HOLD", trigger="Sentiment mismatch")

    # ✅ Multi-timeframe alignment check (15M EMA)
    tf15 = df_dict.get("15M")
    if isinstance(tf15, pd.DataFrame) and len(tf15) > 20:
        ema9 = calculate_ema(tf15, 9).iloc[-1]
        ema21 = calculate_ema(tf15, 21).iloc[-1]
        if result["direction"] == "UP" and ema9 < ema21:
            result.update(status="HOLD", trigger="15M misaligned")
        elif result["direction"] == "DOWN" and ema9 > ema21:
            result.update(status="HOLD", trigger="15M misaligned")

    return result
