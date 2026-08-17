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
logger = logging.getLogger("nse_scanner_v15")
logger.setLevel(logging.INFO)

def _candle_signal_timestamp(df, is_daily: bool = False) -> Tuple[str, str]:
    ts = df["Time"].iloc[-1]
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts_ist = ts.tz_convert(IST)
    return ts_ist.strftime("%d-%b-%Y"), ts_ist.strftime("%H:%M:%S") + " IST"

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
              "current_low":None, "prev_high":None, "prev_low":None}
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
        elif lh and ll:
            result["type"], result["trend"] = "LH/LL", "BEARISH"
        elif hh:
            result["type"], result["trend"] = "HH", "BULLISH"
        elif ll:
            result["type"], result["trend"] = "LL", "BEARISH"
        elif hl:
            result["type"], result["trend"] = "HL", "BULLISH"
        elif lh:
            result["type"], result["trend"] = "LH", "BEARISH"
    return result


def detect_choch(df) -> Dict[str, Any]:
    """Confirmed CHoCH: close breaks the last confirmed opposing swing."""
    ph, pl = _confirmed_pivots(df)
    out = {"bullish_choch":False, "bearish_choch":False, "choch_price":None,
           "choch_type":"NONE", "confirmation":"NONE"}
    if len(ph) < 2 or len(pl) < 2 or len(df) < 10:
        out["confirmation"] = "PENDING"
        return out
    close = float(df["Close"].iloc[-1])
    bearish_structure = ph[-1][1] < ph[-2][1] and pl[-1][1] < pl[-2][1]
    bullish_structure = ph[-1][1] > ph[-2][1] and pl[-1][1] > pl[-2][1]
    if bearish_structure and close > ph[-1][1]:
        out.update(bullish_choch=True, choch_price=ph[-1][1], choch_type="BULLISH_CHoCH", confirmation="CONFIRMED")
    elif bullish_structure and close < pl[-1][1]:
        out.update(bearish_choch=True, choch_price=pl[-1][1], choch_type="BEARISH_CHoCH", confirmation="CONFIRMED")
    return out


def detect_mss(df) -> Dict[str, Any]:
    """Confirmed Market Structure Shift using a close beyond a confirmed pivot."""
    ph, pl = _confirmed_pivots(df)
    out = {"bullish_mss":False, "bearish_mss":False, "mss_type":"NONE", "confirmation":"NONE"}
    if len(ph) < 2 or len(pl) < 2 or len(df) < 10:
        out["confirmation"] = "PENDING"
        return out
    close = float(df["Close"].iloc[-1])
    if ph[-1][1] < ph[-2][1] and pl[-1][1] < pl[-2][1] and close > ph[-1][1]:
        out.update(bullish_mss=True, mss_type="BULLISH_MSS", confirmation="CONFIRMED")
    elif ph[-1][1] > ph[-2][1] and pl[-1][1] > pl[-2][1] and close < pl[-1][1]:
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
    """Load F&O eligible stocks from Fyers or NSE."""
    try:
        resp = requests.get("https://public.fyers.in/sym_details/NSE_FO.csv", timeout=20)
        resp.raise_for_status()
        
        lines = [ln for ln in resp.text.strip().split("\n") if ln.strip()]
        if not lines:
            return []
        
        sample = lines[:min(500, len(lines))]
        split_sample = [ln.split(",") for ln in sample]
        max_cols = max((len(p) for p in split_sample), default=0)
        best_col, best_hits = None, 0
        
        for col_idx in range(max_cols):
            hits = sum(1 for parts in split_sample if len(parts) > col_idx and parts[col_idx].strip().endswith("-EQ"))
            if hits > best_hits:
                best_col, best_hits = col_idx, hits
        
        if best_col is None or best_hits == 0:
            return []
        
        fo_stocks = []
        for line in lines:
            parts = line.split(",")
            if len(parts) <= best_col:
                continue
            sym = parts[best_col].strip()
            if sym.endswith("-EQ"):
                fo_stocks.append(sym)
        
        return sorted(set(_validate_symbols(fo_stocks)))
    
    except Exception as e:
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
    """Fetch OHLCV data for a specific timeframe."""
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
        
        if len(df) > 1:
            last_time = df["Time"].iloc[-1]
            candle_age = (_now_ist() - last_time).total_seconds() / 60
            res_minutes = int(resolution)
            if candle_age < res_minutes + 1:
                df = df.iloc[:-1].reset_index(drop=True)
        
        if len(df) < 10:
            return None
        
        return df
    
    except Exception as e:
        return None

# ════════════════════════════════════════════════════════════════════════════════
# TIMEFRAME ANALYSIS ENGINE (NEW)
# ════════════════════════════════════════════════════════════════════════════════
def analyze_timeframe(fyers, symbol: str, resolution: str) -> Dict[str, Any]:
    """Analyze a specific timeframe."""
    df = _fetch_timeframe_data(fyers, symbol, resolution, lookback_days=30)
    
    if df is None or len(df) < 10:
        return {
            "timeframe": resolution,
            "status": "DATA_UNAVAILABLE",
            "data": None,
        }
    
    try:
        rsi = calculate_rsi(df["Close"])
        macd_line, macd_sig, macd_hist = calculate_macd(df["Close"])
        atr = calculate_atr(df)
        vwap = calculate_vwap(df)
        ema9 = calculate_ema(df["Close"], 9)
        ema21 = calculate_ema(df["Close"], 21)
        ema50 = calculate_ema(df["Close"], 50)
        ema200 = calculate_ema(df["Close"], 200)
        
        structure = detect_structure(df)
        choch = detect_choch(df)
        mss = detect_mss(df)
        swings = find_swing_highs_lows(df)
        
        vol_avg20 = float(df["Volume"].tail(20).mean()) if "Volume" in df.columns else 0
        last_vol = float(df["Volume"].iloc[-1]) if "Volume" in df.columns else 0
        rvol = round(last_vol / vol_avg20, 2) if vol_avg20 > 0 else 0.0
        
        last_close = float(df["Close"].iloc[-1])
        last_high = float(df["High"].iloc[-1])
        last_low = float(df["Low"].iloc[-1])
        last_open = float(df["Open"].iloc[-1])
        
        ema_trend = "BULLISH" if ema9.iloc[-1] > ema21.iloc[-1] > ema50.iloc[-1] else "BEARISH" if ema9.iloc[-1] < ema21.iloc[-1] < ema50.iloc[-1] else "NEUTRAL"
        
        rsi_val = float(rsi.iloc[-1])
        rsi_overbought = rsi_val > 70
        rsi_oversold = rsi_val < 30
        
        macd_bullish = macd_line.iloc[-1] > macd_sig.iloc[-1]
        
        return {
            "timeframe": resolution,
            "status": "OK",
            "data": {
                "last_close": last_close,
                "last_high": last_high,
                "last_low": last_low,
                "last_open": last_open,
                "structure_type": structure["type"],
                "structure_trend": structure["trend"],
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
# OPTIONS CHAIN ANALYSIS (NEW) - ✅ FIXED VERSION
# ════════════════════════════════════════════════════════════════════════════════
def _fyers_optionchain_request(fyers, symbol: str, strikecount: int = OPTIONS_STRIKE_COUNT, timestamp: str = "", greeks: bool = True):
    """✅ FIXED: More robust option chain API request"""
    app_id = getattr(fyers, "client_id", None) or FYERS_APP_ID
    token = getattr(fyers, "token", None) or os.environ.get("FYERS_ACCESS_TOKEN", "")
    
    # Try REST API first with better error handling
    if app_id and token:
        try:
            headers = {"Authorization": f"{app_id}:{token}"}
            params = {
                "symbol": symbol,
                "strikecount": min(int(strikecount), 50),
                "greeks": "1" if greeks else "0"
            }
            if timestamp and timestamp.strip():
                params["timestamp"] = str(timestamp)
            
            r = requests.get(
                "https://api-t1.fyers.in/data/options-chain-v3",
                headers=headers,
                params=params,
                timeout=OPTIONS_HTTP_TIMEOUT
            )
            r.raise_for_status()
            resp = r.json()
            
            if isinstance(resp, dict):
                return resp
        except requests.exceptions.Timeout:
            pass
        except requests.exceptions.ConnectionError:
            pass
        except requests.exceptions.RequestException:
            pass
        except (ValueError, json.JSONDecodeError):
            pass
    
    # Fallback to SDK with better detection
    try:
        fn = getattr(fyers, "option_chain", None)
        if fn and callable(fn):
            try:
                result = fn(symbol=symbol, strikecount=min(int(strikecount), 50))
                if result:
                    return result
            except TypeError:
                pass
        
        fn = getattr(fyers, "optionchain", None)
        if fn and callable(fn):
            try:
                result = fn(symbol=symbol, strikecount=min(int(strikecount), 50))
                if result:
                    return result
            except TypeError:
                pass
    except Exception:
        pass
    
    # Return empty response instead of raising
    return {
        "s": "error",
        "message": "FYERS option-chain API not available",
        "data": {"optionsChain": [], "expiryData": []}
    }


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
    """✅ FIXED: Robust options chain data fetching"""
    empty = {
        "status": "DATA_UNAVAILABLE",
        "message": "No live option-chain data",
        "expiry": None,
        "spot": None,
        "atm_strike": None,
        "ce_oi": 0,
        "pe_oi": 0,
        "ce_oi_change": 0,
        "pe_oi_change": 0,
        "pcr": None,
        "max_pain": None,
        "ce_volume": 0,
        "pe_volume": 0,
        "call_writing": False,
        "put_writing": False,
        "call_unwinding": False,
        "put_unwinding": False,
        "options_bias": "NEUTRAL",
        "chain": [],
        "expiry_data": []
    }
    
    try:
        try:
            resp = _fyers_optionchain_request(fyers, symbol, timestamp=expiry_timestamp)
        except Exception as e:
            empty["message"] = f"API request failed: {str(e)[:100]}"
            return empty
        
        if not isinstance(resp, dict):
            empty["message"] = "Invalid response type"
            return empty
        
        resp_status = resp.get("s")
        if resp_status not in (None, "ok", "success"):
            msg = resp.get("message", f"API status: {resp_status}")
            empty["message"] = str(msg)[:150]
            return empty
        
        data = resp.get("data")
        if data is None:
            data = resp
        
        if not isinstance(data, dict):
            empty["message"] = "Malformed response data"
            return empty
        
        chain = data.get("optionsChain")
        if not isinstance(chain, list):
            chain = []
        
        expiry_data = data.get("expiryData")
        if not isinstance(expiry_data, list):
            expiry_data = []
        
        if not expiry_timestamp and expiry_data and len(expiry_data) > 0:
            try:
                nearest = expiry_data[0]
                expiry_timestamp = str(nearest.get("expiry", ""))
                if expiry_timestamp and expiry_timestamp != "":
                    resp = _fyers_optionchain_request(fyers, symbol, timestamp=expiry_timestamp)
                    if isinstance(resp, dict):
                        data = resp.get("data", resp)
                        chain = data.get("optionsChain", chain) if isinstance(data, dict) else chain
            except Exception:
                pass
        
        spot = None
        try:
            spot_row = next((x for x in chain if isinstance(x, dict) and x.get("option_type", "") == ""), None)
            if spot_row and "ltp" in spot_row:
                ltp = spot_row.get("ltp")
                if ltp is not None:
                    spot = float(ltp)
        except Exception:
            pass
        
        legs = []
        try:
            for x in chain:
                if not isinstance(x, dict):
                    continue
                if x.get("option_type") not in ("CE", "PE"):
                    continue
                sp = x.get("strike_price")
                if sp is None or sp == -1 or sp == "":
                    continue
                legs.append(x)
        except Exception:
            pass
        
        if not legs:
            empty["chain"] = []
            empty["expiry_data"] = expiry_data
            return empty
        
        strikes = []
        try:
            for x in legs:
                sp = x.get("strike_price")
                if sp is not None and sp != -1 and sp != "":
                    strikes.append(float(sp))
            strikes = sorted(set(strikes))
        except Exception:
            strikes = []
        
        atm = None
        if strikes and spot is not None:
            try:
                atm = min(strikes, key=lambda k: abs(k - spot))
            except Exception:
                pass
        
        call_oi = 0.0
        put_oi = 0.0
        call_chg = 0.0
        put_chg = 0.0
        ce_vol = 0.0
        pe_vol = 0.0
        
        try:
            for x in legs:
                ot = x.get("option_type")
                
                if ot == "CE":
                    oi = x.get("oi") or x.get("openInterest", 0)
                    if oi:
                        call_oi += float(oi)
                    oich = x.get("oich") or x.get("oichange", 0)
                    if oich:
                        call_chg += float(oich)
                    vol = x.get("volume") or x.get("vol", 0)
                    if vol:
                        ce_vol += float(vol)
                
                elif ot == "PE":
                    oi = x.get("oi") or x.get("openInterest", 0)
                    if oi:
                        put_oi += float(oi)
                    oich = x.get("oich") or x.get("oichange", 0)
                    if oich:
                        put_chg += float(oich)
                    vol = x.get("volume") or x.get("vol", 0)
                    if vol:
                        pe_vol += float(vol)
        except Exception:
            pass
        
        pcr = None
        try:
            if call_oi > 0:
                pcr = round(put_oi / call_oi, 2)
        except Exception:
            pass
        
        max_pain = None
        try:
            if strikes:
                max_pain = _calculate_max_pain(legs)
        except Exception:
            pass
        
        ce_writing = 0
        pe_writing = 0
        ce_unwind = 0
        pe_unwind = 0
        
        try:
            if atm is not None and len(strikes) > 1:
                strike_diff = max((strikes[1] - strikes[0]), 1)
                near_range = strike_diff * 3
                
                for x in legs:
                    if not isinstance(x, dict):
                        continue
                    
                    sp = x.get("strike_price")
                    if sp is None or abs(float(sp) - atm) > near_range:
                        continue
                    
                    ot = x.get("option_type")
                    oich = float(x.get("oich", 0) or 0)
                    ltpch = float(x.get("ltpch", 0) or 0)
                    
                    if ot == "CE":
                        if oich > 0 and ltpch < 0:
                            ce_writing += 1
                        elif oich < 0 and ltpch > 0:
                            ce_unwind += 1
                    
                    elif ot == "PE":
                        if oich > 0 and ltpch < 0:
                            pe_writing += 1
                        elif oich < 0 and ltpch > 0:
                            pe_unwind += 1
        except Exception:
            pass
        
        bias = "NEUTRAL"
        try:
            bullish_signals = 0
            bearish_signals = 0
            
            if pcr is not None:
                if pcr >= 1.05:
                    bullish_signals += 1
                elif pcr <= 0.80:
                    bearish_signals += 1
            
            if ce_writing > pe_writing:
                bearish_signals += 1
            elif pe_writing > ce_writing:
                bullish_signals += 1
            
            if ce_unwind > pe_unwind:
                bullish_signals += 1
            elif pe_unwind > ce_unwind:
                bearish_signals += 1
            
            if bullish_signals > bearish_signals and bullish_signals > 0:
                bias = "BULLISH"
            elif bearish_signals > bullish_signals and bearish_signals > 0:
                bias = "BEARISH"
            else:
                bias = "NEUTRAL"
        except Exception:
            bias = "NEUTRAL"
        
        return {
            "status": "OK",
            "message": "Live FYERS option chain",
            "expiry": expiry_timestamp or (expiry_data[0].get("date") if expiry_data else None),
            "spot": spot,
            "atm_strike": atm,
            "ce_oi": call_oi,
            "pe_oi": put_oi,
            "ce_oi_change": call_chg,
            "pe_oi_change": put_chg,
            "pcr": pcr,
            "max_pain": max_pain,
            "ce_volume": ce_vol,
            "pe_volume": pe_vol,
            "call_writing": ce_writing > 0,
            "put_writing": pe_writing > 0,
            "call_unwinding": ce_unwind > 0,
            "put_unwinding": pe_unwind > 0,
            "options_bias": bias,
            "chain": legs,
            "expiry_data": expiry_data,
        }
    
    except Exception as e:
        empty["status"] = "ERROR"
        empty["message"] = f"Unexpected error: {str(e)[:150]}"
        return empty

# ════════════════════════════════════════════════════════════════════════════════
# MASTER SIGNAL ENGINE (NEW)
# ════════════════════════════════════════════════════════════════════════════════
def calculate_master_signal(symbol: str, analysis_5m: Dict, analysis_15m: Dict, analysis_1h: Dict, options_data: Dict) -> Dict[str, Any]:
    """Calculate master signal using weighted scoring from multi-timeframe analysis."""
    reasons = []
    scores = {
        "5m_score": 0,
        "15m_score": 0,
        "1h_score": 0,
        "options_score": 50,
        "volume_score": 50,
    }
    
    # 5M ANALYSIS
    if analysis_5m.get("status") == "OK" and analysis_5m.get("data"):
        data_5m = analysis_5m["data"]
        
        score_5m = 50
        
        if data_5m["structure_trend"] == "BULLISH":
            score_5m += 15
            reasons.append("5M: Bullish structure")
        elif data_5m["structure_trend"] == "BEARISH":
            score_5m -= 15
            reasons.append("5M: Bearish structure")
        
        if data_5m["bullish_choch"]:
            score_5m += 10
            reasons.append("5M: Bullish CHoCH")
        elif data_5m["bearish_choch"]:
            score_5m -= 10
            reasons.append("5M: Bearish CHoCH")
        
        if data_5m["bullish_mss"]:
            score_5m += 10
            reasons.append("5M: Bullish MSS")
        elif data_5m["bearish_mss"]:
            score_5m -= 10
            reasons.append("5M: Bearish MSS")
        
        if data_5m["ema_trend"] == "BULLISH":
            score_5m += 5
        elif data_5m["ema_trend"] == "BEARISH":
            score_5m -= 5
        
        if data_5m["rsi"] > 60:
            score_5m += 3
        elif data_5m["rsi"] < 40:
            score_5m -= 3
        
        if data_5m["macd_bullish"]:
            score_5m += 3
        else:
            score_5m -= 3
        
        if data_5m["rvol"] > 1.5:
            score_5m += 2
        
        score_5m = max(0, min(100, score_5m))
        scores["5m_score"] = score_5m
    
    # 15M ANALYSIS
    if analysis_15m.get("status") == "OK" and analysis_15m.get("data"):
        data_15m = analysis_15m["data"]
        
        score_15m = 50
        
        if data_15m["structure_trend"] == "BULLISH":
            score_15m += 15
            reasons.append("15M: Bullish structure")
        elif data_15m["structure_trend"] == "BEARISH":
            score_15m -= 15
            reasons.append("15M: Bearish structure")
        
        if data_15m["bullish_choch"]:
            score_15m += 10
            reasons.append("15M: Bullish CHoCH")
        elif data_15m["bearish_choch"]:
            score_15m -= 10
            reasons.append("15M: Bearish CHoCH")
        
        if data_15m["bullish_mss"]:
            score_15m += 10
            reasons.append("15M: Bullish MSS")
        elif data_15m["bearish_mss"]:
            score_15m -= 10
            reasons.append("15M: Bearish MSS")
        
        if data_15m["ema_trend"] == "BULLISH":
            score_15m += 5
        elif data_15m["ema_trend"] == "BEARISH":
            score_15m -= 5
        
        if data_15m["rsi"] > 60:
            score_15m += 3
        elif data_15m["rsi"] < 40:
            score_15m -= 3
        
        if data_15m["macd_bullish"]:
            score_15m += 3
        else:
            score_15m -= 3
        
        if data_15m["rvol"] > 1.5:
            score_15m += 2
        
        score_15m = max(0, min(100, score_15m))
        scores["15m_score"] = score_15m
    
    # 1H ANALYSIS
    if analysis_1h.get("status") == "OK" and analysis_1h.get("data"):
        data_1h = analysis_1h["data"]
        
        score_1h = 50
        
        if data_1h["structure_trend"] == "BULLISH":
            score_1h += 15
            reasons.append("1H: Bullish trend")
        elif data_1h["structure_trend"] == "BEARISH":
            score_1h -= 15
            reasons.append("1H: Bearish trend")
        
        if data_1h["bullish_choch"]:
            score_1h += 10
            reasons.append("1H: Bullish CHoCH")
        elif data_1h["bearish_choch"]:
            score_1h -= 10
            reasons.append("1H: Bearish CHoCH")
        
        if data_1h["bullish_mss"]:
            score_1h += 10
            reasons.append("1H: Bullish MSS")
        elif data_1h["bearish_mss"]:
            score_1h -= 10
            reasons.append("1H: Bearish MSS")
        
        if data_1h["ema_trend"] == "BULLISH":
            score_1h += 5
        elif data_1h["ema_trend"] == "BEARISH":
            score_1h -= 5
        
        if data_1h["rvol"] > 1.5:
            score_1h += 2
        
        score_1h = max(0, min(100, score_1h))
        scores["1h_score"] = score_1h
    
    opt_bias = options_data.get("options_bias", "NEUTRAL") if isinstance(options_data, dict) else "NEUTRAL"
    if opt_bias == "BULLISH":
        scores["options_score"] = 80
        reasons.append("Options: Bullish bias")
    elif opt_bias == "BEARISH":
        scores["options_score"] = 20
        reasons.append("Options: Bearish bias")
    else:
        scores["options_score"] = 50

    if analysis_5m.get("status") == "OK" and analysis_5m.get("data"):
        d5 = analysis_5m["data"]
        vol_score = 50
        if d5.get("rvol", 0) >= 2.0:
            vol_score += 15
        elif d5.get("rvol", 0) >= 1.5:
            vol_score += 8
        if d5.get("last_close", 0) > d5.get("vwap", 0):
            vol_score += 10
        elif d5.get("last_close", 0) < d5.get("vwap", 0):
            vol_score -= 10
        scores["volume_score"] = max(0, min(100, vol_score))

    total_score = (scores["5m_score"] * 0.20 + scores["15m_score"] * 0.25 +
                   scores["1h_score"] * 0.25 + scores["options_score"] * 0.20 +
                   scores["volume_score"] * 0.10)

    tf_scores = [scores["5m_score"], scores["15m_score"], scores["1h_score"]]
    bullish_tfs = sum(x >= 60 for x in tf_scores)
    bearish_tfs = sum(x <= 40 for x in tf_scores)
    hard_conflict = bullish_tfs > 0 and bearish_tfs > 0

    confidence = round(abs(total_score - 50) * 2, 1)
    confidence = max(0, min(100, confidence))

    if hard_conflict and max(tf_scores) - min(tf_scores) >= 20:
        final_signal = "NEUTRAL / WAIT"
        confidence = min(confidence, 55.0)
        reasons.append("Multi-timeframe conflict: WAIT")
    elif total_score >= 72:
        final_signal = "STRONG BUY"
    elif total_score >= 60:
        final_signal = "BUY"
    elif total_score <= 28:
        final_signal = "STRONG SELL"
    elif total_score <= 40:
        final_signal = "SELL"
    else:
        final_signal = "NEUTRAL / WAIT"
    
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
    else:
        entry, sl, t1, t2, rr_ratio = None, None, None, None, None
    
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
# MASTER SIGNAL SCANNER WORKER (NEW)
# ════════════════════════════════════════════════════════════════════════════════
def _fetch_master_signal(fyers, symbol: str, fo_set=None):
    """Worker for master signal scanner."""
    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "") if isinstance(symbol, str) else str(symbol)
    
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid format"
    
    try:
        analysis_5m = analyze_timeframe(fyers, symbol, "5")
        analysis_15m = analyze_timeframe(fyers, symbol, "15")
        analysis_1h = analyze_timeframe(fyers, symbol, "60")
        
        if all(a.get("status") != "OK" for a in [analysis_5m, analysis_15m, analysis_1h]):
            return None, None
        
        is_fo = symbol in (fo_set or set())
        options_data = fetch_options_chain_data(fyers, symbol) if is_fo else {"status":"DATA_UNAVAILABLE", "options_bias":"NEUTRAL", "message":"Not F&O eligible"}
        
        master = calculate_master_signal(symbol, analysis_5m, analysis_15m, analysis_1h, options_data)
        
        ltp = None
        for analysis in [analysis_5m, analysis_15m, analysis_1h]:
            if analysis.get("status") == "OK" and analysis.get("data"):
                ltp = analysis["data"]["last_close"]
                break
        
        if ltp is None:
            return None, None
        
        data_5m = analysis_5m.get("data") if analysis_5m.get("status") == "OK" else {}
        data_15m = analysis_15m.get("data") if analysis_15m.get("status") == "OK" else {}
        data_1h = analysis_1h.get("data") if analysis_1h.get("status") == "OK" else {}
        
        return {
            "Stock": stock_ticker,
            "LTP": ltp,
            "5M Trend": data_5m.get("structure_trend", "N/A"),
            "15M Trend": data_15m.get("structure_trend", "N/A"),
            "1H Trend": data_1h.get("structure_trend", "N/A"),
            "5M Structure": data_5m.get("structure_type", "N/A"),
            "15M Structure": data_15m.get("structure_type", "N/A"),
            "1H Structure": data_1h.get("structure_type", "N/A"),
            "5M CHoCH": "✅" if data_5m.get("bullish_choch") else "❌" if data_5m.get("bearish_choch") else "−",
            "15M CHoCH": "✅" if data_15m.get("bullish_choch") else "❌" if data_15m.get("bearish_choch") else "−",
            "1H CHoCH": "✅" if data_1h.get("bullish_choch") else "❌" if data_1h.get("bearish_choch") else "−",
            "5M MSS": "✅" if data_5m.get("bullish_mss") else "❌" if data_5m.get("bearish_mss") else "−",
            "15M MSS": "✅" if data_15m.get("bullish_mss") else "❌" if data_15m.get("bearish_mss") else "−",
            "1H MSS": "✅" if data_1h.get("bullish_mss") else "❌" if data_1h.get("bearish_mss") else "−",
            "VWAP": round(data_5m.get("vwap", 0), 2) if data_5m.get("vwap") else "N/A",
            "EMA Trend": data_5m.get("ema_trend", "N/A"),
            "RSI": round(data_5m.get("rsi", 50), 1),
            "MACD": "🟢" if data_5m.get("macd_bullish") else "🔴",
            "RVOL": data_5m.get("rvol", 0),
            "CE OI": options_data.get("ce_oi", "N/A"),
            "PE OI": options_data.get("pe_oi", "N/A"),
            "PCR": options_data.get("pcr", "N/A"),
            "Max Pain": options_data.get("max_pain", "N/A"),
            "Options Bias": options_data.get("options_bias", "NEUTRAL"),
            "Final Signal": master["final_signal"],
            "Confidence %": master["confidence"],
            "Entry": master["entry"],
            "Stop Loss": master["stop_loss"],
            "Target 1": master["target1"],
            "Target 2": master["target2"],
            "Risk:Reward": master["rr_ratio"],
            "Signal Reason": " | ".join(master["reasons"][:2]) if master["reasons"] else "Multi-timeframe analysis",
        }, None
    
    except Exception as e:
        return None, f"{symbol}: analysis error ({type(e).__name__}: {str(e)[:50]})"

def run_master_signal_scan(fyers, symbols, fo_symbols=None):
    """Threaded scan for master signals."""
    symbols = _validate_symbols(symbols)
    fo_set = set(_validate_symbols(fo_symbols or []))
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning Master Signals 0 / {len(symbols)}")
    done = 0
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_master_signal, fyers, s, fo_set): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error ({type(e).__name__})"
                
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / max(len(symbols), 1), text=f"Scanning Master Signals {done} / {len(symbols)}")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress.empty()
    gc.collect()
    return results, errors, stats

# ════════════════════════════════════════════════════════════════════════════════
# F&O SCANNER WORKER (NEW) - ✅ FIXED VERSION
# ════════════════════════════════════════════════════════════════════════════════
def _fetch_fo_signal(fyers, symbol):
    """✅ FIXED: F&O Signal Generation"""
    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "")
    
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid format"
    
    try:
        df = _fetch_timeframe_data(fyers, symbol, "15", lookback_days=10)
        
        if df is None or len(df) < 20:
            return None, None
        
        last_close = float(df["Close"].iloc[-1])
        
        try:
            atr = _last_valid_atr(df, period=14)
            if atr <= 0 or pd.isna(atr):
                atr = last_close * 0.01
        except Exception:
            atr = last_close * 0.01
        
        try:
            rsi = calculate_rsi(df["Close"])
            rsi_val = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0
        except Exception:
            rsi_val = 50.0
        
        try:
            macd_line, macd_sig, _ = calculate_macd(df["Close"])
            macd_bullish = bool(macd_line.iloc[-1] > macd_sig.iloc[-1]) if not (pd.isna(macd_line.iloc[-1]) or pd.isna(macd_sig.iloc[-1])) else True
        except Exception:
            macd_bullish = True
        
        try:
            structure = detect_structure(df)
            if structure is None or structure.get("trend") not in ["BULLISH", "BEARISH"]:
                return None, None
            trend = structure["trend"]
        except Exception as e:
            return None, f"{symbol}: structure detection failed"
        
        try:
            vol_avg = float(df["Volume"].tail(10).mean())
            last_vol = float(df["Volume"].iloc[-1])
            rvol = round(last_vol / vol_avg, 2) if vol_avg > 0 else 1.0
        except Exception:
            rvol = 1.0
        
        try:
            swings = find_swing_highs_lows(df, lookback=20)
            swing_high = swings.get("swing_high")
            swing_low = swings.get("swing_low")
            
            if swing_high is None or swing_low is None or swing_high <= 0 or swing_low <= 0:
                swing_high = float(df["High"].tail(20).max())
                swing_low = float(df["Low"].tail(20).min())
        except Exception:
            swing_high = float(df["High"].tail(20).max())
            swing_low = float(df["Low"].tail(20).min())
        
        is_bullish = trend == "BULLISH"
        
        entry = round(last_close, 2)
        
        if is_bullish:
            sl = round(max(entry - 2.0 * atr, swing_low - atr * 0.5), 2)
        else:
            sl = round(min(entry + 2.0 * atr, swing_high + atr * 0.5), 2)
        
        if abs(entry - sl) < atr * 0.3:
            sl = entry - (2.0 * atr if is_bullish else -2.0 * atr)
        
        t1 = round(entry + 1.5 * atr, 2) if is_bullish else round(entry - 1.5 * atr, 2)
        
        risk = abs(entry - sl)
        reward = abs(t1 - entry)
        rr_ratio = round(reward / risk, 2) if risk > 0.01 else 0.0
        
        signal_date, signal_time = _candle_signal_timestamp(df, is_daily=False)
        
        confidence = min(95.0, max(35.0, 50 + abs(rsi_val - 50) * 0.5 + rvol * 8 + rr_ratio * 5))
        
        return {
            "Signal Date": signal_date,
            "Signal Time": signal_time,
            "Stock": stock_ticker,
            "LTP": entry,
            "Trend": "🟢 BULLISH" if is_bullish else "🔴 BEARISH",
            "Entry": entry,
            "Stop Loss": sl,
            "Target 1": t1,
            "Risk:Reward": rr_ratio,
            "RSI": round(rsi_val, 1),
            "MACD": "🟢 Bullish" if macd_bullish else "🔴 Bearish",
            "RVOL": rvol,
            "Confidence %": round(confidence, 1),
            "Reason": f"F&O {trend} trend with {rvol}x volume",
        }, None
    
    except Exception as e:
        return None, f"{symbol}: analysis error ({type(e).__name__}: {str(e)[:50]})"

def run_fo_scanner(fyers, symbols):
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
                    res, err = None, f"{futures[future]}: worker error ({type(e).__name__})"
                
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / max(len(symbols), 1), text=f"Scanning F&O Stocks {done} / {len(symbols)}")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress.empty()
    gc.collect()
    return results, errors, stats

# ════════════════════════════════════════════════════════════════════════════════
# 15-MIN REVERSAL SCANNER (ORIGINAL - PRESERVED)
# ════════════════════════════════════════════════════════════════════════════════
def _detect_reversal_zone(df):
    """Original reversal detection logic - PRESERVED"""
    if len(df) < 30:
        return {"type": "NONE", "price": None, "strength": None}
    
    d = df.reset_index(drop=True).copy()
    close = d["Close"]
    high = d["High"]
    low = d["Low"]
    
    atr_val = _last_valid_atr(d, REVERSAL_ATR_LENGTH)
    reversal_threshold = max(
        close.iloc[-1] * REVERSAL_MIN_MOVE_PCT / 100.0,
        REVERSAL_ATR_MULTIPLIER * atr_val
    )
    
    lookback = 20
    recent = d.tail(lookback)
    recent_high = float(recent["High"].max())
    recent_low = float(recent["Low"].min())
    last_close = float(close.iloc[-1])
    
    if abs(recent_high - last_close) < reversal_threshold * 1.5:
        if close.iloc[-1] < close.iloc[-2]:
            return {
                "type": "SELL",
                "price": round(recent_high, 2),
                "strength": "Strong" if abs(recent_high - last_close) < reversal_threshold * 0.5 else "Medium",
                "distance_pct": round(abs(recent_high - last_close) / recent_high * 100, 2),
            }
    
    if abs(last_close - recent_low) < reversal_threshold * 1.5:
        if close.iloc[-1] > close.iloc[-2]:
            return {
                "type": "BUY",
                "price": round(recent_low, 2),
                "strength": "Strong" if abs(last_close - recent_low) < reversal_threshold * 0.5 else "Medium",
                "distance_pct": round(abs(last_close - recent_low) / recent_low * 100, 2),
            }
    
    return {"type": "NONE", "price": None, "strength": None}

def _fetch_15min_reversal_signal(fyers, symbol):
    """Original 15-min reversal worker - PRESERVED"""
    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "") if isinstance(symbol, str) else str(symbol)
    
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid format"
    
    date_from = (datetime.today() - timedelta(days=REVERSAL_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")
    
    resp, err = _safe_history(fyers, {
        "symbol": symbol,
        "resolution": REVERSAL_RESOLUTION,
        "date_format": "1",
        "range_from": date_from,
        "range_to": date_to,
        "cont_flag": "1",
    })
    
    if err:
        return None, f"{symbol}: {err}"
    
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 30:
        return None, None
    
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"]).sort_values("Time").reset_index(drop=True)
        
        if len(df) < 30:
            return None, None
        
        if len(df) > 0:
            last_time = df["Time"].iloc[-1]
            candle_age = (_now_ist() - last_time).total_seconds() / 60
            if candle_age < REVERSAL_LOOKBACK_DAYS * 1440:
                if candle_age < 15:
                    df = df.iloc[:-1].reset_index(drop=True)
        
        if len(df) < 30:
            return None, None
        
        reversal = _detect_reversal_zone(df)
        if reversal["type"] == "NONE":
            return None, None
        
        last_close = float(df["Close"].iloc[-1])
        atr = _last_valid_atr(df)
        rsi_val = float(calculate_rsi(df["Close"]).iloc[-1])
        macd_line, macd_sig, _ = calculate_macd(df["Close"])
        macd_bullish = bool(macd_line.iloc[-1] > macd_sig.iloc[-1])
        
        is_buy = reversal["type"] == "BUY"
        entry = round(last_close, 2)
        sl = round(entry - 1.5 * atr, 2) if is_buy else round(entry + 1.5 * atr, 2)
        t1 = round(entry + 1.5 * atr, 2) if is_buy else round(entry - 1.5 * atr, 2)
        t2 = round(entry + 2.5 * atr, 2) if is_buy else round(entry - 2.5 * atr, 2)
        
        risk = abs(entry - sl)
        reward = abs(t1 - entry)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0
        
        vol_avg20 = float(df["Volume"].tail(20).mean())
        rvol = round(float(df["Volume"].iloc[-1] / vol_avg20), 2) if vol_avg20 > 0 else 0.0
        
        signal_date_str, signal_time_str = _candle_signal_timestamp(df, is_daily=False)
        
        confidence = min(95.0, max(35.0, 50 + abs(rsi_val - 50) * 0.5 + rvol * 8 + rr_ratio * 5))
        
        return {
            "Signal Date": signal_date_str,
            "Signal Time": signal_time_str,
            "Stock": stock_ticker,
            "LTP": entry,
            "Type": "🟢 REVERSAL BUY" if is_buy else "🔴 REVERSAL SELL",
            "Entry": entry,
            "Stop Loss": sl,
            "Target 1": t1,
            "Target 2": t2,
            "Risk:Reward": rr_ratio,
            "Confidence %": confidence,
            "RSI": round(rsi_val, 1),
            "MACD": "🟢 Bullish" if macd_bullish else "🔴 Bearish",
            "RVOL": round(rvol, 2),
            "Zone Price": reversal["price"],
            "Zone Strength": reversal["strength"],
            "Distance %": reversal.get("distance_pct"),
            "Reason": f"15-min reversal zone at {reversal['price']} ({reversal['strength']} signal)",
        }, None
        
    except Exception as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"

def run_15min_reversal_scan(fyers, symbols):
    """Original 15-min reversal scan - PRESERVED"""
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning 15-Min Reversals 0 / {len(symbols)}")
    done = 0
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_15min_reversal_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error ({type(e).__name__})"
                
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / max(len(symbols), 1), text=f"Scanning 15-Min Reversals {done} / {len(symbols)}")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress.empty()
    gc.collect()
    return results, errors, stats

# ════════════════════════════════════════════════════════════════════════════════
# VOLUME BIG MOVEMENT SCANNER (ORIGINAL - PRESERVED)
# ════════════════════════════════════════════════════════════════════════════════
def _detect_volume_big_move(df):
    """Original volume detection - PRESERVED"""
    if len(df) < VOL_BIGMOVE_LOOKBACK:
        return "NONE", 0.0
    
    last = df.iloc[-1]
    recent = df.tail(VOL_BIGMOVE_LOOKBACK)
    
    vol_avg = float(recent["Volume"].mean())
    last_vol = float(last["Volume"])
    rvol = last_vol / vol_avg if vol_avg > 0 else 0.0
    
    if rvol < VOL_BIGMOVE_MIN_RVOL:
        return "NONE", 0.0
    
    body = abs(float(last["Close"]) - float(last["Open"]))
    body_pct = (body / float(last["Open"]) * 100) if last["Open"] > 0 else 0.0
    
    if body_pct < VOL_BIGMOVE_MIN_BODY_PCT:
        return "NONE", 0.0
    
    atr = _last_valid_atr(df)
    
    if float(last["Close"]) > float(last["Open"]) and body > atr * 0.6:
        confidence = min(95.0, 40 + (body_pct * 5) + (rvol * 8))
        return "BIG_UP", confidence
    
    if float(last["Close"]) < float(last["Open"]) and body > atr * 0.6:
        confidence = min(95.0, 40 + (body_pct * 5) + (rvol * 8))
        return "BIG_DOWN", confidence
    
    return "NONE", 0.0

def _fetch_volume_big_move_signal(fyers, symbol):
    """Original volume big move worker - PRESERVED"""
    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "") if isinstance(symbol, str) else str(symbol)
    
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid format"
    
    resp, err = _safe_history(fyers, {
        "symbol": symbol,
        "resolution": "D",
        "date_format": "1",
        "range_from": DATE_FROM,
        "range_to": DATE_TO,
        "cont_flag": "1",
    })
    
    if err:
        return None, f"{symbol}: {err}"
    
    candles = resp.get("candles") if resp else None
    if not candles or len(candles) < 30:
        return None, None
    
    try:
        df = pd.DataFrame(candles, columns=["Time", "Open", "High", "Low", "Close", "Volume"])
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        df[["Open", "High", "Low", "Close", "Volume"]] = df[["Open", "High", "Low", "Close", "Volume"]].apply(pd.to_numeric, errors="coerce")
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        
        if len(df) < 30:
            return None, None
        
        move_type, confidence = _detect_volume_big_move(df)
        if move_type == "NONE":
            return None, None
        
        last_close = float(df["Close"].iloc[-1])
        last_open = float(df["Open"].iloc[-1])
        body = abs(last_close - last_open)
        body_pct = (body / last_open * 100) if last_open > 0 else 0.0
        atr = _last_valid_atr(df)
        
        vol_avg = float(df["Volume"].tail(20).mean())
        last_vol = float(df["Volume"].iloc[-1])
        rvol = round(last_vol / vol_avg, 2) if vol_avg > 0 else 0.0
        
        rsi_val = float(calculate_rsi(df["Close"]).iloc[-1])
        macd_line, macd_sig, _ = calculate_macd(df["Close"])
        macd_bullish = bool(macd_line.iloc[-1] > macd_sig.iloc[-1])
        
        is_up = move_type == "BIG_UP"
        entry = round(last_close, 2)
        sl = round(entry - 2.0 * atr, 2) if is_up else round(entry + 2.0 * atr, 2)
        t1 = round(entry + 2.0 * atr, 2) if is_up else round(entry - 2.0 * atr, 2)
        
        risk = abs(entry - sl)
        reward = abs(t1 - entry)
        rr_ratio = round(reward / risk, 2) if risk > 0 else 0.0
        
        signal_date_str, signal_time_str = _candle_signal_timestamp(df, is_daily=True)
        
        return {
            "Signal Date": signal_date_str,
            "Signal Time": signal_time_str,
            "Stock": stock_ticker,
            "LTP": entry,
            "Type": "🟢 BIG UP MOVE" if is_up else "🔴 BIG DOWN MOVE",
            "Entry": entry,
            "Stop Loss": sl,
            "Target 1": t1,
            "Risk:Reward": rr_ratio,
            "Body %": round(body_pct, 2),
            "RVOL": rvol,
            "Confidence %": round(confidence, 1),
            "RSI": round(rsi_val, 1),
            "MACD": "🟢 Bullish" if macd_bullish else "🔴 Bearish",
            "Reason": f"Volume {rvol}x spike with {body_pct:.2f}% body move",
        }, None
        
    except Exception as e:
        return None, f"{symbol}: analysis error ({type(e).__name__})"

def run_volume_big_move_scan(fyers, symbols):
    """Original volume big move scan - PRESERVED"""
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning Volume Big Moves 0 / {len(symbols)}")
    done = 0
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_volume_big_move_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: worker error ({type(e).__name__})"
                
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / max(len(symbols), 1), text=f"Scanning Volume Big Moves {done} / {len(symbols)}")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress.empty()
    gc.collect()
    return results, errors, stats

def _options_summary_dataframe(options_data: Dict[str, Any]) -> pd.DataFrame:
    rows = []
    for x in options_data.get("chain", []):
        rows.append({
            "Symbol": x.get("symbol"), "Strike": x.get("strike_price"), "Type": x.get("option_type"),
            "LTP": x.get("ltp"), "OI": x.get("oi"), "OI Change": x.get("oich"),
            "Volume": x.get("volume"), "Bid": x.get("bid"), "Ask": x.get("ask"),
            "IV": (x.get("greeks") or {}).get("iv"), "Delta": (x.get("greeks") or {}).get("delta")
        })
    return pd.DataFrame(rows)


def _master_view(master_df, columns):
    if master_df is None or master_df.empty:
        return pd.DataFrame(columns=columns)
    return master_df[[c for c in columns if c in master_df.columns]].copy()

# ════════════════════════════════════════════════════════════════════════════════
# EXPORT UTILITIES
# ════════════════════════════════════════════════════════════════════════════════
def to_excel_bytes(dfs_dict: Dict[str, pd.DataFrame]) -> bytes:
    """Export multiple dataframes to Excel with separate sheets"""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for sheet_name, df in dfs_dict.items():
            safe_name = sheet_name[:31]
            if not df.empty:
                df.to_excel(writer, index=False, sheet_name=safe_name)
    buf.seek(0)
    return buf.getvalue()

def to_csv_bytes(df) -> bytes:
    return df.to_csv(index=False).encode("utf-8")

def to_json_bytes(df) -> bytes:
    return df.to_json(orient="records", indent=2, force_ascii=False).encode("utf-8")

# ════════════════════════════════════════════════════════════════════════════════
# MAIN APP
# ════════════════════════════════════════════════════════════════════════════════
def show_scanner(fyers) -> None:
    """Streamlit main app - NSE AI PRO V16 - FULLY FIXED VERSION"""
    
    st.title("🚀 NSE AI PRO V16 — 2300 Stock + F&O + Options + MTF Structure")
    st.caption(f"🕒 Current Time (IST): {_now_ist().strftime('%d-%b-%Y %H:%M:%S')} IST | ✅ FIXED VERSION")
    
    all_symbols = load_nse_equity_symbols()
    fo_symbols = load_fo_stocks()
    
    st.caption(f"📊 Loaded {len(all_symbols)} NSE equity symbols | 📈 F&O Stocks: {len(fo_symbols)}")
    
    if not all_symbols:
        st.warning("❌ No symbols loaded — check network access.")
        return
    
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        limit = st.number_input("Limit symbols (0 = all)", min_value=0, max_value=len(all_symbols), value=min(DEFAULT_SCAN_STOCKS, len(all_symbols)), step=50)
    with col2:
        st.caption(f"~{((limit or len(all_symbols)) / MAX_WORKERS) * 0.5 / 60:.1f}–{((limit or len(all_symbols)) / MAX_WORKERS) * 2.0 / 60:.1f} min")
    with col3:
        st.empty()
    
    scan_universe = all_symbols if limit == 0 else all_symbols[:limit]
    
    tabs = st.tabs([
        "📊 F&O Scanner", "🧠 Master Signal Engine", "⏱️ 5M Analysis",
        "⏱️ 15M Analysis", "🕐 1H Analysis", "🔄 CHoCH / MSS",
        "📂 Options Chain", "⏱️ 15-Min Reversal Scanner", "🌋 Volume Big Movement Scanner",
    ])
    
    # TAB 1: F&O SCANNER
    with tabs[0]:
        st.markdown("### 📊 F&O Stocks Scanner\nDetects bullish/bearish trends in F&O-eligible stocks using 15M analysis. ✅ FIXED")
        
        fo_col1, fo_col2 = st.columns([1, 1])
        with fo_col1:
            fo_lim = st.number_input("Limit F&O symbols (0=all)", min_value=0, max_value=len(fo_symbols), value=min(len(fo_symbols), DEFAULT_SCAN_STOCKS), step=50, key="fo_limit")
        with fo_col2:
            st.empty()
        
        fo_universe = fo_symbols if fo_lim == 0 else fo_symbols[:fo_lim]
        
        if st.button(f"📊 Run F&O Scanner ({len(fo_universe)} symbols)", key="fo_run"):
            if len(fo_universe) > 0:
                with st.spinner("Scanning F&O stocks…"):
                    fo_results, fo_errors, fo_stats = run_fo_scanner(fyers, fo_universe)
                    st.session_state["fo_df"] = pd.DataFrame(fo_results) if fo_results else pd.DataFrame()
                    st.session_state["fo_errors"] = fo_errors
                    st.session_state["fo_stats"] = fo_stats
            else:
                st.warning("⚠️ No F&O stocks available to scan.")
        
        if "fo_stats" in st.session_state:
            _display_scan_summary(st.session_state["fo_stats"])
        
        fo_df = st.session_state.get("fo_df")
        if fo_df is not None and not fo_df.empty:
            fo_sorted = fo_df.sort_values("Confidence %", ascending=False)
            st.dataframe(fo_sorted, use_container_width=True, height=400)
            
            col_e1, col_e2 = st.columns(2)
            with col_e1:
                st.download_button("📥 Excel", data=to_excel_bytes({"F&O Signals": fo_sorted}), 
                                  file_name=f"nse_fo_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", 
                                  mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_fo_xlsx")
            with col_e2:
                st.download_button("📥 CSV", data=to_csv_bytes(fo_sorted), 
                                  file_name=f"nse_fo_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", 
                                  mime="text/csv", key="dl_fo_csv")
        else:
            st.info("Run F&O Scanner above.")
        
        if st.session_state.get("fo_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state.get('fo_errors', []))})"):
                st.text("\n".join(st.session_state.get("fo_errors", [])[:20]))
    
    # TAB 2: MASTER SIGNAL ENGINE
    with tabs[1]:
        st.markdown("### 🧠 Master Signal Engine\n5M + 15M + 1H Multi-Timeframe Analysis with CHoCH, MSS, and Structure Detection. ✅ FIXED")
        
        master_col1, master_col2 = st.columns([1, 1])
        with master_col1:
            master_lim = st.number_input("Limit symbols (0=all)", min_value=0, max_value=len(scan_universe), value=min(DEFAULT_SCAN_STOCKS, len(scan_universe)), step=50, key="master_limit")
        with master_col2:
            st.empty()
        
        master_universe = scan_universe if master_lim == 0 else scan_universe[:master_lim]
        
        if st.button(f"🧠 Run Master Signal Scan ({len(master_universe)} symbols)", key="master_run"):
            with st.spinner("Analyzing 5M + 15M + 1H timeframes…"):
                master_results, master_errors, master_stats = run_master_signal_scan(fyers, master_universe, fo_symbols)
                st.session_state["master_df"] = pd.DataFrame(master_results) if master_results else pd.DataFrame()
                st.session_state["master_errors"] = master_errors
                st.session_state["master_stats"] = master_stats
        
        if "master_stats" in st.session_state:
            _display_scan_summary(st.session_state["master_stats"])
        
        master_df = st.session_state.get("master_df")
        if master_df is not None and not master_df.empty:
            master_sorted = master_df.sort_values("Confidence %", ascending=False)
            st.dataframe(master_sorted, use_container_width=True, height=500)
            
            col_e1, col_e2 = st.columns(2)
            with col_e1:
                st.download_button("📥 Excel", data=to_excel_bytes({"Master Signals": master_sorted}), 
                                  file_name=f"nse_master_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", 
                                  mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_master_xlsx")
            with col_e2:
                st.download_button("📥 CSV", data=to_csv_bytes(master_sorted), 
                                  file_name=f"nse_master_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", 
                                  mime="text/csv", key="dl_master_csv")
        else:
            st.info("Run Master Signal Scan above.")
        
        if st.session_state.get("master_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state.get('master_errors', []))})"):
                st.text("\n".join(st.session_state.get("master_errors", [])[:20]))
    
    # TABS 3-7: MULTI-TIMEFRAME / STRUCTURE / OPTIONS VIEWS
    master_df_live = st.session_state.get("master_df", pd.DataFrame())

    with tabs[2]:
        st.markdown("### ⏱️ 5-Minute Analysis")
        st.info("Run Master Signal Scan first. This view uses the same isolated 5M analysis; no new API scan is required.")
        st.dataframe(_master_view(master_df_live, ["Stock","LTP","5M Trend","5M Structure","5M CHoCH","5M MSS","VWAP","EMA Trend","RSI","MACD","RVOL","Final Signal","Confidence %"]), use_container_width=True, height=450)

    with tabs[3]:
        st.markdown("### ⏱️ 15-Minute Analysis")
        st.dataframe(_master_view(master_df_live, ["Stock","LTP","15M Trend","15M Structure","15M CHoCH","15M MSS","Final Signal","Confidence %","Entry","Stop Loss","Target 1","Target 2"]), use_container_width=True, height=450)

    with tabs[4]:
        st.markdown("### 🕐 1-Hour Analysis")
        st.dataframe(_master_view(master_df_live, ["Stock","LTP","1H Trend","1H Structure","1H CHoCH","1H MSS","Final Signal","Confidence %"]), use_container_width=True, height=450)

    with tabs[5]:
        st.markdown("### 🔄 CHoCH / MSS Multi-Timeframe Structure")
        st.dataframe(_master_view(master_df_live, ["Stock","5M Structure","15M Structure","1H Structure","5M CHoCH","15M CHoCH","1H CHoCH","5M MSS","15M MSS","1H MSS","Final Signal","Confidence %"]), use_container_width=True, height=450)

    with tabs[6]:
        st.markdown("### 📂 Live Options Chain ✅ FIXED")
        if fo_symbols:
            fo_names = [x.replace("NSE:","").replace("-EQ","") for x in fo_symbols]
            selected_name = st.selectbox("Select F&O underlying", fo_names, key="options_underlying")
            selected_symbol = f"NSE:{selected_name}-EQ"
            if st.button("📡 Load Live Options Chain", key="options_load"):
                od = fetch_options_chain_data(fyers, selected_symbol)
                st.session_state["options_selected"] = selected_name
                st.session_state["options_data"] = od
        od = st.session_state.get("options_data")
        if od:
            if od.get("status") == "OK":
                c1,c2,c3,c4,c5 = st.columns(5)
                c1.metric("Spot", od.get("spot") or "N/A"); c2.metric("ATM", od.get("atm_strike") or "N/A")
                c3.metric("PCR", od.get("pcr") if od.get("pcr") is not None else "N/A")
                c4.metric("Max Pain", od.get("max_pain") or "N/A"); c5.metric("Bias", od.get("options_bias","NEUTRAL"))
                st.dataframe(_options_summary_dataframe(od), use_container_width=True, height=500)
            else:
                st.warning(f"Options data unavailable: {od.get('message','unknown error')}")
        else:
            st.info("Select an F&O underlying and load its live option chain.")

    # TAB 8: 15-MINUTE REVERSAL SCANNER
    with tabs[7]:
        st.markdown("### ⏱️ 15-Minute Reversal Zone Scanner\nDetects swing reversal zones on 15-min candles using ATR-based threshold logic.")
        
        rev_col1, rev_col2 = st.columns([1, 1])
        with rev_col1:
            rev_lim = st.number_input("Limit symbols (0=all)", min_value=0, max_value=len(scan_universe), value=min(DEFAULT_SCAN_STOCKS, len(scan_universe)), step=50, key="rev_limit")
        with rev_col2:
            st.empty()
        
        rev_universe = scan_universe if rev_lim == 0 else scan_universe[:rev_lim]
        
        if st.button(f"⏱️ Run 15-Min Reversal Scan ({len(rev_universe)} symbols)", key="rev_run"):
            with st.spinner("Scanning 15-min candles for reversals…"):
                rev_results, rev_errors, rev_stats = run_15min_reversal_scan(fyers, rev_universe)
                st.session_state["rev_df"] = pd.DataFrame(rev_results) if rev_results else pd.DataFrame()
                st.session_state["rev_errors"] = rev_errors
                st.session_state["rev_stats"] = rev_stats
        
        if "rev_stats" in st.session_state:
            _display_scan_summary(st.session_state["rev_stats"])
        
        rev_df = st.session_state.get("rev_df")
        if rev_df is not None and not rev_df.empty:
            rev_sorted = rev_df.sort_values("Confidence %", ascending=False)
            st.dataframe(rev_sorted, use_container_width=True, height=400)
            
            col_e1, col_e2 = st.columns(2)
            with col_e1:
                st.download_button("📥 Excel", data=to_excel_bytes({"15M Reversals": rev_sorted}), 
                                  file_name=f"nse_15min_rev_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", 
                                  mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_rev_xlsx")
            with col_e2:
                st.download_button("📥 CSV", data=to_csv_bytes(rev_sorted), 
                                  file_name=f"nse_15min_rev_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", 
                                  mime="text/csv", key="dl_rev_csv")
        else:
            st.info("Run 15-Min Reversal Scan above.")
        
        if st.session_state.get("rev_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state.get('rev_errors', []))})"):
                st.text("\n".join(st.session_state.get("rev_errors", [])[:20]))
    
    # TAB 9: VOLUME BIG MOVEMENT SCANNER
    with tabs[8]:
        st.markdown("### 🌋 Volume Big Movement Scanner\nDetects large volume-driven moves on daily candles.")
        
        vol_col1, vol_col2 = st.columns([1, 1])
        with vol_col1:
            vol_lim = st.number_input("Limit symbols (0=all)", min_value=0, max_value=len(scan_universe), value=min(DEFAULT_SCAN_STOCKS, len(scan_universe)), step=50, key="vol_limit")
        with vol_col2:
            st.empty()
        
        vol_universe = scan_universe if vol_lim == 0 else scan_universe[:vol_lim]
        
        if st.button(f"🌋 Run Volume Big Move Scan ({len(vol_universe)} symbols)", key="vol_run"):
            with st.spinner("Scanning daily candles for big volume moves…"):
                vol_results, vol_errors, vol_stats = run_volume_big_move_scan(fyers, vol_universe)
                st.session_state["vol_df"] = pd.DataFrame(vol_results) if vol_results else pd.DataFrame()
                st.session_state["vol_errors"] = vol_errors
                st.session_state["vol_stats"] = vol_stats
        
        if "vol_stats" in st.session_state:
            _display_scan_summary(st.session_state["vol_stats"])
        
        vol_df = st.session_state.get("vol_df")
        if vol_df is not None and not vol_df.empty:
            vol_sorted = vol_df.sort_values("Confidence %", ascending=False)
            st.dataframe(vol_sorted, use_container_width=True, height=400)
            
            col_e1, col_e2 = st.columns(2)
            with col_e1:
                st.download_button("📥 Excel", data=to_excel_bytes({"Volume Big Moves": vol_sorted}), 
                                  file_name=f"nse_vol_bigmove_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", 
                                  mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_vol_xlsx")
            with col_e2:
                st.download_button("📥 CSV", data=to_csv_bytes(vol_sorted), 
                                  file_name=f"nse_vol_bigmove_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", 
                                  mime="text/csv", key="dl_vol_csv")
        else:
            st.info("Run Volume Big Move Scan above.")
        
        if st.session_state.get("vol_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state.get('vol_errors', []))})"):
                st.text("\n".join(st.session_state.get("vol_errors", [])[:20]))
    
    gc.collect()

# ════════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    access_token = os.environ.get("FYERS_ACCESS_TOKEN")
    if not access_token:
        st.error("❌ FYERS_ACCESS_TOKEN not set in environment variables")
        st.stop()
    
    try:
        from fyers_api import fyersModel
        app_id = os.environ.get("FYERS_APP_ID", "DEMO")
        fyers = fyersModel.FyersModel(client_id=app_id, token=access_token, log_path="")
        show_scanner(fyers)
    except ImportError:
        st.error("❌ fyers-api not installed. Run: pip install fyers-api")
    except Exception as e:
        st.error(f"❌ Error: {e}")
