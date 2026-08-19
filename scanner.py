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
logger = logging.getLogger("nse_scanner_v16_fixed")
logger.setLevel(logging.INFO)

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
        
        # Green candle = buying pressure
        if close > open_:
            buying_vol += volume
        # Red candle = selling pressure
        elif close < open_:
            selling_vol += volume
        # Doji/neutral volume distributed equally
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
    
    # Determine trend
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
            buy_score = buy_score * 0.95  # Reduce confidence
        scores_count += 1
        
        # Normalize score to 0-100
        buy_score = max(0, min(100, buy_score))
        
        # Calculate confidence based on how clear the signal is
        # Confidence = how far from neutral (50)
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
        # Calculate strength based on distance moved
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
    
    # Add minimum move threshold (0.1% or 0.5 pips) to prevent micro-breaks
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
    
    # Look for last 3 red candles for bearish block
    bearish = prior[prior["Close"] < prior["Open"]].tail(3)
    bullish = prior[prior["Close"] > prior["Open"]].tail(3)
    
    min_move = float(last["Close"]) * 0.001  # 0.1% min move
    
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
    
    # Add minimum move threshold
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
# MASTER SIGNAL ENGINE (IMPROVED WITH STRICTER RULES)
# ════════════════════════════════════════════════════════════════════════════════
def calculate_master_signal(symbol: str, analysis_5m: Dict, analysis_15m: Dict, analysis_1h: Dict, options_data: Dict = None) -> Dict[str, Any]:
    """
    Calculate master signal using weighted scoring from multi-timeframe analysis.
    Includes Next Candle Bias as a confirmation factor.
    """
    if options_data is None:
        options_data = {"status":"DATA_UNAVAILABLE", "options_bias":"NEUTRAL"}
    
    reasons = []
    scores = {
        "5m_score": 50,
        "15m_score": 50,
        "1h_score": 50,
        "pressure_score": 50,
        "options_score": 50,
    }
    
    # ════════════════════════════════════════════════════════════════════════════
    # 5M ANALYSIS (Entry timeframe)
    # ════════════════════════════════════════════════════════════════════════════
    if analysis_5m.get("status") == "OK" and analysis_5m.get("data"):
        data_5m = analysis_5m["data"]
        score_5m = 50
        
        # Structure (Primary signal) - STRICT
        if data_5m["structure_trend"] == "BULLISH" and data_5m["structure_type"] in ("HH/HL", "HH", "HL"):
            score_5m += 20
            reasons.append(f"5M: {data_5m['structure_type']} bullish")
        elif data_5m["structure_trend"] == "BEARISH" and data_5m["structure_type"] in ("LH/LL", "LH", "LL"):
            score_5m -= 20
            reasons.append(f"5M: {data_5m['structure_type']} bearish")
        else:
            score_5m -= 5
        
        # CHoCH & MSS - STRICT confirmation only
        if data_5m["bullish_choch"] or data_5m["bullish_mss"]:
            score_5m += 15
            reasons.append("5M: CHoCH/MSS bullish")
        elif data_5m["bearish_choch"] or data_5m["bearish_mss"]:
            score_5m -= 15
            reasons.append("5M: CHoCH/MSS bearish")
        
        # CISD
        if data_5m.get("bullish_cisd"):
            score_5m += 8
            reasons.append("5M: CISD bullish")
        elif data_5m.get("bearish_cisd"):
            score_5m -= 8
            reasons.append("5M: CISD bearish")
        
        # EMA
        if data_5m["ema_trend"] == "BULLISH":
            score_5m += 8
        elif data_5m["ema_trend"] == "BEARISH":
            score_5m -= 8
        else:
            score_5m -= 2
        
        # RSI
        if 45 < data_5m["rsi"] < 55:
            score_5m -= 3
        elif data_5m["rsi"] >= 70:
            score_5m -= 5
        elif data_5m["rsi"] <= 30:
            score_5m += 5
        
        # MACD
        if data_5m["macd_bullish"]:
            score_5m += 5
        else:
            score_5m -= 5
        
        # Volume
        if data_5m["rvol"] > 1.5:
            score_5m += 3
        elif data_5m["rvol"] < 0.8:
            score_5m -= 2
        
        score_5m = max(0, min(100, score_5m))
        scores["5m_score"] = score_5m
    
    # ════════════════════════════════════════════════════════════════════════════
    # 15M ANALYSIS (Momentum)
    # ════════════════════════════════════════════════════════════════════════════
    if analysis_15m.get("status") == "OK" and analysis_15m.get("data"):
        data_15m = analysis_15m["data"]
        score_15m = 50
        
        if data_15m["structure_trend"] == "BULLISH":
            score_15m += 18
            reasons.append("15M: Bullish")
        elif data_15m["structure_trend"] == "BEARISH":
            score_15m -= 18
            reasons.append("15M: Bearish")
        else:
            score_15m -= 5
        
        if data_15m["bullish_choch"] or data_15m["bullish_mss"]:
            score_15m += 12
        elif data_15m["bearish_choch"] or data_15m["bearish_mss"]:
            score_15m -= 12
        
        if data_15m["ema_trend"] == "BULLISH":
            score_15m += 7
        elif data_15m["ema_trend"] == "BEARISH":
            score_15m -= 7
        
        if data_15m["rsi"] >= 70:
            score_15m -= 4
        elif data_15m["rsi"] <= 30:
            score_15m += 4
        
        if data_15m["macd_bullish"]:
            score_15m += 4
        else:
            score_15m -= 4
        
        if data_15m["rvol"] > 1.5:
            score_15m += 3
        
        score_15m = max(0, min(100, score_15m))
        scores["15m_score"] = score_15m
    
    # ════════════════════════════════════════════════════════════════════════════
    # 1H ANALYSIS (Trend context)
    # ════════════════════════════════════════════════════════════════════════════
    if analysis_1h.get("status") == "OK" and analysis_1h.get("data"):
        data_1h = analysis_1h["data"]
        score_1h = 50
        
        if data_1h["structure_trend"] == "BULLISH":
            score_1h += 15
            reasons.append("1H: Bullish")
        elif data_1h["structure_trend"] == "BEARISH":
            score_1h -= 15
            reasons.append("1H: Bearish")
        else:
            score_1h -= 5
        
        if data_1h["bullish_choch"]:
            score_1h += 8
        elif data_1h["bearish_choch"]:
            score_1h -= 8
        
        if data_1h["ema_trend"] == "BULLISH":
            score_1h += 8
        elif data_1h["ema_trend"] == "BEARISH":
            score_1h -= 8
        
        if data_1h["rvol"] > 1.5:
            score_1h += 2
        
        score_1h = max(0, min(100, score_1h))
        scores["1h_score"] = score_1h
    
    # ════════════════════════════════════════════════════════════════════════════
    # BUYING/SELLING PRESSURE (Confirmation)
    # ════════════════════════════════════════════════════════════════════════════
    if analysis_5m.get("status") == "OK" and analysis_5m.get("data"):
        d5 = analysis_5m["data"]
        pressure_trend = d5.get("pressure_trend", "NEUTRAL")
        
        if pressure_trend == "STRONG_BUYING":
            scores["pressure_score"] = 85
        elif pressure_trend == "BUYING":
            scores["pressure_score"] = 70
        elif pressure_trend == "STRONG_SELLING":
            scores["pressure_score"] = 15
        elif pressure_trend == "SELLING":
            scores["pressure_score"] = 30
        else:
            scores["pressure_score"] = 50
    
    # Options confirmation (F&O only)
    opt_bias = options_data.get("options_bias", "NEUTRAL") if isinstance(options_data, dict) else "NEUTRAL"
    if "BULLISH" in str(opt_bias):
        scores["options_score"] = 75
        reasons.append("Options: Bullish")
    elif "BEARISH" in str(opt_bias):
        scores["options_score"] = 25
        reasons.append("Options: Bearish")
    else:
        scores["options_score"] = 50

    # ════════════════════════════════════════════════════════════════════════════
    # WEIGHTED CALCULATION WITH ALIGNMENT CHECKS
    # ════════════════════════════════════════════════════════════════════════════
    total_score = (scores["5m_score"] * 0.30 + scores["15m_score"] * 0.30 +
                   scores["1h_score"] * 0.20 + scores["pressure_score"] * 0.10 +
                   scores["options_score"] * 0.10)

    # Strict alignment rules
    tf_scores = [scores["5m_score"], scores["15m_score"], scores["1h_score"]]
    bullish_tfs = sum(1 for x in tf_scores if x >= 60)
    bearish_tfs = sum(1 for x in tf_scores if x <= 40)
    
    hard_conflict = bullish_tfs >= 1 and bearish_tfs >= 1

    # Confidence calculation
    confidence = round(abs(total_score - 50) * 0.8, 1)
    confidence = max(0, min(100, confidence))

    # STRICT signal generation
    bullish_aligned = scores["5m_score"] >= 65 and scores["15m_score"] >= 60 and scores["1h_score"] >= 55
    bearish_aligned = scores["5m_score"] <= 35 and scores["15m_score"] <= 40 and scores["1h_score"] <= 45
    
    pressure_bullish = scores["pressure_score"] >= 65
    pressure_bearish = scores["pressure_score"] <= 35
    
    if hard_conflict:
        final_signal = "NEUTRAL"
        confidence = min(confidence, 40.0)
        reasons.append("TF conflict")
    elif bullish_aligned and pressure_bullish and total_score >= 72:
        final_signal = "🟢 STRONG BUY"
    elif bullish_aligned and total_score >= 62:
        final_signal = "🟢 BUY"
    elif bearish_aligned and pressure_bearish and total_score <= 28:
        final_signal = "🔴 STRONG SELL"
    elif bearish_aligned and total_score <= 38:
        final_signal = "🔴 SELL"
    else:
        final_signal = "🟡 NEUTRAL"
        reasons.append("Insufficient alignment")
    
    # Calculate Entry/SL/Targets
    entry = sl = t1 = t2 = rr_ratio = None
    if analysis_5m.get("status") == "OK" and analysis_5m.get("data"):
        data_5m = analysis_5m["data"]
        entry = round(data_5m["last_close"], 2)
        atr_5m = data_5m["atr"]
        
        if "BUY" in final_signal:
            sl = round(data_5m["swing_low"] - atr_5m * 0.5, 2) if data_5m["swing_low"] else round(entry - atr_5m * 2, 2)
            t1 = round(entry + atr_5m * 1.5, 2)
            t2 = round(entry + atr_5m * 2.5, 2)
        else:
            sl = round(data_5m["swing_high"] + atr_5m * 0.5, 2) if data_5m["swing_high"] else round(entry + atr_5m * 2, 2)
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
        "scores": scores,
        "reasons": reasons,
    }

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
        
        # All timeframes unavailable
        if all(a.get("status") != "OK" for a in [analysis_5m, analysis_15m, analysis_1h]):
            return None, None
        
        # NO options data for NSE scanner
        
        # Calculate master signal
        master = calculate_master_signal(symbol, analysis_5m, analysis_15m, analysis_1h)
        
        # Calculate Next Candle Bias
        if analysis_5m.get("status") == "OK" and analysis_5m.get("data"):
            next_bias = calculate_next_candle_bias(analysis_5m.get("df"), analysis_5m)
        else:
            next_bias = {"bias": "NEUTRAL", "confidence": 0.0}
        
        # Get LTP from 5M data
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
        
        # All timeframes unavailable
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
        
        # Get LTP from 5M data
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
# PROFESSIONAL EXCEL EXPORT WITH FORMATTING
# ════════════════════════════════════════════════════════════════════════════════
def _format_excel_output(df, scanner_type: str = "NSE") -> bytes:
    """Create professionally formatted Excel with colors and styling."""
    buf = io.BytesIO()
    
    try:
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            # Write signals sheet
            df.to_excel(writer, index=False, sheet_name="Signals")
            
            # Get workbook and worksheet
            workbook = writer.book
            worksheet = writer.sheets["Signals"]
            
            if OPENPYXL_AVAILABLE:
                # Define colors
                green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
                red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
                yellow_fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
                header_fill = PatternFill(start_color="366092", end_color="366092", fill_type="solid")
                header_font = Font(bold=True, color="FFFFFF", size=11)
                
                # Format header
                for col_num, col_title in enumerate(df.columns, 1):
                    cell = worksheet.cell(row=1, column=col_num)
                    cell.fill = header_fill
                    cell.font = header_font
                    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                
                # Format data rows with signal coloring
                for row_num, row in enumerate(df.itertuples(), 2):
                    signal_col = None
                    for col_num, col_title in enumerate(df.columns, 1):
                        cell = worksheet.cell(row=row_num, column=col_num)
                        cell.alignment = Alignment(horizontal="center", vertical="center")
                        
                        # Color signal columns
                        if col_title == "AI SIGNAL":
                            signal_val = str(row[col_num])
                            if "STRONG BUY" in signal_val or "BUY" in signal_val:
                                cell.fill = green_fill
                            elif "STRONG SELL" in signal_val or "SELL" in signal_val:
                                cell.fill = red_fill
                            else:
                                cell.fill = yellow_fill
                        
                        # Color confidence
                        elif col_title == "AI CONFIDENCE %" and isinstance(row[col_num], (int, float)):
                            if row[col_num] >= 70:
                                cell.fill = green_fill
                            elif row[col_num] >= 50:
                                cell.fill = yellow_fill
                            else:
                                cell.fill = red_fill
                        
                        # Color next candle bias
                        elif col_title == "NEXT CANDLE BIAS":
                            bias_val = str(row[col_num])
                            if "BUY" in bias_val:
                                cell.fill = green_fill
                            elif "SELL" in bias_val:
                                cell.fill = red_fill
                            else:
                                cell.fill = yellow_fill
                
                # Freeze top row
                worksheet.freeze_panes = "A2"
                
                # Auto-fit columns
                for column in worksheet.columns:
                    max_length = 0
                    column_letter = get_column_letter(column[0].column)
                    for cell in column:
                        try:
                            if len(str(cell.value)) > max_length:
                                max_length = len(str(cell.value))
                        except:
                            pass
                    adjusted_width = min(max_length + 2, 50)
                    worksheet.column_dimensions[column_letter].width = adjusted_width
            
            # Add summary sheet
            summary_data = {
                "Metric": ["Total Stocks", "BUY Signals", "SELL Signals", "NEUTRAL", "Avg Confidence"],
                "Value": [
                    len(df),
                    len(df[df["AI SIGNAL"].str.contains("BUY", na=False)]),
                    len(df[df["AI SIGNAL"].str.contains("SELL", na=False)]),
                    len(df[df["AI SIGNAL"].str.contains("NEUTRAL", na=False)]),
                    f"{df['AI CONFIDENCE %'].mean():.1f}%"
                ]
            }
            summary_df = pd.DataFrame(summary_data)
            summary_df.to_excel(writer, index=False, sheet_name="Summary")
    
    except Exception as e:
        # Fallback to simple export
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Signals")
    
    buf.seek(0)
    return buf.getvalue()

def to_csv_bytes(df) -> bytes:
    return df.to_csv(index=False).encode("utf-8")

def to_json_bytes(df) -> bytes:
    return df.to_json(orient="records", indent=2, force_ascii=False).encode("utf-8")

# ════════════════════════════════════════════════════════════════════════════════
# MAIN APP - DUAL TAB INTERFACE
# ════════════════════════════════════════════════════════════════════════════════
def show_scanner(fyers) -> None:
    """Streamlit main app - NSE AI PRO V16.2 with Dual Tabs"""
    
    st.set_page_config(page_title="NSE AI PRO V16.2", layout="wide")
    st.title("🚀 NSE AI PRO V16.2 — Dual Scanners (NSE + F&O)")
    st.caption(f"🕒 Current Time (IST): {_now_ist().strftime('%d-%b-%Y %H:%M:%S')} IST | Built for Production")
    
    # Load symbols
    all_symbols = load_nse_equity_symbols()
    fo_symbols = load_fo_stocks()
    
    if not all_symbols:
        st.error("❌ No symbols loaded — check FYERS API access.")
        return
    
    st.caption(f"📊 NSE Equities: {len(all_symbols)} | 📈 F&O Stocks: {len(fo_symbols)}")
    
    # Create tabs
    tab_nse, tab_fo = st.tabs(["📊 NSE STOCKS", "📈 F&O STOCKS"])
    
    # ════════════════════════════════════════════════════════════════════════════════
    # NSE STOCKS TAB
    # ════════════════════════════════════════════════════════════════════════════════
    with tab_nse:
        st.markdown("### NSE Equity Stocks Scanner\nTechnical analysis only – no options data")
        
        col1, col2 = st.columns([3, 1])
        with col1:
            nse_limit = st.number_input("Scan limit (0=all)", min_value=0, max_value=len(all_symbols),
                                       value=min(500, len(all_symbols)), step=50, key="nse_limit")
        with col2:
            st.metric("Available", len(all_symbols))
        
        nse_universe = all_symbols if nse_limit == 0 else all_symbols[:nse_limit]
        
        if st.button(f"🔍 SCAN NSE ({len(nse_universe)} stocks)", key="nse_run"):
            with st.spinner("Analyzing NSE stocks with technical indicators…"):
                nse_results, nse_errors, nse_stats = run_nse_scan(fyers, nse_universe)
                st.session_state["nse_df"] = pd.DataFrame(nse_results) if nse_results else pd.DataFrame()
                st.session_state["nse_errors"] = nse_errors
                st.session_state["nse_stats"] = nse_stats
        
        # Display NSE summary
        if "nse_stats" in st.session_state:
            _display_scan_summary(st.session_state["nse_stats"])
        
        # NSE Results
        nse_df = st.session_state.get("nse_df")
        if nse_df is not None and not nse_df.empty:
            nse_sorted = nse_df.sort_values("AI CONFIDENCE %", ascending=False)
            
            # NSE Filters
            st.markdown("### 🔽 Filter & Export")
            col_f1, col_f2, col_f3, col_f4 = st.columns(4)
            
            with col_f1:
                nse_min_conf = st.slider("Min Confidence %", 0, 100, 40, step=5, key="nse_conf")
            with col_f2:
                nse_signal = st.selectbox("Signal", ["ALL", "BUY", "SELL", "STRONG ONLY"], key="nse_signal")
            with col_f3:
                nse_bias = st.selectbox("Next Candle", ["ALL", "BUY", "SELL", "NEUTRAL"], key="nse_bias")
            with col_f4:
                nse_sort = st.selectbox("Sort By", ["AI CONFIDENCE %", "LTP", "RVOL", "RISK:REWARD"], key="nse_sort")
            
            # Apply NSE filters
            nse_filtered = nse_sorted.copy()
            nse_filtered = nse_filtered[nse_filtered["AI CONFIDENCE %"] >= nse_min_conf]
            
            if nse_signal != "ALL":
                if nse_signal == "BUY":
                    nse_filtered = nse_filtered[nse_filtered["AI SIGNAL"].str.contains("BUY", na=False)]
                elif nse_signal == "SELL":
                    nse_filtered = nse_filtered[nse_filtered["AI SIGNAL"].str.contains("SELL", na=False)]
                elif nse_signal == "STRONG ONLY":
                    nse_filtered = nse_filtered[nse_filtered["AI SIGNAL"].str.contains("STRONG", na=False)]
            
            if nse_bias != "ALL":
                nse_filtered = nse_filtered[nse_filtered["NEXT CANDLE BIAS"].str.contains(nse_bias, na=False)]
            
            if nse_sort != "AI CONFIDENCE %":
                nse_filtered = nse_filtered.sort_values(nse_sort, ascending=False, na_position='last')
            
            st.subheader(f"✅ NSE Signals: {len(nse_filtered)}")
            
            if len(nse_filtered) > 0:
                st.dataframe(nse_filtered, use_container_width=True, height=500)
                
                # NSE Downloads
                col_d1, col_d2, col_d3 = st.columns(3)
                with col_d1:
                    excel_data = _format_excel_output(nse_filtered, "NSE")
                    st.download_button("📥 Excel", excel_data,
                                     file_name=f"NSE_SIGNALS_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                                     mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                     key="nse_dl_excel")
                with col_d2:
                    csv_data = to_csv_bytes(nse_filtered)
                    st.download_button("📥 CSV", csv_data,
                                     file_name=f"NSE_SIGNALS_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                                     mime="text/csv", key="nse_dl_csv")
                with col_d3:
                    json_data = to_json_bytes(nse_filtered)
                    st.download_button("📥 JSON", json_data,
                                     file_name=f"NSE_SIGNALS_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                                     mime="application/json", key="nse_dl_json")
                
                # NSE Stats
                st.markdown("### 📊 Statistics")
                stat_c1, stat_c2, stat_c3, stat_c4, stat_c5 = st.columns(5)
                with stat_c1:
                    st.metric("Signals", len(nse_filtered))
                with stat_c2:
                    buy_c = len(nse_filtered[nse_filtered["AI SIGNAL"].str.contains("BUY", na=False)])
                    st.metric("BUY", buy_c)
                with stat_c3:
                    sell_c = len(nse_filtered[nse_filtered["AI SIGNAL"].str.contains("SELL", na=False)])
                    st.metric("SELL", sell_c)
                with stat_c4:
                    avg_conf = nse_filtered["AI CONFIDENCE %"].mean()
                    st.metric("Avg Conf", f"{avg_conf:.1f}%")
                with stat_c5:
                    avg_rr = nse_filtered[nse_filtered["RISK:REWARD"] > 0]["RISK:REWARD"].mean()
                    st.metric("Avg R:R", f"{avg_rr:.2f}" if not pd.isna(avg_rr) else "N/A")
            else:
                st.warning("No signals found with current filters")
        else:
            st.info("👈 Click 'SCAN NSE' to analyze stocks")
    
    # ════════════════════════════════════════════════════════════════════════════════
    # F&O STOCKS TAB
    # ════════════════════════════════════════════════════════════════════════════════
    with tab_fo:
        st.markdown("### F&O Stocks Scanner\nTechnical + options analysis for derivatives universe")
        
        col1, col2 = st.columns([3, 1])
        with col1:
            fo_limit = st.number_input("Scan limit (0=all)", min_value=0, max_value=len(fo_symbols),
                                      value=min(200, len(fo_symbols)), step=25, key="fo_limit")
        with col2:
            st.metric("Available", len(fo_symbols))
        
        fo_universe = fo_symbols if fo_limit == 0 else fo_symbols[:fo_limit]
        
        if st.button(f"🔍 SCAN F&O ({len(fo_universe)} stocks)", key="fo_run"):
            with st.spinner("Analyzing F&O stocks with options data…"):
                fo_results, fo_errors, fo_stats = run_fo_scan(fyers, fo_universe)
                st.session_state["fo_df"] = pd.DataFrame(fo_results) if fo_results else pd.DataFrame()
                st.session_state["fo_errors"] = fo_errors
                st.session_state["fo_stats"] = fo_stats
        
        # Display F&O summary
        if "fo_stats" in st.session_state:
            _display_scan_summary(st.session_state["fo_stats"])
        
        # F&O Results
        fo_df = st.session_state.get("fo_df")
        if fo_df is not None and not fo_df.empty:
            fo_sorted = fo_df.sort_values("AI CONFIDENCE %", ascending=False)
            
            # F&O Filters
            st.markdown("### 🔽 Filter & Export")
            col_f1, col_f2, col_f3, col_f4, col_f5 = st.columns(5)
            
            with col_f1:
                fo_min_conf = st.slider("Min Confidence %", 0, 100, 40, step=5, key="fo_conf")
            with col_f2:
                fo_signal = st.selectbox("Signal", ["ALL", "BUY", "SELL", "STRONG ONLY"], key="fo_signal")
            with col_f3:
                fo_bias = st.selectbox("Next Candle", ["ALL", "BUY", "SELL", "NEUTRAL"], key="fo_bias")
            with col_f4:
                fo_opt = st.selectbox("Options", ["ALL", "BULLISH", "BEARISH", "NEUTRAL"], key="fo_opt")
            with col_f5:
                fo_sort = st.selectbox("Sort By", ["AI CONFIDENCE %", "LTP", "RVOL", "PCR"], key="fo_sort")
            
            # Apply F&O filters
            fo_filtered = fo_sorted.copy()
            fo_filtered = fo_filtered[fo_filtered["AI CONFIDENCE %"] >= fo_min_conf]
            
            if fo_signal != "ALL":
                if fo_signal == "BUY":
                    fo_filtered = fo_filtered[fo_filtered["AI SIGNAL"].str.contains("BUY", na=False)]
                elif fo_signal == "SELL":
                    fo_filtered = fo_filtered[fo_filtered["AI SIGNAL"].str.contains("SELL", na=False)]
                elif fo_signal == "STRONG ONLY":
                    fo_filtered = fo_filtered[fo_filtered["AI SIGNAL"].str.contains("STRONG", na=False)]
            
            if fo_bias != "ALL":
                fo_filtered = fo_filtered[fo_filtered["NEXT CANDLE BIAS"].str.contains(fo_bias, na=False)]
            
            if fo_opt != "ALL":
                fo_filtered = fo_filtered[fo_filtered["OPTIONS BIAS"].str.contains(fo_opt, na=False)]
            
            if fo_sort == "PCR":
                fo_filtered = fo_filtered.sort_values("PCR", ascending=False, na_position='last')
            elif fo_sort != "AI CONFIDENCE %":
                fo_filtered = fo_filtered.sort_values(fo_sort, ascending=False, na_position='last')
            
            st.subheader(f"✅ F&O Signals: {len(fo_filtered)}")
            
            if len(fo_filtered) > 0:
                st.dataframe(fo_filtered, use_container_width=True, height=500)
                
                # F&O Downloads
                col_d1, col_d2, col_d3 = st.columns(3)
                with col_d1:
                    excel_data = _format_excel_output(fo_filtered, "FO")
                    st.download_button("📥 Excel", excel_data,
                                     file_name=f"FO_SIGNALS_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                                     mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                     key="fo_dl_excel")
                with col_d2:
                    csv_data = to_csv_bytes(fo_filtered)
                    st.download_button("📥 CSV", csv_data,
                                     file_name=f"FO_SIGNALS_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                                     mime="text/csv", key="fo_dl_csv")
                with col_d3:
                    json_data = to_json_bytes(fo_filtered)
                    st.download_button("📥 JSON", json_data,
                                     file_name=f"FO_SIGNALS_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                                     mime="application/json", key="fo_dl_json")
                
                # F&O Stats
                st.markdown("### 📊 Statistics")
                stat_c1, stat_c2, stat_c3, stat_c4, stat_c5, stat_c6 = st.columns(6)
                with stat_c1:
                    st.metric("Signals", len(fo_filtered))
                with stat_c2:
                    buy_c = len(fo_filtered[fo_filtered["AI SIGNAL"].str.contains("BUY", na=False)])
                    st.metric("BUY", buy_c)
                with stat_c3:
                    sell_c = len(fo_filtered[fo_filtered["AI SIGNAL"].str.contains("SELL", na=False)])
                    st.metric("SELL", sell_c)
                with stat_c4:
                    avg_conf = fo_filtered["AI CONFIDENCE %"].mean()
                    st.metric("Avg Conf", f"{avg_conf:.1f}%")
                with stat_c5:
                    avg_pcr = fo_filtered[fo_filtered["PCR"] > 0]["PCR"].mean()
                    st.metric("Avg PCR", f"{avg_pcr:.2f}" if not pd.isna(avg_pcr) else "N/A")
                with stat_c6:
                    bullish_opt = len(fo_filtered[fo_filtered["OPTIONS BIAS"].str.contains("BULLISH", na=False)])
                    st.metric("Bullish Opt", bullish_opt)
            else:
                st.warning("No signals found with current filters")
        else:
            st.info("👈 Click 'SCAN F&O' to analyze stocks")
    
    gc.collect()

# ════════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    access_token = os.environ.get("FYERS_ACCESS_TOKEN")
    if not access_token:
        st.error("❌ FYERS_ACCESS_TOKEN not set")
        st.stop()
    
    try:
        from fyers_api import fyersModel
        app_id = os.environ.get("FYERS_APP_ID", "DEMO")
        fyers = fyersModel.FyersModel(client_id=app_id, token=access_token, log_path="")
        show_scanner(fyers)
    except ImportError:
        st.error("❌ fyers-api not installed")
    except Exception as e:
        st.error(f"❌ Error: {e}")
