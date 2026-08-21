import streamlit as st
import pandas as pd
import numpy as np
import requests
import time
import io
import os
import re
import json
import csv
import gc
import logging
import sys
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

# ════════════════════════════════════════════════════════════════════════════════
# IMPORTS & CONFIG (FROM ORIGINAL)
# ════════════════════════════════════════════════════════════════════════════════
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30))

try:
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

# ════════════════════════════════════════════════════════════════════════════════
# CONSTANTS (ORIGINAL + NEW)
# ════════════════════════════════════════════════════════════════════════════════
DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO = datetime.today().strftime("%Y-%m-%d")
FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"
NIFTY_BENCHMARK_SYMBOL = "NSE:NIFTY50-INDEX"
MAX_WORKERS = 8
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 1.0
DEFAULT_SCAN_STOCKS = 2300
FYERS_APP_ID = os.environ.get("FYERS_APP_ID", "")
OPTIONS_STRIKE_COUNT = 10
OPTIONS_HTTP_TIMEOUT = 15

# ════════════════════════════════════════════════════════════════════════════════
# V17 NEW CONSTANTS
# ════════════════════════════════════════════════════════════════════════════════
DEFAULT_CONFIDENCE_THRESHOLD = 70
DEFAULT_RVOL_THRESHOLD = 1.2
DEFAULT_STRONG_RVOL = 1.5
GOLDEN_CROSS_MIN_SIGNALS = 1  # Minimum signals needed for Golden Cross dashboard
DEATH_CROSS_MIN_SIGNALS = 1
ADDITIONAL_ANALYSIS_LOOKBACK_DAYS = 30
REFRESH_INTERVALS = [30, 60, 120]  # seconds

# ════════════════════════════════════════════════════════════════════════════════
# 15-MIN REVERSAL SCANNER CONSTANTS (ORIGINAL)
# ════════════════════════════════════════════════════════════════════════════════
REVERSAL_RESOLUTION = "15"
REVERSAL_LOOKBACK_DAYS = 5
REVERSAL_CONFIRMATION_BARS = 0
REVERSAL_ATR_LENGTH = 5
REVERSAL_ATR_MULTIPLIER = 2.8
REVERSAL_MIN_MOVE_PCT = 0.015
REVERSAL_CUSTOM_ABS = 0.05

# ════════════════════════════════════════════════════════════════════════════════
# VOLUME BIG MOVEMENT CONSTANTS (ORIGINAL)
# ════════════════════════════════════════════════════════════════════════════════
VOL_BIGMOVE_MIN_RVOL = 2.5
VOL_BIGMOVE_MIN_BODY_PCT = 0.8
VOL_BIGMOVE_LOOKBACK = 20

# ════════════════════════════════════════════════════════════════════════════════
# MULTI-TIMEFRAME CONSTANTS (NEW)
# ════════════════════════════════════════════════════════════════════════════════
SWING_LOOKBACK_PERIODS = 20
EMA_PERIODS = [9, 21, 50, 200]
VWAP_LOOKBACK = 20
MSS_MIN_CONFIRMATION = 1
STRUCTURE_TIMEFRAMES = ["5", "15", "60"]
MASTER_SIGNAL_LOOKBACK_DAYS = 10

# ════════════════════════════════════════════════════════════════════════════════
# LOGGING & UTILITIES (ORIGINAL)
# ════════════════════════════════════════════════════════════════════════════════
def _now_ist() -> datetime:
    return datetime.now(IST)

def _ensure_app_folders() -> None:
    for folder in ("logs", "charts", "exports"):
        os.makedirs(folder, exist_ok=True)

_ensure_app_folders()
logger = logging.getLogger("nse_scanner_v17")
logger.setLevel(logging.DEBUG)

# Add file handler
if not logger.handlers:
    fh = logging.FileHandler("logs/scanner.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    logger.addHandler(fh)

def _candle_signal_timestamp(df, is_daily: bool = False, resolution: str = "15") -> Tuple[str, str]:
    """Return the CLOSE time of the actual signal candle, not the candle-open time."""
    ts = df["Time"].iloc[-1]
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts_ist = ts.tz_convert(IST)
    if is_daily:
        close_ts = ts_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    else:
        try:
            minutes = int(resolution)
        except Exception:
            minutes = 15
        close_ts = ts_ist + timedelta(minutes=minutes)
    return close_ts.strftime("%d-%b-%Y"), close_ts.strftime("%H:%M:%S") + " IST"

def _generated_timestamp() -> str:
    """Current scanner detection time, separate from the signal candle time."""
    return _now_ist().strftime("%d-%b-%Y %H:%M:%S IST")

# ════════════════════════════════════════════════════════════════════════════════
# CORE INDICATORS (RETAINED FROM ORIGINAL)
# ════════════════════════════════════════════════════════════════════════════════
def calculate_rsi(close, period: int = 14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)

def calculate_macd(close):
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line

def calculate_atr(df, period: int = 14):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

def _last_valid_atr(df, period: int = 14) -> float:
    atr_series = calculate_atr(df, period)
    val = atr_series.iloc[-1] if len(atr_series) else np.nan
    if pd.isna(val) or val <= 0:
        last_close = float(df["Close"].iloc[-1]) if len(df) else 0.0
        val = max(last_close * 0.005, 0.01)
    return float(val)

# ════════════════════════════════════════════════════════════════════════════════
# BUYING/SELLING PRESSURE INDICATORS
# ════════════════════════════════════════════════════════════════════════════════
def calculate_buying_selling_pressure(df) -> Dict[str, Any]:
    """Calculate buying and selling pressure using volume-weighted price analysis."""
    if len(df) < 5:
        return {
            "buying_pressure": 50, "selling_pressure": 50,
            "buying_volume": 0, "selling_volume": 0,
            "pressure_ratio": 1.0, "trend": "NEUTRAL"
        }
    
    recent = df.tail(20).copy()
    buying_vol = 0.0
    selling_vol = 0.0
    
    for idx in range(len(recent)):
        candle = recent.iloc[idx]
        close = float(candle["Close"])
        open_ = float(candle["Open"])
        volume = float(candle["Volume"])
        
        if close > open_:
            buying_vol += volume
        elif close < open_:
            selling_vol += volume
        else:
            buying_vol += volume * 0.5
            selling_vol += volume * 0.5
    
    total_vol = buying_vol + selling_vol
    if total_vol == 0:
        return {
            "buying_pressure": 50, "selling_pressure": 50,
            "buying_volume": 0, "selling_volume": 0,
            "pressure_ratio": 1.0, "trend": "NEUTRAL"
        }
    
    bp_pct = (buying_vol / total_vol) * 100
    sp_pct = (selling_vol / total_vol) * 100
    pressure_ratio = buying_vol / selling_vol if selling_vol > 0 else float('inf')
    
    if bp_pct > 65:
        trend = "STRONG_BUYING"
    elif bp_pct > 55:
        trend = "BUYING"
    elif sp_pct > 65:
        trend = "STRONG_SELLING"
    elif sp_pct > 55:
        trend = "SELLING"
    else:
        trend = "NEUTRAL"
    
    return {
        "buying_pressure": round(bp_pct, 1),
        "selling_pressure": round(sp_pct, 1),
        "buying_volume": round(buying_vol, 0),
        "selling_volume": round(selling_vol, 0),
        "pressure_ratio": round(pressure_ratio, 2) if pressure_ratio != float('inf') else 0,
        "trend": trend
    }

# ════════════════════════════════════════════════════════════════════════════════
# NEXT CANDLE BIAS CALCULATION (NEW)
# ════════════════════════════════════════════════════════════════════════════════
def calculate_next_candle_bias(df, timeframe_analysis: Dict[str, Any]) -> Dict[str, Any]:
    """
    Calculate Next Candle Bias using weighted scoring from confirmed data.
    
    Uses only currently confirmed information - NO future/incomplete candles.
    Returns bias (BUY/SELL/NEUTRAL) and confidence (0-100).
    """
    if df is None or len(df) < 5:
        return {"bias": "NEUTRAL", "confidence": 0.0}
    
    try:
        data = timeframe_analysis.get("data", {})
        if not data:
            return {"bias": "NEUTRAL", "confidence": 0.0}
        
        buy_score = 50.0
        scores_count = 0
        
        # 1. Candle direction (current/last closed candle)
        last_close = float(df["Close"].iloc[-1])
        last_open = float(df["Open"].iloc[-1])
        
        if last_close > last_open:  # Green candle
            buy_score += 8
            scores_count += 1
        elif last_close < last_open:  # Red candle
            buy_score -= 8
            scores_count += 1
        
        # 2. Buying/Selling Pressure
        pressure_trend = data.get("pressure_trend", "NEUTRAL")
        if pressure_trend == "STRONG_BUYING":
            buy_score += 12
        elif pressure_trend == "BUYING":
            buy_score += 8
        elif pressure_trend == "STRONG_SELLING":
            buy_score -= 12
        elif pressure_trend == "SELLING":
            buy_score -= 8
        scores_count += 1
        
        # 3. Price vs VWAP
        vwap = data.get("vwap")
        if vwap is not None:
            if last_close > vwap:
                buy_score += 6
            elif last_close < vwap:
                buy_score -= 6
            scores_count += 1
        
        # 4. EMA Trend
        ema_trend = data.get("ema_trend", "NEUTRAL")
        if ema_trend == "BULLISH":
            buy_score += 7
        elif ema_trend == "BEARISH":
            buy_score -= 7
        scores_count += 1
        
        # 5. RSI (with proper interpretation)
        rsi = data.get("rsi", 50)
        if rsi > 70:  # Overbought - potential pullback
            buy_score -= 4
        elif rsi < 30:  # Oversold - potential bounce
            buy_score += 4
        elif rsi > 60:  # Bullish territory
            buy_score += 3
        elif rsi < 40:  # Bearish territory
            buy_score -= 3
        scores_count += 1
        
        # 6. MACD
        if data.get("macd_bullish"):
            buy_score += 5
        else:
            buy_score -= 5
        scores_count += 1
        
        # 7. Structure
        structure_trend = data.get("structure_trend", "NEUTRAL")
        if structure_trend == "BULLISH":
            buy_score += 6
        elif structure_trend == "BEARISH":
            buy_score -= 6
        scores_count += 1
        
        # 8. BOS/CHoCH/MSS (strong directional signals)
        if data.get("bullish_choch") or data.get("bullish_mss") or data.get("bullish_cisd"):
            buy_score += 10
        elif data.get("bearish_choch") or data.get("bearish_mss") or data.get("bearish_cisd"):
            buy_score -= 10
        scores_count += 1
        
        # 9. Volume/RVOL confirmation
        rvol = data.get("rvol", 1.0)
        if rvol > 1.5:  # Strong volume
            if buy_score > 50:
                buy_score += 4
            else:
                buy_score -= 4
        elif rvol < 0.8:  # Weak volume
            buy_score = buy_score * 0.95
        scores_count += 1
        
        # Normalize score to 0-100
        buy_score = max(0, min(100, buy_score))
        
        # Calculate confidence based on how clear the signal is
        confidence = round(abs(buy_score - 50) * 0.8, 1)
        confidence = max(0, min(100, confidence))
        
        # Determine bias
        if buy_score >= 65:
            bias = "🟢 BUY"
        elif buy_score <= 35:
            bias = "🔴 SELL"
        else:
            bias = "🟡 NEUTRAL"
        
        return {
            "bias": bias,
            "confidence": confidence,
            "score": buy_score,
        }
    
    except Exception as e:
        return {"bias": "NEUTRAL", "confidence": 0.0}

# ════════════════════════════════════════════════════════════════════════════════
# NEW INDICATORS (MULTI-TIMEFRAME)
# ════════════════════════════════════════════════════════════════════════════════
def calculate_vwap(df) -> pd.Series:
    """Calculate VWAP (Volume Weighted Average Price)"""
    if "Volume" not in df.columns or len(df) == 0:
        return pd.Series([np.nan] * len(df), index=df.index)
    
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    
    typical_price = (high + low + close) / 3
    vwap = (typical_price * volume).cumsum() / volume.cumsum()
    return vwap.fillna(method="ffill").fillna(close)

def calculate_ema(close, period: int = 9) -> pd.Series:
    """Calculate EMA"""
    return close.ewm(span=period, adjust=False).mean()

def find_swing_highs_lows(df, lookback: int = SWING_LOOKBACK_PERIODS) -> Dict[str, Any]:
    """Return the latest confirmed swing high/low using only CLOSED candles."""
    empty = {"swing_high": None, "swing_high_idx": None,
             "swing_high_bars_ago": None, "swing_low": None,
             "swing_low_idx": None, "swing_low_bars_ago": None}
    if df is None or len(df) < 7:
        return empty
    d = df.tail(max(lookback, 7)).reset_index(drop=True).copy()
    highs, lows = d["High"].astype(float).to_numpy(), d["Low"].astype(float).to_numpy()
    pivot_highs, pivot_lows = [], []
    for i in range(2, len(d) - 2):
        if highs[i] > highs[i-1] and highs[i] >= highs[i-2] and highs[i] > highs[i+1] and highs[i] >= highs[i+2]:
            pivot_highs.append(i)
        if lows[i] < lows[i-1] and lows[i] <= lows[i-2] and lows[i] < lows[i+1] and lows[i] <= lows[i+2]:
            pivot_lows.append(i)
    if pivot_highs:
        i = pivot_highs[-1]
        empty.update(swing_high=float(highs[i]), swing_high_idx=int(i), swing_high_bars_ago=int(len(d)-1-i))
    if pivot_lows:
        i = pivot_lows[-1]
        empty.update(swing_low=float(lows[i]), swing_low_idx=int(i), swing_low_bars_ago=int(len(d)-1-i))
    return empty

def _confirmed_pivots(df, left: int = 2, right: int = 2):
    """Return confirmed pivot highs/lows. The final `right` candles are excluded."""
    if df is None or len(df) < left + right + 3:
        return [], []
    d = df.reset_index(drop=True)
    highs, lows = d["High"].astype(float).to_numpy(), d["Low"].astype(float).to_numpy()
    ph, pl = [], []
    for i in range(left, len(d) - right):
        if highs[i] >= max(highs[i-left:i]) and highs[i] > max(highs[i+1:i+right+1]):
            ph.append((i, float(highs[i])))
        if lows[i] <= min(lows[i-left:i]) and lows[i] < min(lows[i+1:i+right+1]):
            pl.append((i, float(lows[i])))
    return ph, pl

def detect_structure(df) -> Dict[str, Any]:
    """Detect HH/HL/LH/LL from confirmed pivots only."""
    ph, pl = _confirmed_pivots(df)
    result = {"type":"UNKNOWN", "trend":"NEUTRAL", "current_high":None,
              "current_low":None, "prev_high":None, "prev_low":None, "strength": 0}
    if len(ph) >= 2:
        result["prev_high"], result["current_high"] = ph[-2][1], ph[-1][1]
    if len(pl) >= 2:
        result["prev_low"], result["current_low"] = pl[-2][1], pl[-1][1]
    if len(ph) >= 2 and len(pl) >= 2:
        hh = ph[-1][1] > ph[-2][1]
        hl = pl[-1][1] > pl[-2][1]
        lh = ph[-1][1] < ph[-2][1]
        ll = pl[-1][1] < pl[-2][1]
        if hh and hl:
            result["type"], result["trend"] = "HH/HL", "BULLISH"
            result["strength"] = min(100, abs((ph[-1][1] - ph[-2][1]) / ph[-2][1] * 100) * 10)
        elif lh and ll:
            result["type"], result["trend"] = "LH/LL", "BEARISH"
            result["strength"] = min(100, abs((pl[-1][1] - pl[-2][1]) / pl[-2][1] * 100) * 10)
        elif hh:
            result["type"], result["trend"] = "HH", "BULLISH"
            result["strength"] = 60
        elif ll:
            result["type"], result["trend"] = "LL", "BEARISH"
            result["strength"] = 60
        elif hl:
            result["type"], result["trend"] = "HL", "BULLISH"
            result["strength"] = 40
        elif lh:
            result["type"], result["trend"] = "LH", "BEARISH"
            result["strength"] = 40
    return result

def detect_choch(df) -> Dict[str, Any]:
    """Detect a NEW confirmed CHoCH on the latest CLOSED candle only."""
    ph, pl = _confirmed_pivots(df)
    out = {"bullish_choch":False, "bearish_choch":False, "choch_price":None,
           "choch_type":"NONE", "confirmation":"NONE"}
    if len(ph) < 2 or len(pl) < 2 or len(df) < 10:
        out["confirmation"] = "PENDING"
        return out
    prev_close = float(df["Close"].iloc[-2])
    close = float(df["Close"].iloc[-1])
    bearish_structure = ph[-1][1] < ph[-2][1] and pl[-1][1] < pl[-2][1]
    bullish_structure = ph[-1][1] > ph[-2][1] and pl[-1][1] > pl[-2][1]
    
    min_move = max(ph[-1][1] * 0.001, 0.5)
    
    if bearish_structure and prev_close <= ph[-1][1] and close > (ph[-1][1] + min_move):
        out.update(bullish_choch=True, choch_price=ph[-1][1], choch_type="BULLISH_CHoCH", confirmation="CONFIRMED")
    elif bullish_structure and prev_close >= pl[-1][1] and close < (pl[-1][1] - min_move):
        out.update(bearish_choch=True, choch_price=pl[-1][1], choch_type="BEARISH_CHoCH", confirmation="CONFIRMED")
    return out

def detect_cisd(df: pd.DataFrame) -> Dict[str, Any]:
    """Detect a NEW confirmed CISD event on the latest CLOSED candle only."""
    result = {"bullish_cisd": False, "bearish_cisd": False, "cisd_type": "NONE", "cisd_price": None}
    if df is None or len(df) < 5:
        return result
    d = df.reset_index(drop=True)
    last = d.iloc[-1]
    prev_close = float(d["Close"].iloc[-2])
    prior = d.iloc[:-1]
    
    bearish = prior[prior["Close"] < prior["Open"]].tail(3)
    bullish = prior[prior["Close"] > prior["Open"]].tail(3)
    
    min_move = float(last["Close"]) * 0.001
    
    if not bearish.empty:
        level = float(bearish.iloc[-1]["High"])
        if prev_close <= level and float(last["Close"]) > (level + min_move):
            result.update(bullish_cisd=True, cisd_type="BULLISH_CISD", cisd_price=float(last["Close"]))
    
    if not bullish.empty and not result["bullish_cisd"]:
        level = float(bullish.iloc[-1]["Low"])
        if prev_close >= level and float(last["Close"]) < (level - min_move):
            result.update(bearish_cisd=True, cisd_type="BEARISH_CISD", cisd_price=float(last["Close"]))
    return result

def detect_mss(df) -> Dict[str, Any]:
    """Detect a NEW confirmed MSS event on the latest CLOSED candle only."""
    ph, pl = _confirmed_pivots(df)
    out = {"bullish_mss":False, "bearish_mss":False, "mss_type":"NONE", "confirmation":"NONE"}
    if len(ph) < 2 or len(pl) < 2 or len(df) < 10:
        out["confirmation"] = "PENDING"
        return out
    prev_close = float(df["Close"].iloc[-2])
    close = float(df["Close"].iloc[-1])
    bearish_structure = ph[-1][1] < ph[-2][1] and pl[-1][1] < pl[-2][1]
    bullish_structure = ph[-1][1] > ph[-2][1] and pl[-1][1] > pl[-2][1]
    
    min_move = max(ph[-1][1] * 0.001, 0.5)
    
    if bearish_structure and prev_close <= ph[-1][1] and close > (ph[-1][1] + min_move):
        out.update(bullish_mss=True, mss_type="BULLISH_MSS", confirmation="CONFIRMED")
    elif bullish_structure and prev_close >= pl[-1][1] and close < (pl[-1][1] - min_move):
        out.update(bearish_mss=True, mss_type="BEARISH_MSS", confirmation="CONFIRMED")
    return out

# ════════════════════════════════════════════════════════════════════════════════
# SYMBOL LOADING (RETAINED WITH ENHANCEMENT FOR F&O)
# ════════════════════════════════════════════════════════════════════════════════
_VALID_EQ_SYMBOL_RE = re.compile(r"^NSE:[A-Z0-9&\-]+-EQ$")
_FO_EQUITY_PATTERN = re.compile(r"^NSE:[A-Z0-9&\-]+-EQ$")

def _validate_symbols(symbols) -> List[str]:
    seen = set()
    valid = []
    for s in symbols:
        if not isinstance(s, str):
            continue
        s = s.strip().upper()
        if not s or s in seen:
            continue
        if not _VALID_EQ_SYMBOL_RE.match(s):
            continue
        seen.add(s)
        valid.append(s)
    return valid

@st.cache_data(ttl=60 * 60 * 12)
def load_nse_equity_symbols() -> List[str]:
    try:
        resp = requests.get(FYERS_NSE_CM_SYMBOL_MASTER, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        st.error(f"Could not download Fyers symbol master: {e}")
        return []
    lines = [ln for ln in resp.text.strip().split("\n") if ln.strip()]
    if not lines:
        return []
    sample = lines[:min(500, len(lines))]
    split_sample = [ln.split(",") for ln in sample]
    max_cols = max((len(p) for p in split_sample), default=0)
    best_col, best_hits = None, 0
    for col_idx in range(max_cols):
        hits = sum(1 for parts in split_sample if len(parts) > col_idx and parts[col_idx].strip().startswith("NSE:") and parts[col_idx].strip().endswith("-EQ"))
        if hits > best_hits:
            best_col, best_hits = col_idx, hits
    if best_col is None or best_hits == 0:
        st.error("Could not locate trading-symbol column.")
        return []
    symbols = []
    for line in lines:
        parts = line.split(",")
        if len(parts) <= best_col:
            continue
        sym = parts[best_col].strip()
        if sym.startswith("NSE:") and sym.endswith("-EQ"):
            symbols.append(sym)
    return sorted(set(_validate_symbols(symbols)))

@st.cache_data(ttl=60 * 60 * 12)
def load_fo_stocks() -> List[str]:
    """Load NSE equity symbols that have active equity-derivative contracts."""
    try:
        cm_symbols = set(load_nse_equity_symbols())
        if not cm_symbols:
            return []

        urls = [
            "https://public.fyers.in/sym_details/NSE_FO.csv",
            "http://public.fyers.in/sym_details/NSE_FO.csv",
        ]
        text = None
        last_error = None
        for url in urls:
            try:
                r = requests.get(
                    url,
                    timeout=30,
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"},
                )
                r.raise_for_status()
                if r.text and len(r.text) > 100:
                    text = r.text
                    break
            except Exception as exc:
                last_error = exc

        if not text:
            logging.warning("F&O symbol master unavailable: %s", last_error)
            return []

        fo_underlyings = set()
        reader = csv.reader(io.StringIO(text))
        for row in reader:
            if len(row) < 14:
                continue

            short_sym = str(row[13]).strip().upper()
            contract_symbol = str(row[9]).strip().upper() if len(row) > 9 else ""
            exchange = str(row[10]).strip() if len(row) > 10 else ""

            if exchange not in {"10", "NSE"}:
                continue
            if not short_sym or short_sym in {"NONE", "NAN"}:
                continue

            if contract_symbol.startswith("NSE:") and short_sym:
                candidate = f"NSE:{short_sym}-EQ"
                if candidate in cm_symbols:
                    fo_underlyings.add(candidate)

        result = sorted(fo_underlyings)
        logging.info("Loaded %d NSE F&O equity underlyings", len(result))
        return result

    except Exception as e:
        logging.exception("F&O symbol loading failed: %s", e)
        return []

# ════════════════════════════════════════════════════════════════════════════════
# SAFE HISTORY FETCH (RETAINED)
# ════════════════════════════════════════════════════════════════════════════════
_HISTORY_MAX_RETRIES = 3
_HISTORY_BASE_DELAY_SECONDS = 1.0

def _safe_history(fyers, params: dict, max_retries: int = _HISTORY_MAX_RETRIES, base_delay: float = _HISTORY_BASE_DELAY_SECONDS):
    symbol = params.get("symbol", "UNKNOWN")
    last_err = "unknown error"
    for attempt in range(1, max_retries + 1):
        try:
            resp = fyers.history(params)
        except requests.exceptions.Timeout:
            last_err = "timeout"
        except requests.exceptions.ConnectionError:
            last_err = "network error"
        except requests.exceptions.RequestException as e:
            last_err = f"request error: {e}"
        except (ValueError, TypeError) as e:
            last_err = f"invalid response: {e}"
        except Exception as e:
            last_err = f"unexpected error: {e}"
        else:
            if not isinstance(resp, dict):
                last_err = "empty/invalid response"
            else:
                status = resp.get("s")
                if status == "ok":
                    candles = resp.get("candles")
                    if not isinstance(candles, list):
                        last_err = "malformed candle data"
                    else:
                        return resp, None
                else:
                    message = str(resp.get("message", status or "unknown"))
                    if "rate" in message.lower() or "limit" in message.lower():
                        last_err = f"rate limited: {message}"
                        time.sleep(base_delay * attempt * 2)
                        continue
                    return None, message
        if attempt < max_retries:
            time.sleep(base_delay * attempt)
    return None, f"{symbol}: {last_err} (after {max_retries} attempts)"

# ════════════════════════════════════════════════════════════════════════════════
# SCAN STATS (ORIGINAL)
# ════════════════════════════════════════════════════════════════════════════════
class ScanStats:
    def __init__(self, total: int):
        self.total = total
        self.scanned = 0
        self.successful = 0
        self.skipped = 0
        self.failed = 0
        self._start = time.time()

    def record(self, has_result: bool, has_error: bool) -> None:
        self.scanned += 1
        if has_result:
            self.successful += 1
        elif has_error:
            self.failed += 1
        else:
            self.skipped += 1

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self._start

def _display_scan_summary(stats: "ScanStats") -> None:
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total Stocks", stats.total)
    c2.metric("Scanned", stats.scanned)
    c3.metric("Successful", stats.successful)
    c4.metric("Skipped", stats.skipped)
    c5.metric("Failed", stats.failed)
    c6.metric("Scan Time", f"{stats.elapsed_seconds:.1f}s")

# ════════════════════════════════════════════════════════════════════════════════
# TIMEFRAME DATA FETCHER (NEW)
# ════════════════════════════════════════════════════════════════════════════════
def _fetch_timeframe_data(fyers, symbol, resolution: str, lookback_days: int = 30) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV data for a specific timeframe.
    Returns cleaned DataFrame or None if failed.
    Ensures no data contamination between timeframes.
    """
    date_from = (datetime.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")
    
    resp, err = _safe_history(fyers, {
        "symbol": symbol,
        "resolution": resolution,
        "date_format": "1",
        "range_from": date_from,
        "range_to": date_to,
        "cont_flag": "1",
    })
    
    if err or not resp:
        return None
    
    candles = resp.get("candles")
    if not candles or len(candles) < 10:
        return None
    
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_values("Time").reset_index(drop=True)
        
        if len(df) < 10:
            return None
        
        # Remove unclosed candle (last candle less than resolution old)
        if len(df) > 1:
            last_time = df["Time"].iloc[-1]
            candle_age = (_now_ist() - last_time).total_seconds() / 60
            res_minutes = int(resolution)
            if candle_age < res_minutes + 1:  # Unclosed candle
                df = df.iloc[:-1].reset_index(drop=True)
        
        if len(df) < 10:
            return None
        
        return df
    
    except Exception as e:
        return None

# ════════════════════════════════════════════════════════════════════════════════
# TIMEFRAME ANALYSIS ENGINE (NEW - WITH PRESSURE)
# ════════════════════════════════════════════════════════════════════════════════
def analyze_timeframe(fyers, symbol: str, resolution: str) -> Dict[str, Any]:
    """
    Analyze a specific timeframe.
    Returns comprehensive structure, CHoCH, MSS, and indicator data WITH PRESSURE.
    """
    df = _fetch_timeframe_data(fyers, symbol, resolution, lookback_days=30)
    
    if df is None or len(df) < 10:
        return {
            "timeframe": resolution,
            "status": "DATA_UNAVAILABLE",
            "data": None,
        }
    
    try:
        # Calculate indicators
        rsi = calculate_rsi(df["Close"])
        macd_line, macd_sig, macd_hist = calculate_macd(df["Close"])
        atr = calculate_atr(df)
        vwap = calculate_vwap(df)
        ema9 = calculate_ema(df["Close"], 9)
        ema21 = calculate_ema(df["Close"], 21)
        ema50 = calculate_ema(df["Close"], 50)
        ema200 = calculate_ema(df["Close"], 200)
        
        # Calculate buying/selling pressure
        pressure = calculate_buying_selling_pressure(df)
        
        # Structure analysis
        structure = detect_structure(df)
        choch = detect_choch(df)
        mss = detect_mss(df)
        cisd = detect_cisd(df)
        swings = find_swing_highs_lows(df)
        
        # Volume analysis
        vol_avg20 = float(df["Volume"].tail(20).mean()) if "Volume" in df.columns else 0
        last_vol = float(df["Volume"].iloc[-1]) if "Volume" in df.columns else 0
        rvol = round(last_vol / vol_avg20, 2) if vol_avg20 > 0 else 0.0
        
        # Price levels
        last_close = float(df["Close"].iloc[-1])
        last_high = float(df["High"].iloc[-1])
        last_low = float(df["Low"].iloc[-1])
        last_open = float(df["Open"].iloc[-1])
        
        # Trend determination
        ema_trend = "BULLISH" if ema9.iloc[-1] > ema21.iloc[-1] > ema50.iloc[-1] else "BEARISH" if ema9.iloc[-1] < ema21.iloc[-1] < ema50.iloc[-1] else "NEUTRAL"
        
        rsi_val = float(rsi.iloc[-1])
        rsi_overbought = rsi_val > 70
        rsi_oversold = rsi_val < 30
        
        macd_bullish = macd_line.iloc[-1] > macd_sig.iloc[-1]
        candle_date, candle_close_time = _candle_signal_timestamp(df, is_daily=False, resolution=resolution)
        
        return {
            "timeframe": resolution,
            "status": "OK",
            "data": {
                "last_close": last_close,
                "last_high": last_high,
                "last_low": last_low,
                "last_open": last_open,
                "signal_candle_date": candle_date,
                "signal_candle_time": candle_close_time,
                "signal_generated_at": _generated_timestamp(),
                "structure_type": structure["type"],
                "structure_trend": structure["trend"],
                "structure_strength": round(structure.get("strength", 0), 1),
                "current_high": structure["current_high"],
                "current_low": structure["current_low"],
                "prev_high": structure["prev_high"],
                "prev_low": structure["prev_low"],
                "bullish_choch": choch["bullish_choch"],
                "bearish_choch": choch["bearish_choch"],
                "choch_price": choch["choch_price"],
                "choch_type": choch["choch_type"],
                "bullish_mss": mss["bullish_mss"],
                "bearish_mss": mss["bearish_mss"],
                "mss_type": mss["mss_type"],
                "bullish_cisd": cisd["bullish_cisd"],
                "bearish_cisd": cisd["bearish_cisd"],
                "cisd_type": cisd["cisd_type"],
                "cisd_price": cisd["cisd_price"],
                "swing_high": swings["swing_high"],
                "swing_low": swings["swing_low"],
                "swing_high_bars_ago": swings["swing_high_bars_ago"],
                "swing_low_bars_ago": swings["swing_low_bars_ago"],
                "vwap": float(vwap.iloc[-1]) if len(vwap) > 0 else None,
                "ema9": float(ema9.iloc[-1]) if len(ema9) > 0 else None,
                "ema21": float(ema21.iloc[-1]) if len(ema21) > 0 else None,
                "ema50": float(ema50.iloc[-1]) if len(ema50) > 0 else None,
                "ema200": float(ema200.iloc[-1]) if len(ema200) > 0 else None,
                "ema_trend": ema_trend,
                "rsi": round(rsi_val, 1),
                "rsi_overbought": rsi_overbought,
                "rsi_oversold": rsi_oversold,
                "macd_bullish": macd_bullish,
                "macd_value": round(float(macd_line.iloc[-1]), 4),
                "rvol": rvol,
                "atr": round(float(atr.iloc[-1]), 2),
                # Pressure indicators
                "buying_pressure": pressure["buying_pressure"],
                "selling_pressure": pressure["selling_pressure"],
                "pressure_trend": pressure["trend"],
                "buying_volume": pressure["buying_volume"],
                "selling_volume": pressure["selling_volume"],
                "pressure_ratio": pressure["pressure_ratio"],
            },
            "df": df,
        }
    
    except Exception as e:
        return {
            "timeframe": resolution,
            "status": "ERROR",
            "error": str(e),
            "data": None,
        }

# ════════════════════════════════════════════════════════════════════════════════
# OPTIONS CHAIN ANALYSIS (NEW)
# ════════════════════════════════════════════════════════════════════════════════
def _fyers_optionchain_request(fyers, symbol: str, strikecount: int = OPTIONS_STRIKE_COUNT, timestamp: str = "", greeks: bool = True):
    """Call FYERS v3 option-chain endpoint directly, with SDK fallback."""
    app_id = getattr(fyers, "client_id", None) or FYERS_APP_ID
    token = getattr(fyers, "token", None) or os.environ.get("FYERS_ACCESS_TOKEN", "")
    if app_id and token:
        headers = {"Authorization": f"{app_id}:{token}"}
        params = {"symbol": symbol, "strikecount": min(int(strikecount), 50), "greeks": "1" if greeks else "0"}
        if timestamp:
            params["timestamp"] = str(timestamp)
        r = requests.get("https://api-t1.fyers.in/data/options-chain-v3", headers=headers, params=params, timeout=OPTIONS_HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()
    fn = getattr(fyers, "option_chain", None) or getattr(fyers, "optionchain", None)
    if fn:
        try:
            return fn(symbol=symbol, strikecount=min(int(strikecount), 50), timestamp=timestamp, greeks=greeks)
        except TypeError:
            return fn(data={"symbol":symbol, "strikecount":min(int(strikecount),50), "timestamp":timestamp, "greeks":greeks})
    raise RuntimeError("FYERS option-chain API is not available")

def _calculate_max_pain(chain_rows):
    strikes = sorted({float(x.get("strike_price")) for x in chain_rows if x.get("option_type") in ("CE", "PE") and x.get("strike_price") not in (None, -1)})
    if not strikes:
        return None
    ce = {(float(x.get("strike_price")), float(x.get("oi",0) or 0)) for x in chain_rows if x.get("option_type") == "CE"}
    pe = {(float(x.get("strike_price")), float(x.get("oi",0) or 0)) for x in chain_rows if x.get("option_type") == "PE"}
    ce_map, pe_map = dict(ce), dict(pe)
    pains = {}
    for settle in strikes:
        pain = sum(max(settle-k,0)*oi for k,oi in ce_map.items()) + sum(max(k-settle,0)*oi for k,oi in pe_map.items())
        pains[settle] = pain
    return min(pains, key=pains.get) if pains else None

def fetch_options_chain_data(fyers, symbol: str, expiry_timestamp: str = "") -> Dict[str, Any]:
    """Fetch live FYERS v3 options chain; never fabricate missing data."""
    empty = {"status":"DATA_UNAVAILABLE", "message":"No live option-chain data", "expiry":None, "spot":None,
             "atm_strike":None, "ce_oi":None, "pe_oi":None, "ce_oi_change":None, "pe_oi_change":None,
             "pcr":None, "max_pain":None, "ce_volume":None, "pe_volume":None, "call_writing":False,
             "put_writing":False, "call_unwinding":False, "put_unwinding":False, "options_bias":"NEUTRAL", "chain":[], "expiry_data":[]}
    try:
        resp = _fyers_optionchain_request(fyers, symbol, timestamp=expiry_timestamp)
        if not isinstance(resp, dict) or resp.get("s") not in (None, "ok"):
            empty["message"] = str(resp.get("message", "Option-chain request failed")) if isinstance(resp, dict) else "Invalid response"
            return empty
        data = resp.get("data", resp)
        chain = data.get("optionsChain") or []
        expiry_data = data.get("expiryData") or []
        if not expiry_timestamp and expiry_data:
            nearest = expiry_data[0]
            expiry_timestamp = str(nearest.get("expiry", ""))
            if expiry_timestamp:
                resp = _fyers_optionchain_request(fyers, symbol, timestamp=expiry_timestamp)
                data = resp.get("data", resp) if isinstance(resp, dict) else {}
                chain = data.get("optionsChain") or chain
        spot_row = next((x for x in chain if x.get("option_type", "") == ""), None)
        spot = float(spot_row.get("ltp")) if spot_row and spot_row.get("ltp") is not None else None
        legs = [x for x in chain if x.get("option_type") in ("CE","PE") and x.get("strike_price") not in (None,-1)]
        strikes = sorted({float(x["strike_price"]) for x in legs})
        atm = min(strikes, key=lambda k: abs(k-spot)) if strikes and spot is not None else None
        call_oi = float(data.get("callOi", 0) or 0); put_oi = float(data.get("putOi", 0) or 0)
        call_chg = sum(float(x.get("oich",0) or 0) for x in legs if x.get("option_type")=="CE")
        put_chg = sum(float(x.get("oich",0) or 0) for x in legs if x.get("option_type")=="PE")
        ce_vol = sum(float(x.get("volume",0) or 0) for x in legs if x.get("option_type")=="CE")
        pe_vol = sum(float(x.get("volume",0) or 0) for x in legs if x.get("option_type")=="PE")
        near = [x for x in legs if atm is not None and abs(float(x["strike_price"])-atm) <= max((strikes[1]-strikes[0]) if len(strikes)>1 else 1, 1)*3]
        ce_writing = sum(1 for x in near if x.get("option_type")=="CE" and float(x.get("oich",0) or 0)>0 and float(x.get("ltpch",0) or 0)<0)
        pe_writing = sum(1 for x in near if x.get("option_type")=="PE" and float(x.get("oich",0) or 0)>0 and float(x.get("ltpch",0) or 0)<0)
        ce_unwind = sum(1 for x in near if x.get("option_type")=="CE" and float(x.get("oich",0) or 0)<0 and float(x.get("ltpch",0) or 0)>0)
        pe_unwind = sum(1 for x in near if x.get("option_type")=="PE" and float(x.get("oich",0) or 0)<0 and float(x.get("ltpch",0) or 0)>0)
        pcr = put_oi/call_oi if call_oi > 0 else None
        bull = (pcr is not None and pcr >= 1.05) or pe_writing > ce_writing
        bear = (pcr is not None and pcr <= 0.80) or ce_writing > pe_writing
        if bull and not bear: bias = "🟢 BULLISH"
        elif bear and not bull: bias = "🔴 BEARISH"
        else: bias = "🟡 NEUTRAL"
        return {"status":"OK", "message":"Live FYERS option chain", "expiry": expiry_timestamp or (expiry_data[0].get("date") if expiry_data else None),
                "spot":spot, "atm_strike":atm, "ce_oi":call_oi, "pe_oi":put_oi, "ce_oi_change":call_chg,
                "pe_oi_change":put_chg, "pcr":round(pcr,2) if pcr is not None else None,
                "max_pain":_calculate_max_pain(legs), "ce_volume":ce_vol, "pe_volume":pe_vol,
                "call_writing":ce_writing>0, "put_writing":pe_writing>0, "call_unwinding":ce_unwind>0,
                "put_unwinding":pe_unwind>0, "options_bias":bias, "chain":legs, "expiry_data":expiry_data}
    except Exception as e:
        empty["status"] = "ERROR"; empty["message"] = str(e)[:180]
        return empty

# ════════════════════════════════════════════════════════════════════════════════
# STRICT MULTI-TIMEFRAME SIGNAL VALIDATION ENGINE (FIXED)
# ════════════════════════════════════════════════════════════════════════════════
def calculate_master_signal(symbol: str, analysis_5m: Dict, analysis_15m: Dict, analysis_1h: Dict, options_data: Dict = None) -> Dict[str, Any]:
    """
    STRICT master signal validation engine.
    
    Prevents wrong BUY/SELL by enforcing:
    1. Multi-timeframe alignment (no conflicting timeframes)
    2. Pressure validation (direction must match)
    3. VWAP confirmation (price must be on correct side)
    4. EMA structure (must not contradict)
    5. Market structure validation (CHoCH/MSS/CISD support)
    6. Volume confirmation (RVOL preference)
    7. Next Candle Bias conflict detection (override if strongly opposed)
    8. Options validation for F&O
    
    Returns NEUTRAL/WAIT rather than forcing wrong signals.
    """
    if options_data is None:
        options_data = {"status":"DATA_UNAVAILABLE", "options_bias":"NEUTRAL"}
    
    # ════════════════════════════════════════════════════════════════════════════
    # EXTRACT DATA FROM EACH TIMEFRAME
    # ════════════════════════════════════════════════════════════════════════════
    data_5m = analysis_5m.get("data") if analysis_5m.get("status") == "OK" else None
    data_15m = analysis_15m.get("data") if analysis_15m.get("status") == "OK" else None
    data_1h = analysis_1h.get("data") if analysis_1h.get("status") == "OK" else None
    
    # ════════════════════════════════════════════════════════════════════════════
    # VALIDATION GATE 1: TIMEFRAME AVAILABILITY
    # ════════════════════════════════════════════════════════════════════════════
    if not data_5m or not data_15m or not data_1h:
        return {
            "final_signal": "🟡 NEUTRAL",
            "confidence": 0.0,
            "entry": None,
            "stop_loss": None,
            "target1": None,
            "target2": None,
            "rr_ratio": None,
            "scores": {"5m": 0, "15m": 0, "1h": 0, "pressure": 0, "options": 0},
            "signal_reason": "Insufficient timeframe data",
        }
    
    # ════════════════════════════════════════════════════════════════════════════
    # CLASSIFY EACH TIMEFRAME
    # ════════════════════════════════════════════════════════════════════════════
    def classify_tf_direction(data: Dict) -> str:
        """Return BULLISH, BEARISH, or NEUTRAL based on structure."""
        trend = data.get("structure_trend", "NEUTRAL")
        struct_type = data.get("structure_type", "UNKNOWN")
        
        if trend == "BULLISH":
            return "BULLISH"
        elif trend == "BEARISH":
            return "BEARISH"
        else:
            return "NEUTRAL"
    
    tf_5m = classify_tf_direction(data_5m)
    tf_15m = classify_tf_direction(data_15m)
    tf_1h = classify_tf_direction(data_1h)
    
    # ════════════════════════════════════════════════════════════════════════════
    # VALIDATION GATE 2: MULTI-TIMEFRAME ALIGNMENT
    # ════════════════════════════════════════════════════════════════════════════
    is_bullish_aligned = (
        (tf_5m == "BULLISH") and
        (tf_15m in ["BULLISH", "NEUTRAL"]) and
        (tf_1h != "BEARISH")
    )
    
    is_bearish_aligned = (
        (tf_5m == "BEARISH") and
        (tf_15m in ["BEARISH", "NEUTRAL"]) and
        (tf_1h != "BULLISH")
    )
    
    hard_conflict = (tf_5m == "BULLISH" and tf_1h == "BEARISH") or (tf_5m == "BEARISH" and tf_1h == "BULLISH")
    
    if hard_conflict or (not is_bullish_aligned and not is_bearish_aligned):
        return {
            "final_signal": "🟡 NEUTRAL",
            "confidence": 20.0 if hard_conflict else 35.0,
            "entry": None,
            "stop_loss": None,
            "target1": None,
            "target2": None,
            "rr_ratio": None,
            "scores": {
                "5m": 50,
                "15m": 50,
                "1h": 50,
                "pressure": 50,
                "options": 50,
            },
            "signal_reason": f"Timeframe conflict: 5M {tf_5m} vs 15M {tf_15m} vs 1H {tf_1h}",
        }
    
    # ════════════════════════════════════════════════════════════════════════════
    # VALIDATION GATE 3: PRESSURE CONFIRMATION
    # ════════════════════════════════════════════════════════════════════════════
    pressure_trend = data_5m.get("pressure_trend", "NEUTRAL")
    pressure_buy = pressure_trend in ["BUYING", "STRONG_BUYING"]
    pressure_sell = pressure_trend in ["SELLING", "STRONG_SELLING"]
    pressure_strong_buy = pressure_trend == "STRONG_BUYING"
    pressure_strong_sell = pressure_trend == "STRONG_SELLING"
    
    if is_bullish_aligned and not pressure_buy:
        is_bullish_aligned = False
    
    if is_bearish_aligned and not pressure_sell:
        is_bearish_aligned = False
    
    if not is_bullish_aligned and not is_bearish_aligned:
        return {
            "final_signal": "🟡 NEUTRAL",
            "confidence": 30.0,
            "entry": None,
            "stop_loss": None,
            "target1": None,
            "target2": None,
            "rr_ratio": None,
            "scores": {
                "5m": 50,
                "15m": 50,
                "1h": 50,
                "pressure": 50,
                "options": 50,
            },
            "signal_reason": f"Pressure conflict: {pressure_trend}",
        }
    
    # ════════════════════════════════════════════════════════════════════════════
    # VALIDATION GATE 4: VWAP CONFIRMATION
    # ════════════════════════════════════════════════════════════════════════════
    last_close = data_5m.get("last_close", 0)
    vwap = data_5m.get("vwap")
    
    if vwap is not None:
        price_above_vwap = last_close > vwap
        
        if is_bullish_aligned and not price_above_vwap:
            if not (data_5m.get("bullish_choch") or data_5m.get("bullish_mss")):
                is_bullish_aligned = False
        
        if is_bearish_aligned and price_above_vwap:
            if not (data_5m.get("bearish_choch") or data_5m.get("bearish_mss")):
                is_bearish_aligned = False
    
    if not is_bullish_aligned and not is_bearish_aligned:
        return {
            "final_signal": "🟡 NEUTRAL",
            "confidence": 30.0,
            "entry": None,
            "stop_loss": None,
            "target1": None,
            "target2": None,
            "rr_ratio": None,
            "scores": {
                "5m": 50,
                "15m": 50,
                "1h": 50,
                "pressure": 50,
                "options": 50,
            },
            "signal_reason": f"VWAP conflict: Price {last_close:.2f} vs VWAP {vwap:.2f if vwap else 'N/A'}",
        }
    
    # ════════════════════════════════════════════════════════════════════════════
    # VALIDATION GATE 5: EMA STRUCTURE
    # ════════════════════════════════════════════════════════════════════════════
    ema_trend = data_5m.get("ema_trend", "NEUTRAL")
    
    if is_bullish_aligned and ema_trend == "BEARISH":
        is_bullish_aligned = False
    
    if is_bearish_aligned and ema_trend == "BULLISH":
        is_bearish_aligned = False
    
    if not is_bullish_aligned and not is_bearish_aligned:
        return {
            "final_signal": "🟡 NEUTRAL",
            "confidence": 30.0,
            "entry": None,
            "stop_loss": None,
            "target1": None,
            "target2": None,
            "rr_ratio": None,
            "scores": {
                "5m": 50,
                "15m": 50,
                "1h": 50,
                "pressure": 50,
                "options": 50,
            },
            "signal_reason": f"EMA conflict: Trend {ema_trend} vs Structure {tf_5m}",
        }
    
    # ════════════════════════════════════════════════════════════════════════════
    # CONFIDENCE CALCULATION (Based on agreement of factors)
    # ════════════════════════════════════════════════════════════════════════════
    confirmation_count = 0
    total_factors = 0
    
    # Factor 1: 5M Structure
    if tf_5m == "BULLISH":
        confirmation_count += 1
    elif tf_5m == "BEARISH":
        confirmation_count -= 1
    total_factors += 1
    
    # Factor 2: 15M Structure
    if tf_15m == "BULLISH":
        confirmation_count += 1
    elif tf_15m == "BEARISH":
        confirmation_count -= 1
    total_factors += 1
    
    # Factor 3: 1H Structure
    if tf_1h == "BULLISH":
        confirmation_count += 1
    elif tf_1h == "BEARISH":
        confirmation_count -= 1
    total_factors += 1
    
    # Factor 4: Pressure
    if pressure_buy:
        confirmation_count += 1
    elif pressure_sell:
        confirmation_count -= 1
    total_factors += 1
    
    # Factor 5: VWAP
    if vwap is not None:
        if is_bullish_aligned and price_above_vwap:
            confirmation_count += 1
        elif is_bearish_aligned and not price_above_vwap:
            confirmation_count += 1
        total_factors += 1
    
    # Factor 6: EMA
    if ema_trend == "BULLISH":
        confirmation_count += 1
    elif ema_trend == "BEARISH":
        confirmation_count -= 1
    total_factors += 1
    
    # Factor 7: Market Structure (CHoCH/MSS/CISD)
    has_bullish_structure = (
        data_5m.get("bullish_choch") or 
        data_5m.get("bullish_mss") or 
        data_5m.get("bullish_cisd") or
        (data_5m.get("structure_type") in ["HH/HL", "HL"])
    )
    has_bearish_structure = (
        data_5m.get("bearish_choch") or 
        data_5m.get("bearish_mss") or 
        data_5m.get("bearish_cisd") or
        (data_5m.get("structure_type") in ["LH/LL", "LH"])
    )
    
    if is_bullish_aligned and has_bullish_structure:
        confirmation_count += 1
    elif is_bearish_aligned and has_bearish_structure:
        confirmation_count += 1
    total_factors += 1
    
    # Factor 8: Volume/RVOL
    rvol = data_5m.get("rvol", 1.0)
    if rvol >= 1.2:
        confirmation_count += 1
    elif rvol < 0.8:
        confirmation_count -= 1
    total_factors += 1
    
    # Calculate raw confidence (0-100)
    raw_confidence = (confirmation_count / total_factors) * 100 if total_factors > 0 else 0
    confidence = max(0, min(100, abs(raw_confidence)))
    
    # ════════════════════════════════════════════════════════════════════════════
    # VALIDATION GATE 6: NEXT CANDLE BIAS CONFLICT DETECTION
    # ════════════════════════════════════════════════════════════════════════════
    df_5m = analysis_5m.get("df")
    if df_5m is not None:
        next_bias = calculate_next_candle_bias(df_5m, analysis_5m)
    else:
        next_bias = {"bias": "NEUTRAL", "confidence": 0.0}
    
    next_bias_str = next_bias.get("bias", "NEUTRAL")
    next_bias_conf = next_bias.get("confidence", 0.0)
    
    # Check for strong conflict
    if is_bullish_aligned and "SELL" in next_bias_str and next_bias_conf >= 70:
        return {
            "final_signal": "🟡 NEUTRAL",
            "confidence": 40.0,
            "entry": None,
            "stop_loss": None,
            "target1": None,
            "target2": None,
            "rr_ratio": None,
            "scores": {
                "5m": 70,
                "15m": 60,
                "1h": 55,
                "pressure": 70 if pressure_strong_buy else 60,
                "options": 50,
            },
            "signal_reason": f"Next Candle Bias conflict: Bullish setup but Next Bias {next_bias_str}",
        }
    
    if is_bearish_aligned and "BUY" in next_bias_str and next_bias_conf >= 70:
        return {
            "final_signal": "🟡 NEUTRAL",
            "confidence": 40.0,
            "entry": None,
            "stop_loss": None,
            "target1": None,
            "target2": None,
            "rr_ratio": None,
            "scores": {
                "5m": 30,
                "15m": 40,
                "1h": 45,
                "pressure": 30 if pressure_strong_sell else 40,
                "options": 50,
            },
            "signal_reason": f"Next Candle Bias conflict: Bearish setup but Next Bias {next_bias_str}",
        }
    
    # ════════════════════════════════════════════════════════════════════════════
    # VALIDATION GATE 7: OPTIONS VALIDATION (F&O ONLY)
    # ════════════════════════════════════════════════════════════════════════════
    options_bias = options_data.get("options_bias", "NEUTRAL")
    options_conflict = False
    
    if is_bullish_aligned and "BEARISH" in str(options_bias):
        options_conflict = True
    elif is_bearish_aligned and "BULLISH" in str(options_bias):
        options_conflict = True
    
    if options_conflict:
        if not (has_bullish_structure or has_bearish_structure):
            confidence = min(confidence, 50.0)
    
    # ════════════════════════════════════════════════════════════════════════════
    # STRICT SIGNAL GENERATION THRESHOLDS
    # ════════════════════════════════════════════════════════════════════════════
    composite_score = 50
    
    if is_bullish_aligned:
        if tf_5m == "BULLISH" and tf_15m == "BULLISH":
            composite_score += 20
        elif tf_5m == "BULLISH":
            composite_score += 15
        
        if tf_1h == "BULLISH":
            composite_score += 5
        
        if pressure_strong_buy:
            composite_score += 15
        elif pressure_buy:
            composite_score += 10
        
        if vwap is not None and price_above_vwap:
            composite_score += 8
        
        if has_bullish_structure:
            composite_score += 10
        
        if rvol >= 1.5:
            composite_score += 5
        elif rvol < 1.2:
            composite_score -= 3
        
        if composite_score >= 80 and pressure_strong_buy and has_bullish_structure:
            final_signal = "🟢 STRONG BUY"
            confidence = min(100, confidence + 10)
        elif composite_score >= 70 and pressure_buy:
            final_signal = "🟢 BUY"
            confidence = min(95, confidence + 5)
        else:
            final_signal = "🟡 NEUTRAL"
            confidence = min(confidence, 60)
    
    elif is_bearish_aligned:
        if tf_5m == "BEARISH" and tf_15m == "BEARISH":
            composite_score -= 20
        elif tf_5m == "BEARISH":
            composite_score -= 15
        
        if tf_1h == "BEARISH":
            composite_score -= 5
        
        if pressure_strong_sell:
            composite_score -= 15
        elif pressure_sell:
            composite_score -= 10
        
        if vwap is not None and not price_above_vwap:
            composite_score -= 8
        
        if has_bearish_structure:
            composite_score -= 10
        
        if rvol >= 1.5:
            composite_score -= 5
        elif rvol < 1.2:
            composite_score += 3
        
        if composite_score <= 20 and pressure_strong_sell and has_bearish_structure:
            final_signal = "🔴 STRONG SELL"
            confidence = min(100, confidence + 10)
        elif composite_score <= 30 and pressure_sell:
            final_signal = "🔴 SELL"
            confidence = min(95, confidence + 5)
        else:
            final_signal = "🟡 NEUTRAL"
            confidence = min(confidence, 60)
    
    else:
        final_signal = "🟡 NEUTRAL"
        confidence = 35.0
    
    # ════════════════════════════════════════════════════════════════════════════
    # BUILD SIGNAL REASON
    # ════════════════════════════════════════════════════════════════════════════
    reason_parts = []
    
    if "BUY" in final_signal:
        reason_parts.append(f"5M {tf_5m}")
        reason_parts.append(f"15M {tf_15m}")
        reason_parts.append(f"1H {tf_1h}")
        if pressure_buy:
            reason_parts.append(f"Buy Pressure {data_5m.get('buying_pressure', 'N/A')}%")
        if vwap is not None and price_above_vwap:
            reason_parts.append("Above VWAP")
        if ema_trend == "BULLISH":
            reason_parts.append("EMA Bullish")
        if has_bullish_structure:
            reason_parts.append(f"{data_5m.get('structure_type', 'Structure')} Bullish")
        if rvol >= 1.5:
            reason_parts.append(f"RVOL {rvol}x")
    
    elif "SELL" in final_signal:
        reason_parts.append(f"5M {tf_5m}")
        reason_parts.append(f"15M {tf_15m}")
        reason_parts.append(f"1H {tf_1h}")
        if pressure_sell:
            reason_parts.append(f"Sell Pressure {data_5m.get('selling_pressure', 'N/A')}%")
        if vwap is not None and not price_above_vwap:
            reason_parts.append("Below VWAP")
        if ema_trend == "BEARISH":
            reason_parts.append("EMA Bearish")
        if has_bearish_structure:
            reason_parts.append(f"{data_5m.get('structure_type', 'Structure')} Bearish")
        if rvol >= 1.5:
            reason_parts.append(f"RVOL {rvol}x")
    
    else:
        reason_parts = ["Insufficient confirmation", f"TF: 5M {tf_5m} 15M {tf_15m} 1H {tf_1h}", f"Pressure: {pressure_trend}"]
    
    signal_reason = " + ".join(reason_parts)
    
    # ════════════════════════════════════════════════════════════════════════════
    # CALCULATE ENTRY, SL, TARGETS ONLY FOR VALID SIGNALS
    # ════════════════════════════════════════════════════════════════════════════
    entry = sl = t1 = t2 = rr_ratio = None
    
    if "BUY" in final_signal or "SELL" in final_signal:
        entry = round(last_close, 2)
        atr_5m = data_5m.get("atr", 0)
        swing_high = data_5m.get("swing_high")
        swing_low = data_5m.get("swing_low")
        
        if "BUY" in final_signal:
            sl = round(swing_low - atr_5m * 0.5, 2) if swing_low else round(entry - atr_5m * 2, 2)
            t1 = round(entry + atr_5m * 1.5, 2)
            t2 = round(entry + atr_5m * 2.5, 2)
        else:
            sl = round(swing_high + atr_5m * 0.5, 2) if swing_high else round(entry + atr_5m * 2, 2)
            t1 = round(entry - atr_5m * 1.5, 2)
            t2 = round(entry - atr_5m * 2.5, 2)
        
        risk = abs(entry - sl)
        reward = abs(t1 - entry)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0
    
    return {
        "final_signal": final_signal,
        "confidence": confidence,
        "entry": entry,
        "stop_loss": sl,
        "target1": t1,
        "target2": t2,
        "rr_ratio": rr_ratio,
        "scores": {
            "5m": 70 if tf_5m == "BULLISH" else 30 if tf_5m == "BEARISH" else 50,
            "15m": 70 if tf_15m == "BULLISH" else 30 if tf_15m == "BEARISH" else 50,
            "1h": 70 if tf_1h == "BULLISH" else 30 if tf_1h == "BEARISH" else 50,
            "pressure": 70 if pressure_strong_buy else 60 if pressure_buy else 30 if pressure_strong_sell else 40 if pressure_sell else 50,
            "options": 75 if "BULLISH" in str(options_bias) else 25 if "BEARISH" in str(options_bias) else 50,
        },
        "signal_reason": signal_reason,
    }

# ════════════════════════════════════════════════════════════════════════════════
# NORMALIZED SIGNAL CLASSIFICATION (for filters)
# ════════════════════════════════════════════════════════════════════════════════
def normalize_signal(signal_str: str) -> str:
    """Return normalized signal class: BUY, SELL, or NEUTRAL."""
    if pd.isna(signal_str) or signal_str is None:
        return "NEUTRAL"
    sig = str(signal_str).upper()
    if "BUY" in sig and "SELL" not in sig:
        return "BUY"
    elif "SELL" in sig:
        return "SELL"
    else:
        return "NEUTRAL"

# ════════════════════════════════════════════════════════════════════════════════
# V17 NEW FEATURES: GOLDEN CROSS / DEATH CROSS DETECTION (DAILY)
# ════════════════════════════════════════════════════════════════════════════════
def detect_golden_death_cross(fyers, symbol: str) -> Dict[str, Any]:
    """
    Detect Golden Cross (EMA50 crosses above EMA200) and Death Cross (EMA50 crosses below EMA200).
    Uses DAILY closed candles only.
    
    Returns: {
        "golden_cross": bool,
        "death_cross": bool,
        "ema50": float,
        "ema200": float,
        "ema_trend": str,  # BULLISH/BEARISH/NEUTRAL
        "signal": str,  # Golden Cross / Death Cross / NONE
        "signal_date": str,
        "ltp": float
    }
    """
    empty = {
        "golden_cross": False,
        "death_cross": False,
        "ema50": None,
        "ema200": None,
        "ema_trend": "NEUTRAL",
        "signal": "NONE",
        "signal_date": "N/A",
        "ltp": None,
        "reason": "DATA_UNAVAILABLE"
    }
    
    try:
        # Fetch daily data (last 100 days for accurate EMA)
        date_from = (datetime.today() - timedelta(days=100)).strftime("%Y-%m-%d")
        date_to = datetime.today().strftime("%Y-%m-%d")
        
        resp, err = _safe_history(fyers, {
            "symbol": symbol,
            "resolution": "1D",
            "date_format": "1",
            "range_from": date_from,
            "range_to": date_to,
            "cont_flag": "1",
        })
        
        if err or not resp:
            empty["reason"] = "FETCH_ERROR"
            return empty
        
        candles = resp.get("candles")
        if not candles or len(candles) < 50:
            empty["reason"] = "INSUFFICIENT_DATA"
            return empty
        
        # Build dataframe
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Close"]).sort_values("Time").reset_index(drop=True)
        
        if len(df) < 50:
            empty["reason"] = "INSUFFICIENT_DATA"
            return empty
        
        # Calculate EMAs
        ema50 = calculate_ema(df["Close"], 50)
        ema200 = calculate_ema(df["Close"], 200)
        
        # Get last two values for crossover detection
        if len(ema50) < 2 or len(ema200) < 2:
            empty["reason"] = "EMA_CALCULATION_ERROR"
            return empty
        
        # Previous candle (index -2)
        prev_ema50 = float(ema50.iloc[-2])
        prev_ema200 = float(ema200.iloc[-2])
        
        # Current candle (index -1) - ONLY CLOSED CANDLES
        curr_ema50 = float(ema50.iloc[-1])
        curr_ema200 = float(ema200.iloc[-1])
        
        # Current price
        ltp = float(df["Close"].iloc[-1])
        signal_date = df["Time"].iloc[-1].strftime("%d-%b-%Y")
        
        # Determine EMA trend
        if curr_ema50 > curr_ema200:
            ema_trend = "BULLISH"
        elif curr_ema50 < curr_ema200:
            ema_trend = "BEARISH"
        else:
            ema_trend = "NEUTRAL"
        
        # Detect Golden Cross (EMA50 crosses above EMA200)
        golden_cross = (prev_ema50 <= prev_ema200) and (curr_ema50 > curr_ema200)
        
        # Detect Death Cross (EMA50 crosses below EMA200)
        death_cross = (prev_ema50 >= prev_ema200) and (curr_ema50 < curr_ema200)
        
        # Determine signal
        if golden_cross:
            signal = "🟢 GOLDEN CROSS"
        elif death_cross:
            signal = "🔴 DEATH CROSS"
        else:
            signal = "NONE"
        
        return {
            "golden_cross": golden_cross,
            "death_cross": death_cross,
            "ema50": round(curr_ema50, 2),
            "ema200": round(curr_ema200, 2),
            "ema_trend": ema_trend,
            "signal": signal,
            "signal_date": signal_date,
            "ltp": round(ltp, 2),
            "reason": "OK"
        }
    
    except Exception as e:
        empty["reason"] = f"ERROR: {str(e)}"
        return empty

# ════════════════════════════════════════════════════════════════════════════════
# V17 SIGNAL EXPLANATION GENERATOR
# ════════════════════════════════════════════════════════════════════════════════
def generate_signal_explanation(signal_data: Dict[str, Any], data_5m: Dict[str, Any]) -> str:
    """Generate human-readable explanation for WHY/SELL/NEUTRAL."""
    signal = signal_data.get("final_signal", "NEUTRAL")
    confidence = signal_data.get("confidence", 0)
    reason = signal_data.get("signal_reason", "")
    
    explanation = f"**{signal}** — {confidence:.0f}% Confidence\n\n"
    explanation += f"**Reason:** {reason}\n\n"
    
    # Add detailed factors
    explanation += "**Factors:**\n"
    
    if data_5m:
        # Pressure
        bp = data_5m.get("buying_pressure", "N/A")
        sp = data_5m.get("selling_pressure", "N/A")
        explanation += f"• Buying Pressure: {bp}%\n"
        explanation += f"• Selling Pressure: {sp}%\n"
        
        # Structure
        struct_type = data_5m.get("structure_type", "N/A")
        struct_trend = data_5m.get("structure_trend", "N/A")
        explanation += f"• Market Structure: {struct_type} ({struct_trend})\n"
        
        # Price & VWAP
        price = data_5m.get("last_close", "N/A")
        vwap = data_5m.get("vwap", "N/A")
        if vwap != "N/A":
            position = "Above" if price > vwap else "Below"
            explanation += f"• Price {position} VWAP ({price:.2f} vs {vwap:.2f})\n"
        
        # EMA
        ema_trend = data_5m.get("ema_trend", "N/A")
        explanation += f"• EMA Trend: {ema_trend}\n"
        
        # RSI
        rsi = data_5m.get("rsi", "N/A")
        explanation += f"• RSI: {rsi}\n"
        
        # Structure signals
        if data_5m.get("bullish_choch"):
            explanation += "• ✓ Bullish CHoCH confirmed\n"
        elif data_5m.get("bearish_choch"):
            explanation += "• ✗ Bearish CHoCH confirmed\n"
        
        if data_5m.get("bullish_mss"):
            explanation += "• ✓ Bullish MSS confirmed\n"
        elif data_5m.get("bearish_mss"):
            explanation += "• ✗ Bearish MSS confirmed\n"
        
        # Volume
        rvol = data_5m.get("rvol", "N/A")
        explanation += f"• Volume: {rvol}x average\n"
    
    return explanation

# ════════════════════════════════════════════════════════════════════════════════
# SCAN STATISTICS & MARKET DASHBOARD FUNCTIONS
# ════════════════════════════════════════════════════════════════════════════════
def calculate_market_stats(results_df: pd.DataFrame) -> Dict[str, Any]:
    """Calculate market statistics from scan results."""
    if results_df is None or len(results_df) == 0:
        return {
            "total": 0,
            "buy": 0,
            "sell": 0,
            "neutral": 0,
            "strong_buy": 0,
            "strong_sell": 0,
            "avg_confidence": 0,
            "buy_pct": 0,
            "sell_pct": 0,
            "neutral_pct": 0,
        }
    
    try:
        total = len(results_df)
        
        # Normalize signals
        normalized = results_df.get("AI SIGNAL", pd.Series([])).apply(normalize_signal)
        buy_count = len(normalized[normalized == "BUY"])
        sell_count = len(normalized[normalized == "SELL"])
        neutral_count = len(normalized[normalized == "NEUTRAL"])
        
        # Strong signals
        strong_buy = len(results_df[results_df.get("AI SIGNAL", pd.Series([])).astype(str).str.contains("STRONG BUY", na=False)])
        strong_sell = len(results_df[results_df.get("AI SIGNAL", pd.Series([])).astype(str).str.contains("STRONG SELL", na=False)])
        
        # Average confidence
        try:
            avg_conf = pd.to_numeric(results_df.get("AI CONFIDENCE %", pd.Series([])), errors='coerce').mean()
            avg_conf = 0 if pd.isna(avg_conf) else avg_conf
        except:
            avg_conf = 0
        
        # Percentages
        buy_pct = (buy_count / total * 100) if total > 0 else 0
        sell_pct = (sell_count / total * 100) if total > 0 else 0
        neutral_pct = (neutral_count / total * 100) if total > 0 else 0
        
        return {
            "total": total,
            "buy": buy_count,
            "sell": sell_count,
            "neutral": neutral_count,
            "strong_buy": strong_buy,
            "strong_sell": strong_sell,
            "avg_confidence": round(avg_conf, 1),
            "buy_pct": round(buy_pct, 1),
            "sell_pct": round(sell_pct, 1),
            "neutral_pct": round(neutral_pct, 1),
        }
    
    except Exception as e:
        logger.error(f"Error calculating market stats: {e}")
        return {
            "total": 0,
            "buy": 0,
            "sell": 0,
            "neutral": 0,
            "strong_buy": 0,
            "strong_sell": 0,
            "avg_confidence": 0,
            "buy_pct": 0,
            "sell_pct": 0,
            "neutral_pct": 0,
        }

# ════════════════════════════════════════════════════════════════════════════════
# SAFE EXCEL EXPORT WITH FORMATTING
# ════════════════════════════════════════════════════════════════════════════════
def _safe_convert_df(df: pd.DataFrame) -> pd.DataFrame:
    """Convert all values to safe types for Excel export."""
    df_safe = df.copy()
    for col in df_safe.columns:
        try:
            df_safe[col] = df_safe[col].fillna("N/A")
            if df_safe[col].dtype in ['float64', 'float32']:
                df_safe[col] = df_safe[col].replace([np.inf, -np.inf], "N/A")
            df_safe[col] = df_safe[col].astype(str)
        except Exception:
            pass
    return df_safe

def _format_excel_output(df, scanner_type: str = "NSE") -> bytes:
    """Create professionally formatted Excel with colors and styling."""
    buf = io.BytesIO()
    
    try:
        df_export = _safe_convert_df(df)
        
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df_export.to_excel(writer, index=False, sheet_name="Signals")
            
            workbook = writer.book
            worksheet = writer.sheets["Signals"]
            
            if OPENPYXL_AVAILABLE:
                try:
                    green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
                    red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
                    yellow_fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
                    header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
                    header_font = Font(bold=True, color="FFFFFF", size=11)
                    center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
                    
                    for col_num in range(1, len(df_export.columns) + 1):
                        cell = worksheet.cell(row=1, column=col_num)
                        cell.fill = header_fill
                        cell.font = header_font
                        cell.alignment = center_align
                    
                    for row_num in range(2, len(df_export) + 2):
                        for col_num in range(1, len(df_export.columns) + 1):
                            cell = worksheet.cell(row=row_num, column=col_num)
                            cell.alignment = Alignment(horizontal="center", vertical="center")
                            col_title = df_export.columns[col_num - 1]
                            
                            try:
                                cell_value = str(cell.value)
                                
                                if col_title == "AI SIGNAL":
                                    if "BUY" in cell_value and "SELL" not in cell_value:
                                        cell.fill = green_fill
                                    elif "SELL" in cell_value:
                                        cell.fill = red_fill
                                    else:
                                        cell.fill = yellow_fill
                                
                                elif col_title == "AI CONFIDENCE %":
                                    try:
                                        conf_val = float(cell_value)
                                        if conf_val >= 70:
                                            cell.fill = green_fill
                                        elif conf_val >= 50:
                                            cell.fill = yellow_fill
                                        else:
                                            cell.fill = red_fill
                                    except:
                                        pass
                            except Exception:
                                pass
                    
                    worksheet.freeze_panes = "A2"
                    
                    for col_num, col_title in enumerate(df_export.columns, 1):
                        max_length = len(str(col_title)) + 2
                        adjusted_width = min(max_length + 2, 60)
                        col_letter = get_column_letter(col_num)
                        worksheet.column_dimensions[col_letter].width = adjusted_width
                
                except Exception as format_err:
                    logging.warning(f"Excel formatting error: {format_err}")
        
        try:
            summary_data = {
                "Metric": ["Total Stocks", "BUY Signals", "SELL Signals", "NEUTRAL Signals", "Avg Confidence %"],
                "Value": [
                    str(len(df)),
                    str(len(df[df["AI SIGNAL"].apply(lambda x: normalize_signal(x)) == "BUY"])),
                    str(len(df[df["AI SIGNAL"].apply(lambda x: normalize_signal(x)) == "SELL"])),
                    str(len(df[df["AI SIGNAL"].apply(lambda x: normalize_signal(x)) == "NEUTRAL"])),
                    f"{pd.to_numeric(df.get('AI CONFIDENCE %', pd.Series([])), errors='coerce').mean():.1f}%"
                ]
            }
            summary_df = pd.DataFrame(summary_data)
            summary_df.to_excel(writer, index=False, sheet_name="Summary")
        except Exception:
            pass
    
    except Exception as e:
        logging.error(f"Excel export error: {e}")
        try:
            buf = io.BytesIO()
            df_export = _safe_convert_df(df)
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                df_export.to_excel(writer, index=False, sheet_name="Signals")
        except Exception as final_err:
            logging.error(f"Final Excel fallback failed: {final_err}")
            buf = io.BytesIO()
            buf.write(to_csv_bytes(df))
    
    buf.seek(0)
    return buf.getvalue()

def to_csv_bytes(df) -> bytes:
    """Convert DataFrame to CSV bytes safely."""
    try:
        df_safe = _safe_convert_df(df)
        return df_safe.to_csv(index=False).encode("utf-8")
    except Exception as e:
        logging.error(f"CSV export error: {e}")
        return b"Error exporting to CSV"

def to_json_bytes(df) -> bytes:
    """Convert DataFrame to JSON bytes safely."""
    try:
        df_safe = _safe_convert_df(df)
        return df_safe.to_json(orient="records", indent=2, force_ascii=False).encode("utf-8")
    except Exception as e:
        logging.error(f"JSON export error: {e}")
        return b'{"error": "Could not export to JSON"}'

# ════════════════════════════════════════════════════════════════════════════════
# NSE SIGNAL SCANNER WORKER
# ════════════════════════════════════════════════════════════════════════════════
def _fetch_nse_signal(fyers, symbol: str):
    """Worker for NSE stocks scanning - NO options data."""
    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "") if isinstance(symbol, str) else str(symbol)
    
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid format"
    
    try:
        # Fetch all timeframes
        analysis_5m = analyze_timeframe(fyers, symbol, "5")
        analysis_15m = analyze_timeframe(fyers, symbol, "15")
        analysis_1h = analyze_timeframe(fyers, symbol, "60")
        
        if all(a.get("status") != "OK" for a in [analysis_5m, analysis_15m, analysis_1h]):
            return None, None
        
        # Calculate master signal
        master = calculate_master_signal(symbol, analysis_5m, analysis_15m, analysis_1h)
        
        # Calculate Next Candle Bias
        if analysis_5m.get("status") == "OK" and analysis_5m.get("data"):
            next_bias = calculate_next_candle_bias(analysis_5m.get("df"), analysis_5m)
        else:
            next_bias = {"bias": "NEUTRAL", "confidence": 0.0}
        
        # Get LTP
        ltp = None
        for analysis in [analysis_5m, analysis_15m, analysis_1h]:
            if analysis.get("status") == "OK" and analysis.get("data"):
                ltp = analysis["data"]["last_close"]
                break
        
        if ltp is None:
            return None, None
        
        # Build result
        data_5m = analysis_5m.get("data") if analysis_5m.get("status") == "OK" else {}
        data_15m = analysis_15m.get("data") if analysis_15m.get("status") == "OK" else {}
        data_1h = analysis_1h.get("data") if analysis_1h.get("status") == "OK" else {}
        
        return {
            "Symbol": stock_ticker,
            "LTP": round(float(ltp), 2),
            "Trend": data_5m.get("structure_trend", "N/A"),
            "5M Trend": data_5m.get("structure_trend", "N/A"),
            "15M Trend": data_15m.get("structure_trend", "N/A"),
            "1H Trend": data_1h.get("structure_trend", "N/A"),
            "5M Structure": data_5m.get("structure_type", "N/A"),
            "15M Structure": data_15m.get("structure_type", "N/A"),
            "1H Structure": data_1h.get("structure_type", "N/A"),
            "5M BOS": "✅" if data_5m.get("bullish_choch") else "❌" if data_5m.get("bearish_choch") else "−",
            "15M BOS": "✅" if data_15m.get("bullish_choch") else "❌" if data_15m.get("bearish_choch") else "−",
            "1H BOS": "✅" if data_1h.get("bullish_choch") else "❌" if data_1h.get("bearish_choch") else "−",
            "5M CHoCH": "✅" if data_5m.get("bullish_choch") else "❌" if data_5m.get("bearish_choch") else "−",
            "15M CHoCH": "✅" if data_15m.get("bullish_choch") else "❌" if data_15m.get("bearish_choch") else "−",
            "1H CHoCH": "✅" if data_1h.get("bullish_choch") else "❌" if data_1h.get("bearish_choch") else "−",
            "5M MSS": "✅" if data_5m.get("bullish_mss") else "❌" if data_5m.get("bearish_mss") else "−",
            "15M MSS": "✅" if data_15m.get("bullish_mss") else "❌" if data_15m.get("bearish_mss") else "−",
            "1H MSS": "✅" if data_1h.get("bullish_mss") else "❌" if data_1h.get("bearish_mss") else "−",
            "5M CISD": "✅" if data_5m.get("bullish_cisd") else "❌" if data_5m.get("bearish_cisd") else "−",
            "VWAP": round(data_5m.get("vwap", 0), 2) if data_5m.get("vwap") else "N/A",
            "RSI": round(data_5m.get("rsi", 50), 1),
            "MACD": "🟢" if data_5m.get("macd_bullish") else "🔴",
            "ATR": data_5m.get("atr", "N/A"),
            "Volume": data_5m.get("rvol", "N/A"),
            "RVOL": data_5m.get("rvol", 0),
            "🟢 BUY PRESSURE %": data_5m.get("buying_pressure", "N/A"),
            "🔴 SELL PRESSURE %": data_5m.get("selling_pressure", "N/A"),
            "PRESSURE SIGNAL": data_5m.get("pressure_trend", "N/A"),
            "NEXT CANDLE BIAS": next_bias.get("bias", "NEUTRAL"),
            "NEXT CANDLE CONFIDENCE %": next_bias.get("confidence", 0.0),
            "AI SIGNAL": master["final_signal"],
            "AI CONFIDENCE %": master["confidence"],
            "SIGNAL REASON": master["signal_reason"],
            "ENTRY": master["entry"],
            "STOP LOSS": master["stop_loss"],
            "TARGET 1": master["target1"],
            "TARGET 2": master["target2"],
            "RISK:REWARD": master["rr_ratio"],
        }, None
    
    except Exception as e:
        return None, f"{symbol}: error ({type(e).__name__})"

# ════════════════════════════════════════════════════════════════════════════════
# F&O SIGNAL SCANNER WORKER
# ════════════════════════════════════════════════════════════════════════════════
def _fetch_fo_signal(fyers, symbol: str):
    """Worker for F&O stocks scanning - WITH options data."""
    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "") if isinstance(symbol, str) else str(symbol)
    
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid format"
    
    try:
        # Fetch all timeframes
        analysis_5m = analyze_timeframe(fyers, symbol, "5")
        analysis_15m = analyze_timeframe(fyers, symbol, "15")
        analysis_1h = analyze_timeframe(fyers, symbol, "60")
        
        if all(a.get("status") != "OK" for a in [analysis_5m, analysis_15m, analysis_1h]):
            return None, None
        
        # Fetch options data for F&O
        options_data = fetch_options_chain_data(fyers, symbol)
        
        # Calculate master signal
        master = calculate_master_signal(symbol, analysis_5m, analysis_15m, analysis_1h, options_data)
        
        # Calculate Next Candle Bias
        if analysis_5m.get("status") == "OK" and analysis_5m.get("data"):
            next_bias = calculate_next_candle_bias(analysis_5m.get("df"), analysis_5m)
        else:
            next_bias = {"bias": "NEUTRAL", "confidence": 0.0}
        
        # Get LTP
        ltp = None
        for analysis in [analysis_5m, analysis_15m, analysis_1h]:
            if analysis.get("status") == "OK" and analysis.get("data"):
                ltp = analysis["data"]["last_close"]
                break
        
        if ltp is None:
            return None, None
        
        # Build result
        data_5m = analysis_5m.get("data") if analysis_5m.get("status") == "OK" else {}
        data_15m = analysis_15m.get("data") if analysis_15m.get("status") == "OK" else {}
        data_1h = analysis_1h.get("data") if analysis_1h.get("status") == "OK" else {}
        
        return {
            "Symbol": stock_ticker,
            "LTP": round(float(ltp), 2),
            "Trend": data_5m.get("structure_trend", "N/A"),
            "5M Trend": data_5m.get("structure_trend", "N/A"),
            "15M Trend": data_15m.get("structure_trend", "N/A"),
            "1H Trend": data_1h.get("structure_trend", "N/A"),
            "5M Structure": data_5m.get("structure_type", "N/A"),
            "15M Structure": data_15m.get("structure_type", "N/A"),
            "1H Structure": data_1h.get("structure_type", "N/A"),
            "5M BOS": "✅" if data_5m.get("bullish_choch") else "❌" if data_5m.get("bearish_choch") else "−",
            "15M BOS": "✅" if data_15m.get("bullish_choch") else "❌" if data_15m.get("bearish_choch") else "−",
            "1H BOS": "✅" if data_1h.get("bullish_choch") else "❌" if data_1h.get("bearish_choch") else "−",
            "5M CHoCH": "✅" if data_5m.get("bullish_choch") else "❌" if data_5m.get("bearish_choch") else "−",
            "15M CHoCH": "✅" if data_15m.get("bullish_choch") else "❌" if data_15m.get("bearish_choch") else "−",
            "1H CHoCH": "✅" if data_1h.get("bullish_choch") else "❌" if data_1h.get("bearish_choch") else "−",
            "5M MSS": "✅" if data_5m.get("bullish_mss") else "❌" if data_5m.get("bearish_mss") else "−",
            "15M MSS": "✅" if data_15m.get("bullish_mss") else "❌" if data_15m.get("bearish_mss") else "−",
            "1H MSS": "✅" if data_1h.get("bullish_mss") else "❌" if data_1h.get("bearish_mss") else "−",
            "5M CISD": "✅" if data_5m.get("bullish_cisd") else "❌" if data_5m.get("bearish_cisd") else "−",
            "VWAP": round(data_5m.get("vwap", 0), 2) if data_5m.get("vwap") else "N/A",
            "RSI": round(data_5m.get("rsi", 50), 1),
            "MACD": "🟢" if data_5m.get("macd_bullish") else "🔴",
            "ATR": data_5m.get("atr", "N/A"),
            "Volume": data_5m.get("rvol", "N/A"),
            "RVOL": data_5m.get("rvol", 0),
            "🟢 BUY PRESSURE %": data_5m.get("buying_pressure", "N/A"),
            "🔴 SELL PRESSURE %": data_5m.get("selling_pressure", "N/A"),
            "PRESSURE SIGNAL": data_5m.get("pressure_trend", "N/A"),
            "NEXT CANDLE BIAS": next_bias.get("bias", "NEUTRAL"),
            "NEXT CANDLE CONFIDENCE %": next_bias.get("confidence", 0.0),
            "ATM STRIKE": round(options_data.get("atm_strike", 0), 2) if options_data.get("atm_strike") else "N/A",
            "PCR": options_data.get("pcr", "N/A"),
            "CE OI": options_data.get("ce_oi", "N/A"),
            "PE OI": options_data.get("pe_oi", "N/A"),
            "CE OI CHANGE": options_data.get("ce_oi_change", "N/A"),
            "PE OI CHANGE": options_data.get("pe_oi_change", "N/A"),
            "OPTIONS BIAS": options_data.get("options_bias", "N/A"),
            "AI SIGNAL": master["final_signal"],
            "AI CONFIDENCE %": master["confidence"],
            "SIGNAL REASON": master["signal_reason"],
            "ENTRY": master["entry"],
            "STOP LOSS": master["stop_loss"],
            "TARGET 1": master["target1"],
            "TARGET 2": master["target2"],
            "RISK:REWARD": master["rr_ratio"],
        }, None
    
    except Exception as e:
        return None, f"{symbol}: error ({type(e).__name__})"

# ════════════════════════════════════════════════════════════════════════════════
# THREADED SCAN FUNCTIONS
# ════════════════════════════════════════════════════════════════════════════════
def run_nse_scan(fyers, symbols):
    """Threaded scan for NSE stocks."""
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning NSE Stocks 0 / {len(symbols)}")
    done = 0
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_nse_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error"
                
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / max(len(symbols), 1), text=f"Scanning NSE {done} / {len(symbols)}")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress.empty()
    gc.collect()
    return results, errors, stats

def run_fo_scan(fyers, symbols):
    """Threaded scan for F&O stocks."""
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning F&O Stocks 0 / {len(symbols)}")
    done = 0
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_fo_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error"
                
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / max(len(symbols), 1), text=f"Scanning F&O {done} / {len(symbols)}")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress.empty()
    gc.collect()
    return results, errors, stats

# ════════════════════════════════════════════════════════════════════════════════
# MAIN APP - V17 WITH ALL NEW TABS
# ════════════════════════════════════════════════════════════════════════════════
def show_scanner(fyers) -> None:
    """Streamlit main app - NSE AI PRO V17 with Complete Feature Set"""
    
    try:
        st.set_page_config(page_title="NSE AI PRO V17", layout="wide")
    except:
        pass
    
    st.title("🚀 NSE AI PRO V17 — Professional Intraday + Swing Scanner")
    st.caption(f"🕒 Current Time (IST): {_now_ist().strftime('%d-%b-%Y %H:%M:%S')} IST | Multi-Timeframe Analysis + Golden Cross Detection")
    
    # Load symbols
    try:
        all_symbols = load_nse_equity_symbols()
        fo_symbols = load_fo_stocks()
    except Exception as e:
        st.error(f"❌ Error loading symbols: {e}")
        logger.error(f"Symbol loading error: {e}")
        return
    
    if not all_symbols:
        st.error("❌ No symbols loaded — check FYERS API access.")
        return
    
    st.caption(f"📊 NSE Equities: {len(all_symbols)} | 📈 F&O Stocks: {len(fo_symbols)}")
    
    # Create tabs
    tabs = st.tabs([
        "🇮🇳 NSE STOCKS",
        "📊 F&O STOCKS",
        "⚡ LIVE INTRADAY",
        "🔥 STRONG SIGNALS",
        "📈 SWING (GOLDEN/DEATH CROSS)",
        "🧠 ADDITIONAL ANALYSIS",
        "📊 MARKET DASHBOARD",
        "⚙️ SETTINGS"
    ])
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 0: NSE STOCKS
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[0]:
        st.markdown("### NSE Equity Stocks Scanner\n✅ Strict validation - only high-quality signals")
        
        col1, col2 = st.columns([3, 1])
        with col1:
            nse_limit = st.number_input("Scan limit (0=all)", min_value=0, max_value=len(all_symbols),
                                       value=min(500, len(all_symbols)), step=50, key="nse_limit")
        with col2:
            st.metric("Available", len(all_symbols))
        
        nse_universe = all_symbols if nse_limit == 0 else all_symbols[:nse_limit]
        
        if st.button(f"🔍 SCAN NSE ({len(nse_universe)} stocks)", key="nse_run"):
            with st.spinner("Analyzing NSE stocks…"):
                nse_results, nse_errors, nse_stats = run_nse_scan(fyers, nse_universe)
                st.session_state["nse_df"] = pd.DataFrame(nse_results) if nse_results else pd.DataFrame()
                st.session_state["nse_errors"] = nse_errors
                st.session_state["nse_stats"] = nse_stats
        
        # Display NSE results
        if "nse_stats" in st.session_state:
            _display_scan_summary(st.session_state["nse_stats"])
            
            if st.session_state.get("nse_errors"):
                with st.expander(f"⚠️ Errors ({len(st.session_state['nse_errors'])})", expanded=False):
                    for i, err in enumerate(st.session_state["nse_errors"][:10], 1):
                        st.text(f"{i}. {err}")
        
        nse_df = st.session_state.get("nse_df")
        if nse_df is not None and not nse_df.empty:
            try:
                st.info(f"📊 Loaded: {len(nse_df)} signals")
                
                # Filters
                st.markdown("### 🔽 Filter & Export")
                col_f1, col_f2, col_f3 = st.columns(3)
                
                with col_f1:
                    nse_min_conf = st.slider("Min Confidence %", 0, 100, 70, step=5, key="nse_conf_filter")
                with col_f2:
                    nse_signal = st.selectbox("Signal", ["ALL", "BUY", "SELL", "STRONG ONLY"], key="nse_signal_filter")
                with col_f3:
                    nse_sort = st.selectbox("Sort By", ["AI CONFIDENCE %", "LTP", "RVOL"], key="nse_sort_filter")
                
                # Apply filters
                nse_filtered = nse_df.copy()
                try:
                    confidence_col = pd.to_numeric(nse_filtered["AI CONFIDENCE %"], errors='coerce')
                    nse_filtered = nse_filtered[confidence_col >= nse_min_conf]
                except:
                    pass
                
                if nse_signal != "ALL":
                    try:
                        normalized = nse_filtered["AI SIGNAL"].apply(normalize_signal)
                        if nse_signal == "BUY":
                            nse_filtered = nse_filtered[normalized == "BUY"]
                        elif nse_signal == "SELL":
                            nse_filtered = nse_filtered[normalized == "SELL"]
                        elif nse_signal == "STRONG ONLY":
                            nse_filtered = nse_filtered[nse_filtered["AI SIGNAL"].astype(str).str.contains("STRONG", na=False)]
                    except:
                        pass
                
                try:
                    if nse_sort == "LTP":
                        nse_filtered = nse_filtered.sort_values("LTP", ascending=False)
                    elif nse_sort == "RVOL":
                        nse_filtered = nse_filtered.sort_values("RVOL", ascending=False)
                    else:
                        nse_filtered = nse_filtered.sort_values("AI CONFIDENCE %", ascending=False)
                except:
                    pass
                
                st.dataframe(nse_filtered, use_container_width=True, height=500)
                
                # Download buttons
                st.markdown("### 📥 Download")
                col_d1, col_d2, col_d3 = st.columns(3)
                
                with col_d1:
                    try:
                        excel_data = _format_excel_output(nse_filtered, "NSE")
                        st.download_button("📊 Excel", excel_data, f"NSE_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="nse_xls")
                    except Exception as e:
                        st.error(f"❌ Excel: {str(e)[:50]}")
                
                with col_d2:
                    try:
                        csv_data = to_csv_bytes(nse_filtered)
                        st.download_button("📄 CSV", csv_data, f"NSE_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                                         "text/csv", key="nse_csv")
                    except Exception as e:
                        st.error(f"❌ CSV: {str(e)[:50]}")
                
                with col_d3:
                    try:
                        json_data = to_json_bytes(nse_filtered)
                        st.download_button("📋 JSON", json_data, f"NSE_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                                         "application/json", key="nse_json")
                    except Exception as e:
                        st.error(f"❌ JSON: {str(e)[:50]}")
            
            except Exception as e:
                st.error(f"❌ NSE Tab Error: {str(e)[:100]}")
        else:
            st.info("👈 Click 'SCAN NSE' to start")
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 1: F&O STOCKS
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[1]:
        st.markdown("### F&O Stocks Scanner\n✅ Strict validation + options analysis")
        
        col1, col2 = st.columns([3, 1])
        with col1:
            fo_limit = st.number_input("Scan limit (0=all)", min_value=0, max_value=len(fo_symbols),
                                      value=min(200, len(fo_symbols)), step=25, key="fo_limit")
        with col2:
            st.metric("Available", len(fo_symbols))
        
        fo_universe = fo_symbols if fo_limit == 0 else fo_symbols[:fo_limit]
        
        if st.button(f"🔍 SCAN F&O ({len(fo_universe)} stocks)", key="fo_run"):
            with st.spinner("Analyzing F&O stocks…"):
                fo_results, fo_errors, fo_stats = run_fo_scan(fyers, fo_universe)
                st.session_state["fo_df"] = pd.DataFrame(fo_results) if fo_results else pd.DataFrame()
                st.session_state["fo_errors"] = fo_errors
                st.session_state["fo_stats"] = fo_stats
        
        if "fo_stats" in st.session_state:
            _display_scan_summary(st.session_state["fo_stats"])
        
        fo_df = st.session_state.get("fo_df")
        if fo_df is not None and not fo_df.empty:
            try:
                st.info(f"📊 Loaded: {len(fo_df)} signals")
                
                col_f1, col_f2, col_f3, col_f4 = st.columns(4)
                with col_f1:
                    fo_min_conf = st.slider("Min Confidence %", 0, 100, 70, step=5, key="fo_conf_filter")
                with col_f2:
                    fo_signal = st.selectbox("Signal", ["ALL", "BUY", "SELL", "STRONG ONLY"], key="fo_signal_filter")
                with col_f3:
                    fo_opt = st.selectbox("Options", ["ALL", "BULLISH", "BEARISH"], key="fo_opt_filter")
                with col_f4:
                    fo_sort = st.selectbox("Sort By", ["AI CONFIDENCE %", "LTP", "PCR"], key="fo_sort_filter")
                
                fo_filtered = fo_df.copy()
                try:
                    confidence_col = pd.to_numeric(fo_filtered["AI CONFIDENCE %"], errors='coerce')
                    fo_filtered = fo_filtered[confidence_col >= fo_min_conf]
                except:
                    pass
                
                if fo_signal != "ALL":
                    try:
                        normalized = fo_filtered["AI SIGNAL"].apply(normalize_signal)
                        if fo_signal == "BUY":
                            fo_filtered = fo_filtered[normalized == "BUY"]
                        elif fo_signal == "SELL":
                            fo_filtered = fo_filtered[normalized == "SELL"]
                        elif fo_signal == "STRONG ONLY":
                            fo_filtered = fo_filtered[fo_filtered["AI SIGNAL"].astype(str).str.contains("STRONG", na=False)]
                    except:
                        pass
                
                if fo_opt != "ALL":
                    try:
                        fo_filtered = fo_filtered[fo_filtered["OPTIONS BIAS"].astype(str).str.contains(fo_opt, na=False)]
                    except:
                        pass
                
                try:
                    if fo_sort == "PCR":
                        fo_filtered = fo_filtered.sort_values("PCR", ascending=False)
                    elif fo_sort == "LTP":
                        fo_filtered = fo_filtered.sort_values("LTP", ascending=False)
                    else:
                        fo_filtered = fo_filtered.sort_values("AI CONFIDENCE %", ascending=False)
                except:
                    pass
                
                st.dataframe(fo_filtered, use_container_width=True, height=500)
                
                st.markdown("### 📥 Download")
                col_d1, col_d2, col_d3 = st.columns(3)
                
                with col_d1:
                    try:
                        excel_data = _format_excel_output(fo_filtered, "FO")
                        st.download_button("📊 Excel", excel_data, f"FO_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="fo_xls")
                    except:
                        pass
                
                with col_d2:
                    try:
                        csv_data = to_csv_bytes(fo_filtered)
                        st.download_button("📄 CSV", csv_data, f"FO_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                                         "text/csv", key="fo_csv")
                    except:
                        pass
                
                with col_d3:
                    try:
                        json_data = to_json_bytes(fo_filtered)
                        st.download_button("📋 JSON", json_data, f"FO_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                                         "application/json", key="fo_json")
                    except:
                        pass
            
            except Exception as e:
                st.error(f"❌ F&O Tab Error: {str(e)[:100]}")
        else:
            st.info("👈 Click 'SCAN F&O' to start")
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 2: LIVE INTRADAY
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[2]:
        st.markdown("### ⚡ Live Intraday Scanner\nReal-time multi-timeframe analysis (5M, 15M, 1H)")
        
        col1, col2 = st.columns(2)
        with col1:
            intraday_type = st.radio("Select Universe", ["NSE Stocks", "F&O Stocks"], horizontal=True, key="intraday_type")
            intraday_universe = all_symbols if intraday_type == "NSE Stocks" else fo_symbols
        
        with col2:
            intraday_limit = st.number_input("Scan limit", min_value=10, max_value=len(intraday_universe),
                                            value=min(100, len(intraday_universe)), step=10, key="intraday_limit")
        
        intraday_symbols = intraday_universe[:intraday_limit]
        
        if st.button(f"⚡ SCAN LIVE INTRADAY ({len(intraday_symbols)} stocks)", key="intraday_run"):
            with st.spinner("Fetching live intraday data…"):
                if intraday_type == "NSE Stocks":
                    intraday_results, _, _ = run_nse_scan(fyers, intraday_symbols)
                else:
                    intraday_results, _, _ = run_fo_scan(fyers, intraday_symbols)
                
                if intraday_results:
                    intraday_df = pd.DataFrame(intraday_results)
                    st.session_state["intraday_df"] = intraday_df
        
        intraday_df = st.session_state.get("intraday_df")
        if intraday_df is not None and not intraday_df.empty:
            st.success(f"✅ Live data: {len(intraday_df)} stocks")
            
            col_if1, col_if2, col_if3 = st.columns(3)
            with col_if1:
                intraday_min_rvol = st.slider("Min RVOL", 0.5, 3.0, 1.2, 0.1, key="intraday_rvol")
            with col_if2:
                intraday_signal_filter = st.selectbox("Signal", ["ALL", "BUY", "SELL"], key="intraday_sig_filter")
            with col_if3:
                intraday_show_cols = st.multiselect("Show Columns", intraday_df.columns, 
                                                   default=["Symbol", "LTP", "AI SIGNAL", "AI CONFIDENCE %", "RVOL", "🟢 BUY PRESSURE %", "🔴 SELL PRESSURE %"],
                                                   key="intraday_cols")
            
            intraday_filtered = intraday_df.copy()
            try:
                intraday_filtered = intraday_filtered[pd.to_numeric(intraday_filtered["RVOL"], errors='coerce') >= intraday_min_rvol]
            except:
                pass
            
            if intraday_signal_filter != "ALL":
                try:
                    normalized = intraday_filtered["AI SIGNAL"].apply(normalize_signal)
                    if intraday_signal_filter == "BUY":
                        intraday_filtered = intraday_filtered[normalized == "BUY"]
                    elif intraday_signal_filter == "SELL":
                        intraday_filtered = intraday_filtered[normalized == "SELL"]
                except:
                    pass
            
            if intraday_show_cols:
                st.dataframe(intraday_filtered[intraday_show_cols], use_container_width=True, height=400)
            else:
                st.dataframe(intraday_filtered, use_container_width=True, height=400)
        else:
            st.info("👈 Click 'SCAN LIVE INTRADAY' to fetch data")
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 3: STRONG SIGNALS
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[3]:
        st.markdown("### 🔥 Strong Signals Only\nHigh-confidence setups (≥70%)")
        
        strong_source = st.radio("Source", ["NSE Stocks", "F&O Stocks"], horizontal=True, key="strong_source")
        
        if strong_source == "NSE Stocks":
            nse_df = st.session_state.get("nse_df")
            strong_df = nse_df
        else:
            fo_df = st.session_state.get("fo_df")
            strong_df = fo_df
        
        if strong_df is not None and not strong_df.empty:
            try:
                # Filter for strong signals
                confidence_col = pd.to_numeric(strong_df.get("AI CONFIDENCE %", pd.Series([])), errors='coerce')
                strong_filtered = strong_df[confidence_col >= 75].copy()
                
                # Only BUY/SELL
                normalized = strong_filtered["AI SIGNAL"].apply(normalize_signal)
                strong_filtered = strong_filtered[normalized != "NEUTRAL"]
                
                st.subheader(f"💪 {len(strong_filtered)} Strong Signals")
                
                if len(strong_filtered) > 0:
                    st.dataframe(strong_filtered.sort_values("AI CONFIDENCE %", ascending=False), 
                               use_container_width=True, height=400)
                else:
                    st.warning("No strong signals (≥75% confidence) found. Lower the threshold in Settings tab.")
            
            except Exception as e:
                st.error(f"❌ Error: {str(e)[:100]}")
        else:
            st.info(f"👈 Run '{strong_source}' scanner first")
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 4: SWING (GOLDEN CROSS / DEATH CROSS)
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[4]:
        st.markdown("### 📈 Swing Analysis - Golden Cross & Death Cross Detection\nDaily EMA50/EMA200 crossovers")
        
        col1, col2 = st.columns([3, 1])
        with col1:
            swing_limit = st.number_input("Scan limit", min_value=10, max_value=len(all_symbols),
                                         value=min(100, len(all_symbols)), step=25, key="swing_limit")
        with col2:
            st.metric("Available", len(all_symbols))
        
        swing_symbols = all_symbols[:swing_limit]
        
        if st.button(f"📈 DETECT CROSSOVERS ({len(swing_symbols)} stocks)", key="swing_run"):
            with st.spinner("Analyzing daily charts for Golden/Death Cross…"):
                swing_results = []
                swing_progress = st.progress(0)
                
                for idx, symbol in enumerate(swing_symbols):
                    try:
                        cc_data = detect_golden_death_cross(fyers, symbol)
                        if cc_data.get("reason") == "OK":
                            ticker = symbol.replace("NSE:", "").replace("-EQ", "")
                            swing_results.append({
                                "Symbol": ticker,
                                "LTP": cc_data.get("ltp", "N/A"),
                                "EMA50": cc_data.get("ema50", "N/A"),
                                "EMA200": cc_data.get("ema200", "N/A"),
                                "EMA Trend": cc_data.get("ema_trend", "N/A"),
                                "Signal": cc_data.get("signal", "NONE"),
                                "Signal Date": cc_data.get("signal_date", "N/A"),
                            })
                    except:
                        pass
                    
                    swing_progress.progress((idx + 1) / len(swing_symbols))
                
                swing_progress.empty()
                
                if swing_results:
                    swing_df = pd.DataFrame(swing_results)
                    st.session_state["swing_df"] = swing_df
        
        swing_df = st.session_state.get("swing_df")
        if swing_df is not None and not swing_df.empty:
            st.success(f"✅ Analysis Complete: {len(swing_df)} stocks analyzed")
            
            # Filter by signal type
            col_s1, col_s2 = st.columns(2)
            with col_s1:
                swing_signal_filter = st.selectbox("Signal Type", ["ALL", "🟢 GOLDEN CROSS", "🔴 DEATH CROSS"], key="swing_sig_filter")
            with col_s2:
                swing_trend_filter = st.selectbox("EMA Trend", ["ALL", "BULLISH", "BEARISH", "NEUTRAL"], key="swing_trend_filter")
            
            swing_filtered = swing_df.copy()
            
            if swing_signal_filter != "ALL":
                swing_filtered = swing_filtered[swing_filtered["Signal"] == swing_signal_filter]
            
            if swing_trend_filter != "ALL":
                swing_filtered = swing_filtered[swing_filtered["EMA Trend"] == swing_trend_filter]
            
            if len(swing_filtered) > 0:
                # Separate by signal type
                golden = swing_filtered[swing_filtered["Signal"] == "🟢 GOLDEN CROSS"]
                death = swing_filtered[swing_filtered["Signal"] == "🔴 DEATH CROSS"]
                other = swing_filtered[swing_filtered["Signal"] == "NONE"]
                
                if len(golden) > 0:
                    st.subheader(f"🟢 Golden Cross ({len(golden)})")
                    st.dataframe(golden, use_container_width=True, height=250)
                
                if len(death) > 0:
                    st.subheader(f"🔴 Death Cross ({len(death)})")
                    st.dataframe(death, use_container_width=True, height=250)
                
                if len(other) > 0:
                    with st.expander(f"📊 Other ({len(other)})"):
                        st.dataframe(other, use_container_width=True, height=200)
            else:
                st.warning("No signals match current filters")
        else:
            st.info("👈 Click 'DETECT CROSSOVERS' to start swing analysis")
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 5: ADDITIONAL ANALYSIS
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[5]:
        st.markdown("### 🧠 Additional Analysis - Deep Dive on Single Stock")
        
        col1, col2 = st.columns(2)
        with col1:
            aa_universe = st.radio("Select Universe", ["NSE", "F&O"], horizontal=True, key="aa_universe")
            aa_symbols = all_symbols if aa_universe == "NSE" else fo_symbols
        
        with col2:
            aa_symbol_input = st.selectbox("Choose Stock", aa_symbols, key="aa_stock_select")
        
        if st.button("🔍 ANALYZE", key="aa_run"):
            with st.spinner(f"Analyzing {aa_symbol_input}…"):
                try:
                    analysis_5m = analyze_timeframe(fyers, aa_symbol_input, "5")
                    analysis_15m = analyze_timeframe(fyers, aa_symbol_input, "15")
                    analysis_1h = analyze_timeframe(fyers, aa_symbol_input, "60")
                    
                    # Options for F&O
                    if aa_universe == "F&O":
                        options_data = fetch_options_chain_data(fyers, aa_symbol_input)
                    else:
                        options_data = None
                    
                    # Master signal
                    master_signal = calculate_master_signal(aa_symbol_input, analysis_5m, analysis_15m, analysis_1h, options_data)
                    
                    st.session_state["aa_analysis"] = {
                        "5m": analysis_5m,
                        "15m": analysis_15m,
                        "1h": analysis_1h,
                        "master": master_signal,
                        "options": options_data,
                    }
                
                except Exception as e:
                    st.error(f"❌ Analysis failed: {str(e)}")
        
        aa_analysis = st.session_state.get("aa_analysis")
        if aa_analysis:
            ticker = aa_symbol_input.replace("NSE:", "").replace("-EQ", "")
            
            # Overall signal
            st.markdown(f"## {ticker} Analysis")
            master = aa_analysis.get("master", {})
            signal = master.get("final_signal", "NEUTRAL")
            conf = master.get("confidence", 0)
            
            # Color the signal
            if "BUY" in signal:
                st.success(f"{signal} — {conf:.0f}% Confidence")
            elif "SELL" in signal:
                st.error(f"{signal} — {conf:.0f}% Confidence")
            else:
                st.warning(f"{signal} — {conf:.0f}% Confidence")
            
            # Timeframe analysis
            st.markdown("### ⏱️ Timeframe Analysis")
            col_5m, col_15m, col_1h = st.columns(3)
            
            with col_5m:
                d5 = aa_analysis["5m"].get("data", {})
                if d5:
                    st.write("**5 MINUTE**")
                    st.write(f"Trend: {d5.get('structure_trend', 'N/A')}")
                    st.write(f"Structure: {d5.get('structure_type', 'N/A')}")
                    st.write(f"LTP: {d5.get('last_close', 'N/A')}")
                    st.write(f"RSI: {d5.get('rsi', 'N/A')}")
                    st.write(f"RVOL: {d5.get('rvol', 'N/A')}x")
            
            with col_15m:
                d15 = aa_analysis["15m"].get("data", {})
                if d15:
                    st.write("**15 MINUTE**")
                    st.write(f"Trend: {d15.get('structure_trend', 'N/A')}")
                    st.write(f"Structure: {d15.get('structure_type', 'N/A')}")
                    st.write(f"VWAP: {d15.get('vwap', 'N/A')}")
                    st.write(f"EMA Trend: {d15.get('ema_trend', 'N/A')}")
            
            with col_1h:
                d1h = aa_analysis["1h"].get("data", {})
                if d1h:
                    st.write("**1 HOUR**")
                    st.write(f"Trend: {d1h.get('structure_trend', 'N/A')}")
                    st.write(f"Structure: {d1h.get('structure_type', 'N/A')}")
                    st.write(f"EMA50: {d1h.get('ema50', 'N/A')}")
                    st.write(f"EMA200: {d1h.get('ema200', 'N/A')}")
            
            # Pressure analysis
            st.markdown("### 📊 Pressure Analysis")
            if d5:
                bp = d5.get("buying_pressure", 0)
                sp = d5.get("selling_pressure", 0)
                col_bp1, col_bp2 = st.columns(2)
                with col_bp1:
                    st.metric("🟢 Buying Pressure", f"{bp}%")
                with col_bp2:
                    st.metric("🔴 Selling Pressure", f"{sp}%")
            
            # Market structure
            st.markdown("### 🏗️ Market Structure")
            col_struct1, col_struct2, col_struct3 = st.columns(3)
            
            with col_struct1:
                if d5 and d5.get("bullish_choch"):
                    st.success("✅ Bullish CHoCH")
                elif d5 and d5.get("bearish_choch"):
                    st.error("❌ Bearish CHoCH")
            
            with col_struct2:
                if d5 and d5.get("bullish_mss"):
                    st.success("✅ Bullish MSS")
                elif d5 and d5.get("bearish_mss"):
                    st.error("❌ Bearish MSS")
            
            with col_struct3:
                if d5 and d5.get("bullish_cisd"):
                    st.success("✅ Bullish CISD")
                elif d5 and d5.get("bearish_cisd"):
                    st.error("❌ Bearish CISD")
            
            # Trade plan
            st.markdown("### 💹 Trade Plan")
            col_tp1, col_tp2, col_tp3, col_tp4 = st.columns(4)
            
            entry = master.get("entry", "N/A")
            sl = master.get("stop_loss", "N/A")
            t1 = master.get("target1", "N/A")
            t2 = master.get("target2", "N/A")
            rr = master.get("rr_ratio", "N/A")
            
            with col_tp1:
                st.metric("Entry", f"{entry}")
            with col_tp2:
                st.metric("Stop Loss", f"{sl}")
            with col_tp3:
                st.metric("Target 1", f"{t1}")
            with col_tp4:
                st.metric("Target 2 / R:R", f"{t2} / {rr}")
            
            # Signal explanation
            st.markdown("### 📝 Signal Explanation")
            reason = master.get("signal_reason", "N/A")
            st.info(reason)
            
            # Options (if F&O)
            if aa_universe == "F&O" and aa_analysis.get("options"):
                st.markdown("### 📊 Options Analysis")
                opt = aa_analysis["options"]
                col_opt1, col_opt2, col_opt3, col_opt4 = st.columns(4)
                
                with col_opt1:
                    st.metric("ATM Strike", opt.get("atm_strike", "N/A"))
                with col_opt2:
                    st.metric("PCR", opt.get("pcr", "N/A"))
                with col_opt3:
                    st.metric("CE OI", opt.get("ce_oi", "N/A"))
                with col_opt4:
                    st.metric("Options Bias", opt.get("options_bias", "N/A"))
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 6: MARKET DASHBOARD
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[6]:
        st.markdown("### 📊 Market Dashboard - Statistics & Sentiment")
        
        col_dash1, col_dash2 = st.columns(2)
        
        with col_dash1:
            dashboard_source = st.radio("Data Source", ["NSE Stocks", "F&O Stocks"], horizontal=True, key="dash_source")
        
        if dashboard_source == "NSE Stocks":
            dash_df = st.session_state.get("nse_df")
        else:
            dash_df = st.session_state.get("fo_df")
        
        if dash_df is not None and not dash_df.empty:
            stats = calculate_market_stats(dash_df)
            
            # Market overview
            st.markdown("### 📈 Market Overview")
            col_ov1, col_ov2, col_ov3, col_ov4, col_ov5 = st.columns(5)
            
            with col_ov1:
                st.metric("Total Scanned", stats["total"])
            with col_ov2:
                st.metric("🟢 BUY", stats["buy"])
            with col_ov3:
                st.metric("🔴 SELL", stats["sell"])
            with col_ov4:
                st.metric("🟡 NEUTRAL", stats["neutral"])
            with col_ov5:
                st.metric("Avg Confidence", f"{stats['avg_confidence']:.1f}%")
            
            # Strong signals
            st.markdown("### 💪 Strong Signals")
            col_str1, col_str2 = st.columns(2)
            
            with col_str1:
                st.metric("💪 Strong BUY", stats["strong_buy"])
            with col_str2:
                st.metric("💪 Strong SELL", stats["strong_sell"])
            
            # Market sentiment
            st.markdown("### 😊 Market Sentiment")
            col_sent1, col_sent2, col_sent3 = st.columns(3)
            
            with col_sent1:
                st.metric("Bullish %", f"{stats['buy_pct']:.1f}%")
            with col_sent2:
                st.metric("Bearish %", f"{stats['sell_pct']:.1f}%")
            with col_sent3:
                st.metric("Neutral %", f"{stats['neutral_pct']:.1f}%")
            
            # Chart
            if st.checkbox("Show Chart"):
                try:
                    import matplotlib.pyplot as plt
                    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4))
                    
                    # Pie chart
                    labels = ["🟢 BUY", "🔴 SELL", "🟡 NEUTRAL"]
                    sizes = [stats["buy"], stats["sell"], stats["neutral"]]
                    colors = ["#2ecc71", "#e74c3c", "#f39c12"]
                    ax1.pie(sizes, labels=labels, colors=colors, autopct="%1.1f%%", startangle=90)
                    ax1.set_title("Signal Distribution")
                    
                    # Bar chart
                    categories = ["Strong BUY", "BUY", "SELL", "Strong SELL"]
                    counts = [stats["strong_buy"], stats["buy"] - stats["strong_buy"],
                             stats["sell"] - stats["strong_sell"], stats["strong_sell"]]
                    ax2.bar(categories, counts, color=["#27ae60", "#2ecc71", "#e74c3c", "#c0392b"])
                    ax2.set_ylabel("Count")
                    ax2.set_title("Signal Strength Distribution")
                    plt.tight_layout()
                    st.pyplot(fig)
                except Exception as e:
                    st.warning(f"Chart error: {str(e)[:50]}")
        else:
            st.info(f"👈 Run '{dashboard_source}' scanner first")
    
    # ════════════════════════════════════════════════════════════════════════════════
    # TAB 7: SETTINGS
    # ════════════════════════════════════════════════════════════════════════════════
    with tabs[7]:
        st.markdown("### ⚙️ Scanner Settings & Configuration")
        
        st.markdown("#### 🎯 Signal Filtering")
        col_set1, col_set2, col_set3 = st.columns(3)
        
        with col_set1:
            default_conf = st.number_input("Default Min Confidence %", 0, 100, DEFAULT_CONFIDENCE_THRESHOLD, 5, key="set_conf")
        with col_set2:
            default_rvol = st.slider("Default Min RVOL", 0.5, 3.0, DEFAULT_RVOL_THRESHOLD, 0.1, key="set_rvol")
        with col_set3:
            default_strong_rvol = st.slider("Strong Signal RVOL", 1.0, 3.0, DEFAULT_STRONG_RVOL, 0.1, key="set_strong_rvol")
        
        st.markdown("#### 📊 Scan Parameters")
        col_set4, col_set5 = st.columns(2)
        
        with col_set4:
            max_workers_setting = st.slider("Max Parallel Workers", 2, 16, MAX_WORKERS, 1, key="set_workers")
        with col_set5:
            batch_size_setting = st.slider("Batch Size", 10, 100, BATCH_SIZE, 10, key="set_batch")
        
        st.markdown("#### 🔄 Auto Refresh")
        col_set6, col_set7 = st.columns(2)
        
        with col_set6:
            auto_refresh = st.checkbox("Enable Auto Refresh", value=False, key="set_refresh")
        with col_set7:
            if auto_refresh:
                refresh_interval = st.selectbox("Refresh Interval", REFRESH_INTERVALS, key="set_refresh_int")
        
        st.markdown("#### 📋 Display Options")
        col_set8, col_set9 = st.columns(2)
        
        with col_set8:
            show_errors = st.checkbox("Show Error Details", value=False, key="set_errors")
        with col_set9:
            show_logs = st.checkbox("Show API Logs", value=False, key="set_logs")
        
        st.markdown("#### ℹ️ Information")
        st.info("""
        **Scanner Features:**
        - ✅ Multi-timeframe analysis (5M, 15M, 1H, Daily)
        - ✅ Strict signal validation engine
        - ✅ Pressure-based confirmation
        - ✅ VWAP, EMA, RSI, MACD indicators
        - ✅ Market structure (CHoCH, MSS, CISD)
        - ✅ Options chain analysis (F&O)
        - ✅ Golden Cross / Death Cross detection
        - ✅ Next Candle Bias prediction
        
        **Data Source:** Fyers Live API
        **Timeframes:** 5M, 15M, 1H, Daily
        **Universes:** NSE Equities + F&O Stocks
        """)
    
    gc.collect()

# ════════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    try:
        access_token = os.environ.get("FYERS_ACCESS_TOKEN")
        if not access_token:
            st.error("❌ FYERS_ACCESS_TOKEN not set in environment variables")
            st.info("Please set: export FYERS_ACCESS_TOKEN='your_token_here'")
            st.stop()
        
        try:
            from fyers_api import fyersModel
        except ImportError as ie:
            st.error("❌ fyers-api not installed")
            st.code("pip install fyers-api", language="bash")
            st.stop()
        
        app_id = os.environ.get("FYERS_APP_ID", "DEMO")
        
        try:
            fyers = fyersModel.FyersModel(client_id=app_id, token=access_token, log_path="")
            logger.info("Fyers client initialized successfully")
            show_scanner(fyers)
        except Exception as init_error:
            st.error(f"❌ Failed to initialize Fyers API: {str(init_error)}")
            logger.error(f"Fyers initialization error: {init_error}", exc_info=True)
            
            with st.expander("Debug Information"):
                st.write(f"App ID: {app_id}")
                st.write(f"Token set: {bool(access_token)}")
                st.write(f"Error: {init_error}")
    
    except Exception as e:
        st.error(f"❌ Unexpected Error: {e}")
        logger.error(f"Unexpected error: {e}", exc_info=True)
