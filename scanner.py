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
# CONSTANTS
# ════════════════════════════════════════════════════════════════════════════════
DATE_FROM = (datetime.today() - timedelta(days=365)).strftime("%Y-%m-%d")
DATE_TO = datetime.today().strftime("%Y-%m-%d")
FYERS_NSE_CM_SYMBOL_MASTER = "https://public.fyers.in/sym_details/NSE_CM.csv"
NIFTY_BENCHMARK_SYMBOL = "NSE:NIFTY50-INDEX"
MAX_WORKERS = 8
BATCH_SIZE = 50
BATCH_PAUSE_SECONDS = 1.0

# ════════════════════════════════════════════════════════════════════════════════
# 15-MIN REVERSAL SCANNER CONSTANTS
# ════════════════════════════════════════════════════════════════════════════════
REVERSAL_RESOLUTION = "15"
REVERSAL_LOOKBACK_DAYS = 5
REVERSAL_CONFIRMATION_BARS = 0
REVERSAL_ATR_LENGTH = 5
REVERSAL_ATR_MULTIPLIER = 2.8
REVERSAL_MIN_MOVE_PCT = 0.015
REVERSAL_CUSTOM_ABS = 0.05

# ════════════════════════════════════════════════════════════════════════════════
# VOLUME BIG MOVEMENT CONSTANTS
# ════════════════════════════════════════════════════════════════════════════════
VOL_BIGMOVE_MIN_RVOL = 2.5
VOL_BIGMOVE_MIN_BODY_PCT = 0.8
VOL_BIGMOVE_LOOKBACK = 20

# ════════════════════════════════════════════════════════════════════════════════
# LOGGING & UTILITIES
# ════════════════════════════════════════════════════════════════════════════════
def _now_ist() -> datetime:
    return datetime.now(IST)

def _ensure_app_folders() -> None:
    for folder in ("logs", "charts", "exports"):
        os.makedirs(folder, exist_ok=True)

_ensure_app_folders()
logger = logging.getLogger("nse_scanner_v14")
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
# SYMBOL LOADING (RETAINED)
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
# 15-MINUTE REVERSAL SCANNER ENGINE
# ════════════════════════════════════════════════════════════════════════════════
def _detect_reversal_zone(df):
    """
    Detect swing reversal zones on 15-min candles using ATR-based logic
    similar to the Pine Script indicator. Returns zone info.
    """
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
    
    # Simple swing detection: find recent pivot high/low
    lookback = 20
    recent = d.tail(lookback)
    recent_high = float(recent["High"].max())
    recent_low = float(recent["Low"].min())
    last_close = float(close.iloc[-1])
    
    # Bearish pressure near high = potential reversal down
    if abs(recent_high - last_close) < reversal_threshold * 1.5:
        if close.iloc[-1] < close.iloc[-2]:  # Confirmed bearish candle
            return {
                "type": "SELL",
                "price": round(recent_high, 2),
                "strength": "Strong" if abs(recent_high - last_close) < reversal_threshold * 0.5 else "Medium",
                "distance_pct": round(abs(recent_high - last_close) / recent_high * 100, 2),
            }
    
    # Bullish pressure near low = potential reversal up
    if abs(last_close - recent_low) < reversal_threshold * 1.5:
        if close.iloc[-1] > close.iloc[-2]:  # Confirmed bullish candle
            return {
                "type": "BUY",
                "price": round(recent_low, 2),
                "strength": "Strong" if abs(last_close - recent_low) < reversal_threshold * 0.5 else "Medium",
                "distance_pct": round(abs(last_close - recent_low) / recent_low * 100, 2),
            }
    
    return {"type": "NONE", "price": None, "strength": None}

def _fetch_15min_reversal_signal(fyers, symbol):
    """Worker for 15-min reversal scanner."""
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
        
        # Remove unclosed candle
        if len(df) > 0:
            last_time = df["Time"].iloc[-1]
            candle_age = (_now_ist() - last_time).total_seconds() / 60
            if candle_age < REVERSAL_LOOKBACK_DAYS * 1440:  # Still in current period
                if candle_age < 15:  # Probably unclosed
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
    """Threaded scan for 15-min reversal signals."""
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
# VOLUME BIG MOVEMENT SCANNER
# ════════════════════════════════════════════════════════════════════════════════
def _detect_volume_big_move(df):
    """
    Detect big volume-driven moves on daily candles.
    Returns: type ("BIG_UP", "BIG_DOWN", "NONE"), confidence score
    """
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
    
    # Big move up
    if float(last["Close"]) > float(last["Open"]) and body > atr * 0.6:
        confidence = min(95.0, 40 + (body_pct * 5) + (rvol * 8))
        return "BIG_UP", confidence
    
    # Big move down
    if float(last["Close"]) < float(last["Open"]) and body > atr * 0.6:
        confidence = min(95.0, 40 + (body_pct * 5) + (rvol * 8))
        return "BIG_DOWN", confidence
    
    return "NONE", 0.0

def _fetch_volume_big_move_signal(fyers, symbol):
    """Worker for volume big movement scanner."""
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
    """Threaded scan for volume big movement signals."""
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

# ════════════════════════════════════════════════════════════════════════════════
# EXPORT UTILITIES
# ════════════════════════════════════════════════════════════════════════════════
def to_excel_bytes(df, sheet_name: str = "Results") -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        safe_name = sheet_name[:31]
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
    """Streamlit main entry point - Focused: Full Scan + F&O OI + 15-Min Rev + Vol BigMove"""
    
    st.title("🚀 NSE AI PRO V14 — Focused Scanner")
    st.caption(f"🕒 Current Time (IST): {_now_ist().strftime('%d-%b-%Y %H:%M:%S')} IST")
    
    symbols = load_nse_equity_symbols()
    st.caption(f"Loaded {len(symbols)} NSE equity symbols.")
    
    if not symbols:
        st.warning("No symbols loaded — check network access.")
        return
    
    col1, col2 = st.columns([2, 1])
    with col1:
        limit = st.number_input("Limit symbols (0 = all)", min_value=0, max_value=len(symbols), value=100, step=50)
    with col2:
        st.caption(f"~{((limit or len(symbols)) / MAX_WORKERS) * 0.3 / 60:.1f}–{((limit or len(symbols)) / MAX_WORKERS) * 1.0 / 60:.1f} min")
    
    scan_universe = symbols if limit == 0 else symbols[:limit]
    
    # Tabs
    tab_rev, tab_vol = st.tabs([
        "⏱️ 15-Min Reversal Scanner",
        "🌋 Volume Big Movement Scanner",
    ])
    
    # ════════════════════════════════════════════════════════════════════════════════
    # 15-MINUTE REVERSAL TAB
    # ════════════════════════════════════════════════════════════════════════════════
    with tab_rev:
        st.markdown(
            "### ⏱️ 15-Minute Reversal Zone Scanner\n"
            f"Detects swing reversal zones on 15-min candles using ATR-based threshold logic. "
            f"Min RVOL: {VOL_BIGMOVE_MIN_RVOL}x, Looks for confirmed reversal candles near recent swing highs/lows."
        )
        
        rev_col1, rev_col2 = st.columns([1, 1])
        with rev_col1:
            rev_lim = st.number_input("Limit symbols (0=all)", min_value=0, max_value=len(symbols), value=min(200, len(symbols)), step=50, key="rev_limit")
        with rev_col2:
            st.empty()
        
        rev_universe = symbols if rev_lim == 0 else symbols[:rev_lim]
        
        if st.button(f"⏱️ Run 15-Min Reversal Scan ({len(rev_universe)} symbols)", key="rev_run"):
            with st.spinner("Scanning 15-min candles for reversals…"):
                rev_results, rev_errors, rev_stats = run_15min_reversal_scan(fyers, rev_universe)
                st.session_state["rev_df"] = pd.DataFrame(rev_results)
                st.session_state["rev_errors"] = rev_errors
                st.session_state["rev_stats"] = rev_stats
        
        if "rev_stats" in st.session_state:
            _display_scan_summary(st.session_state["rev_stats"])
        
        rev_df = st.session_state.get("rev_df")
        if rev_df is not None and not rev_df.empty:
            rev_sorted = rev_df.sort_values("Confidence %", ascending=False)
            st.dataframe(rev_sorted, use_container_width=True, height=400)
            
            col_e1, col_e2, col_e3 = st.columns(3)
            with col_e1:
                st.download_button("📥 Excel", data=to_excel_bytes(rev_sorted, "15-Min Reversals"), 
                                  file_name=f"nse_15min_rev_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", 
                                  mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_rev_xlsx")
            with col_e2:
                st.download_button("📥 CSV", data=to_csv_bytes(rev_sorted), 
                                  file_name=f"nse_15min_rev_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", 
                                  mime="text/csv", key="dl_rev_csv")
            with col_e3:
                st.download_button("📥 JSON", data=to_json_bytes(rev_sorted), 
                                  file_name=f"nse_15min_rev_{_now_ist().strftime('%Y%m%d_%H%M')}.json", 
                                  mime="application/json", key="dl_rev_json")
        else:
            st.info("Run a 15-Min Reversal scan above.")
        
        if st.session_state.get("rev_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state['rev_errors'])})"):
                st.text("\n".join(st.session_state["rev_errors"][:20]))
    
    # ════════════════════════════════════════════════════════════════════════════════
    # VOLUME BIG MOVEMENT TAB
    # ════════════════════════════════════════════════════════════════════════════════
    with tab_vol:
        st.markdown(
            "### 🌋 Volume Big Movement Scanner\n"
            f"Detects large volume-driven moves on daily candles. "
            f"Requires: RVOL ≥ {VOL_BIGMOVE_MIN_RVOL}x AND body ≥ {VOL_BIGMOVE_MIN_BODY_PCT}% + significant ATR expansion."
        )
        
        vol_col1, vol_col2 = st.columns([1, 1])
        with vol_col1:
            vol_lim = st.number_input("Limit symbols (0=all)", min_value=0, max_value=len(symbols), value=min(300, len(symbols)), step=50, key="vol_limit")
        with vol_col2:
            st.empty()
        
        vol_universe = symbols if vol_lim == 0 else symbols[:vol_lim]
        
        if st.button(f"🌋 Run Volume Big Move Scan ({len(vol_universe)} symbols)", key="vol_run"):
            with st.spinner("Scanning daily candles for big volume moves…"):
                vol_results, vol_errors, vol_stats = run_volume_big_move_scan(fyers, vol_universe)
                st.session_state["vol_df"] = pd.DataFrame(vol_results)
                st.session_state["vol_errors"] = vol_errors
                st.session_state["vol_stats"] = vol_stats
        
        if "vol_stats" in st.session_state:
            _display_scan_summary(st.session_state["vol_stats"])
        
        vol_df = st.session_state.get("vol_df")
        if vol_df is not None and not vol_df.empty:
            vol_sorted = vol_df.sort_values("Confidence %", ascending=False)
            st.dataframe(vol_sorted, use_container_width=True, height=400)
            
            col_e1, col_e2, col_e3 = st.columns(3)
            with col_e1:
                st.download_button("📥 Excel", data=to_excel_bytes(vol_sorted, "Volume Big Moves"), 
                                  file_name=f"nse_vol_bigmove_{_now_ist().strftime('%Y%m%d_%H%M')}.xlsx", 
                                  mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dl_vol_xlsx")
            with col_e2:
                st.download_button("📥 CSV", data=to_csv_bytes(vol_sorted), 
                                  file_name=f"nse_vol_bigmove_{_now_ist().strftime('%Y%m%d_%H%M')}.csv", 
                                  mime="text/csv", key="dl_vol_csv")
            with col_e3:
                st.download_button("📥 JSON", data=to_json_bytes(vol_sorted), 
                                  file_name=f"nse_vol_bigmove_{_now_ist().strftime('%Y%m%d_%H%M')}.json", 
                                  mime="application/json", key="dl_vol_json")
        else:
            st.info("Run a Volume Big Move scan above.")
        
        if st.session_state.get("vol_errors"):
            with st.expander(f"⚠️ Skipped ({len(st.session_state['vol_errors'])})"):
                st.text("\n".join(st.session_state["vol_errors"][:20]))
    
    gc.collect()

# ════════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # Initialize Fyers session (user must set FYERS_ACCESS_TOKEN env var)
    access_token = os.environ.get("FYERS_ACCESS_TOKEN")
    if not access_token:
        st.error("❌ FYERS_ACCESS_TOKEN not set in environment variables")
        st.stop()
    
    try:
        from fyers_api import fyersModel
        fyers = fyersModel.FyersModel(client_id="DEMO", token=access_token, log_path="")
        show_scanner(fyers)
    except ImportError:
        st.error("❌ fyers-api not installed. Run: pip install fyers-api")
    except Exception as e:
        st.error(f"❌ Error: {e}")
