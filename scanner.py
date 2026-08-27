"""
NSE AI PRO V17 - Core Scanner Engine
Independent scanning module (no Streamlit dependencies)
"""

import pandas as pd
import numpy as np
import requests
import time
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple, Dict, Any
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import os

# ════════════════════════════════════════════════════════════════════════════════
# CONFIGURATION & CONSTANTS
# ════════════════════════════════════════════════════════════════════════════════

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    from datetime import timezone
    IST = timezone(timedelta(hours=5, minutes=30))

# Date ranges
DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO = datetime.today().strftime("%Y-%m-%d")

# API & Symbols
FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"
FYERS_FO_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_FO.csv"
MAX_WORKERS = 8
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 1.0

# Signal thresholds
DEFAULT_CONFIDENCE_THRESHOLD = 70
DEFAULT_RVOL_THRESHOLD = 1.2
DEFAULT_STRONG_RVOL = 1.5

# Momentum scanner
MOMENTUM_MIN_SCORE = 65
MOMENTUM_STRONG_SCORE = 85
LIVE_MOVE_MIN_PCT = 0.35
LIVE_MOVE_BIG_PCT = 0.70
LIVE_MOVE_MIN_RVOL = 1.30
LIVE_MOVE_STRONG_RVOL = 1.80
LIVE_MOVE_MIN_BODY_PCT = 50.0

# Big move setup
BIGMOVE_LOOKBACK_BARS = 30
BIGMOVE_CONSOLIDATION_MIN_BARS = 3
BIGMOVE_CONSOLIDATION_MAX_BARS = 12
BIGMOVE_MAX_RANGE_PCT = 0.8
BIGMOVE_MAX_BAR_ATR_MULT = 1.5
BIGMOVE_MIN_BODY_ATR = 1.2
BIGMOVE_MIN_BODY_PCT = 50.0
BIGMOVE_MIN_RVOL = 2.5
BIGMOVE_MIN_BREAK_PCT = 0.2
BIGMOVE_STRONG_SCORE = 80.0

# Regex patterns
_VALID_EQ_SYMBOL_RE = re.compile(r"^NSE:[A-Z0-9&\-]+-EQ$")

# ════════════════════════════════════════════════════════════════════════════════
# LOGGING
# ════════════════════════════════════════════════════════════════════════════════

logger = logging.getLogger("nse_scanner")
logger.setLevel(logging.DEBUG)

if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# ════════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ════════════════════════════════════════════════════════════════════════════════

def now_ist() -> datetime:
    """Get current time in IST"""
    return datetime.now(IST)

def candle_signal_timestamp(df, is_daily: bool = False, resolution: str = "15") -> Tuple[str, str]:
    """Calculate candle signal timestamp"""
    ts = df["Time"].iloc[-1]
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts_ist = ts.tz_convert(IST)
    if is_daily:
        close_ts = ts_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    else:
        try:
            minutes = int(resolution)
        except:
            minutes = 15
        close_ts = ts_ist + timedelta(minutes=minutes)
    return close_ts.strftime("%d-%b-%Y"), close_ts.strftime("%H:%M:%S") + " IST"

def generated_timestamp() -> str:
    """Get generation timestamp"""
    return now_ist().strftime("%d-%b-%Y %H:%M:%S IST")

def validate_symbols(symbols) -> List[str]:
    """Validate and clean symbols"""
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

def normalize_signal(signal_str: str) -> str:
    """Normalize signal to BUY, SELL, or NEUTRAL"""
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
# SYMBOL LOADING
# ════════════════════════════════════════════════════════════════════════════════

def load_nse_equity_symbols() -> List[str]:
    """Load NSE equity symbols from Fyers"""
    try:
        resp = requests.get(FYERS_NSE_CM_SYMBOL_MASTER, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"Could not download Fyers symbol master: {e}")
        return []
    
    lines = [ln for ln in resp.text.strip().split("\n") if ln.strip()]
    if not lines:
        return []
    
    # Find symbol column
    sample = lines[:min(500, len(lines))]
    split_sample = [ln.split(",") for ln in sample]
    max_cols = max((len(p) for p in split_sample), default=0)
    best_col, best_hits = None, 0
    
    for col_idx in range(max_cols):
        hits = sum(1 for parts in split_sample 
                  if len(parts) > col_idx and parts[col_idx].strip().startswith("NSE:") 
                  and parts[col_idx].strip().endswith("-EQ"))
        if hits > best_hits:
            best_col, best_hits = col_idx, hits
    
    if best_col is None or best_hits == 0:
        logger.error("Could not locate trading-symbol column")
        return []
    
    symbols = []
    for line in lines:
        parts = line.split(",")
        if len(parts) <= best_col:
            continue
        sym = parts[best_col].strip()
        if sym.startswith("NSE:") and sym.endswith("-EQ"):
            symbols.append(sym)
    
    return sorted(set(validate_symbols(symbols)))

def load_fo_stocks() -> List[str]:
    """Load F&O underlying stocks"""
    try:
        cm_symbols = set(load_nse_equity_symbols())
        if not cm_symbols:
            return []

        urls = [
            "https://public.fyers.in/sym_details/NSE_FO.csv",
            "http://public.fyers.in/sym_details/NSE_FO.csv",
        ]
        text = None
        for url in urls:
            try:
                r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
                r.raise_for_status()
                if r.text and len(r.text) > 100:
                    text = r.text
                    break
            except Exception:
                continue

        if not text:
            logger.warning("F&O symbol master unavailable")
            return []

        import csv
        import io
        
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
        logger.info(f"Loaded {len(result)} NSE F&O equity underlyings")
        return result

    except Exception as e:
        logger.exception(f"F&O symbol loading failed: {e}")
        return []

# ════════════════════════════════════════════════════════════════════════════════
# CORE INDICATORS
# ════════════════════════════════════════════════════════════════════════════════

def calculate_rsi(close, period: int = 14):
    """Calculate RSI"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).fillna(50)

def calculate_macd(close):
    """Calculate MACD"""
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line

def calculate_atr(df, period: int = 14):
    """Calculate ATR"""
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

def calculate_ema(close, period: int = 9) -> pd.Series:
    """Calculate EMA"""
    return close.ewm(span=period, adjust=False).mean()

def calculate_vwap(df) -> pd.Series:
    """Calculate VWAP"""
    if "Volume" not in df.columns or len(df) == 0:
        return pd.Series([np.nan] * len(df), index=df.index)
    
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    
    typical_price = (high + low + close) / 3
    vwap = (typical_price * volume).cumsum() / volume.cumsum()
    return vwap.fillna(method="ffill").fillna(close)

def calculate_buying_selling_pressure(df) -> Dict[str, Any]:
    """Calculate buying/selling pressure"""
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
# STRUCTURE DETECTION
# ════════════════════════════════════════════════════════════════════════════════

def confirmed_pivots(df, left: int = 2, right: int = 2):
    """Return confirmed pivot highs/lows"""
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
    """Detect HH/HL/LH/LL structure"""
    ph, pl = confirmed_pivots(df)
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
    """Detect CHoCH (Change of Character)"""
    ph, pl = confirmed_pivots(df)
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
    """Detect MSS (Market Structure Shift)"""
    ph, pl = confirmed_pivots(df)
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

def detect_cisd(df: pd.DataFrame) -> Dict[str, Any]:
    """Detect CISD (Candle Inside Supply/Demand)"""
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

# ════════════════════════════════════════════════════════════════════════════════
# SAFE HISTORY FETCH
# ════════════════════════════════════════════════════════════════════════════════

def safe_history(fyers, params: dict, max_retries: int = 3, base_delay: float = 1.0):
    """Safely fetch OHLCV data with retry logic"""
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

def fetch_timeframe_data(fyers, symbol, resolution: str, lookback_days: int = 30) -> Optional[pd.DataFrame]:
    """Fetch OHLCV data for a specific timeframe"""
    date_from = (datetime.today() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    date_to = datetime.today().strftime("%Y-%m-%d")
    
    resp, err = safe_history(fyers, {
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
            candle_age = (now_ist() - last_time).total_seconds() / 60
            res_minutes = int(resolution)
            if candle_age < res_minutes + 1:
                df = df.iloc[:-1].reset_index(drop=True)
        
        if len(df) < 10:
            return None
        
        return df
    
    except Exception as e:
        logger.error(f"Error processing timeframe data: {e}")
        return None

# ════════════════════════════════════════════════════════════════════════════════
# TIMEFRAME ANALYSIS
# ════════════════════════════════════════════════════════════════════════════════

def analyze_timeframe(fyers, symbol: str, resolution: str) -> Dict[str, Any]:
    """Analyze a specific timeframe"""
    df = fetch_timeframe_data(fyers, symbol, resolution, lookback_days=30)
    
    if df is None or len(df) < 10:
        return {
            "timeframe": resolution,
            "status": "DATA_UNAVAILABLE",
            "data": None,
            "df": None,
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
        
        pressure = calculate_buying_selling_pressure(df)
        structure = detect_structure(df)
        choch = detect_choch(df)
        mss = detect_mss(df)
        cisd = detect_cisd(df)
        
        vol_avg20 = float(df["Volume"].tail(20).mean()) if "Volume" in df.columns else 0
        last_vol = float(df["Volume"].iloc[-1]) if "Volume" in df.columns else 0
        rvol = round(last_vol / vol_avg20, 2) if vol_avg20 > 0 else 0.0
        
        last_close = float(df["Close"].iloc[-1])
        last_high = float(df["High"].iloc[-1])
        last_low = float(df["Low"].iloc[-1])
        last_open = float(df["Open"].iloc[-1])
        
        ema_trend = "BULLISH" if ema9.iloc[-1] > ema21.iloc[-1] > ema50.iloc[-1] else "BEARISH" if ema9.iloc[-1] < ema21.iloc[-1] < ema50.iloc[-1] else "NEUTRAL"
        
        rsi_val = float(rsi.iloc[-1])
        macd_bullish = macd_line.iloc[-1] > macd_sig.iloc[-1]
        candle_date, candle_close_time = candle_signal_timestamp(df, is_daily=False, resolution=resolution)
        
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
                "signal_generated_at": generated_timestamp(),
                "structure_type": structure["type"],
                "structure_trend": structure["trend"],
                "structure_strength": round(structure.get("strength", 0), 1),
                "bullish_choch": choch["bullish_choch"],
                "bearish_choch": choch["bearish_choch"],
                "bullish_mss": mss["bullish_mss"],
                "bearish_mss": mss["bearish_mss"],
                "bullish_cisd": cisd["bullish_cisd"],
                "bearish_cisd": cisd["bearish_cisd"],
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
                "macd_hist": round(float(macd_hist.iloc[-1]), 4),
                "rvol": rvol,
                "atr": round(float(atr.iloc[-1]), 2),
                "buying_pressure": pressure["buying_pressure"],
                "selling_pressure": pressure["selling_pressure"],
                "pressure_trend": pressure["trend"],
            },
            "df": df,
        }
    
    except Exception as e:
        logger.error(f"Timeframe analysis error: {e}")
        return {
            "timeframe": resolution,
            "status": "ERROR",
            "error": str(e),
            "data": None,
            "df": None,
        }

# ════════════════════════════════════════════════════════════════════════════════
# MASTER SIGNAL CALCULATION
# ════════════════════════════════════════════════════════════════════════════════

def calculate_master_signal(symbol: str, analysis_5m: Dict, analysis_15m: Dict, analysis_1h: Dict, options_data: Dict = None) -> Dict[str, Any]:
    """Calculate master signal from multi-timeframe analysis"""
    if options_data is None:
        options_data = {"status":"DATA_UNAVAILABLE", "options_bias":"NEUTRAL"}
    
    data_5m = analysis_5m.get("data") if analysis_5m.get("status") == "OK" else None
    data_15m = analysis_15m.get("data") if analysis_15m.get("status") == "OK" else None
    data_1h = analysis_1h.get("data") if analysis_1h.get("status") == "OK" else None
    
    if not data_5m or not data_15m or not data_1h:
        return {
            "final_signal": "🟡 NEUTRAL",
            "confidence": 0.0,
            "entry": None,
            "stop_loss": None,
            "target1": None,
            "target2": None,
            "rr_ratio": None,
            "scores": {"5m": 50, "15m": 50, "1h": 50, "pressure": 50},
            "signal_reason": "Insufficient timeframe data",
        }
    
    def classify_tf_direction(data: Dict) -> str:
        trend = data.get("structure_trend", "NEUTRAL")
        return "BULLISH" if trend == "BULLISH" else "BEARISH" if trend == "BEARISH" else "NEUTRAL"
    
    tf_5m = classify_tf_direction(data_5m)
    tf_15m = classify_tf_direction(data_15m)
    tf_1h = classify_tf_direction(data_1h)
    
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
            "scores": {"5m": 50, "15m": 50, "1h": 50, "pressure": 50},
            "signal_reason": f"Timeframe conflict: 5M {tf_5m} vs 1H {tf_1h}",
        }
    
    pressure_trend = data_5m.get("pressure_trend", "NEUTRAL")
    pressure_buy = pressure_trend in ["BUYING", "STRONG_BUYING"]
    pressure_sell = pressure_trend in ["SELLING", "STRONG_SELLING"]
    
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
            "scores": {"5m": 50, "15m": 50, "1h": 50, "pressure": 50},
            "signal_reason": f"Pressure conflict: {pressure_trend}",
        }
    
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
            "scores": {"5m": 50, "15m": 50, "1h": 50, "pressure": 50},
            "signal_reason": f"VWAP conflict",
        }
    
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
            "scores": {"5m": 50, "15m": 50, "1h": 50, "pressure": 50},
            "signal_reason": f"EMA conflict",
        }
    
    # Calculate confidence
    confirmation_count = 0
    total_factors = 0
    
    if tf_5m == "BULLISH": confirmation_count += 1
    elif tf_5m == "BEARISH": confirmation_count -= 1
    total_factors += 1
    
    if tf_15m == "BULLISH": confirmation_count += 1
    elif tf_15m == "BEARISH": confirmation_count -= 1
    total_factors += 1
    
    if tf_1h == "BULLISH": confirmation_count += 1
    elif tf_1h == "BEARISH": confirmation_count -= 1
    total_factors += 1
    
    if pressure_buy: confirmation_count += 1
    elif pressure_sell: confirmation_count -= 1
    total_factors += 1
    
    raw_confidence = (confirmation_count / total_factors) * 100 if total_factors > 0 else 0
    confidence = max(0, min(100, abs(raw_confidence)))
    
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
    
    rvol = data_5m.get("rvol", 1.0)
    
    if is_bullish_aligned and has_bullish_structure and pressure_trend in ["STRONG_BUYING"]:
        final_signal = "🟢 STRONG BUY"
        confidence = min(100, confidence + 10)
    elif is_bullish_aligned:
        final_signal = "🟢 BUY"
        confidence = min(95, confidence + 5)
    elif is_bearish_aligned and has_bearish_structure and pressure_trend in ["STRONG_SELLING"]:
        final_signal = "🔴 STRONG SELL"
        confidence = min(100, confidence + 10)
    elif is_bearish_aligned:
        final_signal = "🔴 SELL"
        confidence = min(95, confidence + 5)
    else:
        final_signal = "🟡 NEUTRAL"
        confidence = 35.0
    
    # Trade plan
    entry = round(last_close, 2)
    atr_5m = data_5m.get("atr", 0)
    
    if "BUY" in final_signal:
        sl = round(entry - atr_5m * 2, 2)
        t1 = round(entry + atr_5m * 1.5, 2)
        t2 = round(entry + atr_5m * 2.5, 2)
    elif "SELL" in final_signal:
        sl = round(entry + atr_5m * 2, 2)
        t1 = round(entry - atr_5m * 1.5, 2)
        t2 = round(entry - atr_5m * 2.5, 2)
    else:
        sl = t1 = t2 = None
    
    risk = abs(entry - sl) if sl else 0
    reward = abs(t1 - entry) if t1 else 0
    rr_ratio = round(reward / risk, 2) if risk > 0 else None
    
    return {
        "final_signal": final_signal,
        "confidence": confidence,
        "entry": entry if "BUY" in final_signal or "SELL" in final_signal else None,
        "stop_loss": sl,
        "target1": t1,
        "target2": t2,
        "rr_ratio": rr_ratio,
        "scores": {
            "5m": 70 if tf_5m == "BULLISH" else 30 if tf_5m == "BEARISH" else 50,
            "15m": 70 if tf_15m == "BULLISH" else 30 if tf_15m == "BEARISH" else 50,
            "1h": 70 if tf_1h == "BULLISH" else 30 if tf_1h == "BEARISH" else 50,
            "pressure": 70 if pressure_trend == "STRONG_BUYING" else 60 if pressure_trend == "BUYING" else 30 if pressure_trend == "STRONG_SELLING" else 40 if pressure_trend == "SELLING" else 50,
        },
        "signal_reason": f"{tf_5m} 5M + {tf_15m} 15M + {tf_1h} 1H | {pressure_trend}",
    }

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

# ════════════════════════════════════════════════════════════════════════════════
# SCANNER WORKERS
# ════════════════════════════════════════════════════════════════════════════════

def scan_nse_stock(fyers, symbol: str):
    """Scan a single NSE stock"""
    stock_ticker = symbol.replace("NSE:", "").replace("-EQ", "") if isinstance(symbol, str) else str(symbol)
    
    if not isinstance(symbol, str) or not _VALID_EQ_SYMBOL_RE.match(symbol):
        return None, f"{symbol}: invalid format"
    
    try:
        analysis_5m = analyze_timeframe(fyers, symbol, "5")
        analysis_15m = analyze_timeframe(fyers, symbol, "15")
        analysis_1h = analyze_timeframe(fyers, symbol, "60")
        
        if all(a.get("status") != "OK" for a in [analysis_5m, analysis_15m, analysis_1h]):
            return None, None
        
        master = calculate_master_signal(symbol, analysis_5m, analysis_15m, analysis_1h)
        
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
            "Symbol": stock_ticker,
            "LTP": round(float(ltp), 2),
            "Trend": data_5m.get("structure_trend", "N/A"),
            "5M Trend": data_5m.get("structure_trend", "N/A"),
            "15M Trend": data_15m.get("structure_trend", "N/A"),
            "1H Trend": data_1h.get("structure_trend", "N/A"),
            "VWAP": round(data_5m.get("vwap", 0), 2) if data_5m.get("vwap") else "N/A",
            "RSI": round(data_5m.get("rsi", 50), 1),
            "RVOL": data_5m.get("rvol", 0),
            "🟢 BUY PRESSURE %": data_5m.get("buying_pressure", "N/A"),
            "🔴 SELL PRESSURE %": data_5m.get("selling_pressure", "N/A"),
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
# THREADED SCANNING
# ════════════════════════════════════════════════════════════════════════════════

def run_nse_scan(fyers, symbols, callback=None):
    """Run NSE scan with optional progress callback"""
    symbols = validate_symbols(symbols)
    results, errors = [], []
    stats = ScanStats(total=len(symbols))
    done = 0
    
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = {executor.submit(scan_nse_stock, fyers, s): s for s in batch}
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
                
                if callback:
                    callback(done, len(symbols))
        
        if i + BATCH_SIZE < len(symbols):
            time.sleep(BATCH_PAUSE_SECONDS)
    
    return results, errors, stats

# ════════════════════════════════════════════════════════════════════════════════
# EXPORT UTILITIES
# ════════════════════════════════════════════════════════════════════════════════

def dataframe_to_csv(df: pd.DataFrame) -> str:
    """Convert DataFrame to CSV string"""
    try:
        return df.to_csv(index=False)
    except Exception as e:
        logger.error(f"CSV export error: {e}")
        return ""

def dataframe_to_json(df: pd.DataFrame) -> str:
    """Convert DataFrame to JSON string"""
    try:
        return df.to_json(orient="records", indent=2, force_ascii=False)
    except Exception as e:
        logger.error(f"JSON export error: {e}")
        return ""

if __name__ == "__main__":
    print("NSE AI PRO V17 - Scanner Module")
    print("Import this module to use scanning functions")
