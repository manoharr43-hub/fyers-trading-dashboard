"""
NSE AI PRO V16.1 ENHANCED
=====================================
Multi-timeframe stock scanner with Master Signal Engine
- Scan All NSE Stocks (2300+) or F&O Stocks Only
- Buying/Selling Pressure Indicators
- Strict timeframe alignment
- Live Options Chain Analysis
- Dynamic filtering & Excel export

Run: streamlit run nse_scanner_enhanced.py
"""

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
# IMPORTS & CONFIG
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
# CONSTANTS
# ════════════════════════════════════════════════════════════════════════════════
DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO = datetime.today().strftime("%Y-%m-%d")
FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"
NIFTY_BENCHMARK_SYMBOL = "NSE:NIFTY50-INDEX"
MAX_WORKERS = 8
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 1.0
DEFAULT_SCAN_STOCKS = 500  # Default for "All NSE"
FYERS_APP_ID = os.environ.get("FYERS_APP_ID", "")
OPTIONS_STRIKE_COUNT = 10
OPTIONS_HTTP_TIMEOUT = 15

# Timeframe constants
REVERSAL_RESOLUTION = "15"
REVERSAL_LOOKBACK_DAYS = 5
REVERSAL_ATR_LENGTH = 5
REVERSAL_ATR_MULTIPLIER = 2.8
SWING_LOOKBACK_PERIODS = 20
EMA_PERIODS = [9, 21, 50, 200]
VWAP_LOOKBACK = 20

# ════════════════════════════════════════════════════════════════════════════════
# UTILITIES & LOGGING
# ════════════════════════════════════════════════════════════════════════════════
def _now_ist() -> datetime:
    return datetime.now(IST)

def _ensure_app_folders() -> None:
    for folder in ("logs", "charts", "exports"):
        os.makedirs(folder, exist_ok=True)

_ensure_app_folders()
logger = logging.getLogger("nse_scanner_enhanced")
logger.setLevel(logging.INFO)

def _candle_signal_timestamp(df, is_daily: bool = False, resolution: str = "15") -> Tuple[str, str]:
    """Return the CLOSE time of the actual signal candle."""
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
    """Current scanner detection time."""
    return _now_ist().strftime("%d-%b-%Y %H:%M:%S IST")

# ════════════════════════════════════════════════════════════════════════════════
# CORE INDICATORS
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
# PRESSURE INDICATORS (NEW)
# ════════════════════════════════════════════════════════════════════════════════
def calculate_buying_selling_pressure(df) -> Dict[str, Any]:
    """Calculate buying and selling pressure using volume-weighted analysis."""
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
    """Return the latest confirmed swing high/low."""
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
    """Return confirmed pivot highs/lows."""
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
    """Detect HH/HL/LH/LL from confirmed pivots."""
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
    """Detect a NEW confirmed CHoCH."""
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

def detect_mss(df) -> Dict[str, Any]:
    """Detect a NEW confirmed MSS event."""
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
# SYMBOL LOADING
# ════════════════════════════════════════════════════════════════════════════════
_VALID_EQ_SYMBOL_RE = re.compile(r"^NSE:[A-Z0-9&\-]+-EQ$")

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

@st.cache_data(ttl=60 * 60 * 12, persist="disk", show_spinner=False)
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

@st.cache_data(ttl=60 * 60 * 12, persist="disk", show_spinner=False)
def load_fo_stocks() -> List[str]:
    """Load NSE equity symbols with active futures & options."""
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
# SAFE HISTORY FETCH
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
# SCAN STATS
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
# TIMEFRAME DATA FETCHER
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
        
        # Remove unclosed candle
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
# TIMEFRAME ANALYSIS ENGINE
# ════════════════════════════════════════════════════════════════════════════════
def analyze_timeframe(fyers, symbol: str, resolution: str) -> Dict[str, Any]:
    """Analyze a specific timeframe with pressure indicators."""
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
        
        # Pressure calculation
        pressure = calculate_buying_selling_pressure(df)
        
        # Structure analysis
        structure = detect_structure(df)
        choch = detect_choch(df)
        mss = detect_mss(df)
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
        
        # Trend
        ema_trend = "BULLISH" if ema9.iloc[-1] > ema21.iloc[-1] > ema50.iloc[-1] else "BEARISH" if ema9.iloc[-1] < ema21.iloc[-1] < ema50.iloc[-1] else "NEUTRAL"
        
        rsi_val = float(rsi.iloc[-1])
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
                "rsi_overbought": rsi_val > 70,
                "rsi_oversold": rsi_val < 30,
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
# MASTER SIGNAL ENGINE
# ════════════════════════════════════════════════════════════════════════════════
def calculate_master_signal(symbol: str, analysis_5m: Dict, analysis_15m: Dict, analysis_1h: Dict) -> Dict[str, Any]:
    """Calculate master signal from multi-timeframe analysis."""
    reasons = []
    scores = {
        "5m_score": 50,
        "15m_score": 50,
        "1h_score": 50,
        "pressure_score": 50,
    }
    
    # 5M ANALYSIS (Entry)
    if analysis_5m.get("status") == "OK" and analysis_5m.get("data"):
        data_5m = analysis_5m["data"]
        score_5m = 50
        
        if data_5m["structure_trend"] == "BULLISH" and data_5m["structure_type"] in ("HH/HL", "HH", "HL"):
            score_5m += 20
            reasons.append(f"5M: {data_5m['structure_type']}")
        elif data_5m["structure_trend"] == "BEARISH" and data_5m["structure_type"] in ("LH/LL", "LH", "LL"):
            score_5m -= 20
            reasons.append(f"5M: {data_5m['structure_type']}")
        
        if data_5m["bullish_choch"] or data_5m["bullish_mss"]:
            score_5m += 15
        elif data_5m["bearish_choch"] or data_5m["bearish_mss"]:
            score_5m -= 15
        
        if data_5m["ema_trend"] == "BULLISH":
            score_5m += 8
        elif data_5m["ema_trend"] == "BEARISH":
            score_5m -= 8
        
        if data_5m["rvol"] > 1.5:
            score_5m += 3
        elif data_5m["rvol"] < 0.8:
            score_5m -= 2
        
        score_5m = max(0, min(100, score_5m))
        scores["5m_score"] = score_5m
    
    # 15M ANALYSIS (Momentum)
    if analysis_15m.get("status") == "OK" and analysis_15m.get("data"):
        data_15m = analysis_15m["data"]
        score_15m = 50
        
        if data_15m["structure_trend"] == "BULLISH":
            score_15m += 18
        elif data_15m["structure_trend"] == "BEARISH":
            score_15m -= 18
        
        if data_15m["bullish_choch"] or data_15m["bullish_mss"]:
            score_15m += 12
        elif data_15m["bearish_choch"] or data_15m["bearish_mss"]:
            score_15m -= 12
        
        if data_15m["ema_trend"] == "BULLISH":
            score_15m += 7
        elif data_15m["ema_trend"] == "BEARISH":
            score_15m -= 7
        
        score_15m = max(0, min(100, score_15m))
        scores["15m_score"] = score_15m
    
    # 1H ANALYSIS (Trend context)
    if analysis_1h.get("status") == "OK" and analysis_1h.get("data"):
        data_1h = analysis_1h["data"]
        score_1h = 50
        
        if data_1h["structure_trend"] == "BULLISH":
            score_1h += 15
        elif data_1h["structure_trend"] == "BEARISH":
            score_1h -= 15
        
        score_1h = max(0, min(100, score_1h))
        scores["1h_score"] = score_1h
    
    # PRESSURE (Confirmation)
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
    
    # Weighted score
    total_score = (scores["5m_score"] * 0.35 + scores["15m_score"] * 0.35 +
                   scores["1h_score"] * 0.20 + scores["pressure_score"] * 0.10)
    
    confidence = round(abs(total_score - 50) * 0.8, 1)
    confidence = max(0, min(100, confidence))
    
    # Alignment checks
    if scores["5m_score"] >= 65 and scores["15m_score"] >= 60 and scores["pressure_score"] >= 65 and total_score >= 72:
        final_signal = "STRONG BUY"
    elif scores["5m_score"] >= 60 and total_score >= 62:
        final_signal = "BUY"
    elif scores["5m_score"] <= 35 and scores["15m_score"] <= 40 and scores["pressure_score"] <= 35 and total_score <= 28:
        final_signal = "STRONG SELL"
    elif scores["5m_score"] <= 40 and total_score <= 38:
        final_signal = "SELL"
    else:
        final_signal = "NEUTRAL"
    
    # Calculate entry/SL/targets
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
# MASTER SIGNAL SCANNER WORKER
# ════════════════════════════════════════════════════════════════════════════════
def _fetch_master_signal(fyers, symbol: str):
    """Worker for master signal scanner."""
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
        
        # Calculate master signal
        master = calculate_master_signal(symbol, analysis_5m, analysis_15m, analysis_1h)
        
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
            "Stock": stock_ticker,
            "LTP": round(float(ltp), 2),
            "Signal Time": _generated_timestamp(),
            "Signal Candle Time": data_5m.get("signal_candle_time", "N/A"),
            "5M Trend": data_5m.get("structure_trend", "N/A"),
            "15M Trend": data_15m.get("structure_trend", "N/A"),
            "1H Trend": data_1h.get("structure_trend", "N/A"),
            "5M Structure": data_5m.get("structure_type", "N/A"),
            "15M Structure": data_15m.get("structure_type", "N/A"),
            "1H Structure": data_1h.get("structure_type", "N/A"),
            "5M Buying Pressure": data_5m.get("buying_pressure", "N/A"),
            "5M Selling Pressure": data_5m.get("selling_pressure", "N/A"),
            "Pressure Trend": data_5m.get("pressure_trend", "N/A"),
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
            "Final Signal": master["final_signal"],
            "Confidence %": master["confidence"],
            "Entry": master["entry"],
            "Stop Loss": master["stop_loss"],
            "Target 1": master["target1"],
            "Target 2": master["target2"],
            "Risk:Reward": master["rr_ratio"],
            "Reason": " | ".join(master["reasons"][:3]) if master["reasons"] else "Multi-TF analysis",
        }, None
    
    except Exception as e:
        return None, f"{symbol}: {type(e).__name__}"

def run_master_signal_scan(fyers, symbols):
    """Threaded scan for master signals."""
    symbols = _validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    progress = st.progress(0.0, text=f"Scanning 0 / {len(symbols)}")
    done = 0
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(_fetch_master_signal, fyers, s): s for s in batch}
            for future in as_completed(futures):
                try:
                    res, err = future.result()
                except Exception as e:
                    res, err = None, f"{futures[future]}: error"
                
                if res:
                    results.append(res)
                if err:
                    errors.append(err)
                stats.record(has_result=bool(res), has_error=bool(err))
                done += 1
                progress.progress(done / max(len(symbols), 1), text=f"Scanning {done} / {len(symbols)}")
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    progress.empty()
    gc.collect()
    return results, errors, stats

# ════════════════════════════════════════════════════════════════════════════════
# EXPORT UTILITIES
# ════════════════════════════════════════════════════════════════════════════════
def to_excel_bytes(dfs_dict: Dict[str, pd.DataFrame]) -> bytes:
    """Export to Excel"""
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
def _load_scan_universe_once() -> Tuple[List[str], List[str]]:
    """Load NSE/F&O symbol masters once per Streamlit session.

    The underlying functions also use a 12-hour persistent Streamlit cache.
    This wrapper avoids repeated lookup calls during Streamlit reruns in the
    same session and keeps the loaded universe in session_state.
    """
    if "symbol_universe_loaded" not in st.session_state:
        st.session_state["symbol_universe_loaded"] = False

    if not st.session_state["symbol_universe_loaded"]:
        with st.spinner("Loading NSE & F&O symbol master (first time only)…"):
            st.session_state["all_symbols"] = load_nse_equity_symbols()
            st.session_state["fo_symbols"] = load_fo_stocks()
        st.session_state["symbol_universe_loaded"] = True

    return (
        st.session_state.get("all_symbols", []),
        st.session_state.get("fo_symbols", []),
    )


def _clear_symbol_universe_cache() -> None:
    """Clear symbol caches and force a fresh universe load on next run."""
    st.session_state.pop("all_symbols", None)
    st.session_state.pop("fo_symbols", None)
    st.session_state["symbol_universe_loaded"] = False
    try:
        load_nse_equity_symbols.clear()
        load_fo_stocks.clear()
    except Exception:
        pass


def show_scanner(fyers) -> None:
    """Main Streamlit app with universe selector."""
    
    st.set_page_config(page_title="NSE AI PRO", layout="wide")
    st.title("🚀 NSE AI PRO V16.1 ENHANCED")
    st.caption("⚡ Symbol master loads once per session; subsequent scans reuse the cached universe. "
               "Live 5M/15M/1H candles are refreshed for each scan.")
    st.caption(f"🕒 Current Time (IST): {_now_ist().strftime('%d-%b-%Y %H:%M:%S')} IST")
    
    # Load symbols only once per Streamlit session.
    # Persistent st.cache_data keeps the symbol master available for 12 hours.
    all_symbols, fo_symbols = _load_scan_universe_once()

    col_info, col_refresh = st.columns([5, 1])
    with col_info:
        st.caption(
            f"📊 Loaded {len(all_symbols)} NSE equity symbols | "
            f"📈 F&O Stocks: {len(fo_symbols)} | "
            f"Symbol Master: {'Cached' if st.session_state.get('symbol_universe_loaded') else 'Loading'}"
        )
    with col_refresh:
        if st.button("🔄 Reload Symbols", use_container_width=True):
            _clear_symbol_universe_cache()
            st.rerun()
    
    if not all_symbols:
        st.error("❌ No symbols loaded — check FYERS API access.")
        return
    
    # ════════════════════════════════════════════════════════════════════════════
    # SCAN UNIVERSE SELECTOR (ENHANCED)
    # ════════════════════════════════════════════════════════════════════════════
    st.markdown("## 🎯 Choose Scan Universe")
    
    col_scan1, col_scan2 = st.columns([1, 2])
    
    with col_scan1:
        scan_type = st.radio(
            "Select stocks to scan:",
            ["📊 All NSE Stocks (2300+)", "📈 F&O Stocks Only"],
            help="All NSE: Broad market | F&O Only: Liquid, optionable stocks"
        )
    
    with col_scan2:
        if "F&O" in scan_type:
            st.info(f"""
            **📈 F&O Stocks Only** ({len(fo_symbols)} stocks)
            
            ✓ Stocks with active futures & options contracts
            ✓ Higher liquidity & average volume
            ✓ Faster scan (~2-5 min for all)
            ✓ Options chain analysis included
            """)
        else:
            st.info(f"""
            **📊 All NSE Stocks** ({len(all_symbols)} stocks)
            
            ✓ Complete NSE Equity market coverage
            ✓ Includes large-cap, mid-cap, small-cap
            ✓ Slower scan (10-20 min for 2300)
            ✓ Start with a limit for faster results
            """)
    
    # Determine scan universe
    if "F&O" in scan_type:
        base_universe = fo_symbols
        default_limit = len(fo_symbols)  # Scan all by default
        universe_name = f"F&O ({len(fo_symbols)})"
    else:
        base_universe = all_symbols
        default_limit = min(DEFAULT_SCAN_STOCKS, len(all_symbols))
        universe_name = f"All NSE ({len(all_symbols)})"
    
    # ════════════════════════════════════════════════════════════════════════════
    # SCANNER SETTINGS
    # ════════════════════════════════════════════════════════════════════════════
    st.markdown("## ⚙️ Scanner Settings")
    
    col_set1, col_set2, col_set3 = st.columns([2, 1, 1])
    
    with col_set1:
        limit = st.number_input(
            f"Limit symbols (0 = scan all from {universe_name})", 
            min_value=0, 
            max_value=len(base_universe), 
            value=default_limit, 
            step=50
        )
    
    with col_set2:
        st.metric(
            "Will Scan",
            len(base_universe) if limit == 0 else min(limit, len(base_universe))
        )
    
    with col_set3:
        estimated_time = (len(base_universe) if limit == 0 else min(limit, len(base_universe))) / 30  # ~30 stocks/min
        st.metric("Est. Time", f"{estimated_time:.0f} min")
    
    scan_universe = base_universe if limit == 0 else base_universe[:limit]
    
    # ════════════════════════════════════════════════════════════════════════════
    # RUN SCAN
    # ════════════════════════════════════════════════════════════════════════════
    st.markdown("## 🧠 Master Signal Engine")
    st.write("Multi-timeframe analysis with buying/selling pressure confirmation")
    
    if st.button(f"▶️ RUN SCAN ({len(scan_universe)} stocks)", use_container_width=True, type="primary"):
        with st.spinner(f"Analyzing {len(scan_universe)} stocks with multi-timeframe engine…"):
            master_results, master_errors, master_stats = run_master_signal_scan(fyers, scan_universe)
            st.session_state["master_df"] = pd.DataFrame(master_results) if master_results else pd.DataFrame()
            st.session_state["master_errors"] = master_errors
            st.session_state["master_stats"] = master_stats
            st.rerun()
    
    # ════════════════════════════════════════════════════════════════════════════
    # DISPLAY RESULTS
    # ════════════════════════════════════════════════════════════════════════════
    if "master_stats" in st.session_state:
        st.markdown("### 📊 Scan Summary")
        _display_scan_summary(st.session_state["master_stats"])
    
    master_df = st.session_state.get("master_df")
    if master_df is not None and not master_df.empty:
        master_sorted = master_df.sort_values("Confidence %", ascending=False)
        
        # Filters
        st.markdown("### 🔍 Filter & Export")
        col_f1, col_f2, col_f3 = st.columns(3)
        
        with col_f1:
            min_confidence = st.slider("Min Confidence %", 0, 100, 50, step=5)
        with col_f2:
            signal_filter = st.selectbox("Signal Type", 
                                        ["ALL", "BUY ONLY", "SELL ONLY", "STRONG ONLY"])
        with col_f3:
            sort_by = st.selectbox("Sort By", ["Confidence %", "LTP", "Risk:Reward"])
        
        # Apply filters
        filtered_df = master_sorted.copy()
        filtered_df = filtered_df[filtered_df["Confidence %"] >= min_confidence]
        
        if signal_filter == "BUY ONLY":
            filtered_df = filtered_df[filtered_df["Final Signal"].str.contains("BUY", na=False)]
        elif signal_filter == "SELL ONLY":
            filtered_df = filtered_df[filtered_df["Final Signal"].str.contains("SELL", na=False)]
        elif signal_filter == "STRONG ONLY":
            filtered_df = filtered_df[filtered_df["Final Signal"].str.contains("STRONG", na=False)]
        
        if sort_by == "LTP":
            filtered_df = filtered_df.sort_values("LTP", ascending=False)
        elif sort_by == "Risk:Reward":
            filtered_df = filtered_df.sort_values("Risk:Reward", ascending=False, na_position='last')
        
        # Display
        result_count = len(filtered_df)
        st.subheader(f"✅ Results: {result_count} signal{'s' if result_count != 1 else ''}")
        
        if result_count > 0:
            # Table
            st.dataframe(filtered_df, use_container_width=True, height=600)
            
            # Downloads
            st.markdown("### 💾 Download Results")
            col_d1, col_d2, col_d3 = st.columns(3)
            
            with col_d1:
                excel_data = to_excel_bytes({"Signals": filtered_df})
                st.download_button(
                    label="📥 Excel",
                    data=excel_data,
                    file_name=f"nse_signals_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
            
            with col_d2:
                csv_data = to_csv_bytes(filtered_df)
                st.download_button(
                    label="📥 CSV",
                    data=csv_data,
                    file_name=f"nse_signals_{_now_ist().strftime('%Y%m%d_%H%M')}.csv",
                    mime="text/csv"
                )
            
            with col_d3:
                json_data = to_json_bytes(filtered_df)
                st.download_button(
                    label="📥 JSON",
                    data=json_data,
                    file_name=f"nse_signals_{_now_ist().strftime('%Y%m%d_%H%M')}.json",
                    mime="application/json"
                )
            
            # Stats
            st.markdown("### 📈 Statistics")
            stat_col1, stat_col2, stat_col3, stat_col4 = st.columns(4)
            
            with stat_col1:
                buy_count = len(filtered_df[filtered_df["Final Signal"].str.contains("BUY", na=False)])
                st.metric("Buy Signals", buy_count)
            
            with stat_col2:
                sell_count = len(filtered_df[filtered_df["Final Signal"].str.contains("SELL", na=False)])
                st.metric("Sell Signals", sell_count)
            
            with stat_col3:
                avg_conf = filtered_df["Confidence %"].mean()
                st.metric("Avg Confidence", f"{avg_conf:.1f}%")
            
            with stat_col4:
                avg_rr = filtered_df[filtered_df["Risk:Reward"] > 0]["Risk:Reward"].mean()
                st.metric("Avg RR Ratio", f"{avg_rr:.2f}" if not pd.isna(avg_rr) else "N/A")
        
        else:
            st.warning(f"❌ No signals found with confidence ≥ {min_confidence}%")
            st.info("Try lowering the confidence threshold or selecting a different signal type")
            st.subheader("📊 Top Results (for reference):")
            st.dataframe(master_sorted.head(20), use_container_width=True)
    
    else:
        st.info("👈 Click 'RUN SCAN' to analyze stocks and generate signals")
    
    gc.collect()

# ════════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    access_token = os.environ.get("FYERS_ACCESS_TOKEN")
    if not access_token:
        st.error("❌ FYERS_ACCESS_TOKEN environment variable not set")
        st.stop()
    
    try:
        from fyers_api import fyersModel
        app_id = os.environ.get("FYERS_APP_ID", "DEMO")
        fyers = fyersModel.FyersModel(client_id=app_id, token=access_token, log_path="")
        show_scanner(fyers)
    except ImportError:
        st.error("❌ fyers-api not installed: pip install fyers-api")
    except Exception as e:
        st.error(f"❌ Error: {e}")

# -----------------------------------------------------------------------------
# V16.2 CACHE NOTE
# -----------------------------------------------------------------------------
# NSE/F&O symbol masters are loaded once per Streamlit session and persisted
# through Streamlit's disk cache for 12 hours. Use "Reload Symbols" in the UI
# when the broker's symbol master changes. Live timeframe candles remain fresh
# for every scan, so signal calculations are not frozen by the symbol cache.
